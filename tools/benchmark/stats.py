from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .errors import ValidationError
from .util import sha256_json


@dataclass(frozen=True)
class PairedCase:
    case_id: str
    family: str
    target: str
    weight: float
    baseline_value: float
    candidate_value: float
    source_group: str | None = None

    @property
    def speedup(self) -> float:
        return self.baseline_value / self.candidate_value

    @property
    def source_group_id(self) -> str:
        return self.source_group or self.case_id


@dataclass(frozen=True)
class Comparison:
    pairs: tuple[PairedCase, ...]
    correctness_failures: int
    censored_cases: int
    excluded_cases: int
    metric_unit: str

    @property
    def geometric_mean_speedup(self) -> float | None:
        """Official case-weighted GM (all case occurrences retain their weight)."""
        if not self.pairs:
            return None
        return case_geometric_mean(self.pairs)

    @property
    def source_group_geometric_mean_speedup(self) -> float | None:
        if not self.pairs:
            return None
        return source_group_geometric_mean(self.pairs)


def weighted_geometric_mean(values: Iterable[tuple[float, float]]) -> float:
    weighted_logs = 0.0
    total_weight = 0.0
    for value, weight in values:
        if not math.isfinite(value) or value <= 0:
            raise ValidationError("geometric mean values must be finite and greater than zero")
        if not math.isfinite(weight) or weight <= 0:
            raise ValidationError("geometric mean weights must be finite and greater than zero")
        weighted_logs += weight * math.log(value)
        total_weight += weight
    if total_weight == 0:
        raise ValidationError("geometric mean requires at least one observation")
    return math.exp(weighted_logs / total_weight)


def source_group_geometric_mean(pairs: Sequence[PairedCase]) -> float:
    if not pairs:
        raise ValidationError("source-group geometric mean requires observations")
    groups: dict[str, list[PairedCase]] = {}
    for pair in pairs:
        groups.setdefault(pair.source_group_id, []).append(pair)
    group_values = [
        weighted_geometric_mean((pair.speedup, pair.weight) for pair in group)
        for _, group in sorted(groups.items())
    ]
    return weighted_geometric_mean((value, 1.0) for value in group_values)


def case_geometric_mean(pairs: Sequence[PairedCase]) -> float:
    if not pairs:
        raise ValidationError("case geometric mean requires observations")
    return weighted_geometric_mean((pair.speedup, pair.weight) for pair in pairs)


def invert_pairs(pairs: Sequence[PairedCase]) -> tuple[PairedCase, ...]:
    """Reverse a normal baseline/candidate ratio for ablation contribution.

    Normal compiler comparison is baseline/candidate.  With FULL as the
    baseline and without-X as the candidate, existing optimization contribution
    is intentionally without-X/FULL, so the numeric pair direction is reversed.
    """

    return tuple(
        PairedCase(
            case_id=pair.case_id,
            family=pair.family,
            target=pair.target,
            weight=pair.weight,
            baseline_value=pair.candidate_value,
            candidate_value=pair.baseline_value,
            source_group=pair.source_group,
        )
        for pair in pairs
    )


def metric_spec(run: Mapping[str, Any], metric_id: str | None = None) -> Mapping[str, Any]:
    selected = metric_id or run["configuration"]["primary_metric_id"]
    for spec in run["configuration"]["metrics"]:
        if spec["metric_id"] == selected:
            return spec
    raise ValidationError(f"run does not declare metric: {selected}")


def case_metric(case: Mapping[str, Any], primary_metric_id: str) -> float | None:
    if case["status"] != "passed":
        return None
    values: list[float | None] = []
    for sample in case["samples"]:
        by_id = {item["metric_id"]: item["value"] for item in sample["measurements"]}
        values.append(by_id.get(primary_metric_id))
    if not values or any(value is None for value in values):
        raise ValidationError(f"passed case {case['case_id']} has no complete metric samples")
    numeric = [float(value) for value in values]
    if any(not math.isfinite(value) or value <= 0 for value in numeric):
        raise ValidationError(f"case {case['case_id']} contains an invalid metric")
    return float(statistics.median(numeric))


def run_case_metrics(run: Mapping[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    primary_metric_id = run["configuration"]["primary_metric_id"]
    for case in run["cases"]:
        value = case_metric(case, primary_metric_id)
        if value is not None:
            result[case["case_id"]] = value
    return result


def _validate_initial_to_derived_timeout(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any]
) -> None:
    left = baseline["configuration"]
    right = candidate["configuration"]
    if left["timeout_policy"] != "initial" or right["timeout_policy"] != "baseline_derived":
        raise ValidationError("ablation timeout protocols must match or use initial FULL to baseline-derived variant")
    if left["timeout_cap_seconds"] != 1800.0 or any(
        not math.isclose(case["effective_timeout_seconds"], 1800.0, rel_tol=0, abs_tol=1e-12)
        for case in baseline["cases"]
    ):
        raise ValidationError("FULL initial timeout evidence must use the 1800-second singleton bound")
    if (
        right["baseline_timeout_run_id"] != baseline["run_id"]
        or right["baseline_timeout_run_sha256"] != sha256_json(baseline)
        or right["timeout_minimum_seconds"] != 120.0
        or right["timeout_multiplier"] != 3.0
        or right["timeout_cap_seconds"] != 1800.0
    ):
        raise ValidationError("variant timeout provenance does not bind the exact FULL run and 120/3x/1800 protocol")
    baseline_cases = {case["case_id"]: case for case in baseline["cases"]}
    for case in candidate["cases"]:
        observation = baseline_cases[case["case_id"]]
        if observation["status"] == "timeout":
            expected = 1800.0
        elif observation["status"] == "passed" and observation["samples"]:
            durations = [float(sample["duration_ns"]) / 1_000_000_000 for sample in observation["samples"]]
            expected = min(1800.0, max(120.0, 3.0 * float(statistics.median(durations))))
        else:
            raise ValidationError("FULL timeout evidence is incomplete for a variant case")
        if not math.isclose(case["effective_timeout_seconds"], expected, rel_tol=0, abs_tol=1e-12):
            raise ValidationError(f"derived timeout differs from FULL evidence: {case['case_id']}")


def _ensure_comparable_runs(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    mode: str,
) -> None:
    if mode not in {"pipeline_ablation", "cross_toolchain"}:
        raise ValidationError(f"unknown comparison mode: {mode}")
    if baseline["suite_id"] != candidate["suite_id"]:
        raise ValidationError("cannot compare runs from different suites")
    if baseline["manifest_sha256"] != candidate["manifest_sha256"]:
        raise ValidationError("cannot compare runs from different manifest snapshots")
    for key in ("measurement_protocol_id", "measurement_protocol_sha256"):
        if baseline["provenance"][key] != candidate["provenance"][key]:
            raise ValidationError(f"cannot compare runs when measurement protocol changed: {key}")
    if mode == "pipeline_ablation":
        for key in ("repo_commit", "repo_dirty", "tracked_diff_sha256", "compiler_artifact_sha256"):
            if baseline["provenance"][key] != candidate["provenance"][key]:
                raise ValidationError(f"cannot attribute a pipeline difference when provenance changed: {key}")
    comparable_configuration = [
        "linker", "runner", "primary_metric_id", "metric_profile_id", "metrics", "compile_timeout_seconds",
        "compile_repetitions", "reuse_compile_cache", "compile_storage_contract", "link_timeout_seconds", "analyze_timeout_seconds",
        "repetitions", "max_workers", "keep_going", "retry_failures", "seed",
        "artifact_suffix", "binary_suffix", "wsl_distribution_sha256", "environment_label",
        "evidence_level", "metric_file_sha256", "analysis_file_sha256",
        "output_contract", "result_file_sha256", "consistency_fraction", "consistency_repetitions",
    ]
    if mode == "pipeline_ablation":
        comparable_configuration.extend(("compiler", "analyzer", "remarks_file_sha256", "tool_versions"))
    else:
        # Cross-toolchain comparison intentionally changes compiler identity and
        # version evidence, but never the runtime, metric, or correctness path.
        comparable_configuration.extend((
            "run_timeout_seconds", "timeout_policy", "baseline_timeout_run_sha256",
            "baseline_timeout_run_id", "timeout_minimum_seconds", "timeout_multiplier", "timeout_cap_seconds",
        ))
        left_analyzer = baseline["configuration"]["analyzer"]
        right_analyzer = candidate["configuration"]["analyzer"]
        if (left_analyzer is None) != (right_analyzer is None):
            raise ValidationError("cross-toolchain comparison requires the same analyzer metric protocol")
        if left_analyzer is not None and any(
            left_analyzer[key] != right_analyzer[key]
            for key in ("kind", "adapter", "executable", "environment_keys")
        ):
            raise ValidationError("cross-toolchain analyzer runtime identity differs")
        compiler_tokens = ("gcc", "clang", "accela", "compiler", "java", "javac")
        runtime_tools: dict[str, tuple[str, str | None, str]] = {}
        for side, record in (("baseline", baseline), ("candidate", candidate)):
            for item in record["configuration"]["tool_versions"]:
                name = item["tool"].lower()
                if any(token in name for token in compiler_tokens):
                    continue
                value = (item["actual"], item["official_expected"], item["comparison"])
                key = f"{side}:{item['tool']}"
                runtime_tools[key] = value
        left_runtime = {key.split(":", 1)[1]: value for key, value in runtime_tools.items() if key.startswith("baseline:")}
        right_runtime = {key.split(":", 1)[1]: value for key, value in runtime_tools.items() if key.startswith("candidate:")}
        if left_runtime != right_runtime:
            raise ValidationError("cross-toolchain QEMU/linker/analyzer version evidence differs")
    for key in comparable_configuration:
        if baseline["configuration"][key] != candidate["configuration"][key]:
            raise ValidationError(f"cannot attribute a difference when run configuration changed: {key}")
    if mode == "pipeline_ablation":
        left_timeout = baseline["configuration"]["timeout_policy"]
        right_timeout = candidate["configuration"]["timeout_policy"]
        timeout_fields = (
            "run_timeout_seconds", "timeout_policy", "baseline_timeout_run_sha256", "baseline_timeout_run_id",
            "timeout_minimum_seconds", "timeout_multiplier", "timeout_cap_seconds",
        )
        if left_timeout == right_timeout:
            for key in timeout_fields:
                if baseline["configuration"][key] != candidate["configuration"][key]:
                    raise ValidationError(f"cannot attribute a difference when timeout configuration changed: {key}")
        else:
            _validate_initial_to_derived_timeout(baseline, candidate)
    baseline_primary = baseline["configuration"]["primary_metric_id"]
    candidate_primary = candidate["configuration"]["primary_metric_id"]
    if baseline_primary != candidate_primary:
        raise ValidationError("cannot compare runs with different primary metrics")
    baseline_spec = metric_spec(baseline)
    candidate_spec = metric_spec(candidate)
    baseline_unit = baseline_spec["unit"]
    candidate_unit = candidate_spec["unit"]
    if baseline_unit != candidate_unit:
        raise ValidationError("cannot compare runs with different metric units")
    baseline_source = baseline_spec["source"]
    candidate_source = candidate_spec["source"]
    if baseline_source != candidate_source:
        raise ValidationError("cannot compare runs with different metric sources")
    baseline_cases = {case["case_id"]: case for case in baseline["cases"]}
    candidate_cases = {case["case_id"]: case for case in candidate["cases"]}
    if baseline_cases.keys() != candidate_cases.keys():
        raise ValidationError("cannot compare runs with different case sets")
    for case_id, left in baseline_cases.items():
        right = candidate_cases[case_id]
        immutable = (
            "family", "source_group", "target", "weight", "source_sha256",
            "input_sha256", "expected_output_sha256",
        )
        if any(left[field] != right[field] for field in immutable):
            raise ValidationError(f"case metadata differs between runs: {case_id}")


def compare_runs(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    mode: str = "pipeline_ablation",
) -> Comparison:
    _ensure_comparable_runs(baseline, candidate, mode=mode)
    candidate_by_id = {case["case_id"]: case for case in candidate["cases"]}
    pairs: list[PairedCase] = []
    correctness_failures = 0
    censored_cases = 0
    excluded_cases = 0
    for left in baseline["cases"]:
        right = candidate_by_id[left["case_id"]]
        if left["status"] == "timeout" or right["status"] == "timeout":
            censored_cases += 1
            continue
        if left["status"] != "passed" or right["status"] != "passed":
            excluded_cases += 1
            if right["status"] not in {"passed", "timeout", "pending", "cancelled"}:
                correctness_failures += 1
            continue
        primary_metric_id = baseline["configuration"]["primary_metric_id"]
        left_value = case_metric(left, primary_metric_id)
        right_value = case_metric(right, primary_metric_id)
        assert left_value is not None and right_value is not None
        pairs.append(
            PairedCase(
                case_id=left["case_id"],
                family=left["family"],
                target=left["target"],
                weight=float(left["weight"]),
                baseline_value=left_value,
                candidate_value=right_value,
                source_group=left["source_group"],
            )
        )
    return Comparison(
        tuple(pairs),
        correctness_failures,
        censored_cases,
        excluded_cases,
        metric_spec(baseline)["unit"],
    )


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        raise ValidationError("percentile requires observations")
    if not 0 <= fraction <= 1:
        raise ValidationError("percentile fraction must be between zero and one")
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def bootstrap_geometric_mean_ci(
    pairs: Sequence[PairedCase],
    *,
    samples: int,
    seed: int,
) -> tuple[float, float] | None:
    if not pairs:
        return None
    if samples < 100:
        raise ValidationError("bootstrap requires at least 100 resamples")
    generator = random.Random(seed)
    estimates: list[float] = []
    by_family: dict[str, list[PairedCase]] = {}
    for pair in pairs:
        by_family.setdefault(pair.family, []).append(pair)
    families = sorted(by_family)
    family_metrics = {family: case_geometric_mean(group) for family, group in by_family.items()}
    size = len(families)
    for _ in range(samples):
        selected = [family_metrics[families[generator.randrange(size)]] for _ in range(size)]
        estimates.append(weighted_geometric_mean((value, 1.0) for value in selected))
    return percentile(estimates, 0.025), percentile(estimates, 0.975)


def family_geometric_means(pairs: Sequence[PairedCase]) -> list[dict[str, Any]]:
    groups: dict[str, list[PairedCase]] = {}
    for pair in pairs:
        groups.setdefault(pair.family, []).append(pair)
    return [
        {
            "family": family,
            "comparable_cases": len(group),
            "geometric_mean_speedup": case_geometric_mean(group),
        }
        for family, group in sorted(groups.items())
    ]


def target_geometric_means(pairs: Sequence[PairedCase]) -> dict[str, float]:
    groups: dict[str, list[PairedCase]] = {}
    for pair in pairs:
        groups.setdefault(pair.target, []).append(pair)
    return {
        target: case_geometric_mean(group)
        for target, group in sorted(groups.items())
    }


def leave_one_family_out(pairs: Sequence[PairedCase]) -> list[dict[str, Any]]:
    if not pairs:
        return []
    metric_full = case_geometric_mean(pairs)
    families = sorted({pair.family for pair in pairs})
    result: list[dict[str, Any]] = []
    for family in families:
        remaining = [pair for pair in pairs if pair.family != family]
        metric_without = (
            case_geometric_mean(remaining)
            if remaining
            else None
        )
        result.append(
            {
                "family": family,
                "metric_without": metric_without,
                "metric_full": metric_full,
                "contribution_ratio": None if metric_without is None else metric_without / metric_full,
            }
        )
    return result
