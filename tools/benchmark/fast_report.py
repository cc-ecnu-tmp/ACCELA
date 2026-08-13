from __future__ import annotations

import html
import math
import random
import re
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import ConfigurationError, ValidationError
from .journal import durable_create_bytes, durable_create_json
from .report import (
    _svg_cache_hotblock,
    _svg_heatmap,
    _svg_pareto,
    _svg_ratio_diverging,
)
from .schema import validate_document
from .stats import run_case_metrics
from .util import (
    canonical_json_bytes,
    read_json,
    resolve_without_symlinks,
    sha256_bytes,
    sha256_file,
    sha256_json,
    validate_relative_path,
)


_MANIFEST_VERSION = "candidate-fast-report-manifest.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RANKING_STAGES = ("B3", "B4", "B5", "B6")
_ALL_STAGES = ("B2", *_RANKING_STAGES)
_BOOTSTRAP_SAMPLES = 10_000
_BOOTSTRAP_SEED = 20260809
_ARTIFACT_FILENAMES = {
    "report": "report.md",
    "single_candidate_chart": "single_candidate_chart.svg",
    "suite_chart": "suite_chart.svg",
    "ranking_chart": "ranking_chart.svg",
    "pair_heatmap": "pair_heatmap.svg",
    "oracle_capture_chart": "oracle_capture_chart.svg",
    "cache_hotblock_chart": "cache_hotblock_chart.svg",
    "pareto_chart": "pareto_chart.svg",
}


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return (
        ordered[lower] * (upper - position)
        + ordered[upper] * (position - lower)
    )


def _equal_case_bootstrap_ci(speedups: Sequence[float]) -> tuple[float, float] | None:
    """Return the deterministic paired-case bootstrap interval used by the report.

    The champion contract gives every case one vote, regardless of the manifest's
    legacy weight field.  Resampling the case log-ratios preserves that contract
    and never fabricates an interval when there is no complete paired evidence.
    """

    if not speedups:
        return None
    logs = [math.log(value) for value in speedups]
    count = len(logs)
    generator = random.Random(_BOOTSTRAP_SEED)
    estimates = [
        math.exp(
            sum(logs[generator.randrange(count)] for _ in range(count)) / count
        )
        for _ in range(_BOOTSTRAP_SAMPLES)
    ]
    return _percentile(estimates, 0.025), _percentile(estimates, 0.975)


def _root(path: Path) -> Path:
    root = resolve_without_symlinks(path, label="fast report workspace")
    if not root.is_dir():
        raise ConfigurationError("fast report workspace must be a directory")
    return root


def _existing(root: Path, path: Path, *, label: str) -> tuple[Path, str]:
    candidate = path if path.is_absolute() else root / path
    physical = resolve_without_symlinks(candidate, label=label)
    try:
        relative = physical.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValidationError(f"{label} must remain inside the workspace") from exc
    validate_relative_path(relative, label=label)
    if not physical.is_file():
        raise ValidationError(f"{label} must be a regular file")
    return physical, relative


def _load(root: Path, path: Path, version: str, *, label: str) -> tuple[dict[str, Any], Path]:
    physical, _ = _existing(root, path, label=label)
    value = read_json(physical)
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be a JSON object")
    validate_document(value)
    if value.get("schema_version") != version:
        raise ValidationError(f"{label} has an unexpected schema version")
    return value, physical


def _artifact(root: Path, path: Path, *, label: str) -> dict[str, str]:
    physical, relative = _existing(root, path, label=label)
    value = read_json(physical) if physical.suffix.lower() == ".json" else None
    return {
        "path": relative,
        "canonical_sha256": sha256_json(value) if value is not None else sha256_file(physical),
        "physical_sha256": sha256_file(physical),
    }


def _verify_artifact(root: Path, artifact: Mapping[str, str], *, label: str) -> Path:
    relative = validate_relative_path(artifact["path"], label=f"{label} path")
    physical, _ = _existing(root, root.joinpath(*relative.parts), label=label)
    if _artifact(root, physical, label=label) != dict(artifact):
        raise ValidationError(f"{label} canonical or physical hash differs")
    return physical


def _output_directory(root: Path, path: Path) -> tuple[Path, str]:
    candidate = (path if path.is_absolute() else root / path).absolute()
    try:
        relative = candidate.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValidationError("fast report output directory must remain inside the workspace") from exc
    validate_relative_path(relative, label="fast report output directory")
    cursor = root
    for part in Path(relative).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValidationError("fast report output directory cannot traverse a symbolic link")
    return candidate, relative


def _receipt_ref_is_bound(
    ref: Mapping[str, Any], index: Mapping[str, Any], bootstrap: Mapping[str, Any]
) -> bool:
    if any(dict(row) == dict(ref) for row in index["receipts"]):
        return True
    return any(
        row["task_id"] == ref["task_id"]
        and row["run_id"] == ref["run_id"]
        and row["run_artifact"] == ref["receipt"]
        and row["terminal_commitment_sha256"] == ref["terminal_commitment_sha256"]
        for row in bootstrap["imported_receipts"]
    )


def _load_run_from_ref(
    root: Path, ref: Mapping[str, Any], bootstrap: Mapping[str, Any]
) -> dict[str, Any]:
    imported = next(
        (row for row in bootstrap["imported_receipts"] if row["run_artifact"] == ref["receipt"]),
        None,
    )
    if imported is not None:
        physical = _verify_artifact(root, imported["run_artifact"], label="fast report imported run")
        run = read_json(physical)
    else:
        receipt_path = _verify_artifact(root, ref["receipt"], label="fast report receipt")
        receipt = read_json(receipt_path)
        if not isinstance(receipt, dict) or receipt.get("schema_version") != "candidate-fast-run-receipt.v1":
            raise ValidationError("fast report receipt has an unexpected schema version")
        if (
            receipt["task_id"] != ref["task_id"]
            or receipt["run_id"] != ref["run_id"]
            or receipt["terminal"]["commitment_sha256"] != ref["terminal_commitment_sha256"]
        ):
            raise ValidationError("fast report receipt reference differs")
        physical = _verify_artifact(root, receipt["run_artifact"], label="fast report normalized run")
        run = read_json(physical)
    if not isinstance(run, dict) or run.get("schema_version") != "run-record.v1":
        raise ValidationError("fast report normalized run has an unexpected schema version")
    validate_document(run)
    return run


def _measured(case: Mapping[str, Any], metric_id: str) -> float | None:
    for item in case["measurements"]:
        if item["metric_id"] == metric_id:
            value = item["value"]
            if item["availability"] != "measured" or value is None:
                return None
            numeric = float(value)
            return numeric if math.isfinite(numeric) and numeric > 0 else None
    return None


def _stage_projection(
    *,
    root: Path,
    study: Mapping[str, Any],
    bootstrap: Mapping[str, Any],
    index: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    baseline = _load_run_from_ref(root, study["baseline"], bootstrap)
    baseline_metrics = run_case_metrics(baseline)
    baseline_cases = {row["case_id"]: row for row in baseline["cases"]}
    result: dict[str, dict[str, Any]] = {}
    for row in study["candidates"]:
        ref = row["receipt"]
        if not _receipt_ref_is_bound(ref, index, bootstrap):
            raise ValidationError("fast report study receipt is not bound by index/bootstrap")
        run = _load_run_from_ref(root, ref, bootstrap)
        candidate_metrics = run_case_metrics(run)
        if row["eligible"]:
            if set(candidate_metrics) != set(baseline_metrics):
                raise ValidationError("fast report eligible study case set differs")
            case_ids = sorted(baseline_metrics)
            speedups = [
                baseline_metrics[case_id] / candidate_metrics[case_id]
                for case_id in case_ids
            ]
            if any(not math.isfinite(value) or value <= 0 for value in speedups):
                raise ValidationError("fast report study contains an invalid speedup")
            observed = math.exp(sum(math.log(value) for value in speedups) / len(speedups))
            declared_cases = row.get("per_cases")
            if declared_cases is not None and (
                [item["case_id"] for item in declared_cases]
                != case_ids
                or any(
                    not math.isclose(
                        float(item["speedup"]),
                        speedups[index],
                        rel_tol=1e-12,
                        abs_tol=1e-12,
                    )
                    for index, item in enumerate(declared_cases)
                )
                or any(
                    not math.isclose(
                        float(item["weight"]),
                        float(baseline_cases[item["case_id"]]["weight"]),
                        rel_tol=0,
                        abs_tol=1e-12,
                    )
                    for item in declared_cases
                )
            ):
                raise ValidationError("fast report study per-case evidence differs from normalized runs")
            if not math.isclose(observed, float(row["geometric_mean_speedup"]), rel_tol=1e-12, abs_tol=1e-12):
                raise ValidationError("fast report study geometric mean differs from normalized runs")
            candidate_cases = {case["case_id"]: case for case in run["cases"]}
            text_pairs = [
                (_measured(baseline_cases[case_id], "elf_text_bytes"), _measured(candidate_cases[case_id], "elf_text_bytes"))
                for case_id in case_ids
            ]
            text_complete = bool(text_pairs) and all(left is not None and right is not None for left, right in text_pairs)
            full_text = sum(float(left) for left, _ in text_pairs if left is not None) if text_complete else None
            candidate_text = sum(float(right) for _, right in text_pairs if right is not None) if text_complete else None
        else:
            speedups = []
            full_text = None
            candidate_text = None
        interval = (
            _equal_case_bootstrap_ci(speedups)
            if study["stage"] == "B3" and speedups
            else None
        )
        result[row["candidate_id"]] = {
            "eligible": bool(row["eligible"]),
            "reason": row["ineligibility_reason"],
            "case_count": len(speedups),
            "geometric_mean_speedup": row["geometric_mean_speedup"],
            "per_case_speedups": speedups,
            "confidence_interval_95": (
                None
                if interval is None
                else {"low": interval[0], "high": interval[1]}
            ),
            "static_text_bytes_full": full_text,
            "static_text_bytes_candidate": candidate_text,
            "baseline_run_id": baseline["run_id"],
            "candidate_run_id": run["run_id"],
        }
    return result


def _indexed_run(
    root: Path,
    index: Mapping[str, Any],
    bootstrap: Mapping[str, Any],
    task_id: str,
) -> dict[str, Any] | None:
    ref = next((row for row in index["receipts"] if row["task_id"] == task_id), None)
    if ref is None:
        return None
    return _load_run_from_ref(root, ref, bootstrap)


def _case_metric(run: Mapping[str, Any], metric_id: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for case in run["cases"]:
        samples: list[float] = []
        for sample in case["samples"]:
            measured = next(
                (
                    item["value"]
                    for item in sample["measurements"]
                    if item["metric_id"] == metric_id
                    and item["availability"] == "measured"
                    and item["value"] is not None
                ),
                None,
            )
            if measured is not None:
                samples.append(float(measured))
        if samples and all(math.isfinite(value) and value > 0 for value in samples):
            values[case["case_id"]] = float(statistics.median(samples))
    return values


def _equal_case_speedup(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any], metric_id: str
) -> tuple[float | None, int]:
    left = run_case_metrics(baseline) if metric_id == baseline["configuration"]["primary_metric_id"] else _case_metric(baseline, metric_id)
    right = run_case_metrics(candidate) if metric_id == candidate["configuration"]["primary_metric_id"] else _case_metric(candidate, metric_id)
    if not left or set(left) != set(right):
        return None, 0
    ratios = [left[case_id] / right[case_id] for case_id in sorted(left)]
    if any(not math.isfinite(value) or value <= 0 for value in ratios):
        return None, 0
    return math.exp(sum(math.log(value) for value in ratios) / len(ratios)), len(ratios)


def _unique_static_artifact(
    plan: Mapping[str, Any], artifact_id: str
) -> Mapping[str, str] | None:
    artifacts = {
        sha256_json(binding["artifact"]): binding["artifact"]
        for task in plan["tasks"]
        for binding in task["static_bindings"]
        if binding["artifact_id"] == artifact_id
    }
    if len(artifacts) > 1:
        raise ValidationError(f"fast report has conflicting {artifact_id} bindings")
    return next(iter(artifacts.values()), None)


def _screening_projection(
    root: Path, plan: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, str], dict[str, dict[str, str]]]:
    artifact_ids = {
        "screening": "candidate-screening",
        "oracle_capture": "candidate-oracle-capture",
        "candidate_evidence": "candidate-evidence",
        "screening_spec": "candidate-screening-spec",
    }
    artifacts: dict[str, Mapping[str, str]] = {}
    for key, artifact_id in artifact_ids.items():
        artifact = _unique_static_artifact(plan, artifact_id)
        if artifact is None:
            raise ValidationError(
                f"fast report plan lacks required {artifact_id} qualification evidence"
            )
        artifacts[key] = artifact

    from .fast_campaign import verify_fast_oracle_static_artifacts

    closure = verify_fast_oracle_static_artifacts(
        workspace_root=root,
        named_artifacts=[
            {"artifact_id": artifact_ids[key], "artifact": dict(artifact)}
            for key, artifact in artifacts.items()
        ],
    )
    screening = closure["candidate-screening"]["document"]
    capture = closure["candidate-oracle-capture"]["document"]
    oracle_plan_path = _verify_artifact(
        root, capture["sources"]["oracle_plan"], label="fast report Oracle plan"
    )
    oracle_plan = read_json(oracle_plan_path)
    if (
        not isinstance(oracle_plan, dict)
        or oracle_plan.get("schema_version") != "oracle-plan.v1"
        or capture["oracle_plan_sha256"] != sha256_json(oracle_plan)
    ):
        raise ValidationError("fast report Oracle plan binding differs")
    validate_document(oracle_plan)
    pair_ids = [
        structure["sizes"][size]["pair_id"]
        for candidate in capture["candidates"]
        for structure in candidate["structures"]
        for size in ("small", "medium", "large")
    ]
    if len(pair_ids) != 99 or len(pair_ids) != len(set(pair_ids)):
        raise ValidationError("fast report Oracle capture must contain 99 distinct pairs")

    qualified = [
        row for row in screening["candidates"]
        if row["qualification_status"] == "qualified"
    ]
    qualified_ids = [row["implementation_candidate_id"] for row in qualified]
    if qualified_ids != plan["candidate_ids"] or any(value is None for value in qualified_ids):
        raise ValidationError("fast report plan candidates differ from qualified screening order")
    rows: list[dict[str, Any]] = []
    risks: dict[str, str] = {}
    for row in qualified:
        implementation_id = row["implementation_candidate_id"]
        assert implementation_id is not None
        allowed = [
            float(structure["geometric_mean_speedup"])
            for structure in row["oracle_structures"]
            if structure["eligible_for_candidate_screening"]
            and structure["eligible_for_ranking"]
            and structure["geometric_mean_speedup"] is not None
        ]
        if not allowed:
            raise ValidationError("fast report qualified candidate lacks an Oracle upper bound")
        rows.append(
            {
                "candidate_id": implementation_id,
                "oracle_upper_bound": max(allowed),
                "b3_measured_speedup": None,
                "capture_rate": None,
                "eligible_structures": len(row["eligible_oracle_structure_refs"]),
                "qualifying_structures": len(row["qualifying_oracle_structure_refs"]),
                "qualification_status": row["qualification_status"],
                "reason": "B3_measurement_not_joined",
            }
        )
        risks[implementation_id] = row["risk"]
    source_evidence = {
        key: dict(artifacts[key]) for key in artifact_ids
    }
    return (
        {
            "available": True,
            "reason": None,
            "rows": rows,
            "baseline_run_id": capture["baseline"]["run_id"],
            "optimized_run_id": capture["optimized"]["run_id"],
            "capture_sha256": sha256_json(capture),
            "pair_count": capture["pair_count"],
        },
        risks,
        source_evidence,
    )


def _sample_metric(sample: Mapping[str, Any], metric_id: str) -> float | None:
    matches = [
        row for row in sample["measurements"] if row["metric_id"] == metric_id
    ]
    if len(matches) > 1:
        raise ValidationError(f"fast report sample repeats metric {metric_id}")
    if not matches:
        return None
    row = matches[0]
    if row["availability"] != "measured" or row["value"] is None:
        return None
    value = float(row["value"])
    if not math.isfinite(value) or value < 0:
        raise ValidationError(f"fast report sample has invalid metric {metric_id}")
    return value


def _cache_run_projection(
    *, label: str, run: Mapping[str, Any], complete: bool, failure_reason: str | None
) -> dict[str, Any]:
    if not complete:
        return {
            "label": label,
            "run_id": run["run_id"],
            "mean_l1d_misses_per_1000_dynamic_loads": None,
            "mean_hottest_block_dynamic_instruction_share_percent": None,
            "sample_count": 0,
            "reason": failure_reason or "cache_hotblock_run_incomplete",
        }
    cache_rates: list[float] = []
    hot_shares: list[float] = []
    complete_samples = 0
    for case in run["cases"]:
        if case["status"] != "passed":
            continue
        for sample in case["samples"]:
            if sample["status"] != "passed":
                continue
            dynamic = _sample_metric(sample, "dynamic_instruction_count")
            loads = _sample_metric(sample, "dynamic_load_count")
            misses = _sample_metric(sample, "l1d_miss_count")
            hottest = _sample_metric(
                sample, "hotblock_hottest_dynamic_instructions"
            )
            if any(value is None for value in (dynamic, loads, misses, hottest)):
                continue
            assert dynamic is not None and loads is not None
            assert misses is not None and hottest is not None
            if dynamic <= 0 or hottest > dynamic:
                raise ValidationError("fast report cache/hotblock sample is inconsistent")
            complete_samples += 1
            if loads > 0:
                cache_rates.append(1000.0 * misses / loads)
            hot_shares.append(100.0 * hottest / dynamic)
    if complete_samples == 0:
        raise ValidationError(
            "fast report complete cache/hotblock run lacks the required metrics"
        )
    return {
        "label": label,
        "run_id": run["run_id"],
        "mean_l1d_misses_per_1000_dynamic_loads": (
            None if not cache_rates else statistics.fmean(cache_rates)
        ),
        "mean_hottest_block_dynamic_instruction_share_percent": (
            statistics.fmean(hot_shares)
        ),
        "sample_count": complete_samples,
        "reason": None if cache_rates else "all_dynamic_load_counts_are_zero",
    }


def _diagnostic_projection(
    root: Path,
    diagnostic: Mapping[str, Any],
    bootstrap: Mapping[str, Any],
    index: Mapping[str, Any],
    b3_study: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    baseline_ref = b3_study["baseline"]
    if not _receipt_ref_is_bound(baseline_ref, index, bootstrap):
        raise ValidationError("fast report diagnostic baseline is absent from evidence")
    baseline = _load_run_from_ref(root, baseline_ref, bootstrap)
    baseline_metrics = run_case_metrics(baseline)
    baseline_cases = {row["case_id"]: row for row in baseline["cases"]}
    b3_candidates = {row["candidate_id"]: row for row in b3_study["candidates"]}
    pairs: list[dict[str, Any]] = []
    for row in diagnostic["pairs"]:
        ref = row["receipt"]
        if not _receipt_ref_is_bound(ref, index, bootstrap):
            raise ValidationError("fast report pair receipt is absent from current evidence")
        run = _load_run_from_ref(root, ref, bootstrap)
        observed_pair: float | None = None
        observed_expected: float | None = None
        observed_delta: float | None = None
        if row["eligible"]:
            candidate_metrics = run_case_metrics(run)
            if set(candidate_metrics) != set(baseline_metrics):
                raise ValidationError("fast report pair case set differs from B3 FULL")
            weighted_logs = 0.0
            total_weight = 0.0
            for case_id in sorted(baseline_metrics):
                speedup = baseline_metrics[case_id] / candidate_metrics[case_id]
                weight = float(baseline_cases[case_id]["weight"])
                weighted_logs += weight * math.log(speedup)
                total_weight += weight
            observed_pair = math.exp(weighted_logs / total_weight)
            try:
                singles = [b3_candidates[candidate_id] for candidate_id in row["candidate_ids"]]
            except KeyError as exc:
                raise ValidationError("fast report pair references an unknown B3 candidate") from exc
            if any(
                not single["eligible"]
                or single["geometric_mean_speedup"] is None
                for single in singles
            ):
                raise ValidationError("fast report eligible pair lacks eligible single evidence")
            observed_expected = math.prod(
                float(single["geometric_mean_speedup"]) for single in singles
            )
            observed_delta = math.log(observed_pair) - math.log(observed_expected)
            declared = (
                row["pair_geometric_mean_speedup"],
                row["expected_multiplicative_speedup"],
                row["delta_ln_geometric_mean"],
            )
            recomputed = (observed_pair, observed_expected, observed_delta)
            if row["comparable_case_count"] != len(baseline_metrics) or any(
                not math.isclose(
                    float(left), float(right), rel_tol=1e-12, abs_tol=1e-12
                )
                for left, right in zip(declared, recomputed)
            ):
                raise ValidationError("fast report pair interaction differs from normalized runs")
        pairs.append(
            {
                "candidate_ids": row["candidate_ids"],
                "geometric_mean_speedup": observed_pair,
                "expected_multiplicative_speedup": observed_expected,
                "delta_ln_geometric_mean": observed_delta,
                "interaction_factor": (
                    None if observed_delta is None else math.exp(observed_delta)
                ),
                "eligible": row["eligible"],
                "reason": row["ineligibility_reason"],
                "run_id": run["run_id"],
            }
        )

    cache_full_row = diagnostic["cache_full"]
    if not _receipt_ref_is_bound(cache_full_row["receipt"], index, bootstrap):
        raise ValidationError("fast report cache FULL receipt is absent from current evidence")
    cache_full = _load_run_from_ref(root, cache_full_row["receipt"], bootstrap)
    cache_rows = [
        _cache_run_projection(
            label="FULL",
            run=cache_full,
            complete=(
                cache_full_row["terminal_state"] == "completed"
                and cache_full_row["correctness_passed"]
                and cache_full_row["metrics_complete"]
            ),
            failure_reason=(
                None
                if cache_full_row["terminal_state"] == "completed"
                else "terminal_failure"
            ),
        )
    ]
    for row in diagnostic["cache_candidates"]:
        if not _receipt_ref_is_bound(row["receipt"], index, bootstrap):
            raise ValidationError("fast report cache receipt is absent from current evidence")
        candidate = _load_run_from_ref(root, row["receipt"], bootstrap)
        complete = (
            row["terminal_state"] == "completed"
            and row["correctness_passed"]
            and row["metrics_complete"]
        )
        reason = None
        if row["terminal_state"] != "completed":
            reason = "terminal_failure"
        elif not row["correctness_passed"]:
            reason = "correctness_failure"
        elif not row["metrics_complete"]:
            reason = "metrics_incomplete"
        cache_rows.append(
            _cache_run_projection(
                label=row["candidate_id"],
                run=candidate,
                complete=complete,
                failure_reason=reason,
            )
        )
    return pairs, cache_rows


def _reference_projection(
    root: Path,
    index: Mapping[str, Any],
    bootstrap: Mapping[str, Any],
) -> list[dict[str, Any]]:
    full = _indexed_run(root, index, bootstrap, "run.B3.full")
    rows: list[dict[str, Any]] = []
    for label, task_id in (("GCC 13.3 -O2", "run.B3.gcc"), ("Clang 18 -O3", "run.B3.clang")):
        reference = _indexed_run(root, index, bootstrap, task_id)
        if full is None or reference is None:
            rows.append(
                {
                    "reference": label,
                    "status": "N/A",
                    "gap_ratio": None,
                    "reason": "normalized_reference_run_absent",
                    "full_run_id": None if full is None else full["run_id"],
                    "reference_run_id": None if reference is None else reference["run_id"],
                }
            )
            continue
        gap, count = _equal_case_speedup(full, reference, full["configuration"]["primary_metric_id"])
        rows.append(
            {
                "reference": label,
                "status": "complete" if gap is not None else "N/A",
                "gap_ratio": gap,
                "case_count": count,
                "reason": None if gap is not None else "incomplete_or_incomparable_metrics",
                "full_run_id": full["run_id"],
                "reference_run_id": reference["run_id"],
            }
        )
    return rows


def build_fast_report_projection(
    *,
    bootstrap_path: Path,
    plan_path: Path,
    index_path: Path,
    status_path: Path,
    audit_paths: Mapping[str, Path],
    study_paths: Mapping[str, Path],
    diagnostic_study_path: Path,
    workspace_root: Path,
) -> dict[str, Any]:
    root = _root(workspace_root)
    bootstrap, bootstrap_physical = _load(root, bootstrap_path, "candidate-fast-bootstrap.v1", label="fast report bootstrap")
    plan, plan_physical = _load(root, plan_path, "candidate-fast-campaign-plan.v1", label="fast report plan")
    index, index_physical = _load(root, index_path, "candidate-fast-run-index.v1", label="fast report index")
    status, status_physical = _load(root, status_path, "candidate-fast-status.v1", label="fast report status")
    if (
        bootstrap["campaign_id"] != plan["campaign_id"]
        or plan["bootstrap"] != _artifact(root, bootstrap_physical, label="fast report bootstrap")
        or index["campaign_id"] != plan["campaign_id"]
        or index["bootstrap_sha256"] != sha256_json(bootstrap)
        or index["plan_sha256"] != sha256_json(plan)
        or status["campaign_id"] != plan["campaign_id"]
        or status["bootstrap"] != plan["bootstrap"]
        or status["plan"] != _artifact(root, plan_physical, label="fast report plan")
        or status["index"] != _artifact(root, index_physical, label="fast report index")
        or status["state"] != "running"
    ):
        raise ValidationError("fast report inputs bind different campaign snapshots")
    final_tasks = [task for task in plan["tasks"] if task["kind"] == "final"]
    final_row = next((row for row in status["tasks"] if final_tasks and row["task_id"] == final_tasks[0]["task_id"]), None)
    if len(final_tasks) != 1 or final_row is None or final_row["state"] != "ready" or final_tasks[0]["task_id"] not in status["ready_tasks"]:
        raise ValidationError("fast report requires the exact final-ready status")
    if set(audit_paths) != {"bootstrap", "B2", "B3", "final"}:
        raise ConfigurationError("fast report requires exactly four checkpoint audits")
    audit_refs: dict[str, dict[str, str]] = {}
    status_audits = {row["artifact_id"]: row["artifact"] for row in status["audits"]}
    for checkpoint, path in audit_paths.items():
        audit, physical = _load(root, path, "candidate-fast-audit.v1", label=f"fast report {checkpoint} audit")
        audited_status_path = _verify_artifact(
            root, audit["status"], label=f"fast report {checkpoint} audited status"
        )
        audited_status = read_json(audited_status_path)
        if (
            not isinstance(audited_status, dict)
            or audited_status.get("schema_version") != "candidate-fast-status.v1"
        ):
            raise ValidationError("fast report audit status has an unexpected schema version")
        validate_document(audited_status)
        audit_artifact = _artifact(root, physical, label=f"fast report {checkpoint} audit")
        if (
            audit["checkpoint"] != checkpoint
            or audit["campaign_id"] != plan["campaign_id"]
            or audit["bootstrap_sha256"] != sha256_json(bootstrap)
            or audit["plan_sha256"] != sha256_json(plan)
            or audit["status_sha256"] != sha256_json(audited_status)
            or not audit["passed"]
            or status_audits.get(audit["audit_id"]) != audit_artifact
        ):
            raise ValidationError("fast report audit binding differs or failed")
        if checkpoint == "final":
            audited_task = next(
                (
                    row
                    for row in audited_status["tasks"]
                    if row["task_id"] == "audit.final"
                ),
                None,
            )
            if (
                audited_status["campaign_id"] != plan["campaign_id"]
                or audited_status["plan"]
                != _artifact(root, plan_physical, label="fast report plan")
                or audited_status["index"]
                != _artifact(root, index_physical, label="fast report index")
                or audit["index_sha256"] != sha256_json(index)
                or audited_status["generation"] + 1 != status["generation"]
                or audited_task is None
                or audited_task["state"] != "ready"
                or "audit.final" not in audited_status["ready_tasks"]
                or any(
                    row["artifact_id"] == audit["audit_id"]
                    for row in audited_status["audits"]
                )
            ):
                raise ValidationError(
                    "fast report final audit does not bind the prior pre-audit status"
                )
        audit_refs[checkpoint] = audit_artifact
    if not {"B2", "B3"} <= set(study_paths) or not set(study_paths) <= set(_ALL_STAGES):
        raise ConfigurationError("fast report requires B2/B3 and optional B4-B6 studies")
    study_docs: dict[str, dict[str, Any]] = {}
    study_refs: dict[str, dict[str, str]] = {}
    projections: dict[str, dict[str, dict[str, Any]]] = {}
    status_studies = {row["artifact_id"]: row["artifact"] for row in status["studies"]}
    for stage, path in study_paths.items():
        study, physical = _load(root, path, "candidate-fast-study.v1", label=f"fast report {stage} study")
        if (
            study["stage"] != stage
            or study["campaign_id"] != plan["campaign_id"]
            or study["bootstrap_sha256"] != sha256_json(bootstrap)
            or study["plan_sha256"] != sha256_json(plan)
        ):
            raise ValidationError("fast report study binding differs")
        refs = [study["baseline"], *[row["receipt"] for row in study["candidates"]]]
        if not all(_receipt_ref_is_bound(ref, index, bootstrap) for ref in refs):
            raise ValidationError("fast report study receipt is absent from current evidence")
        study_docs[stage] = study
        study_refs[stage] = _artifact(root, physical, label=f"fast report {stage} study")
        if status_studies.get(study["study_id"]) != study_refs[stage]:
            raise ValidationError("fast report study is absent from the final-ready status")
        projections[stage] = _stage_projection(root=root, study=study, bootstrap=bootstrap, index=index)
    promoted = [
        candidate_id for candidate_id in plan["candidate_ids"]
        if projections["B3"][candidate_id]["eligible"]
        and float(projections["B3"][candidate_id]["geometric_mean_speedup"]) > 1.0
    ]
    validation_stages = set(study_paths) & set(_RANKING_STAGES[1:])
    if promoted and validation_stages != {"B4", "B5", "B6"}:
        raise ValidationError("fast report promoted candidates require B4-B6 studies")
    if not promoted and validation_stages:
        raise ValidationError("fast report without promotion cannot claim B4-B6 studies")
    if promoted and any(study_docs[stage]["evaluated_candidate_ids"] != promoted for stage in ("B4", "B5", "B6")):
        raise ValidationError("fast report B4-B6 subset differs from B3 promotion")
    diagnostic, diagnostic_physical = _load(root, diagnostic_study_path, "candidate-fast-diagnostic-study.v1", label="fast report diagnostic study")
    if (
        diagnostic["campaign_id"] != plan["campaign_id"]
        or diagnostic["bootstrap_sha256"] != sha256_json(bootstrap)
        or diagnostic["plan_sha256"] != sha256_json(plan)
        or diagnostic["b3_study"] != study_refs["B3"]
        or status["diagnostic_study"] != _artifact(root, diagnostic_physical, label="fast report diagnostic study")
    ):
        raise ValidationError("fast report diagnostic study binding differs")
    pair_projection, cache_projection = _diagnostic_projection(
        root, diagnostic, bootstrap, index, study_docs["B3"]
    )
    oracle_projection, risk_by_candidate, source_evidence = _screening_projection(
        root, plan
    )
    reference_gaps = _reference_projection(root, index, bootstrap)
    candidates: list[dict[str, Any]] = []
    for candidate_id in plan["candidate_ids"]:
        is_promoted = candidate_id in promoted
        stages = _RANKING_STAGES if is_promoted else ("B3",)
        evidence = [projections[stage][candidate_id] for stage in stages]
        reasons = _candidate_ineligibility_reasons(
            is_promoted=is_promoted,
            stage_results={
                stage: projections[stage][candidate_id]
                for stage in ("B2", *stages)
            },
        )
        speedups = [value for item in evidence for value in item["per_case_speedups"]]
        full_text_values = [item["static_text_bytes_full"] for item in evidence]
        candidate_text_values = [item["static_text_bytes_candidate"] for item in evidence]
        expected_count = 267 if is_promoted else evidence[0]["case_count"]
        if is_promoted and len(speedups) != 267:
            reasons.append("combined_case_count_not_267")
        if is_promoted and (any(value is None for value in full_text_values) or any(value is None for value in candidate_text_values)):
            reasons.append("missing_static_text_evidence")
        eligible = is_promoted and not reasons
        combined = math.exp(sum(math.log(value) for value in speedups) / len(speedups)) if eligible else None
        full_text = sum(float(value) for value in full_text_values if value is not None) if eligible else None
        candidate_text = sum(float(value) for value in candidate_text_values if value is not None) if eligible else None
        candidates.append(
            {
                "candidate_id": candidate_id,
                "promoted": is_promoted,
                "eligible_for_final": eligible,
                "ineligibility_reasons": reasons,
                "combined_case_count": len(speedups) if is_promoted else expected_count,
                "combined_geometric_mean_speedup": combined,
                "b3_geometric_mean_speedup": projections["B3"][candidate_id]["geometric_mean_speedup"],
                "combined_static_text_bytes_candidate": candidate_text,
                "combined_static_text_ratio": None if not eligible else full_text / candidate_text,
                "rank": None,
                "risk": risk_by_candidate.get(candidate_id, "unknown"),
                "ranking_run_ids": [
                    {
                        "stage": stage,
                        "baseline_run_id": projections[stage][candidate_id][
                            "baseline_run_id"
                        ],
                        "candidate_run_id": projections[stage][candidate_id][
                            "candidate_run_id"
                        ],
                    }
                    for stage in stages
                ],
                "stages": {
                    stage: (
                        None if stage not in projections or candidate_id not in projections[stage]
                        else {
                            "eligible": projections[stage][candidate_id]["eligible"],
                            "geometric_mean_speedup": projections[stage][candidate_id]["geometric_mean_speedup"],
                            "case_count": projections[stage][candidate_id]["case_count"],
                            "confidence_interval_95": projections[stage][candidate_id][
                                "confidence_interval_95"
                            ],
                            "baseline_run_id": projections[stage][candidate_id][
                                "baseline_run_id"
                            ],
                            "candidate_run_id": projections[stage][candidate_id][
                                "candidate_run_id"
                            ],
                        }
                    )
                    for stage in _ALL_STAGES
                },
            }
        )
    eligible_rows = sorted(
        (row for row in candidates if row["eligible_for_final"]),
        key=lambda row: (
            -float(row["combined_geometric_mean_speedup"]),
            -float(row["b3_geometric_mean_speedup"]),
            float(row["combined_static_text_bytes_candidate"]),
            row["candidate_id"],
        ),
    )
    for rank, row in enumerate(eligible_rows, 1):
        row["rank"] = rank
    candidates_by_id = {row["candidate_id"]: row for row in candidates}
    for row in oracle_projection["rows"]:
        candidate = candidates_by_id[row["candidate_id"]]
        b3 = candidate["b3_geometric_mean_speedup"]
        upper = row["oracle_upper_bound"]
        if b3 is None:
            row["reason"] = "B3_measurement_unavailable"
            continue
        row["b3_measured_speedup"] = float(b3)
        row["capture_rate"] = float(b3) / float(upper)
        row["reason"] = None
    return {
        "campaign_id": plan["campaign_id"],
        "evidence_level": "qemu_proxy",
        "bootstrap_sha256": sha256_json(bootstrap),
        "plan_sha256": sha256_json(plan),
        "index_sha256": sha256_json(index),
        "status_sha256": sha256_json(status),
        "audits": audit_refs,
        "studies": {stage: study_refs.get(stage) for stage in _ALL_STAGES},
        "diagnostic_study": _artifact(root, diagnostic_physical, label="fast report diagnostic study"),
        "diagnostic_top3": diagnostic["top3_candidate_ids"],
        "diagnostic_pair_count": len(diagnostic["pairs"]),
        "diagnostic_pairs": pair_projection,
        "cache_hotblock": cache_projection,
        "oracle_capture": oracle_projection,
        "source_evidence": source_evidence,
        "reference_gaps": reference_gaps,
        "promoted_candidate_ids": promoted,
        "candidates": candidates,
        "ranking": [
            {
                "rank": row["rank"],
                "candidate_id": row["candidate_id"],
                "combined_geometric_mean_speedup": row["combined_geometric_mean_speedup"],
                "b3_geometric_mean_speedup": row["b3_geometric_mean_speedup"],
                "combined_static_text_bytes_full_plus_candidate": row[
                    "combined_static_text_bytes_candidate"
                ],
                "combined_static_text_ratio": row[
                    "combined_static_text_ratio"
                ],
                "stable_id_tiebreak": row["candidate_id"],
            }
            for row in eligible_rows
        ],
    }


def _fmt(value: float | None, digits: int = 4) -> str:
    return "N/A" if value is None else f"{value:.{digits}f}"


def _run_ids(value: Sequence[Mapping[str, Any]]) -> str:
    ids = [
        run_id
        for row in value
        for run_id in (row["baseline_run_id"], row["candidate_run_id"])
    ]
    return ", ".join(f"`{run_id}`" for run_id in ids) or "N/A"


def _candidate_ineligibility_reasons(
    *,
    is_promoted: bool,
    stage_results: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    if not is_promoted:
        return ["not_promoted_by_B3"]
    return [
        f"{stage}:{stage_results[stage]['reason']}"
        for stage in ("B2", *_RANKING_STAGES)
        if not stage_results[stage]["eligible"]
    ]


def _report_markdown(projection: Mapping[str, Any]) -> bytes:
    lines = [
        "# ACCELA fast candidate evaluation",
        "",
        f"Campaign: `{projection['campaign_id']}`  ",
        "Evidence: `qemu_proxy` (not BOOM hardware or release evidence)  ",
        f"Plan: `{projection['plan_sha256']}`  ",
        f"Index: `{projection['index_sha256']}`  ",
        f"Status: `{projection['status_sha256']}`",
        "",
        "## Final ranking",
        "",
        "Only B3-B6 (267 cases) enter the combined geometric mean. B2 remains a mandatory correctness and coverage eligibility gate.",
        "",
        "| Rank | Candidate | B3-B6 GM | B3 GM | Static text bytes | Normalized run IDs |",
        "| ---: | --- | ---: | ---: | ---: | --- |",
    ]
    if projection["ranking"]:
        candidates = {row["candidate_id"]: row for row in projection["candidates"]}
        for row in projection["ranking"]:
            lines.append(
                f"| {row['rank']} | `{row['candidate_id']}` | {_fmt(row['combined_geometric_mean_speedup'])} | "
                f"{_fmt(row['b3_geometric_mean_speedup'])} | {int(row['combined_static_text_bytes_full_plus_candidate'])} | "
                f"{_run_ids(candidates[row['candidate_id']]['ranking_run_ids'])} |"
            )
    else:
        lines.append("| - | No eligible candidate | N/A | N/A | N/A | N/A |")
    lines.extend(["", "## Candidate gates", "", "| Candidate | Promoted | Final eligible | B3 95% CI | Reasons |", "| --- | --- | --- | --- | --- |"])
    for row in projection["candidates"]:
        reasons = ", ".join(row["ineligibility_reasons"]) or "-"
        interval = row["stages"]["B3"]["confidence_interval_95"]
        ci = "N/A" if interval is None else f"[{_fmt(interval['low'])}, {_fmt(interval['high'])}]"
        lines.append(f"| `{row['candidate_id']}` | {'yes' if row['promoted'] else 'no'} | {'yes' if row['eligible_for_final'] else 'no'} | {ci} | {reasons} |")
    lines.extend(
        [
            "",
            "## Suite evidence",
            "",
            "| Candidate | B2 | B3 | B4 | B5 | B6 |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in projection["candidates"]:
        values = [
            "n/a" if row["stages"][stage] is None else _fmt(row["stages"][stage]["geometric_mean_speedup"])
            for stage in _ALL_STAGES
        ]
        lines.append(f"| `{row['candidate_id']}` | " + " | ".join(values) + " |")
    lines.extend(
        [
            "",
            "## Oracle qualification and capture",
            "",
            f"This view is derived from the bound normalized 99-pair Oracle closure. Capture SHA-256: `{projection['oracle_capture']['capture_sha256']}`; source run IDs: `{projection['oracle_capture']['baseline_run_id']}`, `{projection['oracle_capture']['optimized_run_id']}`.",
            "",
            "| Candidate | Oracle upper bound | B3 measured | Capture rate | Eligible / qualifying structures | Reason |",
            "| --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in projection["oracle_capture"]["rows"]:
        lines.append(
            f"| `{row['candidate_id']}` | {_fmt(row['oracle_upper_bound'])} | {_fmt(row['b3_measured_speedup'])} | "
            f"{_fmt(row['capture_rate'])} | {row['eligible_structures']} / {row['qualifying_structures']} | {row['reason'] or '-'} |"
        )
    if not projection["oracle_capture"]["rows"]:
        lines.append("| N/A | N/A | N/A | N/A | N/A | normalized_oracle_rows_absent |")
    lines.extend(
        [
            "",
            "## Top3 pair diagnostics",
            "",
            "Top3: " + (", ".join(f"`{item}`" for item in projection["diagnostic_top3"]) or "none"),
            f"Pair observations: {projection['diagnostic_pair_count']}",
            "",
            "| Candidates | Pair GM | Independent product | Delta ln(GM) | Interaction factor | Eligible / reason | Run ID |",
            "| --- | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for row in projection["diagnostic_pairs"]:
        lines.append(
            f"| {' + '.join(f'`{item}`' for item in row['candidate_ids'])} | {_fmt(row['geometric_mean_speedup'])} | "
            f"{_fmt(row['expected_multiplicative_speedup'])} | {_fmt(row['delta_ln_geometric_mean'], 6)} | "
            f"{_fmt(row['interaction_factor'])} | {'yes' if row['eligible'] else 'no'} / {row['reason'] or '-'} | `{row['run_id']}` |"
        )
    if not projection["diagnostic_pairs"]:
        lines.append("| N/A | N/A | N/A | N/A | N/A | fewer_than_two_top3_candidates | N/A |")
    lines.extend(
        [
            "",
            "## Cache and hotblock diagnostics",
            "",
            "| Profile | L1D misses / 1000 dynamic loads | Hottest block share (%) | Samples | Reason | Run ID |",
            "| --- | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for row in projection["cache_hotblock"]:
        lines.append(
            f"| `{row['label']}` | {_fmt(row['mean_l1d_misses_per_1000_dynamic_loads'])} | "
            f"{_fmt(row['mean_hottest_block_dynamic_instruction_share_percent'])} | {row['sample_count']} | "
            f"{row['reason'] or '-'} | `{row['run_id']}` |"
        )
    if not projection["cache_hotblock"]:
        lines.append("| N/A | N/A | N/A | N/A | normalized_cache_runs_absent | N/A |")
    lines.extend(
        [
            "",
            "## GCC/Clang gap",
            "",
            "The ratio is ACCELA B3 FULL dynamic instructions divided by the reference run; values above 1 mean the reference used fewer instructions.",
            "",
            "| Reference | Status | Gap ratio | Cases | Reason | Normalized run IDs |",
            "| --- | --- | ---: | ---: | --- | --- |",
        ]
    )
    for row in projection.get("reference_gaps", []):
        run_ids = ", ".join(
            f"`{value}`"
            for value in (row.get("full_run_id"), row.get("reference_run_id"))
            if value is not None
        ) or "N/A"
        lines.append(
            f"| {row['reference']} | {row['status']} | {_fmt(row['gap_ratio'])} | {row.get('case_count', 0)} | {row['reason'] or '-'} | {run_ids} |"
        )
    lines.extend(
        [
            "",
            "## Figure contract",
            "",
            "Seven deterministic SVGs are emitted: single-candidate gain with deterministic 10,000-sample paired-case bootstrap intervals (seed 20260809), per-suite results, combined ranking, pair heatmap, Oracle capture, cache/hotblock diagnostics, and gain/code-size/risk Pareto.",
            "",
            "All paths in the bound manifest are workspace-relative. This report does not enable any candidate in the judge pipeline.",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def _svg_document(
    title: str,
    rows: Sequence[tuple[str, float]],
    *,
    x_label: str,
    evidence: Mapping[str, Any] | None = None,
    unavailable_reason: str | None = None,
) -> bytes:
    width = 960
    margin_left = 230
    plot_width = 670
    row_height = 36
    height = 100 + max(1, len(rows)) * row_height
    maximum = max((value for _, value in rows), default=1.0)
    scale_max = max(1.0, maximum) * 1.05
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f'<metadata id="evidence">{html.escape(canonical_json_bytes(dict(evidence or {})).decode("utf-8"))}</metadata>',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="24" y="32" font-family="sans-serif" font-size="20" font-weight="700">{html.escape(title)}</text>',
        f'<text x="{margin_left}" y="58" font-family="sans-serif" font-size="12">{html.escape(x_label)}</text>',
        f'<line x1="{margin_left}" y1="68" x2="{margin_left + plot_width}" y2="68" stroke="#334155"/>',
    ]
    if not rows:
        reason = unavailable_reason or "no_eligible_evidence"
        parts.append(
            f'<text x="24" y="96" font-family="sans-serif" font-size="14">N/A: {html.escape(reason)}</text>'
        )
    for index, (label, value) in enumerate(rows):
        y = 82 + index * row_height
        bar = max(1.0, plot_width * value / scale_max)
        parts.extend(
            [
                f'<text x="24" y="{y + 16}" font-family="sans-serif" font-size="12">{html.escape(label)}</text>',
                f'<rect x="{margin_left}" y="{y}" width="{bar:.3f}" height="20" fill="#2563eb"/>',
                f'<text x="{margin_left + bar + 6:.3f}" y="{y + 15}" font-family="sans-serif" font-size="12">{value:.4f}</text>',
            ]
        )
    parts.append("</svg>")
    return ("\n".join(parts) + "\n").encode("utf-8")


def _with_svg_metadata(
    svg: str | bytes,
    evidence: Mapping[str, Any],
    *,
    unavailable_reason: str | None = None,
) -> bytes:
    payload = svg.encode("utf-8") if isinstance(svg, str) else svg
    marker = payload.find(b">")
    if marker < 0:
        raise ValidationError("fast report SVG lacks a root start tag")
    annotation = (
        '<metadata id="evidence">'
        + html.escape(canonical_json_bytes(dict(evidence)).decode("utf-8"))
        + "</metadata>"
    )
    if unavailable_reason is not None:
        annotation += (
            f'<text data-role="na" x="24" y="78" font-family="sans-serif" '
            f'font-size="12">N/A: {html.escape(unavailable_reason)}</text>'
        )
    return (
        payload[: marker + 1]
        + b"\n"
        + annotation.encode("utf-8")
        + payload[marker + 1 :]
    )


def _svg_single_candidate_ci(
    rows: Sequence[tuple[str, float, float, float]],
    *,
    evidence: Mapping[str, Any],
) -> bytes:
    width, left, right, top, row_height = 1120, 300, 170, 88, 42
    height = max(180, top + row_height * max(1, len(rows)) + 54)
    transformed = [
        (label, point, low, high, 100 * math.log(point), 100 * math.log(low), 100 * math.log(high))
        for label, point, low, high in rows
        if all(math.isfinite(value) and value > 0 for value in (point, low, high))
        and low <= point <= high
    ]
    if len(transformed) != len(rows):
        raise ValidationError("fast report B3 confidence interval is invalid")
    extent = max(
        (abs(value) for row in transformed for value in row[4:]), default=1.0
    )
    extent = max(extent, 0.5)
    center = left + (width - left - right) / 2
    half = (width - left - right) / 2
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" data-chart-kind="confidence-interval">',
        '<rect width="100%" height="100%" fill="#fff"/>',
        '<style>text{font-family:system-ui,sans-serif;fill:#172033}.t{font-size:20px;font-weight:700}.l{font-size:12px}.n{font-size:11px}.axis{stroke:#596579}.ci{stroke:#334155;stroke-width:3}.cap{stroke:#334155}.point{fill:#2563eb}</style>',
        '<text class="t" x="24" y="32">B3 single-candidate gain and 95% CI</text>',
        '<text class="n" x="24" y="54">Paired-case bootstrap: 10,000 samples; seed 20260809; axis = 100 × ln(speedup).</text>',
        f'<line class="axis" x1="{center:.2f}" y1="{top - 10}" x2="{center:.2f}" y2="{height - 32}"/>',
    ]
    if not transformed:
        parts.append('<text class="l" x="24" y="112" data-role="na">N/A: no complete B3 candidate measurements</text>')
    for index, (label, point, low, high, point_log, low_log, high_log) in enumerate(transformed):
        y = top + index * row_height + 13
        scale = half / extent
        x_low, x_point, x_high = (
            center + low_log * scale,
            center + point_log * scale,
            center + high_log * scale,
        )
        parts.extend(
            [
                f'<g data-candidate="{html.escape(label)}">',
                f'<text class="l" x="24" y="{y + 4}">{html.escape(label)}</text>',
                f'<line class="ci" data-role="ci95" x1="{x_low:.2f}" y1="{y}" x2="{x_high:.2f}" y2="{y}"/>',
                f'<line class="cap" x1="{x_low:.2f}" y1="{y - 7}" x2="{x_low:.2f}" y2="{y + 7}"/>',
                f'<line class="cap" x1="{x_high:.2f}" y1="{y - 7}" x2="{x_high:.2f}" y2="{y + 7}"/>',
                f'<circle class="point" data-role="estimate" cx="{x_point:.2f}" cy="{y}" r="6"/>',
                f'<text class="n" x="{width - 24}" y="{y + 4}" text-anchor="end">{point:.4f} [{low:.4f}, {high:.4f}]</text>',
                "</g>",
            ]
        )
    parts.append("</svg>")
    return _with_svg_metadata("\n".join(parts) + "\n", evidence)


def _report_payloads(projection: Mapping[str, Any]) -> dict[str, bytes]:
    run_evidence = {
        row["candidate_id"]: row["ranking_run_ids"]
        for row in projection["candidates"]
    }
    stage_evidence = {
        row["candidate_id"]: {
            stage: {
                "baseline_run_id": result["baseline_run_id"],
                "candidate_run_id": result["candidate_run_id"],
            }
            for stage in _RANKING_STAGES
            if (result := row["stages"].get(stage)) is not None
        }
        for row in projection["candidates"]
    }
    single_rows: list[tuple[str, float, float, float]] = []
    for row in projection["candidates"]:
        b3 = row["stages"]["B3"]
        if b3 is None or b3["geometric_mean_speedup"] is None:
            continue
        interval = b3["confidence_interval_95"]
        if interval is None:
            raise ValidationError("fast report measurable B3 row lacks its bootstrap interval")
        single_rows.append(
            (
                row["candidate_id"],
                float(b3["geometric_mean_speedup"]),
                float(interval["low"]),
                float(interval["high"]),
            )
        )
    suite_rows = [
        (
            f"{row['candidate_id']} / {stage}",
            float(row["stages"][stage]["geometric_mean_speedup"]),
        )
        for row in projection["candidates"]
        for stage in _RANKING_STAGES
        if row["stages"][stage] is not None
        and row["stages"][stage]["geometric_mean_speedup"] is not None
    ]
    ranking_rows = [
        (row["candidate_id"], float(row["combined_geometric_mean_speedup"]))
        for row in projection["ranking"]
    ]
    pair_ids = projection["diagnostic_top3"]
    pair_values = {
        tuple(row["candidate_ids"]): float(row["interaction_factor"])
        for row in projection["diagnostic_pairs"]
        if row["interaction_factor"] is not None
    }
    pair_values.update(
        {(right, left): value for (left, right), value in list(pair_values.items())}
    )
    oracle_rows = [
        (f"{row['candidate_id']} / Oracle", float(row["oracle_upper_bound"]))
        for row in projection["oracle_capture"]["rows"]
        if row["oracle_upper_bound"] is not None
    ] + [
        (
            f"{row['candidate_id']} / B3 / capture={_fmt(row['capture_rate'])}",
            float(row["b3_measured_speedup"]),
        )
        for row in projection["oracle_capture"]["rows"]
        if row["b3_measured_speedup"] is not None
    ]
    cache_rows = [
        (
            row["label"],
            row["mean_l1d_misses_per_1000_dynamic_loads"],
            row["mean_hottest_block_dynamic_instruction_share_percent"],
        )
        for row in projection["cache_hotblock"]
    ]
    pareto_rows = [
        (
            row["candidate_id"],
            float(row["combined_static_text_bytes_candidate"]),
            float(row["combined_geometric_mean_speedup"]),
            row["risk"],
        )
        for row in projection["candidates"]
        if row["eligible_for_final"]
        and row["combined_geometric_mean_speedup"] is not None
        and row["combined_static_text_bytes_candidate"] is not None
    ]
    oracle_evidence = {
        "pair_count": projection["oracle_capture"]["pair_count"],
        "baseline_run_id": projection["oracle_capture"]["baseline_run_id"],
        "optimized_run_id": projection["oracle_capture"]["optimized_run_id"],
        "capture_sha256": projection["oracle_capture"]["capture_sha256"],
        "b3_run_ids": {
            candidate_id: evidence.get("B3")
            for candidate_id, evidence in stage_evidence.items()
        },
    }
    pair_evidence = {
        "run_ids": [row["run_id"] for row in projection["diagnostic_pairs"]],
        "top3_candidate_ids": pair_ids,
    }
    cache_evidence = {
        "run_ids": [row["run_id"] for row in projection["cache_hotblock"]]
    }
    return {
        "report": _report_markdown(projection),
        "single_candidate_chart": _svg_single_candidate_ci(
            single_rows, evidence={"run_ids": stage_evidence, "seed": _BOOTSTRAP_SEED, "samples": _BOOTSTRAP_SAMPLES}
        ),
        "suite_chart": _with_svg_metadata(
            _svg_ratio_diverging(
                "B3-B6 per-suite geometric mean speedup",
                suite_rows,
                evidence_note="Normalized QEMU proxy runs; missing or ineligible stages remain N/A.",
            ),
            {"run_ids": stage_evidence},
            unavailable_reason=None if suite_rows else "no_complete_suite_measurements",
        ),
        "ranking_chart": _with_svg_metadata(
            _svg_ratio_diverging(
                "267-case combined ranking",
                ranking_rows,
                evidence_note="All 267 B3-B6 cases have equal weight; fixed four-level tie-break.",
            ),
            {"run_ids": run_evidence},
            unavailable_reason=None if ranking_rows else "no_finally_eligible_candidate",
        ),
        "pair_heatmap": _with_svg_metadata(
            _svg_heatmap(
                "B3 Top3 interaction 100×delta-ln(GM)",
                pair_ids,
                pair_ids,
                pair_values,
            ),
            pair_evidence,
            unavailable_reason=None if pair_values else "no_eligible_top3_pair_measurements",
        ),
        "oracle_capture_chart": _with_svg_metadata(
            _svg_ratio_diverging(
                "Oracle upper bound and measured B3 capture",
                oracle_rows,
                evidence_note="Oracle is a normalized 99-pair upper bound; B3 is measured QEMU proxy evidence.",
            ),
            oracle_evidence,
            unavailable_reason=None if oracle_rows else "normalized_oracle_or_B3_rows_absent",
        ),
        "cache_hotblock_chart": _with_svg_metadata(
            _svg_cache_hotblock("B3 FULL and Top3 cache/hotblock diagnostics", cache_rows),
            cache_evidence,
            unavailable_reason=None if cache_rows else "normalized_cache_hotblock_runs_absent",
        ),
        "pareto_chart": _with_svg_metadata(
            _svg_pareto("Candidate gain, text bytes, and risk Pareto", pareto_rows),
            {"run_ids": run_evidence, "risk_source": "candidate-screening.v1"},
            unavailable_reason=None if pareto_rows else "no_finally_eligible_candidate_with_size_and_risk",
        ),
    }


def _publish_create_only(path: Path, payload: bytes, *, label: str) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise ValidationError(f"{label} already exists with different bytes")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    resolve_without_symlinks(path.parent, label=f"{label} parent")
    durable_create_bytes(path, payload)


def build_fast_report(
    *,
    bootstrap_path: Path,
    plan_path: Path,
    index_path: Path,
    status_path: Path,
    audit_paths: Mapping[str, Path],
    study_paths: Mapping[str, Path],
    diagnostic_study_path: Path,
    output_directory: Path,
    workspace_root: Path,
) -> dict[str, Any]:
    root = _root(workspace_root)
    projection = build_fast_report_projection(
        bootstrap_path=bootstrap_path,
        plan_path=plan_path,
        index_path=index_path,
        status_path=status_path,
        audit_paths=audit_paths,
        study_paths=study_paths,
        diagnostic_study_path=diagnostic_study_path,
        workspace_root=root,
    )
    output, relative = _output_directory(root, output_directory)
    payloads = _report_payloads(projection)
    files: dict[str, dict[str, Any]] = {}
    for artifact_id, filename in _ARTIFACT_FILENAMES.items():
        path = output / filename
        payload = payloads[artifact_id]
        files[artifact_id] = {
            "path": f"{relative}/{filename}",
            "physical_sha256": sha256_bytes(payload),
            "size_bytes": len(payload),
            "media_type": "text/markdown; charset=utf-8" if artifact_id == "report" else "image/svg+xml",
        }
    manifest: dict[str, Any] = {
        "schema_version": _MANIFEST_VERSION,
        "campaign_id": projection["campaign_id"],
        "evidence_level": "qemu_proxy",
        "input_commitments": fast_report_input_commitments_from_projection(
            projection
        ),
        "ranking": projection["ranking"],
        "ranking_sha256": sha256_json(projection["ranking"]),
        "files": files,
        "manifest_commitment_sha256": "0" * 64,
    }
    manifest["manifest_commitment_sha256"] = sha256_json(
        {key: value for key, value in manifest.items() if key != "manifest_commitment_sha256"}
    )
    _validate_manifest(manifest)
    manifest_path = output / "manifest.json"
    manifest_payload = canonical_json_bytes(manifest) + b"\n"
    for path in [*(output / filename for filename in _ARTIFACT_FILENAMES.values()), manifest_path]:
        if path.exists():
            expected = manifest_payload if path == manifest_path else payloads[next(key for key, filename in _ARTIFACT_FILENAMES.items() if path.name == filename)]
            if path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
                raise ValidationError("fast report output set already differs")
    for artifact_id, filename in _ARTIFACT_FILENAMES.items():
        _publish_create_only(output / filename, payloads[artifact_id], label=f"fast report {artifact_id}")
    if manifest_path.exists():
        if manifest_path.read_bytes() != manifest_payload:
            raise ValidationError("fast report manifest already differs")
    else:
        durable_create_json(manifest_path, manifest)
    return manifest


def _validate_manifest(document: Mapping[str, Any]) -> None:
    if set(document) != {
        "schema_version", "campaign_id", "evidence_level", "input_commitments",
        "ranking", "ranking_sha256", "files", "manifest_commitment_sha256",
    } or document["schema_version"] != _MANIFEST_VERSION or document["evidence_level"] != "qemu_proxy":
        raise ValidationError("fast report manifest top-level contract differs")
    if set(document["files"]) != set(_ARTIFACT_FILENAMES):
        raise ValidationError("fast report manifest file set differs")
    if (
        not isinstance(document["ranking"], list)
        or document["ranking_sha256"] != sha256_json(document["ranking"])
        or not _SHA256.fullmatch(document["ranking_sha256"])
    ):
        raise ValidationError("fast report manifest ranking hash is invalid")
    for artifact_id, filename in _ARTIFACT_FILENAMES.items():
        row = document["files"][artifact_id]
        expected_media_type = (
            "text/markdown; charset=utf-8"
            if artifact_id == "report"
            else "image/svg+xml"
        )
        if (
            set(row) != {"path", "physical_sha256", "size_bytes", "media_type"}
            or Path(row["path"]).name != filename
            or not _SHA256.fullmatch(row["physical_sha256"])
            or not isinstance(row["size_bytes"], int)
            or isinstance(row["size_bytes"], bool)
            or row["size_bytes"] <= 0
            or row["media_type"] != expected_media_type
        ):
            raise ValidationError("fast report manifest artifact contract differs")
        validate_relative_path(row["path"], label=f"fast report {artifact_id} path")
    expected = sha256_json({key: value for key, value in document.items() if key != "manifest_commitment_sha256"})
    if document["manifest_commitment_sha256"] != expected:
        raise ValidationError("fast report manifest commitment differs")


def fast_report_input_commitments_from_projection(
    projection: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "bootstrap_sha256": projection["bootstrap_sha256"],
        "plan_sha256": projection["plan_sha256"],
        "index_sha256": projection["index_sha256"],
        "status_sha256": projection["status_sha256"],
        "diagnostic_study_sha256": projection["diagnostic_study"][
            "canonical_sha256"
        ],
        "audit_sha256s": {
            key: value["canonical_sha256"]
            for key, value in projection["audits"].items()
        },
        "study_sha256s": {
            key: None if value is None else value["canonical_sha256"]
            for key, value in projection["studies"].items()
        },
    }


def load_and_verify_fast_report_manifest(
    *,
    workspace_root: Path,
    manifest_path: Path,
    expected_input_commitments: Mapping[str, Any] | None = None,
    expected_projection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = _root(workspace_root)
    physical, _ = _existing(root, manifest_path, label="fast report manifest")
    document = read_json(physical)
    if not isinstance(document, dict):
        raise ValidationError("fast report manifest must be a JSON object")
    _validate_manifest(document)
    if expected_projection is not None:
        projection_commitments = fast_report_input_commitments_from_projection(
            expected_projection
        )
        if expected_input_commitments is not None and dict(
            expected_input_commitments
        ) != projection_commitments:
            raise ValidationError("fast report expected commitments disagree")
        expected_input_commitments = projection_commitments
        if document["ranking"] != expected_projection["ranking"]:
            raise ValidationError("fast report manifest ranking differs from projection")
    if expected_input_commitments is not None and document["input_commitments"] != dict(expected_input_commitments):
        raise ValidationError("fast report manifest binds another final projection")
    expected_payloads = (
        None if expected_projection is None else _report_payloads(expected_projection)
    )
    manifest_parent = physical.parent.relative_to(root).as_posix()
    for artifact_id, row in document["files"].items():
        if Path(row["path"]).parent.as_posix() != manifest_parent:
            raise ValidationError("fast report artifacts must be manifest siblings")
        artifact, _ = _existing(root, Path(row["path"]), label=f"fast report {artifact_id}")
        payload = artifact.read_bytes()
        if (
            sha256_bytes(payload) != row["physical_sha256"]
            or len(payload) != row["size_bytes"]
            or expected_payloads is not None
            and payload != expected_payloads[artifact_id]
        ):
            raise ValidationError("fast report artifact differs from its manifest")
        if artifact_id.endswith("chart"):
            try:
                import xml.etree.ElementTree as ET

                xml = ET.fromstring(payload)
            except Exception as exc:
                raise ValidationError("fast report chart is not parseable SVG") from exc
            if xml.tag != "{http://www.w3.org/2000/svg}svg":
                raise ValidationError("fast report chart root is not SVG")
    return dict(document)


__all__ = [
    "build_fast_report",
    "build_fast_report_projection",
    "fast_report_input_commitments_from_projection",
    "load_and_verify_fast_report_manifest",
]
