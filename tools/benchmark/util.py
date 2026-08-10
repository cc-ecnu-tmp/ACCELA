from __future__ import annotations

import errno as errno_module
import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from .errors import ConfigurationError, ValidationError

_PATH_TERMINATORS = r"\s\"'()<>{}\[\],;"
_WINDOWS_ABSOLUTE = re.compile(
    rf"(?i)(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/][^{_PATH_TERMINATORS}]+|\\\\[^\\/\s]+[\\/][^{_PATH_TERMINATORS}]+)"
)
_POSIX_ABSOLUTE = re.compile(
    rf"(?<![A-Za-z0-9/])/(?!/)[^{_PATH_TERMINATORS}]+"
)
_ENVIRONMENT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_without_symlinks(path: Path, *, label: str) -> Path:
    """Resolve an existing path after rejecting every lexical symlink component."""

    lexical = path.absolute()
    cursor = Path(lexical.anchor)
    for component in lexical.parts[1:]:
        cursor = cursor / component
        if cursor.is_symlink():
            raise ValidationError(f"{label} must not traverse a symbolic link")
    try:
        return lexical.resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"{label} is missing or unreadable") from exc


def sha256_artifact(path: Path) -> str:
    """Hash a compiler binary or class tree without recording its external path.

    Tree entries are normalized to POSIX relative paths before sorting.  The
    ordering key is the UTF-8 byte sequence of that normalized path; unlike
    ``Path`` ordering, this contract is independent of host path case-folding.
    UTF-8 byte order preserves the previous POSIX code-point order, so existing
    Linux/WSL artifact digests remain stable.
    """
    resolved = resolve_without_symlinks(path, label="compiler artifact")
    if resolved.is_file():
        return sha256_file(resolved)
    if not resolved.is_dir():
        raise ValidationError("compiler artifact must be a regular file or directory")
    entries: list[dict[str, Any]] = []
    for item in resolved.rglob("*"):
        if item.is_symlink():
            raise ValidationError("compiler artifact trees must not contain symbolic links")
        if item.is_file():
            entries.append(
                {
                    "path": item.relative_to(resolved).as_posix(),
                    "sha256": sha256_file(item),
                    "size_bytes": item.stat().st_size,
                }
            )
        elif not item.is_dir():
            raise ValidationError("compiler artifact trees must contain only regular files/directories")
    if not entries:
        raise ValidationError("compiler artifact directory is empty")
    entries.sort(key=lambda entry: entry["path"].encode("utf-8"))
    return sha256_json({"tree_version": 1, "files": entries})


def raw_attempt_identity(
    *,
    run_id: str,
    manifest_sha256: str,
    case_id: str,
    attempt_index: int,
    started_at: str,
    configuration_sha256: str,
) -> dict[str, Any]:
    """Return the portable identity shared by normalized and raw attempt evidence."""
    return {
        "schema_version": "benchmark-raw-attempt.v1",
        "run_id": run_id,
        "manifest_sha256": manifest_sha256,
        "case_id": case_id,
        "attempt_index": attempt_index,
        "started_at": started_at,
        "configuration_sha256": configuration_sha256,
    }


def raw_attempt_identity_sha256(
    *,
    run_id: str,
    manifest_sha256: str,
    case_id: str,
    attempt_index: int,
    started_at: str,
    configuration_sha256: str,
) -> str:
    """Hash a raw-attempt identity using the canonical JSON contract."""
    return sha256_json(
        raw_attempt_identity(
            run_id=run_id,
            manifest_sha256=manifest_sha256,
            case_id=case_id,
            attempt_index=attempt_index,
            started_at=started_at,
            configuration_sha256=configuration_sha256,
        )
    )


def file_ref(path: Path, relative_to: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    root = relative_to.resolve(strict=True)
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValidationError("benchmark files must remain under the declared suite root") from exc
    return {
        "path": relative.as_posix(),
        "sha256": sha256_file(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def validate_relative_path(value: str, *, label: str = "path") -> PurePosixPath:
    if "\\" in value:
        raise ValidationError(f"{label} must use portable forward slashes")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValidationError(f"{label} must be a normalized relative path without parent traversal")
    if re.match(r"^[A-Za-z]:", value):
        raise ValidationError(f"{label} must not be an absolute Windows path")
    return path


def resolve_manifest_path(root: Path, value: str) -> Path:
    relative = validate_relative_path(value)
    try:
        root_resolved = root.resolve(strict=True)
        candidate = root_resolved.joinpath(*relative.parts).resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"manifest file is missing or unreadable: {value}") from exc
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ValidationError("resolved manifest path escapes the suite root") from exc
    return candidate


def read_json(path: Path) -> Any:
    def reject_nonstandard_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON numeric constant: {value}")

    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream, parse_constant=reject_nonstandard_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValidationError(f"cannot read valid UTF-8 JSON from {sanitize_text(str(path))}: {exc}") from exc


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sanitize_text(value: str, roots: Iterable[Path] = (), limit: int = 4096) -> str:
    sanitized = value.replace("\x00", "�")
    replacements: list[tuple[str, str]] = []
    for index, root in enumerate(roots):
        try:
            resolved = str(root.resolve())
        except OSError:
            resolved = str(root)
        replacements.extend(
            [
                (resolved, f"$ROOT{index}"),
                (resolved.replace("\\", "/"), f"$ROOT{index}"),
            ]
        )
    try:
        home = str(Path.home().resolve())
        replacements.extend([(home, "$HOME"), (home.replace("\\", "/"), "$HOME")])
    except OSError:
        pass
    for raw, replacement in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        if raw:
            sanitized = sanitized.replace(raw, replacement)
    sanitized = _WINDOWS_ABSOLUTE.sub("<redacted-path>", sanitized)
    sanitized = _POSIX_ABSOLUTE.sub("<redacted-path>", sanitized)
    if len(sanitized) > limit:
        sanitized = sanitized[-limit:]
    return sanitized


def describe_os_error(exc: OSError) -> str:
    """Return path-free, machine-actionable operating-system error identity.

    ``str(OSError)`` commonly includes the affected absolute path and does not
    always retain the symbolic errno name.  Durable benchmark evidence needs a
    stable diagnostic that can be compared across hosts without exposing either
    the checkout or raw-run location.
    """

    details = [f"class={type(exc).__name__}"]
    if exc.errno is None:
        details.extend(("errno_name=UNKNOWN", "errno_code=none"))
    else:
        details.extend(
            (
                f"errno_name={errno_module.errorcode.get(exc.errno, 'UNKNOWN')}",
                f"errno_code={exc.errno}",
            )
        )
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        details.append(f"winerror_code={winerror}")
    return ", ".join(details)


def render_cli_error(exc: BaseException, roots: Iterable[Path] = ()) -> str:
    """Render an exception without depending on a still-valid process CWD."""

    privacy_roots = list(roots)
    cwd_error: OSError | None = None
    try:
        privacy_roots.append(Path.cwd())
    except OSError as observed:
        cwd_error = observed

    rendered = f"{type(exc).__name__}: {sanitize_text(str(exc), privacy_roots)}"
    if isinstance(exc, OSError):
        rendered += f" ({describe_os_error(exc)})"
    if cwd_error is not None:
        rendered += (
            "; current_working_directory=unavailable "
            f"({describe_os_error(cwd_error)})"
        )
    return rendered


def parse_command_json(raw: str | None, *, label: str, required: bool) -> tuple[str, ...] | None:
    if raw is None:
        if required:
            raise ConfigurationError(f"{label} is required")
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{label} must be a JSON array of argv strings") from exc
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ConfigurationError(f"{label} must be a non-empty JSON array of non-empty strings")
    return tuple(value)


def parse_environment(values: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not _ENVIRONMENT_KEY.fullmatch(key):
            raise ConfigurationError(f"environment override must be KEY=VALUE, got {value!r}")
        if key in result:
            raise ConfigurationError(f"duplicate environment override: {key}")
        result[key] = item
    return result


class _StrictFormatMap(dict[str, str]):
    def __missing__(self, key: str) -> str:
        raise ConfigurationError(f"unknown command placeholder: {{{key}}}")


def render_command(template: Sequence[str], values: Mapping[str, str]) -> list[str]:
    rendered: list[str] = []
    mapping = _StrictFormatMap(values)
    for token in template:
        try:
            rendered.append(token.format_map(mapping))
        except (ValueError, KeyError) as exc:
            raise ConfigurationError(f"invalid command template token {token!r}: {exc}") from exc
    if not rendered or not rendered[0]:
        raise ConfigurationError("rendered command has no executable")
    return rendered


def executable_label(command: Sequence[str] | None) -> str | None:
    if not command:
        return None
    value = command[0].replace("\\", "/").rstrip("/")
    return value.rsplit("/", 1)[-1] or None


def safe_slug(value: str) -> str:
    prefix = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")[:80] or "case"
    return f"{prefix}-{sha256_bytes(value.encode('utf-8'))[:12]}"


def require_positive_finite(value: float, label: str) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ConfigurationError(f"{label} must be finite and greater than zero")
    return value
