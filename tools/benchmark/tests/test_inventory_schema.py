from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from tools.benchmark.errors import ValidationError
from tools.benchmark.audit import build_cross_suite_audit
from tools.benchmark.inventory import inventory_cleanroom_manifest, inventory_suite, subset_manifest
from tools.benchmark.schema import validate_document
from tools.benchmark.util import atomic_write_json


def test_inventory_records_orphans_duplicates_and_source_groups(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    for stem in ("matrix1", "matrix2"):
        (suite / f"{stem}.sy").write_bytes(stem.encode())
        (suite / f"{stem}.out").write_bytes(b"0\n")
        (suite / f"{stem}.in").write_bytes(b"same-input")
    (suite / "legacy.in").write_bytes(b"same-input")

    with pytest.raises(ValidationError, match="orphan"):
        inventory_suite(
            suite,
            suite_id="quality-suite",
            target="rv64gc",
            data_role="B3",
            origin_source="finals-snapshot",
            origin_snapshot_sha256="a" * 64,
            license_expression="NOASSERTION",
        )

    manifest = inventory_suite(
        suite,
        suite_id="quality-suite",
        target="rv64gc",
        data_role="B3",
        origin_source="finals-snapshot",
        origin_snapshot_sha256="a" * 64,
        license_expression="NOASSERTION",
        ignore_orphans=True,
    )
    quality = manifest["data_quality"]
    assert manifest["provenance"]["data_role"] == "B3"
    assert manifest["provenance"]["validity"] == {
        "status": "included_with_exclusions",
        "reason": "packaging_defect",
    }
    assert all(case["provenance"]["validity"]["status"] == "included" for case in manifest["cases"])
    assert manifest["provenance"]["origin"]["snapshot_sha256"] == "a" * 64
    assert manifest["provenance"]["license"]["spdx_expression"] == "NOASSERTION"
    assert quality["orphan_count"] == 1
    orphan = quality["orphan_sidecars"][0]
    assert {key: orphan[key] for key in ("logical_id", "suffix", "role", "size_bytes")} == {
        "logical_id": "legacy",
        "suffix": ".in",
        "role": "orphan",
        "size_bytes": len(b"same-input"),
    }
    duplicate = next(
        group for group in quality["duplicate_file_groups"] if group["size_bytes"] == len(b"same-input")
    )
    assert {member["logical_id"] for member in duplicate["members"]} == {"matrix1", "matrix2", "legacy"}
    assert quality["source_group_count"] == 2
    assert sum(group["case_count"] for group in quality["source_groups"]) == 2
    assert all(group["source_group"].startswith("sg-") for group in quality["source_groups"])
    assert quality["family_groups"] == [
        {"family": "matrix", "target": "rv64gc", "case_count": 2, "distinct_source_groups": 2}
    ]
    assert quality["orphan_sidecars"][0]["provenance"]["validity"] == {
        "status": "excluded",
        "reason": "packaging_defect",
    }
    validate_document(manifest, suite_root=suite, verify_files=True)


def test_excluded_suite_keeps_verified_case_bytes_for_audit(tmp_path: Path) -> None:
    suite = tmp_path / "excluded"
    suite.mkdir()
    (suite / "duplicate.sy").write_bytes(b"source")
    (suite / "duplicate.out").write_bytes(b"0\n")
    manifest = inventory_suite(
        suite, suite_id="duplicate-audit", target="rv64gc", data_role="B1",
        origin_source="duplicate-snapshot", origin_snapshot_sha256="d" * 64,
        license_expression="NOASSERTION", validity_status="excluded",
        validity_reason="manual_exclusion",
    )
    assert manifest["provenance"]["validity"] == {
        "status": "excluded", "reason": "manual_exclusion"
    }
    assert manifest["cases"][0]["provenance"]["validity"] == {
        "status": "included", "reason": "verified"
    }


def test_schema_is_strict_and_checks_quality_counts(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "one.sy").write_bytes(b"x")
    (suite / "one.out").write_bytes(b"")
    manifest = inventory_suite(
        suite,
        suite_id="strict-suite",
        target="rv64gc",
        data_role="B1",
        origin_source="strict-snapshot",
        origin_snapshot_sha256="b" * 64,
        license_expression="Apache-2.0",
    )

    extra = deepcopy(manifest)
    extra["unexpected"] = True
    with pytest.raises(ValidationError, match="Additional properties"):
        validate_document(extra)

    wrong_count = deepcopy(manifest)
    wrong_count["data_quality"]["orphan_count"] = 1
    with pytest.raises(ValidationError, match="orphan_count"):
        validate_document(wrong_count)

    wrong_target = deepcopy(manifest)
    wrong_target["cases"][0]["target"] = "arm64"
    wrong_target["data_quality"]["family_groups"][0]["target"] = "arm64"
    with pytest.raises(ValidationError, match="rv64gc"):
        validate_document(wrong_target)

    for private_value in ("C:/Users/alice/Downloads/private", "E:\\files\\private", "/home/alice/private"):
        leaked = deepcopy(manifest)
        leaked["suite_id"] = private_value
        with pytest.raises(ValidationError, match="absolute paths"):
            validate_document(leaked)
    for diagnostic in (
        "failed(path:/home/alice/case.sy)",
        "see(C:\\Users\\alice\\private.sy)",
    ):
        leaked = deepcopy(manifest)
        leaked["provenance"]["license"]["spdx_expression"] = diagnostic
        with pytest.raises(ValidationError, match="absolute paths"):
            validate_document(leaked)
    allowed_url = deepcopy(manifest)
    allowed_url["provenance"]["license"]["spdx_expression"] = "https://example.invalid/spec"
    allowed_url["cases"][0]["provenance"]["license"]["spdx_expression"] = "https://example.invalid/spec"
    validate_document(allowed_url)


def test_inventory_is_byte_deterministic_without_captured_at(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "stable.sy").write_bytes(b"stable-source")
    (suite / "stable.out").write_bytes(b"0\n")
    kwargs = {
        "suite_id": "stable-suite",
        "target": "rv64gc",
        "data_role": "B1",
        "origin_source": "stable-snapshot",
        "origin_snapshot_sha256": "c" * 64,
        "license_expression": "NOASSERTION",
    }
    first = inventory_suite(suite, **kwargs)
    second = inventory_suite(suite, **kwargs)
    assert first == second
    assert "captured_at" not in first
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    atomic_write_json(first_path, first)
    atomic_write_json(second_path, second)
    assert first_path.read_bytes() == second_path.read_bytes()


def test_repository_cleanroom_manifest_normalizes_b5_b6_and_paired_oracles() -> None:
    corpus_manifest = Path(__file__).resolve().parents[3] / "benchmarks" / "manifest.json"
    common = dict(target="rv64gc", origin_source="repo-cleanroom")
    b5 = inventory_cleanroom_manifest(corpus_manifest, suite_id="B5", data_role="B5", **common)
    b6 = inventory_cleanroom_manifest(
        corpus_manifest, suite_id="B6-medium", data_role="B6", tiers=["medium"], **common
    )
    oracle = inventory_cleanroom_manifest(
        corpus_manifest, suite_id="oracle-medium", data_role="oracle", tiers=["medium"], **common
    )
    oracle_baseline = inventory_cleanroom_manifest(
        corpus_manifest,
        suite_id="oracle-medium-baseline",
        data_role="oracle",
        tiers=["medium"],
        oracle_legs=["baseline"],
        **common,
    )
    assert len(b5["cases"]) == 60
    assert len(b5["data_quality"]["family_groups"]) == 20
    assert len(b6["cases"]) == 22
    assert len(oracle["cases"]) == 66
    assert len(oracle_baseline["cases"]) == 33
    assert all(case["oracle_pair"]["counterpart_case_id"].endswith(":optimized") for case in oracle_baseline["cases"])


def test_b2_subset_selects_exactly_one_case_per_twenty_b3_families(tmp_path: Path) -> None:
    suite = tmp_path / "b3"
    suite.mkdir()
    selected_ids = []
    for family_index in range(20):
        for tier in range(1, 4):
            stem = f"family{family_index:02d}_{tier}"
            (suite / f"{stem}.sy").write_bytes(f"source-{family_index}-{tier}".encode())
            (suite / f"{stem}.in").write_bytes(f"input-{tier}".encode())
            (suite / f"{stem}.out").write_bytes(b"0\n")
            if tier == 2:
                selected_ids.append(f"rv64gc:{stem}")
    b3 = inventory_suite(
        suite,
        suite_id="B3",
        target="rv64gc",
        data_role="B3",
        origin_source="finals",
        origin_snapshot_sha256="d" * 64,
        license_expression="NOASSERTION",
    )
    b3_path = tmp_path / "b3.json"
    atomic_write_json(b3_path, b3)
    b2 = subset_manifest(
        b3_path,
        suite_root=suite,
        suite_id="B2",
        case_ids=selected_ids,
        data_role="B2",
        origin_source="B3-medium-selector",
        origin_snapshot_sha256=None,
        license_expression=None,
        require_one_per_family=True,
    )
    assert len(b2["cases"]) == 20
    assert len(b2["data_quality"]["family_groups"]) == 20
    assert all(group["case_count"] == 1 for group in b2["data_quality"]["family_groups"])
    assert b2["provenance"]["origin"]["snapshot_sha256"] == b2["provenance"]["derived_from"]["manifest_sha256"]
    assert b2["provenance"]["derived_from"]["suite_id"] == "B3"


def test_cross_suite_audit_proves_identical_renamed_content_without_paths(tmp_path: Path) -> None:
    manifests = []
    for suite_name, stem in (("old", "case-old"), ("new", "renamed-case")):
        suite = tmp_path / suite_name
        suite.mkdir()
        (suite / f"{stem}.sy").write_bytes(b"same source")
        (suite / f"{stem}.in").write_bytes(b"same input")
        (suite / f"{stem}.out").write_bytes(b"same output")
        manifest = inventory_suite(
            suite,
            suite_id=suite_name,
            target="rv64gc",
            data_role="B3",
            origin_source=suite_name,
            origin_snapshot_sha256=("a" if suite_name == "old" else "b") * 64,
            license_expression="NOASSERTION",
        )
        path = tmp_path / f"{suite_name}.json"
        atomic_write_json(path, manifest)
        manifests.append((path, suite))
    audit = build_cross_suite_audit(
        left_path=manifests[0][0], right_path=manifests[1][0],
        left_root=manifests[0][1], right_root=manifests[1][1],
    )
    assert audit["identical_content_multiset"] is True
    assert audit["renamed"] is True
    assert str(tmp_path) not in str(audit)


def test_cross_suite_audit_exposes_signature_multiplicity_mismatch(tmp_path: Path) -> None:
    roots = []
    manifests = []
    for name, stems in (("left", ("a", "b")), ("right", ("c",))):
        root = tmp_path / name
        root.mkdir()
        for stem in stems:
            (root / f"{stem}.sy").write_bytes(b"same-source")
            (root / f"{stem}.in").write_bytes(b"same-input")
            (root / f"{stem}.out").write_bytes(b"same-output")
        manifest = inventory_suite(
            root, suite_id=name, target="rv64gc", data_role="B3", origin_source=name,
            origin_snapshot_sha256=("a" if name == "left" else "b") * 64,
            license_expression="NOASSERTION",
        )
        path = tmp_path / f"{name}.json"
        atomic_write_json(path, manifest)
        roots.append(root)
        manifests.append(path)
    audit = build_cross_suite_audit(
        left_path=manifests[0], right_path=manifests[1], left_root=roots[0], right_root=roots[1]
    )
    assert audit["identical_content_multiset"] is False
    assert audit["mappings"][0]["status"] == "multiplicity_mismatch"
    assert audit["counts"]["matched_cases"] == 1
    assert audit["counts"]["left_only_cases"] == 1
