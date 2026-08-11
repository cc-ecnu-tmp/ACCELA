from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


_IDENTIFIER_START = frozenset(
    b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
)
_IDENTIFIER_CONTINUE = _IDENTIFIER_START | frozenset(b"0123456789")
_DECIMAL_DIGITS = frozenset(b"0123456789")
_HEX_DIGITS = _DECIMAL_DIGITS | frozenset(b"abcdefABCDEF")
_WHITESPACE = frozenset(b" \t\r\n\v\f")
_SYSY_TWO_BYTE_TOKENS = frozenset({b"<=", b">=", b"==", b"!=", b"&&", b"||"})
_NON_SYSY_TWO_BYTE_TOKENS = frozenset(
    {
        b"++",
        b"--",
        b"+=",
        b"-=",
        b"*=",
        b"/=",
        b"%=",
        b"&=",
        b"|=",
        b"^=",
        b"<<",
        b">>",
        b"->",
        b"::",
    }
)
_SYSY_ONE_BYTE_TOKENS = frozenset(b"+-*/%<>!=;,()[]{}")

REFERENCE_SOURCE_ADAPTER_PATH = "tools/benchmark/reference_source.py"
REFERENCE_COMPILE_DRIVER_PATH = "scripts/reference-compile.sh"
REFERENCE_BUILTIN_HEADER_PATH = "tools/qemu/sysy-builtins.h"
REFERENCE_DOCKERFILE_PATH = "tools/benchmark/reference-toolchain.Dockerfile"
REFERENCE_LAUNCHER_CONTRACT = {
    "python_executable": "python3",
    "python_mode": "isolated",
    "python_argv_prefix": ["python3", "-I"],
    "python_version_source": "proxy_execution.python",
    "python_version_probe": "platform.python_version",
    "docker_candidate_order": ["native", "windows"],
    "docker_readiness_argv": ["version", "--format", "{{.Server.Version}}"],
    "docker_fallback_policy": "transport_unreachable_only",
    "docker_image_inspect_identity": "frozen_tag_to_exact_id",
    "docker_run_identity": "exact_image_id",
    "docker_native_host_storage": "bind",
    "docker_container_identity": "hostname_inspect_exact_id",
    "docker_container_image_identity": "frozen_candidate_image_id",
    "docker_container_storage": "single_labeled_named_volume",
    "docker_container_mount_transport": "volume_subpath",
    "docker_container_socket": "/var/run/docker.sock",
    "docker_container_cli_identity": "frozen_path_sha256_version",
    "stderr_records": [
        "ACCELA_REFERENCE_PYTHON",
        "ACCELA_REFERENCE_DOCKER_CANDIDATE",
        "ACCELA_REFERENCE_DOCKER",
        "ACCELA_REFERENCE_STORAGE",
        "ACCELA_REFERENCE_COMMAND",
    ],
}
REFERENCE_COMMON_SEMANTICS = (
    "source-adapter:sysy-lexical-boundary-v1",
    "source-adapter:cxx17-keywords-v1",
    "source-adapter:int32-literals-v1",
    "source-adapter:binary32-literals-v1",
    "-fwrapv",
    "-fno-fast-math",
    "-ffp-contract=off",
    "-ffreestanding",
    "-fno-builtin",
)
REFERENCE_BASELINES = {
    "gcc_13_3_o2": {
        "profile_id": "gcc-13.3-o2",
        "frontend": "gcc",
        "tool": "riscv-gcc",
        "version": "13.3.0",
        "optimization": "-O2",
        "executable": "/usr/bin/riscv64-linux-gnu-g++-13",
        "package": "gcc-13-riscv64-linux-gnu=13.3.0-6ubuntu2~24.04.1cross1",
        "cxx_package": "g++-13-riscv64-linux-gnu=13.3.0-6ubuntu2~24.04.1cross1",
    },
    "clang_18_o3": {
        "profile_id": "clang-18-o3",
        "frontend": "clang",
        "tool": "clang",
        "version": "18.1.3",
        "optimization": "-O3",
        "executable": "/usr/bin/clang-18",
        "package": "clang-18=1:18.1.3-1ubuntu1",
    },
}
REFERENCE_FRONTEND_ARGV = {
    "gcc": (
        "/usr/bin/riscv64-linux-gnu-g++-13",
        "-march=rv64gc",
        "-mabi=lp64d",
        "-mcmodel=medany",
        "-O2",
        "-fwrapv",
        "-fno-fast-math",
        "-ffp-contract=off",
        "-ffreestanding",
        "-fno-builtin",
        "-fno-exceptions",
        "-fno-rtti",
        "-fno-threadsafe-statics",
        "-fno-use-cxa-atexit",
        "-nostdinc++",
        "-std=c++17",
        "-x",
        "c++",
        "-include",
        "/support/sysy-builtins.h",
        "-S",
        "/output/.accela-sysy-reference.cpp",
        "-o",
        "/output/{artifact_name}",
    ),
    "clang": (
        "/usr/bin/clang-18",
        "--target=riscv64-unknown-elf",
        "-march=rv64gc",
        "-mabi=lp64d",
        "-mcmodel=medany",
        "-O3",
        "-fwrapv",
        "-fno-fast-math",
        "-ffp-contract=off",
        "-ffreestanding",
        "-fno-builtin",
        "-fno-addrsig",
        "-fno-exceptions",
        "-fno-rtti",
        "-fno-threadsafe-statics",
        "-fno-use-cxa-atexit",
        "-nostdinc++",
        "-std=c++17",
        "-x",
        "c++",
        "-include",
        "/support/sysy-builtins.h",
        "-S",
        "/output/.accela-sysy-reference.cpp",
        "-o",
        "/output/{artifact_name}",
    ),
}

# SysY has a deliberately smaller keyword set than C++17.  A legal SysY
# identifier may therefore be a C++ keyword.  The adapter renames only that
# difference; shared SysY/C++ keywords retain their language meaning.
_SYSY_KEYWORDS = frozenset(
    {"break", "const", "continue", "else", "float", "if", "int", "return", "void", "while"}
)
_CXX17_KEYWORDS = frozenset(
    {
        "alignas",
        "alignof",
        "and",
        "and_eq",
        "asm",
        "auto",
        "bitand",
        "bitor",
        "bool",
        "break",
        "case",
        "catch",
        "char",
        "char16_t",
        "char32_t",
        "class",
        "compl",
        "const",
        "constexpr",
        "const_cast",
        "continue",
        "decltype",
        "default",
        "delete",
        "do",
        "double",
        "dynamic_cast",
        "else",
        "enum",
        "explicit",
        "export",
        "extern",
        "false",
        "float",
        "for",
        "friend",
        "goto",
        "if",
        "inline",
        "int",
        "long",
        "mutable",
        "namespace",
        "new",
        "noexcept",
        "not",
        "not_eq",
        "nullptr",
        "operator",
        "or",
        "or_eq",
        "private",
        "protected",
        "public",
        "register",
        "reinterpret_cast",
        "return",
        "short",
        "signed",
        "sizeof",
        "static",
        "static_assert",
        "static_cast",
        "struct",
        "switch",
        "template",
        "this",
        "thread_local",
        "throw",
        "true",
        "try",
        "typedef",
        "typeid",
        "typename",
        "union",
        "unsigned",
        "using",
        "virtual",
        "void",
        "volatile",
        "wchar_t",
        "while",
        "xor",
        "xor_eq",
    }
)
_CXX_ONLY_KEYWORDS = _CXX17_KEYWORDS - _SYSY_KEYWORDS


class ReferenceSourceError(ValueError):
    """A source cannot be translated without changing the SysY contract."""


@dataclass(frozen=True)
class AdaptedSource:
    payload: bytes
    identifier_prefix: str | None
    renamed_identifiers: tuple[str, ...]
    integer_literal_count: int
    float_literal_count: int


@dataclass(frozen=True)
class ReferenceFrontendContract:
    image_tag: str
    image_id: str
    source_adapter_sha256: str
    builtin_header_sha256: str
    compiler_argv_sha256: str
    compiler_argv: tuple[str, ...]
    named_volume_name: str
    named_volume_campaign: str
    named_volume_purpose: str
    candidate_image_id: str
    docker_cli_install_path: str
    docker_cli_sha256: str
    docker_cli_version_output: str


@dataclass(frozen=True)
class ReferenceVolumeMount:
    volume_name: str
    output_subpath: str
    support_subpath: str
    volume_name_sha256: str
    container_id_sha256: str


@dataclass(frozen=True)
class _Piece:
    kind: str
    payload: bytes
    offset: int


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _physical_support_hash(
    root: Path,
    frontends: dict[str, object],
    *,
    path_key: str,
    hash_key: str,
    expected_path: str,
) -> str:
    observed_path = frontends.get(path_key)
    observed_hash = frontends.get(hash_key)
    if observed_path != expected_path or not isinstance(observed_hash, str):
        raise ReferenceSourceError(f"reference contract has invalid {path_key}")
    if re.fullmatch(r"[0-9a-f]{64}", observed_hash) is None:
        raise ReferenceSourceError(f"reference contract has invalid {hash_key}")
    relative = Path(*expected_path.split("/"))
    physical = (root / relative).resolve(strict=True)
    try:
        physical.relative_to(root)
    except ValueError as exc:
        raise ReferenceSourceError("reference support path escapes the workspace") from exc
    if not physical.is_file() or hashlib.sha256(physical.read_bytes()).hexdigest() != observed_hash:
        raise ReferenceSourceError(f"reference support hash mismatch: {path_key}")
    return observed_hash


def validate_reference_named_volume_contract(
    frontends: dict[str, object],
) -> tuple[str, str, str]:
    contract = frontends.get("named_volume_contract")
    if not isinstance(contract, dict) or set(contract) != {"name", "required_labels"}:
        raise ReferenceSourceError("reference named-volume contract is invalid")
    name = contract.get("name")
    labels = contract.get("required_labels")
    if (
        not isinstance(name, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", name) is None
        or not isinstance(labels, dict)
        or set(labels)
        != {"org.accela.campaign", "org.accela.purpose"}
    ):
        raise ReferenceSourceError("reference named-volume contract is invalid")
    campaign = labels.get("org.accela.campaign")
    purpose = labels.get("org.accela.purpose")
    if (
        not isinstance(campaign, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", campaign) is None
        or not isinstance(purpose, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", purpose) is None
    ):
        raise ReferenceSourceError("reference named-volume labels are invalid")
    return name, campaign, purpose


def _reference_candidate_runtime_contract(
    proxy: object,
) -> tuple[str, str, str, str]:
    candidate = proxy.get("candidate_toolchain") if isinstance(proxy, dict) else None
    docker_cli = candidate.get("docker_cli") if isinstance(candidate, dict) else None
    if (
        not isinstance(candidate, dict)
        or not isinstance(docker_cli, dict)
        or not isinstance(candidate.get("image_id"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", candidate["image_id"]) is None
        or candidate["image_id"] == "sha256:" + "0" * 64
        or docker_cli.get("install_path") != "/usr/local/bin/docker"
        or not isinstance(docker_cli.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", docker_cli["sha256"]) is None
        or docker_cli["sha256"] == "0" * 64
        or not isinstance(docker_cli.get("version_output"), str)
        or re.fullmatch(
            r"Docker version [0-9]+(?:\.[0-9]+){2}, build [0-9a-f]+",
            docker_cli["version_output"],
        )
        is None
    ):
        raise ReferenceSourceError("candidate Docker runtime contract is invalid")
    return (
        candidate["image_id"],
        docker_cli["install_path"],
        docker_cli["sha256"],
        docker_cli["version_output"],
    )


def _read_inspect_document(path: Path, *, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.resolve(strict=True).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReferenceSourceError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ReferenceSourceError(f"{label} must contain exactly one object")
    return payload[0]


def _safe_volume_subpath(path: Path, mountpoint: Path, *, label: str) -> str:
    try:
        relative = path.relative_to(mountpoint)
    except ValueError as exc:
        raise ReferenceSourceError(f"{label} is outside the campaign volume") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ReferenceSourceError(f"{label} has an invalid volume subpath")
    value = relative.as_posix()
    if (
        value.startswith("/")
        or "\\" in value
        or any(character in value for character in ",\r\n\x00")
    ):
        raise ReferenceSourceError(f"{label} has an unsafe volume subpath")
    return value


def _effective_mount(
    path: Path,
    mounts: list[tuple[dict[str, object], Path]],
    *,
    label: str,
) -> tuple[dict[str, object], Path]:
    containing: list[tuple[dict[str, object], Path]] = []
    for item, mountpoint in mounts:
        try:
            path.relative_to(mountpoint)
        except ValueError:
            continue
        containing.append((item, mountpoint))
    if not containing:
        raise ReferenceSourceError(f"{label} is not backed by a Docker mount")
    maximum_depth = max(len(mountpoint.parts) for _, mountpoint in containing)
    effective = [row for row in containing if len(row[1].parts) == maximum_depth]
    if len(effective) != 1:
        raise ReferenceSourceError(f"{label} has an ambiguous effective Docker mount")
    return effective[0]


def select_reference_volume_mount(
    *,
    root: Path,
    output_dir: Path,
    support_dir: Path,
    container_inspect_path: Path,
    volume_inspect_path: Path,
    expected_volume_name: str,
    expected_campaign: str,
    expected_purpose: str,
    expected_candidate_image_id: str,
    observed_hostname: str,
) -> ReferenceVolumeMount:
    """Select the one Docker named volume that owns every reference-compile path."""

    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", observed_hostname) is None:
        raise ReferenceSourceError("current Docker container hostname is invalid")
    workspace = root.resolve(strict=True)
    output = output_dir.resolve(strict=True)
    support = support_dir.resolve(strict=True)
    if not workspace.is_dir() or not output.is_dir() or not support.is_dir():
        raise ReferenceSourceError("reference named-volume paths must be directories")

    container = _read_inspect_document(
        container_inspect_path, label="reference outer-container inspect"
    )
    container_id = container.get("Id")
    container_name = container.get("Name")
    config = container.get("Config")
    mounts = container.get("Mounts")
    if (
        not isinstance(container_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", container_id) is None
        or container.get("Image") != expected_candidate_image_id
        or not isinstance(config, dict)
        or config.get("Hostname") != observed_hostname
        or not isinstance(mounts, list)
    ):
        raise ReferenceSourceError("reference outer-container identity is invalid")
    if re.fullmatch(r"[0-9a-f]{12,64}", observed_hostname) is not None:
        if not container_id.startswith(observed_hostname):
            raise ReferenceSourceError("reference outer-container identity is invalid")
    elif container_name != f"/{observed_hostname}":
        raise ReferenceSourceError("reference outer-container name/hostname differs")

    normalized_mounts: list[tuple[dict[str, object], Path]] = []
    for item in mounts:
        if not isinstance(item, dict):
            raise ReferenceSourceError("reference outer-container mount record is invalid")
        destination = item.get("Destination")
        if not isinstance(destination, str) or not destination.startswith("/"):
            raise ReferenceSourceError("reference outer-container mount destination is invalid")
        mountpoint = Path(destination)
        if not mountpoint.is_absolute() or ".." in mountpoint.parts:
            raise ReferenceSourceError(
                "reference outer-container mount destination is not normalized"
            )
        normalized_mounts.append((item, mountpoint))
    workspace_mount = _effective_mount(
        workspace, normalized_mounts, label="reference workspace"
    )
    output_mount = _effective_mount(
        output, normalized_mounts, label="reference output"
    )
    support_mount = _effective_mount(
        support, normalized_mounts, label="reference support"
    )
    if output_mount != workspace_mount or support_mount != workspace_mount:
        raise ReferenceSourceError(
            "reference paths must share one effective Docker mount"
        )
    mount, mountpoint = workspace_mount
    if (
        mount.get("Type") != "volume"
        or mount.get("Name") != expected_volume_name
        or mount.get("RW") is not True
    ):
        raise ReferenceSourceError(
            "reference workspace is not on the required writable named volume"
        )

    volume = _read_inspect_document(
        volume_inspect_path, label="reference named-volume inspect"
    )
    labels = volume.get("Labels")
    if (
        volume.get("Name") != expected_volume_name
        or volume.get("Driver") != "local"
        or volume.get("Scope") != "local"
        or not isinstance(labels, dict)
        or labels.get("org.accela.campaign") != expected_campaign
        or labels.get("org.accela.purpose") != expected_purpose
    ):
        raise ReferenceSourceError("reference named-volume identity or labels differ")

    return ReferenceVolumeMount(
        volume_name=expected_volume_name,
        output_subpath=_safe_volume_subpath(output, mountpoint, label="reference output"),
        support_subpath=_safe_volume_subpath(support, mountpoint, label="reference support"),
        volume_name_sha256=hashlib.sha256(expected_volume_name.encode("utf-8")).hexdigest(),
        container_id_sha256=hashlib.sha256(container_id.encode("ascii")).hexdigest(),
    )


def load_reference_frontend_contract(
    *,
    root: Path,
    snapshot_path: Path,
    frontend: str,
    artifact_name: str,
) -> ReferenceFrontendContract:
    if frontend not in REFERENCE_FRONTEND_ARGV:
        raise ReferenceSourceError(f"unknown reference frontend: {frontend}")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,254}", artifact_name) is None:
        raise ReferenceSourceError("reference artifact name contains unsupported bytes")
    workspace = root.resolve(strict=True)
    if not workspace.is_dir():
        raise ReferenceSourceError("reference workspace is not a directory")
    try:
        snapshot = json.loads(snapshot_path.resolve(strict=True).read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReferenceSourceError("toolchain snapshot is not valid UTF-8 JSON") from exc
    if not isinstance(snapshot, dict) or snapshot.get("schema") != "accela-toolchain-snapshot.v1":
        raise ReferenceSourceError("toolchain snapshot has an unsupported schema")
    if snapshot.get("target") != {"isa": "rv64gc", "abi": "lp64d", "code_model": "medany"}:
        raise ReferenceSourceError("toolchain snapshot target is not RV64GC/LP64D/medany")
    frontends = snapshot.get("reference_frontends")
    proxy = snapshot.get("proxy_execution")
    if not isinstance(frontends, dict):
        raise ReferenceSourceError("toolchain snapshot lacks reference_frontends")
    if frontends.get("frontend_language") != "c++17" or frontends.get(
        "common_semantics"
    ) != list(REFERENCE_COMMON_SEMANTICS):
        raise ReferenceSourceError("reference frontend language semantics have drifted")
    if frontends.get("launcher_contract") != REFERENCE_LAUNCHER_CONTRACT:
        raise ReferenceSourceError("reference launcher contract has drifted")
    named_volume_name, named_volume_campaign, named_volume_purpose = (
        validate_reference_named_volume_contract(frontends)
    )
    (
        candidate_image_id,
        docker_cli_install_path,
        docker_cli_sha256,
        docker_cli_version_output,
    ) = _reference_candidate_runtime_contract(proxy)

    _physical_support_hash(
        workspace,
        frontends,
        path_key="compile_driver_path",
        hash_key="compile_driver_sha256",
        expected_path=REFERENCE_COMPILE_DRIVER_PATH,
    )
    source_adapter_sha256 = _physical_support_hash(
        workspace,
        frontends,
        path_key="source_adapter_path",
        hash_key="source_adapter_sha256",
        expected_path=REFERENCE_SOURCE_ADAPTER_PATH,
    )
    builtin_header_sha256 = _physical_support_hash(
        workspace,
        frontends,
        path_key="builtin_header_path",
        hash_key="builtin_header_sha256",
        expected_path=REFERENCE_BUILTIN_HEADER_PATH,
    )
    dockerfile_hash = frontends.get("dockerfile_sha256")
    dockerfile = (workspace / Path(*REFERENCE_DOCKERFILE_PATH.split("/"))).resolve(strict=True)
    if (
        not isinstance(dockerfile_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", dockerfile_hash) is None
        or not dockerfile.is_file()
        or hashlib.sha256(dockerfile.read_bytes()).hexdigest() != dockerfile_hash
    ):
        raise ReferenceSourceError("reference Dockerfile hash mismatch")

    image_tag = frontends.get("local_image_tag")
    image_id = frontends.get("local_image_id")
    if not isinstance(image_tag, str) or re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", image_tag
    ) is None:
        raise ReferenceSourceError("reference image tag is invalid")
    if not isinstance(image_id, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        raise ReferenceSourceError("reference image ID is invalid")

    baseline = next(
        item for item in REFERENCE_BASELINES.values() if item["frontend"] == frontend
    )
    observed = frontends.get(frontend)
    if not isinstance(observed, dict):
        raise ReferenceSourceError(f"toolchain snapshot lacks {frontend}")
    for field in ("version", "optimization", "executable", "package"):
        if observed.get(field) != baseline[field]:
            raise ReferenceSourceError(f"reference frontend identity drift: {frontend}/{field}")
    if "cxx_package" in baseline and observed.get("cxx_package") != baseline["cxx_package"]:
        raise ReferenceSourceError(f"reference frontend identity drift: {frontend}/cxx_package")
    expected_argv = REFERENCE_FRONTEND_ARGV[frontend]
    observed_argv = observed.get("compiler_argv")
    if observed_argv != list(expected_argv):
        raise ReferenceSourceError(f"reference frontend argv drift: {frontend}")
    compiler_argv_sha256 = _canonical_sha256({"argv": observed_argv})
    if observed.get("compiler_argv_sha256") != compiler_argv_sha256:
        raise ReferenceSourceError(f"reference frontend argv hash mismatch: {frontend}")
    placeholders = sum(argument.count("{artifact_name}") for argument in expected_argv)
    if placeholders != 1:
        raise AssertionError("reference frontend argv must have exactly one artifact placeholder")
    rendered = tuple(
        argument.replace("{artifact_name}", artifact_name) for argument in expected_argv
    )
    if any("\n" in argument or "\r" in argument or not argument for argument in rendered):
        raise AssertionError("reference frontend argv is not line serializable")
    return ReferenceFrontendContract(
        image_tag=image_tag,
        image_id=image_id,
        source_adapter_sha256=source_adapter_sha256,
        builtin_header_sha256=builtin_header_sha256,
        compiler_argv_sha256=compiler_argv_sha256,
        compiler_argv=rendered,
        named_volume_name=named_volume_name,
        named_volume_campaign=named_volume_campaign,
        named_volume_purpose=named_volume_purpose,
        candidate_image_id=candidate_image_id,
        docker_cli_install_path=docker_cli_install_path,
        docker_cli_sha256=docker_cli_sha256,
        docker_cli_version_output=docker_cli_version_output,
    )


def _location(payload: bytes, offset: int) -> str:
    line = payload.count(b"\n", 0, offset) + 1
    previous = payload.rfind(b"\n", 0, offset)
    column = offset - previous
    return f"line {line}, column {column}"


def _error(payload: bytes, offset: int, message: str) -> ReferenceSourceError:
    return ReferenceSourceError(f"{message} at {_location(payload, offset)}")


def _comment_piece(payload: bytes, offset: int) -> tuple[_Piece, int]:
    if payload[offset + 1] == ord("/"):
        cursor = offset + 2
        while cursor < len(payload) and payload[cursor] not in (ord("\n"), ord("\r")):
            cursor += 1
        return _Piece("raw", payload[offset:cursor], offset), cursor
    closing = payload.find(b"*/", offset + 2)
    if closing < 0:
        raise _error(payload, offset, "unterminated block comment")
    end = closing + 2
    return _Piece("raw", payload[offset:end], offset), end


def _consume_suffix(payload: bytes, cursor: int) -> int:
    while cursor < len(payload) and payload[cursor] in b"fFlLuU":
        cursor += 1
    return cursor


def _number_piece(payload: bytes, offset: int) -> tuple[_Piece, int]:
    cursor = offset
    hexadecimal = (
        cursor + 1 < len(payload)
        and payload[cursor] == ord("0")
        and payload[cursor + 1] in (ord("x"), ord("X"))
    )
    floating = False

    if hexadecimal:
        cursor += 2
        mantissa_digits = 0
        while cursor < len(payload) and payload[cursor] in _HEX_DIGITS:
            cursor += 1
            mantissa_digits += 1
        has_point = cursor < len(payload) and payload[cursor] == ord(".")
        if has_point:
            floating = True
            cursor += 1
            while cursor < len(payload) and payload[cursor] in _HEX_DIGITS:
                cursor += 1
                mantissa_digits += 1
        if mantissa_digits == 0:
            raise _error(payload, offset, "hexadecimal constant has no digits")
        if cursor < len(payload) and payload[cursor] in (ord("p"), ord("P")):
            floating = True
            cursor += 1
            if cursor < len(payload) and payload[cursor] in (ord("+"), ord("-")):
                cursor += 1
            exponent_start = cursor
            while cursor < len(payload) and payload[cursor] in _DECIMAL_DIGITS:
                cursor += 1
            if cursor == exponent_start:
                raise _error(payload, offset, "hexadecimal float has no exponent digits")
        elif has_point:
            raise _error(payload, offset, "hexadecimal float requires a binary exponent")
    else:
        if payload[cursor] == ord("."):
            floating = True
            cursor += 1
            while cursor < len(payload) and payload[cursor] in _DECIMAL_DIGITS:
                cursor += 1
        else:
            while cursor < len(payload) and payload[cursor] in _DECIMAL_DIGITS:
                cursor += 1
            if cursor < len(payload) and payload[cursor] == ord("."):
                floating = True
                cursor += 1
                while cursor < len(payload) and payload[cursor] in _DECIMAL_DIGITS:
                    cursor += 1
        if cursor < len(payload) and payload[cursor] in (ord("e"), ord("E")):
            floating = True
            cursor += 1
            if cursor < len(payload) and payload[cursor] in (ord("+"), ord("-")):
                cursor += 1
            exponent_start = cursor
            while cursor < len(payload) and payload[cursor] in _DECIMAL_DIGITS:
                cursor += 1
            if cursor == exponent_start:
                raise _error(payload, offset, "decimal float has no exponent digits")

    core_end = cursor
    end = _consume_suffix(payload, cursor)
    suffix = payload[core_end:end]
    if suffix:
        if any(value in b"fF" for value in suffix):
            floating = True
        if floating:
            if len(suffix) != 1 or suffix not in (b"f", b"F", b"l", b"L"):
                raise _error(payload, core_end, "invalid floating constant suffix")
        elif any(value not in b"uUlL" for value in suffix):
            raise _error(payload, core_end, "invalid integer constant suffix")

    core = payload[offset:core_end]
    if floating:
        if not hexadecimal and b"." not in core and b"e" not in core and b"E" not in core:
            core += b".0"
        return _Piece("float", core + b"f", offset), end

    if hexadecimal:
        magnitude = int(core[2:], 16)
    elif len(core) > 1 and core.startswith(b"0"):
        if any(value not in b"01234567" for value in core):
            raise _error(payload, offset, "invalid octal integer constant")
        magnitude = int(core, 8)
    else:
        magnitude = int(core, 10)
    if magnitude > 0xFFFFFFFF:
        raise _error(payload, offset, "integer constant exceeds the SysY 32-bit bit-pattern range")
    signed = magnitude if magnitude <= 0x7FFFFFFF else magnitude - 0x100000000
    if signed == -0x80000000:
        normalized = b"((int)(-2147483647 - 1))"
    else:
        normalized = f"((int){signed})".encode("ascii")
    return _Piece("integer", normalized, offset), end


def _lex(payload: bytes) -> list[_Piece]:
    pieces: list[_Piece] = []
    cursor = 0
    while cursor < len(payload):
        value = payload[cursor]
        if value == 0:
            raise _error(payload, cursor, "NUL byte is not valid SysY source")
        if value == ord("/") and cursor + 1 < len(payload) and payload[cursor + 1] in (
            ord("/"),
            ord("*"),
        ):
            piece, cursor = _comment_piece(payload, cursor)
            pieces.append(piece)
            continue
        if value in (ord('"'), ord("'")):
            raise _error(payload, cursor, "quoted literals are not part of the SysY lexical contract")
        if value in _IDENTIFIER_START:
            end = cursor + 1
            while end < len(payload) and payload[end] in _IDENTIFIER_CONTINUE:
                end += 1
            pieces.append(_Piece("identifier", payload[cursor:end], cursor))
            cursor = end
            continue
        if value in _DECIMAL_DIGITS or (
            value == ord(".")
            and cursor + 1 < len(payload)
            and payload[cursor + 1] in _DECIMAL_DIGITS
        ):
            piece, cursor = _number_piece(payload, cursor)
            pieces.append(piece)
            continue
        if value >= 0x80:
            raise _error(payload, cursor, "non-ASCII byte outside a comment")
        if value < 0x20 and value not in _WHITESPACE:
            raise _error(payload, cursor, "control byte is not valid SysY source")
        if value in _WHITESPACE:
            pieces.append(_Piece("raw", payload[cursor : cursor + 1], cursor))
            cursor += 1
            continue
        token = payload[cursor : cursor + 2]
        if token in _SYSY_TWO_BYTE_TOKENS:
            pieces.append(_Piece("raw", token, cursor))
            cursor += 2
            continue
        if token in _NON_SYSY_TWO_BYTE_TOKENS:
            raise _error(payload, cursor, "operator is not part of the SysY lexical contract")
        if value in _SYSY_ONE_BYTE_TOKENS:
            pieces.append(_Piece("raw", payload[cursor : cursor + 1], cursor))
            cursor += 1
            continue
        raise _error(payload, cursor, "byte is not part of the SysY lexical contract")
    return pieces


def _identifier_mapping(pieces: list[_Piece]) -> tuple[str | None, dict[bytes, bytes]]:
    identifiers = {
        piece.payload.decode("ascii") for piece in pieces if piece.kind == "identifier"
    }
    renamed = sorted(identifiers & _CXX_ONLY_KEYWORDS)
    if not renamed:
        return None, {}
    attempt = 0
    while True:
        prefix = f"__accela_sysy_cxx_{attempt}_"
        generated = {prefix + identifier for identifier in renamed}
        if generated.isdisjoint(identifiers):
            return prefix, {
                identifier.encode("ascii"): (prefix + identifier).encode("ascii")
                for identifier in renamed
            }
        attempt += 1


def adapt_source(payload: bytes) -> AdaptedSource:
    pieces = _lex(payload)
    prefix, mapping = _identifier_mapping(pieces)
    output: list[bytes] = []
    integers = 0
    floats = 0
    for piece in pieces:
        if piece.kind == "identifier":
            output.append(mapping.get(piece.payload, piece.payload))
        else:
            output.append(piece.payload)
        integers += piece.kind == "integer"
        floats += piece.kind == "float"
    adapted = b"".join(output)
    if adapted.count(b"\n") != payload.count(b"\n"):
        raise AssertionError("reference source adapter changed physical line count")
    return AdaptedSource(
        payload=adapted,
        identifier_prefix=prefix,
        renamed_identifiers=tuple(sorted(value.decode("ascii") for value in mapping)),
        integer_literal_count=integers,
        float_literal_count=floats,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and adapt the frozen SysY C++17 reference frontend."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    adapt = commands.add_parser("adapt", help="translate SysY tokens into C++17 input")
    adapt.add_argument("source", type=Path)
    adapt.add_argument("output", type=Path)
    contract = commands.add_parser(
        "contract", help="validate the frozen frontend and emit its exact argv"
    )
    contract.add_argument("--root", type=Path, required=True)
    contract.add_argument("--snapshot", type=Path, required=True)
    contract.add_argument("--frontend", choices=sorted(REFERENCE_FRONTEND_ARGV), required=True)
    contract.add_argument("--artifact-name", required=True)
    contract.add_argument("--argv-output", type=Path, required=True)
    select_volume = commands.add_parser(
        "select-volume",
        help="validate the outer Docker container and select exact volume subpaths",
    )
    select_volume.add_argument("--root", type=Path, required=True)
    select_volume.add_argument("--output-dir", type=Path, required=True)
    select_volume.add_argument("--support-dir", type=Path, required=True)
    select_volume.add_argument("--container-inspect", type=Path, required=True)
    select_volume.add_argument("--volume-inspect", type=Path, required=True)
    select_volume.add_argument("--expected-volume-name", required=True)
    select_volume.add_argument("--expected-campaign", required=True)
    select_volume.add_argument("--expected-purpose", required=True)
    select_volume.add_argument("--expected-candidate-image-id", required=True)
    select_volume.add_argument("--observed-hostname", required=True)
    return parser


def _adapt_command(args: argparse.Namespace) -> int:
    try:
        source = args.source.read_bytes()
    except OSError:
        print("reference-source: cannot read source", file=sys.stderr)
        return 2
    try:
        adapted = adapt_source(source)
    except ReferenceSourceError as exc:
        print(f"reference-source: {exc}", file=sys.stderr)
        return 2
    try:
        with args.output.open("xb") as stream:
            stream.write(adapted.payload)
    except FileExistsError:
        print("reference-source: output already exists", file=sys.stderr)
        return 2
    except OSError:
        print("reference-source: cannot write output", file=sys.stderr)
        return 2
    return 0


def _contract_command(args: argparse.Namespace) -> int:
    try:
        contract = load_reference_frontend_contract(
            root=args.root,
            snapshot_path=args.snapshot,
            frontend=args.frontend,
            artifact_name=args.artifact_name,
        )
    except ReferenceSourceError as exc:
        print(f"reference-source: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("reference-source: cannot read reference frontend contract", file=sys.stderr)
        return 2
    try:
        with args.argv_output.open("x", encoding="utf-8", newline="\n") as stream:
            for argument in contract.compiler_argv:
                stream.write(argument)
                stream.write("\n")
    except FileExistsError:
        print("reference-source: argv output already exists", file=sys.stderr)
        return 2
    except OSError:
        print("reference-source: cannot write argv output", file=sys.stderr)
        return 2
    print(contract.image_tag)
    print(contract.image_id)
    print(contract.source_adapter_sha256)
    print(contract.builtin_header_sha256)
    print(contract.compiler_argv_sha256)
    print(contract.named_volume_name)
    print(contract.named_volume_campaign)
    print(contract.named_volume_purpose)
    print(contract.candidate_image_id)
    print(contract.docker_cli_install_path)
    print(contract.docker_cli_sha256)
    print(contract.docker_cli_version_output)
    return 0


def _select_volume_command(args: argparse.Namespace) -> int:
    try:
        mount = select_reference_volume_mount(
            root=args.root,
            output_dir=args.output_dir,
            support_dir=args.support_dir,
            container_inspect_path=args.container_inspect,
            volume_inspect_path=args.volume_inspect,
            expected_volume_name=args.expected_volume_name,
            expected_campaign=args.expected_campaign,
            expected_purpose=args.expected_purpose,
            expected_candidate_image_id=args.expected_candidate_image_id,
            observed_hostname=args.observed_hostname,
        )
    except (OSError, ReferenceSourceError) as exc:
        print(f"reference-source: {exc}", file=sys.stderr)
        return 2
    print(mount.volume_name)
    print(mount.output_subpath)
    print(mount.support_subpath)
    print(mount.volume_name_sha256)
    print(mount.container_id_sha256)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "adapt":
        return _adapt_command(args)
    if args.command == "contract":
        return _contract_command(args)
    if args.command == "select-volume":
        return _select_volume_command(args)
    raise AssertionError(f"unhandled reference-source command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
