from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import re
import sys
import tomllib
from pathlib import Path
from typing import Any, Iterable

from .errors import ConfigurationError, ValidationError
from .util import canonical_json_bytes, resolve_without_symlinks, sha256_file, sha256_json


INVENTORY_SCHEMA = "candidate-python-inventory.v1"
INVENTORY_PATH = "docs/optimization/data/candidate-python-inventory.v1.json"
LOCK_PATH = "tools/benchmark/requirements-linux-x86_64-py314.lock"
VENV_PATH = ".venv"
PYTHON_VERSION = "3.14.6"
PIP_VERSION = "26.1.2"
SETUPTOOLS_VERSION = "83.0.0"
PROJECT_MANIFEST_PATH = "pyproject.toml"
_NORMALIZED_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_LOCK_REQUIREMENT = re.compile(
    r"([A-Za-z0-9_.-]+)==([^\s\\]+) \\ --hash=sha256:([0-9a-f]{64})"
)


def normalize_distribution_name(value: str) -> str:
    normalized = re.sub(r"[-_.]+", "-", value).lower()
    if _NORMALIZED_NAME.fullmatch(normalized) is None:
        raise ValidationError("Python distribution name is invalid")
    return normalized


def _read_inventory(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("cannot read candidate Python inventory as UTF-8 JSON") from exc
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "platform",
        "python",
        "distributions",
    }:
        raise ValidationError("candidate Python inventory fields differ")
    if document["schema"] != INVENTORY_SCHEMA:
        raise ValidationError("candidate Python inventory schema differs")
    if document["platform"] != {"machine": "x86_64", "system": "Linux"}:
        raise ValidationError("candidate Python inventory platform differs")
    if document["python"] != {
        "implementation": "CPython",
        "version": PYTHON_VERSION,
    }:
        raise ValidationError("candidate Python inventory interpreter differs")
    rows = document["distributions"]
    if not isinstance(rows, list) or not rows:
        raise ValidationError("candidate Python inventory distributions are invalid")
    normalized: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"name", "version"}:
            raise ValidationError("candidate Python inventory distribution fields differ")
        name = row.get("name")
        version = row.get("version")
        if (
            not isinstance(name, str)
            or normalize_distribution_name(name) != name
            or not isinstance(version, str)
            or not version
        ):
            raise ValidationError("candidate Python inventory distribution is invalid")
        normalized.append({"name": name, "version": version})
    if normalized != sorted(normalized, key=lambda row: row["name"]):
        raise ValidationError("candidate Python inventory distributions are not sorted")
    if len({row["name"] for row in normalized}) != len(normalized):
        raise ValidationError("candidate Python inventory repeats a distribution")
    if {row["name"]: row["version"] for row in normalized}.get("pip") != PIP_VERSION:
        raise ValidationError("candidate Python inventory pip version differs")
    if (
        {row["name"]: row["version"] for row in normalized}.get("setuptools")
        != SETUPTOOLS_VERSION
    ):
        raise ValidationError("candidate Python inventory setuptools version differs")
    return document


def candidate_python_contract(*, root: Path) -> dict[str, Any]:
    workspace = resolve_without_symlinks(root, label="candidate workspace")
    inventory_path = workspace / INVENTORY_PATH
    lock_path = workspace / LOCK_PATH
    project_manifest_path = workspace / PROJECT_MANIFEST_PATH
    inventory = _read_inventory(inventory_path)
    if not lock_path.is_file():
        raise ValidationError("candidate Python requirements lock is missing")
    _validate_requirements_lock(lock_path, inventory)
    try:
        project_manifest = tomllib.loads(project_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError("cannot read candidate Python project manifest") from exc
    if project_manifest.get("build-system") != {
        "requires": [f"setuptools=={SETUPTOOLS_VERSION}"],
        "build-backend": "setuptools.build_meta",
    }:
        raise ValidationError("candidate Python build backend differs")
    return {
        "version": PYTHON_VERSION,
        "implementation": "CPython",
        "platform_system": "Linux",
        "platform_machine": "x86_64",
        "venv_path": VENV_PATH,
        "pip_version": PIP_VERSION,
        "setuptools_version": SETUPTOOLS_VERSION,
        "project_manifest": {
            "path": PROJECT_MANIFEST_PATH,
            "physical_sha256": sha256_file(project_manifest_path),
        },
        "requirements_lock": {
            "path": LOCK_PATH,
            "physical_sha256": sha256_file(lock_path),
        },
        "installed_inventory": {
            "path": INVENTORY_PATH,
            "canonical_sha256": sha256_json(inventory),
            "physical_sha256": sha256_file(inventory_path),
        },
    }


def _validate_requirements_lock(path: Path, inventory: dict[str, Any]) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError("cannot read candidate Python requirements lock") from exc
    logical: list[str] = []
    pending: str | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if pending is None:
            if not stripped.endswith("\\"):
                raise ValidationError("candidate Python requirements lock entry is malformed")
            pending = stripped
            continue
        logical.append(f"{pending} {stripped}")
        pending = None
    if pending is not None:
        raise ValidationError("candidate Python requirements lock entry is incomplete")
    requirements: dict[str, tuple[str, str]] = {}
    for entry in logical:
        match = _LOCK_REQUIREMENT.fullmatch(entry)
        if match is None:
            raise ValidationError("candidate Python requirements lock entry is malformed")
        name = normalize_distribution_name(match.group(1))
        version = match.group(2)
        digest = match.group(3)
        if name in requirements:
            raise ValidationError("candidate Python requirements lock repeats a distribution")
        if digest == "0" * 64:
            raise ValidationError("candidate Python requirements lock contains an all-zero hash")
        requirements[name] = (version, digest)
    expected = {
        row["name"]: row["version"]
        for row in inventory["distributions"]
        if row["name"] not in {"accela-benchmark", "pip"}
    }
    if {name: value[0] for name, value in requirements.items()} != expected:
        raise ValidationError(
            "candidate Python requirements lock differs from installed inventory"
        )


def _installed_distributions(
    distributions: Iterable[importlib.metadata.Distribution] | None = None,
) -> list[dict[str, str]]:
    observed: list[dict[str, str]] = []
    for distribution in distributions or importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        version = distribution.version
        if not isinstance(name, str) or not isinstance(version, str) or not version:
            raise ValidationError("installed Python distribution metadata is incomplete")
        observed.append(
            {"name": normalize_distribution_name(name), "version": version}
        )
    observed.sort(key=lambda row: row["name"])
    if len({row["name"] for row in observed}) != len(observed):
        raise ValidationError("installed Python environment repeats a distribution")
    return observed


def verify_installed_environment(*, root: Path) -> dict[str, Any]:
    workspace = resolve_without_symlinks(root, label="candidate workspace")
    expected_prefix = (workspace / VENV_PATH).resolve(strict=True)
    observed_prefix = Path(sys.prefix).resolve(strict=True)
    if sys.prefix == sys.base_prefix or observed_prefix != expected_prefix:
        raise ValidationError("candidate Python must run from the repository .venv")
    if platform.python_implementation() != "CPython":
        raise ValidationError("candidate Python implementation differs")
    if platform.python_version() != PYTHON_VERSION:
        raise ValidationError("candidate Python version differs")
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValidationError("candidate Python platform differs")
    inventory = _read_inventory(workspace / INVENTORY_PATH)
    observed = _installed_distributions()
    if observed != inventory["distributions"]:
        raise ValidationError("installed Python distribution inventory differs")
    return {
        "schema": INVENTORY_SCHEMA,
        "inventory_canonical_sha256": sha256_json(inventory),
        "installed_distributions_sha256": sha256_json(observed),
        "python_version": platform.python_version(),
        "venv_path": VENV_PATH,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the frozen ACCELA candidate Python workspace."
    )
    parser.add_argument("--root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = verify_installed_environment(root=args.root)
    except (ConfigurationError, ValidationError, OSError) as exc:
        print(f"candidate-workspace: {exc}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(canonical_json_bytes(result) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
