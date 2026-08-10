from __future__ import annotations

import csv
import math
import json
import re
import xml.etree.ElementTree as ET
from copy import deepcopy
from pathlib import Path

import pytest

from tools.benchmark.ablation import build_ablation_remark
from tools.benchmark.errors import ValidationError
from tools.benchmark.cli import main as benchmark_main
from tools.benchmark.report import _hotblock_diagnostics, build_report
from tools.benchmark.metrics import cache_hotblock_metrics_v1, rv64gc_qemu_v1
from tools.benchmark.process import extract_metric
from tools.benchmark.schema import validate_document
from tools.benchmark.stats import PairedCase, bootstrap_geometric_mean_ci, compare_runs, leave_one_family_out
from tools.benchmark.schema import load_and_validate_jsonl
from tools.benchmark.util import atomic_write_json, atomic_write_text, sha256_file, sha256_json, utc_now


SHA = "0" * 64


def _rebind_synthetic_run_configuration(run: dict) -> None:
    configuration_sha256 = sha256_json(
        {"configuration": run["configuration"], "provenance": run["provenance"]}
    )
    run["configuration_sha256"] = configuration_sha256
    for case in run["cases"]:
        assert not case["attempts"]
        if case["attempt_started_at"] is not None:
            case["attempt_configuration_sha256"] = configuration_sha256


def _stage(kind: str) -> dict:
    return {
        "kind": kind,
        "adapter": "host",
        "command_sha256": None,
        "executable": None,
        "environment_keys": [],
    }


def _phase_record() -> dict:
    return {
        "status": "ok",
        "duration_ns": 10,
        "exit_code": 0,
        "stdout": {"sha256": SHA, "size_bytes": 0},
        "stderr": {"sha256": SHA, "size_bytes": 0},
        "diagnostic": None,
    }


def _compile_sample_record(*, with_remarks: bool = True) -> dict:
    return {
        **_phase_record(),
        "artifact_sha256": "8" * 64,
        "artifact_size_bytes": 1,
        "remarks_sha256": "6" * 64 if with_remarks else None,
        "remarks_event_count": 1 if with_remarks else None,
    }


def make_run(
    run_id: str,
    values: dict[str, tuple[str, float]],
    *,
    source: str = "stderr",
    profile_id: str = "full",
    profile_sha256: str = "2" * 64,
) -> dict:
    now = utc_now()
    preset = rv64gc_qemu_v1()
    formal_specs = [
        {
            "metric_id": preset["primary_metric_id"],
            "source": preset["metric_source"],
            "pattern_sha256": sha256_json(preset["metric_pattern"]),
            "unit": preset["metric_unit"],
        },
        *[
            {
                "metric_id": item["metric_id"],
                "source": item["source"],
                "pattern_sha256": (
                    None if item["pattern"] is None else sha256_json(item["pattern"])
                ),
                "unit": item["unit"],
            }
            for item in preset["additional"]
        ],
    ]
    configuration = {
        "compiler": _stage("benchmark-compiler") if source != "wall_time" else _stage("none"),
        "pipeline_profile_file_sha256": profile_sha256 if source != "wall_time" else None,
        "linker": _stage("external") if source != "wall_time" else None,
        "analyzer": _stage("analyzer") if source != "wall_time" else None,
        "runner": _stage("qemu"),
        "primary_metric_id": "dynamic_instruction_count" if source != "wall_time" else "wall_time_ns",
        "metric_profile_id": "rv64gc-qemu-v1" if source != "wall_time" else None,
        "metrics": (formal_specs if source != "wall_time" else [
            {"metric_id": "wall_time_ns", "source": "wall_time", "pattern_sha256": None, "unit": "ns"},
        ]),
        "compile_timeout_seconds": 1.0,
        "compile_repetitions": 5,
        "reuse_compile_cache": False,
        "compile_storage_contract": "attempt_local_v1",
        "link_timeout_seconds": 1.0,
        "analyze_timeout_seconds": 1.0,
        "run_timeout_seconds": 1800.0 if source != "wall_time" else 1.0,
        "timeout_policy": "initial" if source != "wall_time" else "fixed",
        "baseline_timeout_run_sha256": None,
        "baseline_timeout_run_id": None,
        "timeout_minimum_seconds": 120.0,
        "timeout_multiplier": 3.0,
        "timeout_cap_seconds": 1800.0,
        "repetitions": 3,
        "max_workers": 1,
        "keep_going": False,
        "retry_failures": False,
        "seed": 20260809,
        "artifact_suffix": ".s",
        "binary_suffix": ".elf",
        "wsl_distribution_sha256": None,
        "metric_file_sha256": SHA if source != "wall_time" else None,
        "analysis_file_sha256": SHA if source != "wall_time" else None,
        "remarks_file_sha256": SHA if source != "wall_time" else None,
        "result_file_sha256": None,
        "output_contract": "lf_return_trailer",
        "environment_label": "local_reference",
        "evidence_level": "qemu_proxy" if source != "wall_time" else "qemu_correctness",
        "tool_versions": [
            {
                "tool": "riscv-gcc",
                "actual": "13.2",
                "official_expected": "13.3",
                "comparison": "mismatch",
            }
        ],
        "consistency_fraction": 0.1,
        "consistency_repetitions": 3,
    }
    primary = configuration["primary_metric_id"]
    unit = configuration["metrics"][0]["unit"]
    cases = []
    for index, (case_id, (family, value)) in enumerate(sorted(values.items())):
        source_sha256 = f"{index + 1:064x}"
        samples = [
            {
                "index": sample_index,
                "status": "passed",
                "duration_ns": 10,
                "exit_code": 0,
                "measurements": [
                    {
                        "metric_id": spec["metric_id"],
                        "value": value,
                        "unit": spec["unit"],
                        "origin": "observed",
                        "availability": "measured",
                        "reason": None,
                    }
                    for spec in configuration["metrics"]
                    if spec["source"] == ("file" if source != "wall_time" else "wall_time")
                ],
                "censoring": "none",
                "censor_bound": None,
                "censor_unit": None,
                "censor_metric_id": None,
                "stdout": {"sha256": SHA, "size_bytes": 0},
                "program_stdout": {"sha256": SHA, "size_bytes": 0},
                "stderr": {"sha256": SHA, "size_bytes": 0},
                "expected_return_uint8": 0,
                "observed_return_uint8": 0,
                "first_mismatch_offset": None,
                "diagnostic": None,
            }
            for sample_index in range(3)
        ]
        cases.append(
            {
                "case_id": case_id,
                "family": family,
                "source_group": f"sg-{source_sha256}",
                "target": "rv64gc",
                "weight": 1.0,
                "tags": [],
                "data_role": "B3",
                "oracle_pair": None,
                "effective_timeout_seconds": 1800.0 if source != "wall_time" else 1.0,
                "timeout_derivation": None,
                "attempt_index": 0,
                "attempt_started_at": now,
                "attempt_configuration_sha256": None,
                "source_sha256": source_sha256,
                "input_sha256": None,
                "expected_output_sha256": SHA,
                "artifact_sha256": "8" * 64,
                "binary_sha256": "9" * 64,
                "remarks_sha256": "6" * 64 if source != "wall_time" else None,
                "remarks_event_count": 1 if source != "wall_time" else None,
                "candidate_remark_summary": None,
                "analysis_sha256": "7" * 64 if source != "wall_time" else None,
                "attempt_journal_sha256": "5" * 64,
                "attempt_journal_event_count": 1,
                "status": "passed",
                "cancellation_reason": None,
                "cache_hit": False,
                "compile": _phase_record() if source != "wall_time" else None,
                "compile_samples": (
                    [_compile_sample_record() for _ in range(5)] if source != "wall_time" else []
                ),
                "compile_statistics": (
                    {"sample_count": 5, "median_duration_ns": 10.0, "mad_duration_ns": 0.0}
                    if source != "wall_time" else None
                ),
                "link": _phase_record() if source != "wall_time" else None,
                "analyze": _phase_record() if source != "wall_time" else None,
                "measurements": ([
                    {
                        "metric_id": spec["metric_id"],
                        "value": 0.0 if spec["metric_id"] == "static_vector_instructions" else 1.0,
                        "unit": spec["unit"],
                        "origin": "observed",
                        "availability": "measured",
                        "reason": None,
                    }
                    for spec in configuration["metrics"]
                    if spec["source"] != "file"
                ] if source != "wall_time" else []),
                "samples": samples,
                "consistency_selected": False,
                "consistency_passed": None,
                "consistency_mismatched_metrics": [],
                "diagnostic": None,
                "attempts": [],
            }
        )
    selected_case = min(
        cases,
        key=lambda case: sha256_json({"seed": configuration["seed"], "case_id": case["case_id"]}),
    )
    selected_case["consistency_selected"] = True
    selected_case["consistency_passed"] = True
    provenance = {
        "repo_commit": "1" * 40,
        "repo_dirty": False,
        "tracked_diff_sha256": None,
        "pipeline_profile_id": profile_id,
        "pipeline_profile_sha256": profile_sha256,
        "compiler_artifact_sha256": "3" * 64,
        "measurement_protocol_id": "test-rv64gc-qemu" if source != "wall_time" else None,
        "measurement_protocol_sha256": "4" * 64 if source != "wall_time" else None,
    }
    configuration_sha256 = sha256_json(
        {"configuration": configuration, "provenance": provenance}
    )
    for case in cases:
        case["attempt_configuration_sha256"] = configuration_sha256
    run = {
        "schema_version": "run-record.v1",
        "run_id": run_id,
        "suite_id": "analysis-suite",
        "manifest_sha256": SHA,
        "manifest_case_count": len(cases),
        "manifest_case_ids_sha256": sha256_json([case["case_id"] for case in cases]),
        "configuration_sha256": configuration_sha256,
        "started_at": now,
        "updated_at": now,
        "completed_at": now,
        "state": "completed",
        "provenance": provenance,
        "configuration": configuration,
        "cases": cases,
        "summary": {
            "total_cases": len(cases),
            "passed_cases": len(cases),
            "failed_cases": 0,
            "pending_cases": 0,
            "censored_cases": 0,
            "consistency_selected_cases": 1,
            "consistency_passed_cases": 1,
        },
    }
    return validate_document(run)


def write_matrix(path: Path, families: tuple[str, ...]) -> Path:
    profiles = [
        {"profile_id": "full", "kind": "full", "logical_families": [], "profile_sha256": "2" * 64, "path": "profiles/full.json"},
        {"profile_id": "mandatory", "kind": "mandatory", "logical_families": [], "profile_sha256": "3" * 64, "path": "profiles/mandatory.json"},
    ]
    for index, family in enumerate(families, 4):
        profiles.append({
            "profile_id": f"without.{family}", "kind": "family_ablation", "logical_families": [family],
            "profile_sha256": f"{index:x}" * 64, "path": f"profiles/without-{family}.json",
        })
    if len(families) >= 2:
        profiles.append({
            "profile_id": f"without.{families[0]}+{families[1]}", "kind": "pair_ablation",
            "logical_families": [families[0], families[1]], "profile_sha256": "f" * 64,
            "path": "profiles/without-pair.json",
        })
    matrix = {
        "schema_version": "ablation-matrix.v1",
        "registry_sha256": "a" * 64,
        "profiles": profiles,
        "schedule": [
            {
                "baseline_profile_id": "full",
                "candidate_profile_id": profile["profile_id"],
                "kind": "mandatory_control" if profile["kind"] == "mandatory" else profile["kind"],
            }
            for profile in profiles if profile["kind"] != "full"
        ],
        "unschedulable_families": [],
    }
    atomic_write_json(path, validate_document(matrix))
    return path


def make_hotblock_run(
    run_id: str,
    *,
    profile_id: str = "full",
    profile_sha256: str = "2" * 64,
) -> dict:
    run = make_run(
        run_id,
        {"hot-case": ("hot-family", 1_000)},
        profile_id=profile_id,
        profile_sha256=profile_sha256,
    )
    extension = cache_hotblock_metrics_v1()
    run["configuration"]["runner"]["environment_keys"] = [
        "QEMU_HOTBLOCK_PLUGIN"
    ]
    run["configuration"]["metrics"].extend(
        {
            "metric_id": item["metric_id"],
            "source": item["source"],
            "pattern_sha256": sha256_json(item["pattern"]),
            "unit": item["unit"],
        }
        for item in extension
    )
    values = {
        "hotblock_hottest_address": 0x800003BC,
        "hotblock_hottest_executions": 20,
        "hotblock_hottest_dynamic_instructions": 120,
    }
    for case in run["cases"]:
        for sample in case["samples"]:
            sample["measurements"].extend(
                {
                    "metric_id": item["metric_id"],
                    "value": float(values[item["metric_id"]]),
                    "unit": item["unit"],
                    "origin": "observed",
                    "availability": "measured",
                    "reason": None,
                }
                for item in extension
            )
    run["provenance"]["measurement_protocol_id"] = "cache-hotblock-test"
    run["provenance"]["measurement_protocol_sha256"] = "5" * 64
    _rebind_synthetic_run_configuration(run)
    return validate_document(run)


def test_bootstrap_resamples_families_not_individual_cases() -> None:
    pairs = (
        PairedCase("a1", "only-family", "rv64gc", 1, 8, 1),
        PairedCase("a2", "only-family", "rv64gc", 1, 2, 1),
    )
    interval = bootstrap_geometric_mean_ci(pairs, samples=10_000, seed=20260809)
    assert interval is not None
    assert interval[0] == pytest.approx(4.0)
    assert interval[1] == pytest.approx(4.0)


def test_leave_one_family_out_direction_is_without_over_full() -> None:
    pairs = (
        PairedCase("a", "family-a", "rv64gc", 1, 2, 1),
        PairedCase("b", "family-b", "rv64gc", 1, 8, 1),
    )
    result = {row["family"]: row for row in leave_one_family_out(pairs)}
    assert result["family-a"]["metric_full"] == pytest.approx(4.0)
    assert result["family-a"]["metric_without"] == pytest.approx(8.0)
    assert result["family-a"]["contribution_ratio"] == pytest.approx(2.0)
    assert result["family-b"]["contribution_ratio"] == pytest.approx(0.5)


def test_geometric_mean_deduplicates_identical_source_groups() -> None:
    baseline = make_run(
        "baseline-groups",
        {"a": ("family-a", 100), "b": ("family-a", 100), "c": ("family-b", 100)},
    )
    candidate = make_run(
        "candidate-groups",
        {"a": ("family-a", 50), "b": ("family-a", 12.5), "c": ("family-b", 50)},
    )
    for run in (baseline, candidate):
        run["cases"][1]["source_sha256"] = run["cases"][0]["source_sha256"]
        run["cases"][1]["source_group"] = run["cases"][0]["source_group"]
        validate_document(run)
    comparison = compare_runs(baseline, candidate)
    assert len({pair.source_group_id for pair in comparison.pairs}) == 2
    assert comparison.geometric_mean_speedup == pytest.approx(32 ** (1 / 3))
    assert comparison.source_group_geometric_mean_speedup == pytest.approx(math.sqrt(8))


def test_comparison_rejects_measurement_protocol_drift() -> None:
    baseline = make_run("protocol-baseline", {"a": ("family-a", 100)})
    candidate = make_run("protocol-candidate", {"a": ("family-a", 90)})
    candidate["provenance"]["measurement_protocol_sha256"] = "5" * 64
    _rebind_synthetic_run_configuration(candidate)
    validate_document(candidate)
    with pytest.raises(ValidationError, match="measurement protocol changed"):
        compare_runs(baseline, candidate)


def test_ablation_interaction_delta_and_report_artifacts(tmp_path: Path) -> None:
    baseline = make_run("baseline", {"a": ("family-a", 100), "b": ("family-b", 100)})
    variant_a = make_run(
        "variant-a", {"a": ("family-a", 120), "b": ("family-b", 120)},
        profile_id="without.opt-a", profile_sha256="4" * 64,
    )
    variant_b = make_run(
        "variant-b", {"a": ("family-a", 130), "b": ("family-b", 130)},
        profile_id="without.opt-b", profile_sha256="5" * 64,
    )
    combined = make_run(
        "combined", {"a": ("family-a", 170), "b": ("family-b", 170)},
        profile_id="without.opt-a+opt-b", profile_sha256="f" * 64,
    )
    paths = {}
    for name, run in (("baseline", baseline), ("a", variant_a), ("b", variant_b), ("combined", combined)):
        path = tmp_path / f"{name}.json"
        atomic_write_json(path, run)
        paths[name] = path

    remark = build_ablation_remark(
        matrix_path=write_matrix(tmp_path / "matrix.json", ("opt-a", "opt-b")),
        baseline_path=paths["baseline"],
        variant_paths={"without.opt-a": paths["a"], "without.opt-b": paths["b"]},
        interaction_paths={("opt-a", "opt-b"): paths["combined"]},
        study_id="study",
        title="Study",
    )
    interaction = remark["interactions"][0]
    assert remark["variants"][0]["case_geometric_mean_contribution"] == pytest.approx(1.2)
    assert interaction["expected_multiplicative_contribution"] == pytest.approx(1.56)
    assert interaction["interaction_factor"] == pytest.approx(1.7 / 1.56)
    assert interaction["delta_ln_geometric_mean"] == pytest.approx(math.log(1.7 / 1.56))
    assert remark["bootstrap_samples"] == 10_000
    assert remark["seed"] == 20260809
    assert remark["schema_version"] == "ablation-study.v1"
    remark_path = tmp_path / "remark.json"
    atomic_write_json(remark_path, remark)
    pass_events = [
        {
            "schema_version": "optimization-remark.v1",
            "sequence": 1,
            "event_type": "pass_summary",
            "pass": "gvn",
            "occurrence": 1,
            "stage": "ir_function",
            "target_kind": "function",
            "target_name": "main",
            "elapsed_ns": 12,
            "changed": True,
            "before": {"instructions": 10},
            "after": {"instructions": 8},
            "delta": {"instructions": -2},
            "details": {},
            "decision_observability": "available",
        },
        {
            "schema_version": "optimization-remark.v1",
            "sequence": 2,
            "event_type": "decision",
            "pass": "gvn",
            "occurrence": 1,
            "stage": "ir_function",
            "target_kind": "function",
            "target_name": "main",
            "decision": "candidate",
            "reason": "candidate_matched",
        },
    ]
    events_path = tmp_path / "remarks.jsonl"
    atomic_write_text(events_path, "".join(json.dumps(event) + "\n" for event in pass_events))
    assert len(load_and_validate_jsonl(events_path)) == 2
    events_sha256 = sha256_file(events_path)
    for record, path in ((baseline, paths["baseline"]), (variant_a, paths["a"])):
        record["configuration"]["remarks_file_sha256"] = sha256_json("remarks.jsonl")
        for case in record["cases"]:
            case["remarks_sha256"] = events_sha256
        _rebind_synthetic_run_configuration(record)
        atomic_write_json(path, validate_document(record))

    output = tmp_path / "report"
    summary = build_report(
        run_path=paths["a"],
        baseline_path=paths["baseline"],
        remark_paths={"a": events_path},
        ablation_paths=[remark_path],
        output_directory=output,
    )
    assert summary["environment_label"] == "local_reference"
    assert summary["tool_versions"][0]["comparison"] == "mismatch"
    assert summary["pass_remarks"][0]["pass"] == "gvn"
    assert summary["pass_remarks"][0]["decisions"]["candidate"] == 1
    assert summary["optimization_ranking"][0]["baseline_run_id"] == "baseline"
    assert summary["optimization_ranking"][0]["variant_run_id"] in {"variant-a", "variant-b"}
    assert summary["ablation_interactions"][0]["run_id"] == "combined"
    report_text = (output / "report.md").read_text(encoding="utf-8")
    assert "Top5 双消融交互" in report_text
    assert "combined" in report_text
    expected_artifacts = {
        "cases.csv", "summary.json", "report.md", "speedups.svg", "ablation-waterfall.svg",
        "family-pass-heatmap.svg", "toolchain-gap.svg", "oracle-scaling.svg", "benefit-cost-risk-pareto.svg",
    }
    assert {path.name for path in output.iterdir()} == expected_artifacts
    for name in expected_artifacts:
        if name.endswith(".svg"):
            ET.parse(output / name)
    markdown = (output / "report.md").read_text(encoding="utf-8")
    assert "不代表官方比赛环境" in markdown
    assert "13.2" in markdown and "13.3" in markdown and "mismatch" in markdown


def test_wall_clock_cannot_produce_main_optimization_ranking(tmp_path: Path) -> None:
    baseline = make_run("baseline-wall", {"a": ("family-a", 100)}, source="wall_time")
    candidate = make_run(
        "candidate-wall", {"a": ("family-a", 90)}, source="wall_time",
        profile_id="without.candidate", profile_sha256="4" * 64,
    )
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    atomic_write_json(baseline_path, baseline)
    atomic_write_json(candidate_path, candidate)
    with pytest.raises(ValidationError, match="wall-clock"):
        build_ablation_remark(
            matrix_path=write_matrix(tmp_path / "matrix.json", ("candidate",)),
            baseline_path=baseline_path,
            variant_paths={"without.candidate": candidate_path},
            interaction_paths={},
            study_id="wall-study",
            title="Wall proxy",
        )


def test_oracle_plan_report_and_candidate_priority_use_distinct_source_legs(tmp_path: Path) -> None:
    baseline = make_run(
        "oracle-baseline:oracle-suite", {"baseline-case": ("loop-summary", 100)},
        profile_id="oracle-full", profile_sha256="c" * 64,
    )
    optimized = make_run(
        "oracle-optimized:oracle-suite", {"optimized-case": ("loop-summary", 50)},
        profile_id="oracle-full", profile_sha256="c" * 64,
    )
    for record, leg, case_id, counterpart, source_sha in (
        (baseline, "baseline", "baseline-case", "optimized-case", "1" * 64),
        (optimized, "optimized", "optimized-case", "baseline-case", "2" * 64),
    ):
        record["suite_id"] = f"oracle-suite-{leg}"
        case = record["cases"][0]
        case["case_id"] = case_id
        case["family"] = "loop-summary"
        case["source_sha256"] = source_sha
        case["source_group"] = f"sg-{source_sha}"
        case["data_role"] = "oracle"
        case["tags"] = [f"oracle-leg:{leg}", "oracle-variant:synthetic", "tier:medium"]
        case["oracle_pair"] = {
            "pair_id": "loop-summary:synthetic:medium", "leg": leg,
            "counterpart_case_id": counterpart,
        }
        validate_document(record)
    baseline_path = tmp_path / "oracle-baseline.json"
    optimized_path = tmp_path / "oracle-optimized.json"
    atomic_write_json(baseline_path, baseline)
    atomic_write_json(optimized_path, optimized)
    plan = validate_document({
        "schema_version": "oracle-plan.v1", "evidence_class": "cleanroom",
        "manifest_data_role": "oracle", "suite_id": "oracle-suite",
        "manifest_sha256": "d" * 64,
        "pipeline_profile": {"profile_id": "oracle-full", "profile_sha256": "c" * 64},
        "baseline_run_id": baseline["run_id"], "optimized_run_id": optimized["run_id"],
        "pairs": [{
            "pair_id": "loop-summary:synthetic:medium", "family": "loop-summary", "target": "rv64gc",
            "input_sha256": None, "expected_output_sha256": SHA,
            "baseline": {"case_id": "baseline-case", "source_group": "sg-" + "1" * 64, "source_sha256": "1" * 64},
            "optimized": {"case_id": "optimized-case", "source_group": "sg-" + "2" * 64, "source_sha256": "2" * 64},
        }],
    })
    plan_path = tmp_path / "oracle-plan.json"
    atomic_write_json(plan_path, plan)
    candidate = validate_document({
        "schema_version": "candidate-evidence.v1", "snapshot_id": "candidate-snapshot",
        "candidates": [{
            "candidate_id": "loop-summary", "cleanroom_oracle_family_id": "loop-summary",
            "official_oracle_refs": [], "holdout_or_mature_refs": [],
            "legality_proof_path": "clear", "legality_obligation_ids": ["integer-overflow"],
            "implementation_cost": "medium", "risk": "low",
            "specification_status": "clear", "requires_boom_feature": False,
        }],
    })
    candidate_path = tmp_path / "candidate.json"
    atomic_write_json(candidate_path, candidate)
    output = tmp_path / "oracle-report"
    summary = build_report(
        run_path=optimized_path, baseline_path=baseline_path, oracle_plan_path=plan_path,
        candidate_evidence_path=candidate_path, output_directory=output,
    )
    assert summary["comparison"] is None
    assert summary["rankings"]["oracle"][0]["geometric_mean_speedup"] == pytest.approx(2.0)
    implementation = summary["rankings"]["implementation_priority"][0]
    assert implementation["priority"] == "P2"
    assert implementation["cleanroom_oracle_geometric_mean_upper_bound"] == pytest.approx(2.0)
    assert implementation["official_oracle_geometric_mean"] is None
    assert "baseline/optimized" in (output / "oracle-scaling.svg").read_text(encoding="utf-8")

    mislabeled = deepcopy(candidate)
    mislabeled["snapshot_id"] = "candidate-mislabeled"
    mislabeled["candidates"][0]["official_oracle_refs"] = [{
        "plan_sha256": sha256_json(plan),
        "baseline_run_id": baseline["run_id"],
        "optimized_run_id": optimized["run_id"],
        "family_ids": ["loop-summary"],
    }]
    mislabeled_path = tmp_path / "candidate-mislabeled.json"
    atomic_write_json(mislabeled_path, validate_document(mislabeled))
    with pytest.raises(ValidationError, match="official Oracle plan"):
        build_report(
            run_path=optimized_path, baseline_path=baseline_path, oracle_plan_path=plan_path,
            candidate_evidence_path=mislabeled_path, output_directory=tmp_path / "mislabeled-report",
        )

    official_plan = deepcopy(plan)
    official_plan["evidence_class"] = "official"
    official_plan["manifest_data_role"] = "B3"
    official_plan["baseline_run_id"] = "official-baseline"
    official_plan["optimized_run_id"] = "official-optimized"
    holdout_plan = deepcopy(plan)
    holdout_plan["evidence_class"] = "holdout_or_mature"
    holdout_plan["manifest_data_role"] = "B6"
    holdout_plan["baseline_run_id"] = "holdout-baseline"
    holdout_plan["optimized_run_id"] = "holdout-optimized"
    holdout_plan["pairs"][0]["input_sha256"] = "8" * 64
    holdout_plan["pairs"][0]["expected_output_sha256"] = "9" * 64
    holdout_plan["pairs"][0]["baseline"]["source_sha256"] = "e" * 64
    holdout_plan["pairs"][0]["baseline"]["source_group"] = "sg-" + "e" * 64
    holdout_plan["pairs"][0]["optimized"]["source_sha256"] = "f" * 64
    holdout_plan["pairs"][0]["optimized"]["source_group"] = "sg-" + "f" * 64
    official_plan_path = tmp_path / "official-plan.json"
    holdout_plan_path = tmp_path / "holdout-plan.json"
    atomic_write_json(official_plan_path, validate_document(official_plan))
    atomic_write_json(holdout_plan_path, validate_document(holdout_plan))
    referenced = deepcopy(candidate)
    referenced["snapshot_id"] = "candidate-referenced"
    referenced_candidate = referenced["candidates"][0]
    referenced_candidate["official_oracle_refs"] = [{
        "plan_sha256": sha256_json(official_plan),
        "baseline_run_id": official_plan["baseline_run_id"],
        "optimized_run_id": official_plan["optimized_run_id"],
        "family_ids": ["loop-summary"],
    }]
    referenced_candidate["holdout_or_mature_refs"] = [{
        "plan_sha256": sha256_json(holdout_plan),
        "baseline_run_id": holdout_plan["baseline_run_id"],
        "optimized_run_id": holdout_plan["optimized_run_id"],
        "pair_ids": ["loop-summary:synthetic:medium"],
    }]
    referenced_path = tmp_path / "candidate-referenced.json"
    atomic_write_json(referenced_path, validate_document(referenced))
    candidate_run_paths = []
    for name, source_record, run_id, data_role in (
        ("official-baseline", baseline, "official-baseline", "B3"),
        ("official-optimized", optimized, "official-optimized", "B3"),
        ("holdout-baseline", baseline, "holdout-baseline", "B6"),
        ("holdout-optimized", optimized, "holdout-optimized", "B6"),
    ):
        candidate_run = deepcopy(source_record)
        candidate_run["run_id"] = run_id
        candidate_run["cases"][0]["data_role"] = data_role
        if data_role == "B6":
            source_sha = ("e" if name.endswith("baseline") else "f") * 64
            candidate_run["cases"][0]["source_sha256"] = source_sha
            candidate_run["cases"][0]["source_group"] = f"sg-{source_sha}"
            candidate_run["cases"][0]["input_sha256"] = "8" * 64
            candidate_run["cases"][0]["expected_output_sha256"] = "9" * 64
        candidate_path = tmp_path / f"{name}.json"
        atomic_write_json(candidate_path, validate_document(candidate_run))
        candidate_run_paths.append(candidate_path)
    referenced_summary = build_report(
        run_path=optimized_path, baseline_path=baseline_path, oracle_plan_path=plan_path,
        candidate_evidence_path=referenced_path,
        candidate_plan_paths=[official_plan_path, holdout_plan_path],
        candidate_run_paths=candidate_run_paths,
        output_directory=tmp_path / "referenced-report",
    )
    referenced_priority = referenced_summary["rankings"]["implementation_priority"][0]
    assert referenced_priority["priority"] == "P1"
    assert referenced_priority["official_oracle_geometric_mean"] == pytest.approx(2.0)
    assert referenced_priority["maximum_official_family_upper_bound"] == pytest.approx(2.0)
    assert referenced_priority["holdout_or_mature_hits"] == 1


def test_run_state_semantics_reject_impossible_completed_and_failed_records() -> None:
    run = make_run("state", {"a": ("family-a", 1)})
    impossible_completed = deepcopy(run)
    impossible_completed["completed_at"] = None
    with pytest.raises(ValidationError, match="completed run"):
        validate_document(impossible_completed)
    impossible_failed = deepcopy(run)
    impossible_failed["state"] = "failed"
    with pytest.raises(ValidationError, match="failed run"):
        validate_document(impossible_failed)


def test_run_commitments_reject_case_sample_or_consistency_selection_tampering() -> None:
    run = make_run("commitments", {"a": ("family-a", 1), "b": ("family-b", 1)})
    selected = next(case for case in run["cases"] if case["consistency_selected"])
    other = next(case for case in run["cases"] if not case["consistency_selected"])
    swapped = deepcopy(run)
    for case in swapped["cases"]:
        case["consistency_selected"] = case["case_id"] == other["case_id"]
        case["consistency_passed"] = True if case["consistency_selected"] else None
    with pytest.raises(ValidationError, match="fixed seed"):
        validate_document(swapped)

    shortened = deepcopy(run)
    next(case for case in shortened["cases"] if case["case_id"] == other["case_id"])["samples"].pop()
    with pytest.raises(ValidationError, match="sample count"):
        validate_document(shortened)

    missing = deepcopy(run)
    missing["cases"].pop()
    missing["summary"].update(total_cases=1, passed_cases=1, consistency_selected_cases=1, consistency_passed_cases=1)
    with pytest.raises(ValidationError, match="manifest commitment"):
        validate_document(missing)


def test_interaction_with_correctness_failure_has_no_inferred_delta(tmp_path: Path) -> None:
    matrix_path = write_matrix(tmp_path / "matrix.json", ("opt-a", "opt-b"))
    baseline = make_run("baseline-i", {"a": ("family-a", 100), "b": ("family-b", 100)})
    left = make_run(
        "left-i", {"a": ("family-a", 110), "b": ("family-b", 110)},
        profile_id="without.opt-a", profile_sha256="4" * 64,
    )
    right = make_run(
        "right-i", {"a": ("family-a", 120), "b": ("family-b", 120)},
        profile_id="without.opt-b", profile_sha256="5" * 64,
    )
    combined = make_run(
        "combined-i", {"a": ("family-a", 140), "b": ("family-b", 140)},
        profile_id="without.opt-a+opt-b", profile_sha256="f" * 64,
    )
    combined["cases"][0]["status"] = "wrong_output"
    combined["cases"][0]["samples"][0]["status"] = "wrong_output"
    combined["cases"][0]["diagnostic"] = "wrong output"
    combined["state"] = "failed"
    combined["summary"].update(passed_cases=1, failed_cases=1)
    paths = {}
    for name, record in (("full", baseline), ("left", left), ("right", right), ("both", validate_document(combined))):
        paths[name] = tmp_path / f"{name}.json"
        atomic_write_json(paths[name], record)
    study = build_ablation_remark(
        matrix_path=matrix_path, baseline_path=paths["full"],
        variant_paths={"without.opt-a": paths["left"], "without.opt-b": paths["right"]},
        interaction_paths={("opt-a", "opt-b"): paths["both"]}, study_id="interaction-failure",
        title="Interaction failure",
    )
    interaction = study["interactions"][0]
    assert interaction["eligible_for_ranking"] is False
    assert interaction["ineligibility_reason"] == "correctness_failure"
    assert interaction["delta_ln_geometric_mean"] is None


def test_singleton_study_preserves_failed_correctness_variant_for_campaign_promotion(
    tmp_path: Path,
) -> None:
    matrix_path = write_matrix(tmp_path / "matrix-singleton.json", ("opt-a",))
    baseline = make_run(
        "baseline-singleton", {"a": ("family-a", 100), "b": ("family-b", 100)}
    )
    candidate = make_run(
        "failed-singleton", {"a": ("family-a", 110), "b": ("family-b", 110)},
        profile_id="without.opt-a", profile_sha256="4" * 64,
    )
    candidate["cases"][0]["status"] = "wrong_output"
    candidate["cases"][0]["samples"][0]["status"] = "wrong_output"
    candidate["cases"][0]["diagnostic"] = "wrong output"
    candidate["state"] = "failed"
    candidate["summary"].update(passed_cases=1, failed_cases=1)
    baseline_path = tmp_path / "baseline-singleton.json"
    candidate_path = tmp_path / "failed-singleton.json"
    atomic_write_json(baseline_path, baseline)
    atomic_write_json(candidate_path, validate_document(candidate))

    study = build_ablation_remark(
        matrix_path=matrix_path,
        baseline_path=baseline_path,
        variant_paths={"without.opt-a": candidate_path},
        interaction_paths=None,
        study_id="singleton-correctness-failure",
        title="Singleton correctness failure",
    )
    variant = study["variants"][0]
    assert variant["run_id"] == "failed-singleton"
    assert variant["correctness_failures"] == 1
    assert variant["eligible_for_ranking"] is False
    assert variant["ineligibility_reason"] == "correctness_failure"


def test_hotblock_patterns_select_rank_one_in_a_top20_log(tmp_path: Path) -> None:
    metric_log = tmp_path / "metrics.log"
    metric_log.write_text(
        "hotblock_rank=1 address=0x800003bc address_decimal=2147484604 "
        "executions=20 instructions=6 dynamic=120\n"
        "hotblock_rank=2 address=0x80000400 address_decimal=2147484672 "
        "executions=10 instructions=5 dynamic=50\n",
        encoding="utf-8",
    )
    expected = {
        "hotblock_hottest_address": 2147484604.0,
        "hotblock_hottest_executions": 20.0,
        "hotblock_hottest_dynamic_instructions": 120.0,
    }
    for spec in cache_hotblock_metrics_v1():
        assert extract_metric(
            metric_log, re.compile(spec["pattern"]), allow_zero=True
        ) == expected[spec["metric_id"]]


def test_hotblock_report_preserves_sample_provenance_without_ranking(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    full = make_hotblock_run("hot-full")
    without = make_hotblock_run(
        "hot-without", profile_id="without.gvn", profile_sha256="6" * 64
    )
    full_path = tmp_path / "full.json"
    without_path = tmp_path / "without.json"
    atomic_write_json(full_path, full)
    atomic_write_json(without_path, without)
    output = tmp_path / "report"

    exit_code = benchmark_main(
        [
            "report",
            str(full_path),
            "--hotblock-run",
            f"FULL={full_path}",
            "--hotblock-run",
            f"without-gvn={without_path}",
            "--output-dir",
            str(output),
        ]
    )
    assert exit_code == 0
    cli_result = json.loads(capsys.readouterr().out)
    assert "hotblocks.csv" in cli_result["artifacts"]
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    diagnostics = summary["hotblock_diagnostics"]
    assert diagnostics["participates_in_rankings"] is False
    assert diagnostics["run_count"] == 2
    assert diagnostics["sample_count"] == 6
    assert {item["run_id"] for item in diagnostics["runs"]} == {
        "hot-full",
        "hot-without",
    }
    assert summary["rankings"]["benefit"] == []
    assert summary["rankings"]["oracle"] == []
    assert summary["rankings"]["implementation_priority"] == []
    with (output / "hotblocks.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 6
    assert {row["run_id"] for row in rows} == {"hot-full", "hot-without"}
    assert {row["pipeline_profile_id"] for row in rows} == {
        "full",
        "without.gvn",
    }
    assert {row["case_id"] for row in rows} == {"hot-case"}
    assert {row["hotblock_hottest_address_hex"] for row in rows} == {
        "0x800003bc"
    }
    assert all(
        float(row["hotblock_dynamic_instruction_share"]) == pytest.approx(0.12)
        for row in rows
    )
    markdown = (output / "report.md").read_text(encoding="utf-8")
    assert "热点诊断（不参与收益排名）" in markdown
    assert "hot-full" in markdown and "hot-without" in markdown
    assert "cache-hotblock-test" in markdown


def test_report_without_hotblock_inputs_does_not_create_hotblock_evidence(
    tmp_path: Path,
) -> None:
    run = make_run("ordinary", {"a": ("family-a", 100)})
    run_path = tmp_path / "run.json"
    atomic_write_json(run_path, run)
    output = tmp_path / "report"
    output.mkdir()
    (output / "hotblocks.csv").write_text("stale evidence\n", encoding="utf-8")
    summary = build_report(run_path=run_path, output_directory=output)
    assert summary["hotblock_diagnostics"] == {
        "participates_in_rankings": False,
        "purpose": "diagnostic_only",
        "run_count": 0,
        "sample_count": 0,
        "runs": [],
        "samples": [],
    }
    assert not (output / "hotblocks.csv").exists()
    assert "热点诊断" not in (output / "report.md").read_text(encoding="utf-8")


def test_hotblock_report_rejects_incomplete_drifted_or_duplicate_evidence(
    tmp_path: Path,
) -> None:
    valid = make_hotblock_run("hot-valid")
    missing = deepcopy(valid)
    missing["run_id"] = "hot-missing"
    missing["cases"][0]["samples"][0]["measurements"] = [
        item
        for item in missing["cases"][0]["samples"][0]["measurements"]
        if item["metric_id"] != "hotblock_hottest_executions"
    ]
    missing_path = tmp_path / "missing.json"
    atomic_write_json(missing_path, missing)
    with pytest.raises(ValidationError):
        build_report(
            run_path=missing_path,
            hotblock_run_paths={"missing": missing_path},
            output_directory=tmp_path / "missing-report",
        )

    unavailable = deepcopy(valid)
    unavailable["run_id"] = "hot-unavailable"
    measurement = next(
        item
        for item in unavailable["cases"][0]["samples"][0]["measurements"]
        if item["metric_id"] == "hotblock_hottest_dynamic_instructions"
    )
    measurement.update(
        value=None,
        availability="unavailable",
        reason="not_collected_by_protocol",
    )
    unavailable_path = tmp_path / "unavailable.json"
    atomic_write_json(unavailable_path, unavailable)
    with pytest.raises(ValidationError):
        build_report(
            run_path=unavailable_path,
            hotblock_run_paths={"unavailable": unavailable_path},
            output_directory=tmp_path / "unavailable-report",
        )

    drifted = deepcopy(valid)
    drifted["run_id"] = "hot-drifted"
    next(
        item
        for item in drifted["configuration"]["metrics"]
        if item["metric_id"] == "hotblock_hottest_address"
    )["pattern_sha256"] = "f" * 64
    _rebind_synthetic_run_configuration(drifted)
    drifted_path = tmp_path / "drifted.json"
    atomic_write_json(drifted_path, validate_document(drifted))
    with pytest.raises(ValidationError, match="specification drift"):
        build_report(
            run_path=drifted_path,
            hotblock_run_paths={"drifted": drifted_path},
            output_directory=tmp_path / "drifted-report",
        )

    valid_path = tmp_path / "valid.json"
    atomic_write_json(valid_path, valid)
    with pytest.raises(ValidationError, match="repeat run_id"):
        build_report(
            run_path=valid_path,
            hotblock_run_paths={"first": valid_path, "second": valid_path},
            output_directory=tmp_path / "duplicate-report",
        )
    with pytest.raises(ValidationError, match="portable logical identifier"):
        _hotblock_diagnostics({"".join(("C", ":/", "private/run")): valid})

    incomplete_samples = deepcopy(valid)
    for sample in incomplete_samples["cases"][0]["samples"]:
        sample["measurements"] = [
            item
            for item in sample["measurements"]
            if item["metric_id"] != "hotblock_hottest_executions"
        ]
    with pytest.raises(ValidationError, match="passed sample"):
        _hotblock_diagnostics({"incomplete": incomplete_samples})

    impossible_share = deepcopy(valid)
    for sample in impossible_share["cases"][0]["samples"]:
        next(
            item
            for item in sample["measurements"]
            if item["metric_id"] == "hotblock_hottest_dynamic_instructions"
        )["value"] = 1_001.0
    with pytest.raises(ValidationError, match="exceed the total"):
        _hotblock_diagnostics({"impossible-share": impossible_share})

    running = deepcopy(valid)
    running["state"] = "running"
    with pytest.raises(ValidationError, match="completed run"):
        _hotblock_diagnostics({"running": running})
