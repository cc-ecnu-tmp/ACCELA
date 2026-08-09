from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigurationError, ValidationError
from .metrics import rv64gc_qemu_v1
from .schema import load_and_validate, validate_document
from .stats import (
    bootstrap_geometric_mean_ci,
    case_geometric_mean,
    compare_runs,
    family_geometric_means,
    invert_pairs,
    leave_one_family_out,
    metric_spec,
    source_group_geometric_mean,
)
from .util import sha256_json, utc_now


def _load_run(path: Path) -> dict[str, Any]:
    run = load_and_validate(path)
    if run["schema_version"] != "run-record.v1":
        raise ValidationError("ablation inputs must be run-record.v1 documents")
    return run


def _require_no_historical_attempts(
    run: Mapping[str, Any],
    *,
    context: str,
) -> None:
    historical_attempts = [
        (case["case_id"], attempt["attempt_index"], attempt["status"])
        for case in run["cases"]
        for attempt in case["attempts"]
    ]
    if historical_attempts:
        case_id, attempt_index, status = historical_attempts[0]
        raise ValidationError(
            f"{context} rejects profiles with historical failed attempts: "
            f"case={case_id}, attempt={attempt_index}, status={status}"
        )


def _require_formal_measurement(
    run: Mapping[str, Any],
    *,
    require_accela_pipeline: bool = False,
    allow_metric_superset: bool = False,
) -> None:
    if (
        run["configuration"]["compile_repetitions"] != 5
        or run["configuration"]["reuse_compile_cache"]
        or run["configuration"]["compile_storage_contract"] != "attempt_local_v1"
    ):
        raise ValidationError(
            "formal optimization ranking requires five cold compiler starts with compile-cache reuse disabled"
        )
    configuration = run["configuration"]
    if configuration["evidence_level"] not in {"qemu_proxy", "boom_hardware"}:
        raise ValidationError("formal optimization ranking requires qemu_proxy or boom_hardware evidence")
    if configuration["output_contract"] == "raw_stdout":
        raise ValidationError("formal optimization ranking must independently validate main return uint8")
    if configuration["primary_metric_id"] != "dynamic_instruction_count":
        raise ValidationError("formal optimization ranking primary metric must be dynamic_instruction_count")
    if configuration["metric_profile_id"] != "rv64gc-qemu-v1":
        raise ValidationError("formal optimization ranking requires metric-profile=rv64gc-qemu-v1")
    if configuration["timeout_policy"] not in {"initial", "baseline_derived"}:
        raise ValidationError(
            "formal optimization ranking requires initial or baseline-derived timeout evidence"
        )
    if any(
        not math.isclose(configuration[field], expected, rel_tol=0, abs_tol=1e-12)
        for field, expected in (
            ("run_timeout_seconds", 1800.0),
            ("timeout_minimum_seconds", 120.0),
            ("timeout_multiplier", 3.0),
            ("timeout_cap_seconds", 1800.0),
        )
    ):
        raise ValidationError(
            "formal optimization ranking requires the recorded 1800/120/3x/1800 timeout protocol"
        )
    if (
        run["provenance"]["measurement_protocol_id"] is None
        or run["provenance"]["measurement_protocol_sha256"] is None
    ):
        raise ValidationError("formal optimization ranking requires a versioned measurement protocol snapshot")
    by_id = {item["metric_id"]: item for item in configuration["metrics"]}
    preset = rv64gc_qemu_v1()
    expected_specs = {
        preset["primary_metric_id"]: {
            "metric_id": preset["primary_metric_id"],
            "source": preset["metric_source"],
            "pattern_sha256": sha256_json(preset["metric_pattern"]),
            "unit": preset["metric_unit"],
        },
        **{
            item["metric_id"]: {
                "metric_id": item["metric_id"],
                "source": item["source"],
                "pattern_sha256": (
                    None if item["pattern"] is None else sha256_json(item["pattern"])
                ),
                "unit": item["unit"],
            }
            for item in preset["additional"]
        },
    }
    required = set(expected_specs)
    if (not allow_metric_superset and set(by_id) != required) or (
        allow_metric_superset and not required.issubset(by_id)
    ):
        raise ValidationError("formal optimization ranking lacks the complete rv64gc-qemu-v1 metric catalog")
    if any(by_id[metric_id] != expected for metric_id, expected in expected_specs.items()):
        raise ValidationError("formal optimization ranking rv64gc-qemu-v1 metric definitions drifted")
    if configuration["evidence_level"] == "qemu_proxy":
        runtime_ids = {preset["primary_metric_id"]} | {
            item["metric_id"] for item in preset["additional"] if item["source"] == "file"
        }
        if configuration["runner"]["kind"] != "qemu" or any(
            by_id[item]["source"] != "file" for item in runtime_ids
        ):
            raise ValidationError("qemu_proxy ranking requires QEMU runner metrics from the explicit plugin file")
    if not configuration["tool_versions"]:
        raise ValidationError("formal optimization ranking lacks toolchain version evidence")
    compiler_kind = configuration["compiler"]["kind"]
    if compiler_kind not in {"benchmark-compiler", "external"}:
        raise ValidationError("formal optimization ranking requires BenchmarkCompiler or an external toolchain")
    if require_accela_pipeline and compiler_kind != "benchmark-compiler":
        raise ValidationError("ACCELA ablation/Oracle ranking requires BenchmarkCompiler")
    if compiler_kind == "benchmark-compiler":
        if (
            configuration.get("pipeline_profile_file_sha256")
            != run["provenance"]["pipeline_profile_sha256"]
        ):
            raise ValidationError(
                "ACCELA ranking requires a physical pipeline profile bound to provenance by SHA-256"
            )
        if configuration["remarks_file_sha256"] is None:
            raise ValidationError("ACCELA ranking requires configured optimization remarks")
    _require_no_historical_attempts(run, context="formal optimization ranking")
    runtime_ids = {preset["primary_metric_id"]} | {
        item["metric_id"] for item in preset["additional"] if item["source"] == "file"
    }
    for case in run["cases"]:
        if case["status"] != "passed":
            continue
        if (
            case["cache_hit"]
            or case["compile"] is None
            or case["compile"]["status"] != "ok"
            or len(case["compile_samples"]) != 5
            or case["compile_statistics"] is None
        ):
            raise ValidationError("formal optimization ranking lacks five recorded cold compile attempts")
        if case["link"] is None or case["link"]["status"] != "ok":
            raise ValidationError("formal optimization ranking lacks a successful link record")
        if case["analyze"] is None or case["analyze"]["status"] != "ok":
            raise ValidationError("formal optimization ranking lacks a successful static analyzer record")
        if compiler_kind == "benchmark-compiler" and (
            case["remarks_sha256"] is None
            or case.get("remarks_event_count") is None
            or case["remarks_event_count"] <= 0
        ):
            raise ValidationError("ACCELA ranking case lacks validated non-empty optimization remarks")
        for sample in case["samples"]:
            sample_measurements = {item["metric_id"]: item for item in sample["measurements"]}
            if not runtime_ids.issubset(sample_measurements) or any(
                sample_measurements[metric_id]["availability"] != "measured"
                or sample_measurements[metric_id]["value"] is None
                for metric_id in runtime_ids
            ):
                raise ValidationError(
                    "formal optimization ranking passed sample lacks complete measured file metrics"
                )
        measured = {item["metric_id"] for item in case["measurements"]}
        case_required = {
            item["metric_id"] for item in preset["additional"] if item["source"] != "file"
        }
        if not case_required.issubset(measured):
            raise ValidationError("formal optimization ranking case lacks auxiliary metric records")
        vector = next(
            item for item in case["measurements"]
            if item["metric_id"] == "static_vector_instructions"
        )
        if vector["availability"] != "measured" or vector["value"] != 0:
            raise ValidationError("formal RV64GC ranking rejects vector or unavailable ISA evidence")


def build_ablation_remark(
    *,
    matrix_path: Path,
    baseline_path: Path,
    variant_paths: Mapping[str, Path],
    interaction_paths: Mapping[tuple[str, str], Path] | None,
    study_id: str,
    title: str,
    bootstrap_samples: int = 10_000,
    seed: int = 20260809,
) -> dict[str, Any]:
    if not variant_paths:
        raise ConfigurationError("ablation requires at least one optimization variant")
    baseline = _load_run(baseline_path)
    suite_id = baseline["suite_id"]
    manifest_sha256 = baseline["manifest_sha256"]
    data_role = _ablation_data_role(baseline, label="baseline")
    matrix = load_and_validate(matrix_path)
    if matrix["schema_version"] != "ablation-matrix.v1":
        raise ValidationError("ablation analysis requires ablation-matrix.v1")
    profiles = {item["profile_id"]: item for item in matrix["profiles"]}
    full_profile = profiles.get("full")
    if full_profile is None or (
        baseline["provenance"]["pipeline_profile_id"] != "full"
        or baseline["provenance"]["pipeline_profile_sha256"] != full_profile["profile_sha256"]
    ):
        raise ValidationError("ablation baseline run does not match the matrix FULL profile")
    primary_spec = metric_spec(baseline)
    if primary_spec["source"] == "wall_time":
        raise ValidationError(
            "optimization ranking requires a counter/size metric; wall-clock is proxy evidence only"
        )
    _require_formal_measurement(baseline, require_accela_pipeline=True)
    variants: list[dict[str, Any]] = []
    speedups: dict[str, float | None] = {}
    variant_eligibility: dict[str, bool] = {}
    for profile_id, path in sorted(variant_paths.items()):
        profile = profiles.get(profile_id)
        if profile is None or profile["kind"] != "family_ablation" or len(profile["logical_families"]) != 1:
            raise ValidationError(f"ablation benefit variant must be a matrix family_ablation profile: {profile_id}")
        optimization_id = profile["logical_families"][0]
        candidate = _load_run(path)
        _validate_ablation_run_binding(
            candidate,
            suite_id=suite_id,
            data_role=data_role,
            manifest_sha256=manifest_sha256,
            label=f"variant {profile_id}",
        )
        if (
            candidate["provenance"]["pipeline_profile_id"] != profile_id
            or candidate["provenance"]["pipeline_profile_sha256"] != profile["profile_sha256"]
        ):
            raise ValidationError(f"variant run provenance does not match matrix profile: {profile_id}")
        _require_formal_measurement(candidate, require_accela_pipeline=True)
        comparison = compare_runs(baseline, candidate)
        contribution_pairs = invert_pairs(comparison.pairs)
        case_contribution = case_geometric_mean(contribution_pairs) if contribution_pairs else None
        source_group_contribution = (
            source_group_geometric_mean(contribution_pairs) if contribution_pairs else None
        )
        speedups[optimization_id] = case_contribution
        interval = bootstrap_geometric_mean_ci(
            contribution_pairs,
            samples=bootstrap_samples,
            seed=seed,
        )
        if comparison.correctness_failures:
            ineligibility_reason = "correctness_failure"
        elif comparison.excluded_cases:
            ineligibility_reason = "incomplete_profile"
        elif comparison.censored_cases:
            ineligibility_reason = "right_censored"
        elif not contribution_pairs:
            ineligibility_reason = "no_comparable_cases"
        else:
            ineligibility_reason = None
        variants.append(
            {
                "optimization_id": optimization_id,
                "profile_id": profile_id,
                "profile_sha256": profile["profile_sha256"],
                "run_id": candidate["run_id"],
                "comparable_cases": len(comparison.pairs),
                "comparable_source_groups": len({pair.source_group_id for pair in comparison.pairs}),
                "correctness_failures": comparison.correctness_failures,
                "censored_cases": comparison.censored_cases,
                "excluded_cases": comparison.excluded_cases,
                "eligible_for_ranking": ineligibility_reason is None,
                "ineligibility_reason": ineligibility_reason,
                "case_geometric_mean_contribution": case_contribution,
                "source_group_geometric_mean_contribution": source_group_contribution,
                "confidence_interval_95": (
                    None if interval is None else {"low": interval[0], "high": interval[1]}
                ),
                "per_cases": [
                    {
                        "case_id": pair.case_id,
                        "source_group": pair.source_group_id,
                        "family": pair.family,
                        "target": pair.target,
                        "weight": pair.weight,
                        "metric_without": pair.baseline_value,
                        "metric_full": pair.candidate_value,
                        "contribution_ratio": pair.speedup,
                    }
                    for pair in contribution_pairs
                ],
                "families": family_geometric_means(contribution_pairs),
                "leave_one_family_out": leave_one_family_out(contribution_pairs),
            }
        )
        variant_eligibility[optimization_id] = ineligibility_reason is None

    interactions: list[dict[str, Any]] = []
    for pair, path in sorted((interaction_paths or {}).items()):
        left, right = tuple(sorted(pair))
        if left not in speedups or right not in speedups:
            raise ConfigurationError(f"interaction {left}+{right} references an unknown variant")
        combined = _load_run(path)
        _validate_ablation_run_binding(
            combined,
            suite_id=suite_id,
            data_role=data_role,
            manifest_sha256=manifest_sha256,
            label=f"interaction {left}+{right}",
        )
        pair_profile = next(
            (
                item for item in matrix["profiles"]
                if item["kind"] == "pair_ablation" and set(item["logical_families"]) == {left, right}
            ),
            None,
        )
        if pair_profile is None:
            raise ValidationError(f"interaction is not scheduled by the matrix: {left}+{right}")
        if (
            combined["provenance"]["pipeline_profile_id"] != pair_profile["profile_id"]
            or combined["provenance"]["pipeline_profile_sha256"] != pair_profile["profile_sha256"]
        ):
            raise ValidationError(f"interaction run provenance does not match matrix profile: {left}+{right}")
        _require_formal_measurement(combined, require_accela_pipeline=True)
        comparison = compare_runs(baseline, combined)
        contribution_pairs = invert_pairs(comparison.pairs)
        if comparison.correctness_failures:
            ineligibility_reason = "correctness_failure"
        elif comparison.excluded_cases:
            ineligibility_reason = "incomplete_profile"
        elif comparison.censored_cases:
            ineligibility_reason = "right_censored"
        elif not contribution_pairs:
            ineligibility_reason = "no_comparable_cases"
        elif not variant_eligibility[left] or not variant_eligibility[right]:
            ineligibility_reason = "constituent_ineligible"
        else:
            ineligibility_reason = None
        observed = (
            case_geometric_mean(contribution_pairs)
            if contribution_pairs and ineligibility_reason is None
            else None
        )
        left_value = speedups[left]
        right_value = speedups[right]
        expected = (
            left_value * right_value
            if ineligibility_reason is None and left_value is not None and right_value is not None
            else None
        )
        factor = None if observed is None or expected is None else observed / expected
        delta_ln = None if factor is None else math.log(factor)
        interactions.append(
            {
                "left": left,
                "right": right,
                "profile_id": pair_profile["profile_id"],
                "profile_sha256": pair_profile["profile_sha256"],
                "run_id": combined["run_id"],
                "comparable_cases": len(comparison.pairs),
                "comparable_source_groups": len({pair.source_group_id for pair in comparison.pairs}),
                "correctness_failures": comparison.correctness_failures,
                "censored_cases": comparison.censored_cases,
                "excluded_cases": comparison.excluded_cases,
                "eligible_for_ranking": ineligibility_reason is None,
                "ineligibility_reason": ineligibility_reason,
                "observed_case_geometric_mean_contribution": observed,
                "expected_multiplicative_contribution": expected,
                "interaction_factor": factor,
                "delta_ln_geometric_mean": delta_ln,
            }
        )

    metric_unit = primary_spec["unit"]
    remark = {
        "schema_version": "ablation-study.v1",
        "study_id": study_id,
        "title": title,
        "generated_at": utc_now(),
        "suite_id": suite_id,
        "data_role": data_role,
        "manifest_sha256": manifest_sha256,
        "matrix_sha256": sha256_json(matrix),
        "registry_sha256": matrix["registry_sha256"],
        "baseline_profile_id": "full",
        "baseline_profile_sha256": full_profile["profile_sha256"],
        "baseline_run_id": baseline["run_id"],
        "primary_metric_id": baseline["configuration"]["primary_metric_id"],
        "metric_unit": metric_unit,
        "bootstrap_samples": bootstrap_samples,
        "seed": seed,
        "variants": variants,
        "interactions": interactions,
        "notes": [
            "Per-case repetitions are reduced by median before paired speedup calculation.",
            "Optimization contribution is without-pass/FULL; normal compiler speedup remains baseline/candidate.",
            "The official case-weighted GM and source-hash-deduplicated GM are reported separately; ranking uses the official case-weighted GM.",
            "The 95% confidence interval uses deterministic family-cluster bootstrap resampling (seed 20260809, 10000 samples).",
            "Correctness failures, incomplete profiles, and right-censored cases remain visible but are ineligible for benefit ranking.",
        ],
    }
    return validate_document(remark)


def _ablation_data_role(run: Mapping[str, Any], *, label: str) -> str:
    roles = {case["data_role"] for case in run["cases"]}
    if len(roles) != 1:
        raise ValidationError(f"ablation {label} run must contain exactly one data_role")
    return next(iter(roles))


def _validate_ablation_run_binding(
    run: Mapping[str, Any],
    *,
    suite_id: str,
    data_role: str,
    manifest_sha256: str,
    label: str,
) -> None:
    if run["suite_id"] != suite_id:
        raise ValidationError(f"ablation {label} run suite_id differs from the FULL baseline")
    if run["manifest_sha256"] != manifest_sha256:
        raise ValidationError(f"ablation {label} run manifest_sha256 differs from the FULL baseline")
    if _ablation_data_role(run, label=label) != data_role:
        raise ValidationError(f"ablation {label} run data_role differs from the FULL baseline")
