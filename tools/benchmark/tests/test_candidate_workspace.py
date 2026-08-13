from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tools.benchmark.candidate_workspace import (
    INVENTORY_PATH,
    LOCK_PATH,
    candidate_python_contract,
    normalize_distribution_name,
)
from tools.benchmark.errors import ValidationError
from tools.benchmark.util import sha256_file


ROOT = Path(__file__).resolve().parents[3]


def _copy_contract_files(target: Path) -> None:
    for relative in (INVENTORY_PATH, LOCK_PATH):
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)


def test_repository_candidate_python_contract_is_exact_and_hash_bound() -> None:
    contract = candidate_python_contract(root=ROOT)

    assert contract["version"] == "3.14.6"
    assert contract["platform_system"] == "Linux"
    assert contract["platform_machine"] == "x86_64"
    assert contract["venv_path"] == ".venv"
    assert contract["pip_version"] == "26.1.2"
    assert contract["setuptools_version"] == "83.0.0"
    assert contract["requirements_lock"]["physical_sha256"] == sha256_file(
        ROOT / LOCK_PATH
    )
    assert contract["installed_inventory"]["physical_sha256"] == sha256_file(
        ROOT / INVENTORY_PATH
    )


def test_candidate_python_inventory_rejects_unsorted_or_duplicate_names(
    tmp_path: Path,
) -> None:
    _copy_contract_files(tmp_path)
    inventory_path = tmp_path / INVENTORY_PATH
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["distributions"][0], inventory["distributions"][1] = (
        inventory["distributions"][1],
        inventory["distributions"][0],
    )
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")

    with pytest.raises(ValidationError, match="not sorted"):
        candidate_python_contract(root=tmp_path)

    _copy_contract_files(tmp_path)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["distributions"][1] = dict(inventory["distributions"][0])
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    with pytest.raises(ValidationError, match="not sorted|repeats"):
        candidate_python_contract(root=tmp_path)


def test_candidate_python_lock_rejects_all_zero_hash(tmp_path: Path) -> None:
    _copy_contract_files(tmp_path)
    lock_path = tmp_path / LOCK_PATH
    payload = lock_path.read_text(encoding="utf-8")
    first_hash = payload.index("--hash=sha256:") + len("--hash=sha256:")
    lock_path.write_text(
        payload[:first_hash] + "0" * 64 + payload[first_hash + 64 :],
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="all-zero hash"):
        candidate_python_contract(root=tmp_path)


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("rpds_py", "rpds-py"),
        ("JSONSchema.Specifications", "jsonschema-specifications"),
        ("accela-benchmark", "accela-benchmark"),
    ],
)
def test_distribution_names_use_pep503_normalization(
    raw: str, normalized: str
) -> None:
    assert normalize_distribution_name(raw) == normalized
