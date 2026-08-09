from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from tools.benchmark.ablation import build_ablation_remark
from tools.benchmark.errors import ValidationError
from tools.benchmark.report import build_report
from tools.benchmark.schema import validate_document
from tools.benchmark.tests.test_stats_ablation_report import make_run, write_matrix
from tools.benchmark.util import atomic_write_json, sha256_json


def _bind_suite(run: dict, *, suite_id: str, data_role: str, manifest_sha256: str) -> dict:
    run = deepcopy(run)
    run["suite_id"] = suite_id
    run["manifest_sha256"] = manifest_sha256
    for case in run["cases"]:
        case["data_role"] = data_role
    return validate_document(run)


def _write(path: Path, document: dict) -> Path:
    atomic_write_json(path, document)
    return path


def _build_singleton_study(
    tmp_path: Path,
    *,
    stem: str,
    suite_id: str,
    data_role: str,
    manifest_sha256: str,
    without_value: float,
) -> tuple[dict, Path, Path, Path]:
    baseline = _bind_suite(
        make_run(f"{stem}-full", {"case": ("family", 100)}),
        suite_id=suite_id,
        data_role=data_role,
        manifest_sha256=manifest_sha256,
    )
    variant = _bind_suite(
        make_run(
            f"{stem}-without",
            {"case": ("family", without_value)},
            profile_id="without.shared-opt",
            profile_sha256="4" * 64,
        ),
        suite_id=suite_id,
        data_role=data_role,
        manifest_sha256=manifest_sha256,
    )
    baseline_path = _write(tmp_path / f"{stem}-full.json", baseline)
    variant_path = _write(tmp_path / f"{stem}-without.json", variant)
    matrix_path = write_matrix(tmp_path / f"{stem}-matrix.json", ("shared-opt",))
    study = build_ablation_remark(
        matrix_path=matrix_path,
        baseline_path=baseline_path,
        variant_paths={"without.shared-opt": variant_path},
        interaction_paths={},
        study_id=f"{stem}-study",
        title=f"{data_role} study",
    )
    study_path = _write(tmp_path / f"{stem}-study.json", study)
    return study, baseline_path, variant_path, study_path


def test_ablation_study_binds_suite_and_report_ranks_each_suite_independently(
    tmp_path: Path,
) -> None:
    b2, b2_full, b2_without, b2_study = _build_singleton_study(
        tmp_path,
        stem="b2",
        suite_id="smoke-20",
        data_role="B2",
        manifest_sha256="a" * 64,
        without_value=150,
    )
    b3, _, _, b3_study = _build_singleton_study(
        tmp_path,
        stem="b3",
        suite_id="official-60",
        data_role="B3",
        manifest_sha256="b" * 64,
        without_value=120,
    )
    _, _, _, b4_study = _build_singleton_study(
        tmp_path,
        stem="b4",
        suite_id="historical-holdout",
        data_role="B4",
        manifest_sha256="c" * 64,
        without_value=130,
    )
    _, _, _, b6_study = _build_singleton_study(
        tmp_path,
        stem="b6",
        suite_id="mature-cleanroom",
        data_role="B6",
        manifest_sha256="d" * 64,
        without_value=140,
    )

    assert (b2["suite_id"], b2["data_role"], b2["manifest_sha256"]) == (
        "smoke-20",
        "B2",
        "a" * 64,
    )
    assert b2["baseline_run_id"] == "b2-full"
    assert b2["variants"][0]["run_id"] == "b2-without"

    missing_binding = deepcopy(b2)
    del missing_binding["manifest_sha256"]
    with pytest.raises(ValidationError, match="manifest_sha256"):
        validate_document(missing_binding)

    mismatched_variant = _bind_suite(
        make_run(
            "b2-wrong-role",
            {"case": ("family", 150)},
            profile_id="without.shared-opt",
            profile_sha256="4" * 64,
        ),
        suite_id="smoke-20",
        data_role="B4",
        manifest_sha256="a" * 64,
    )
    mismatched_path = _write(tmp_path / "b2-wrong-role.json", mismatched_variant)
    with pytest.raises(ValidationError, match="data_role differs"):
        build_ablation_remark(
            matrix_path=tmp_path / "b2-matrix.json",
            baseline_path=b2_full,
            variant_paths={"without.shared-opt": mismatched_path},
            interaction_paths={},
            study_id="mismatched-study",
            title="mismatched",
        )

    output = tmp_path / "report"
    summary = build_report(
        run_path=b2_without,
        baseline_path=b2_full,
        ablation_paths=[b2_study, b6_study, b3_study, b4_study],
        output_directory=output,
    )
    groups = summary["optimization_evidence_by_suite"]
    assert [(group["data_role"], group["suite_id"]) for group in groups] == [
        ("B3", "official-60"),
        ("B2", "smoke-20"),
        ("B4", "historical-holdout"),
        ("B6", "mature-cleanroom"),
    ]
    assert [group["ranking"][0]["rank"] for group in groups] == [1, 1, 1, 1]
    assert {group["ranking"][0]["optimization_id"] for group in groups} == {"shared-opt"}
    assert summary["optimization_ranking"][0]["data_role"] == "B3"
    assert summary["optimization_ranking"][0]["case_geometric_mean_contribution"] == pytest.approx(1.2)
    assert b3["variants"][0]["case_geometric_mean_contribution"] == pytest.approx(1.2)

    markdown = (output / "report.md").read_text(encoding="utf-8")
    assert "已实现优化实测收益主榜（B3 官方代理）" in markdown
    assert "B2 独立消融证据" in markdown
    assert "B4 独立消融证据" in markdown
    assert "B6 独立消融证据" in markdown
    assert all(
        suite in markdown
        for suite in ("official-60", "smoke-20", "historical-holdout", "mature-cleanroom")
    )


def _oracle_plan_and_runs(
    *,
    label: str,
    family: str,
    pair_id: str,
    baseline_source_sha256: str,
    optimized_source_sha256: str,
) -> tuple[dict, dict, dict]:
    baseline_case_id = f"{label}-baseline-case"
    optimized_case_id = f"{label}-optimized-case"
    baseline = make_run(
        f"{label}-baseline-run",
        {baseline_case_id: (family, 100)},
        profile_id="oracle-full",
        profile_sha256="c" * 64,
    )
    optimized = make_run(
        f"{label}-optimized-run",
        {optimized_case_id: (family, 50)},
        profile_id="oracle-full",
        profile_sha256="c" * 64,
    )
    for record, leg, source_sha256, counterpart in (
        (baseline, "baseline", baseline_source_sha256, optimized_case_id),
        (optimized, "optimized", optimized_source_sha256, baseline_case_id),
    ):
        record["suite_id"] = f"{label}-{leg}"
        case = record["cases"][0]
        case["family"] = family
        case["source_sha256"] = source_sha256
        case["source_group"] = f"sg-{source_sha256}"
        case["data_role"] = "B3"
        case["oracle_pair"] = {
            "pair_id": pair_id,
            "leg": leg,
            "counterpart_case_id": counterpart,
        }
        validate_document(record)
    plan = validate_document(
        {
            "schema_version": "oracle-plan.v1",
            "evidence_class": "official",
            "manifest_data_role": "B3",
            "suite_id": f"{label}-suite",
            "manifest_sha256": "d" * 64,
            "pipeline_profile": {
                "profile_id": "oracle-full",
                "profile_sha256": "c" * 64,
            },
            "baseline_run_id": baseline["run_id"],
            "optimized_run_id": optimized["run_id"],
            "pairs": [
                {
                    "pair_id": pair_id,
                    "family": family,
                    "target": "rv64gc",
                    "input_sha256": None,
                    "expected_output_sha256": "0" * 64,
                    "baseline": {
                        "case_id": baseline_case_id,
                        "source_group": f"sg-{baseline_source_sha256}",
                        "source_sha256": baseline_source_sha256,
                    },
                    "optimized": {
                        "case_id": optimized_case_id,
                        "source_group": f"sg-{optimized_source_sha256}",
                        "source_sha256": optimized_source_sha256,
                    },
                }
            ],
        }
    )
    return plan, validate_document(baseline), validate_document(optimized)


def test_candidate_rejects_renamed_duplicate_content_across_plans(tmp_path: Path) -> None:
    first_plan, first_baseline, first_optimized = _oracle_plan_and_runs(
        label="first",
        family="family-original",
        pair_id="pair-original",
        baseline_source_sha256="1" * 64,
        optimized_source_sha256="2" * 64,
    )
    renamed_plan, renamed_baseline, renamed_optimized = _oracle_plan_and_runs(
        label="renamed",
        family="family-renamed",
        pair_id="pair-renamed",
        baseline_source_sha256="1" * 64,
        optimized_source_sha256="2" * 64,
    )
    plan_paths = [
        _write(tmp_path / "first-plan.json", first_plan),
        _write(tmp_path / "renamed-plan.json", renamed_plan),
    ]
    run_paths = [
        _write(tmp_path / "first-baseline.json", first_baseline),
        _write(tmp_path / "first-optimized.json", first_optimized),
        _write(tmp_path / "renamed-baseline.json", renamed_baseline),
        _write(tmp_path / "renamed-optimized.json", renamed_optimized),
    ]
    evidence = validate_document(
        {
            "schema_version": "candidate-evidence.v1",
            "snapshot_id": "renamed-content",
            "candidates": [
                {
                    "candidate_id": "candidate",
                    "cleanroom_oracle_family_id": None,
                    "official_oracle_refs": [
                        {
                            "plan_sha256": sha256_json(first_plan),
                            "baseline_run_id": first_baseline["run_id"],
                            "optimized_run_id": first_optimized["run_id"],
                            "family_ids": ["family-original"],
                        },
                        {
                            "plan_sha256": sha256_json(renamed_plan),
                            "baseline_run_id": renamed_baseline["run_id"],
                            "optimized_run_id": renamed_optimized["run_id"],
                            "family_ids": ["family-renamed"],
                        },
                    ],
                    "holdout_or_mature_refs": [],
                    "legality_proof_path": "clear",
                    "legality_obligation_ids": ["proof"],
                    "implementation_cost": "medium",
                    "risk": "low",
                    "specification_status": "clear",
                    "requires_boom_feature": False,
                }
            ],
        }
    )
    evidence_path = _write(tmp_path / "candidate.json", evidence)

    with pytest.raises(ValidationError, match="repeats workload content"):
        build_report(
            run_path=run_paths[1],
            candidate_evidence_path=evidence_path,
            candidate_plan_paths=plan_paths,
            candidate_run_paths=run_paths,
            output_directory=tmp_path / "duplicate-report",
        )

    with pytest.raises(ValidationError, match="non-empty"):
        validate_document(
            {
                "schema_version": "candidate-evidence.v1",
                "snapshot_id": "empty",
                "candidates": [],
            }
        )
