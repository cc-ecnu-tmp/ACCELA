from __future__ import annotations

import csv
import io
import math
import re
import statistics
from itertools import combinations
from pathlib import Path
from typing import Any, Mapping, Sequence
from xml.sax.saxutils import escape as xml_escape

from .candidates import validate_candidate_final_completion
from .errors import ValidationError
from .ablation import _require_formal_measurement
from .campaign import _require_hotblock_evidence
from .schema import load_and_validate, load_and_validate_jsonl, validate_document
from .stats import (
    bootstrap_geometric_mean_ci,
    case_metric,
    compare_runs,
    family_geometric_means,
    target_geometric_means,
    weighted_geometric_mean,
    metric_spec,
)
from .util import (
    atomic_write_json,
    atomic_write_text,
    read_json,
    resolve_without_symlinks,
    sha256_file,
    sha256_json,
    utc_now,
)


def _load_version(path: Path, version: str) -> dict[str, Any]:
    document = load_and_validate(path)
    if document["schema_version"] != version:
        raise ValidationError(f"expected {version}, got {document['schema_version']}")
    return document


def _prepare_report_output_directory(path: Path, *, label: str) -> Path:
    """Create a report directory without following any lexical symlink."""

    lexical = path.absolute()
    cursor = Path(lexical.anchor)
    for component in lexical.parts[1:]:
        cursor = cursor / component
        if cursor.is_symlink():
            raise ValidationError(f"{label} must not traverse a symbolic link")
    try:
        lexical.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValidationError(f"{label} cannot be created") from exc
    resolved = resolve_without_symlinks(lexical, label=label)
    if not resolved.is_dir():
        raise ValidationError(f"{label} must be a directory")
    return resolved


def _format_number(value: float | None, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _format_ratio(value: float | None, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    return f"{_format_number(value, digits)}x"


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _optimization_rankings_by_suite(
    remarks: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for remark in remarks:
        key = (remark["data_role"], remark["suite_id"], remark["manifest_sha256"])
        group = grouped.setdefault(
            key,
            {
                "suite_id": remark["suite_id"],
                "data_role": remark["data_role"],
                "manifest_sha256": remark["manifest_sha256"],
                "study_ids": [],
                "ranking": [],
                "interactions": [],
            },
        )
        group["study_ids"].append(remark["study_id"])
        for variant in remark["variants"]:
            if any(
                row["optimization_id"] == variant["optimization_id"]
                for row in group["ranking"]
            ):
                raise ValidationError(
                    "ablation studies repeat an optimization within the same suite snapshot: "
                    f"{remark['data_role']}/{remark['suite_id']}/{variant['optimization_id']}"
                )
            group["ranking"].append(
                {
                    "study_id": remark["study_id"],
                    "suite_id": remark["suite_id"],
                    "data_role": remark["data_role"],
                    "manifest_sha256": remark["manifest_sha256"],
                    "baseline_run_id": remark["baseline_run_id"],
                    "variant_run_id": variant["run_id"],
                    "optimization_id": variant["optimization_id"],
                    "case_geometric_mean_contribution": variant["case_geometric_mean_contribution"],
                    "source_group_geometric_mean_contribution": variant["source_group_geometric_mean_contribution"],
                    "ci95": variant["confidence_interval_95"],
                    "comparable_cases": variant["comparable_cases"],
                    "comparable_source_groups": variant["comparable_source_groups"],
                    "correctness_failures": variant["correctness_failures"],
                    "censored_cases": variant["censored_cases"],
                    "excluded_cases": variant["excluded_cases"],
                    "eligible_for_ranking": variant["eligible_for_ranking"],
                    "ineligibility_reason": variant["ineligibility_reason"],
                    "per_cases": variant["per_cases"],
                    "families": variant["families"],
                    "leave_one_family_out": variant["leave_one_family_out"],
                }
            )
        group["interactions"].extend(
            {
                "study_id": remark["study_id"],
                "suite_id": remark["suite_id"],
                "data_role": remark["data_role"],
                "manifest_sha256": remark["manifest_sha256"],
                "baseline_run_id": remark["baseline_run_id"],
                **interaction,
            }
            for interaction in remark["interactions"]
        )

    role_order = {"B3": 0, "B2": 1, "B4": 2, "B6": 3, "B5": 4}
    groups = list(grouped.values())
    groups.sort(
        key=lambda item: (
            role_order.get(item["data_role"], 99),
            item["suite_id"],
            item["manifest_sha256"],
        )
    )
    for group in groups:
        group["study_ids"].sort()
        group["ranking"].sort(key=lambda row: (
            not row["eligible_for_ranking"],
            -(row["case_geometric_mean_contribution"] or -math.inf),
            row["optimization_id"],
        ))
        rank = 0
        for row in group["ranking"]:
            if row["eligible_for_ranking"]:
                rank += 1
                row["rank"] = rank
            else:
                row["rank"] = None
        interaction_keys = [
            tuple(sorted((item["left"], item["right"])))
            for item in group["interactions"]
        ]
        if len(interaction_keys) != len(set(interaction_keys)):
            raise ValidationError(
                "ablation studies repeat an interaction within the same suite snapshot: "
                f"{group['data_role']}/{group['suite_id']}"
            )
        group["interactions"].sort(
            key=lambda item: (item["left"], item["right"], item["study_id"])
        )
    return groups


def _primary_optimization_group(
    groups: Sequence[Mapping[str, Any]],
    run: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    official = [group for group in groups if group["data_role"] == "B3"]
    if len(official) > 1:
        identities = ", ".join(
            f"{group['suite_id']}@{group['manifest_sha256']}" for group in official
        )
        raise ValidationError(f"report cannot choose between multiple B3 suite snapshots: {identities}")
    if official:
        return official[0]
    run_roles = {case["data_role"] for case in run["cases"]}
    matching = [
        group for group in groups
        if group["suite_id"] == run["suite_id"]
        and group["manifest_sha256"] == run["manifest_sha256"]
        and group["data_role"] in run_roles
    ]
    return matching[0] if len(matching) == 1 else (groups[0] if groups else None)


def _implementation_priorities(
    evidence: Mapping[str, Any] | None,
    oracle_rows: Sequence[Mapping[str, Any]],
    *,
    oracle_plans: Mapping[str, Mapping[str, Any]],
    runs: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if evidence is None:
        return []
    oracle_by_family = {row["family"]: row for row in oracle_rows}
    analysis_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
    content_owners: dict[tuple[str, str, str, str | None, str], str] = {}

    def pair_signature(pair: Mapping[str, Any]) -> tuple[str, str, str, str | None, str]:
        return (
            pair["target"],
            pair["baseline"]["source_sha256"],
            pair["optimized"]["source_sha256"],
            pair["input_sha256"],
            pair["expected_output_sha256"],
        )

    def content_digest(signature: tuple[str, str, str, str | None, str]) -> str:
        target, baseline_source, optimized_source, input_hash, output_hash = signature
        return sha256_json(
            {
                "target": target,
                "baseline_source_sha256": baseline_source,
                "optimized_source_sha256": optimized_source,
                "input_sha256": input_hash,
                "expected_output_sha256": output_hash,
            }
        )

    def resolve(reference: Mapping[str, Any]) -> dict[str, Any]:
        key = (
            reference["plan_sha256"],
            reference["baseline_run_id"],
            reference["optimized_run_id"],
        )
        if key in analysis_cache:
            return analysis_cache[key]
        plan = oracle_plans.get(reference["plan_sha256"])
        if plan is None:
            raise ValidationError(
                "candidate evidence references an Oracle plan that was not supplied: "
                + reference["plan_sha256"]
            )
        if (
            plan["baseline_run_id"] != reference["baseline_run_id"]
            or plan["optimized_run_id"] != reference["optimized_run_id"]
        ):
            raise ValidationError("candidate evidence run ids differ from its referenced Oracle plan")
        baseline = runs.get(reference["baseline_run_id"])
        optimized = runs.get(reference["optimized_run_id"])
        if baseline is None or optimized is None:
            raise ValidationError("candidate evidence references a run record that was not supplied")
        analysis_cache[key] = _oracle_analysis(plan, baseline, optimized)
        return analysis_cache[key]

    priorities: list[dict[str, Any]] = []
    for candidate in evidence["candidates"]:
        candidate_content_keys: set[tuple[str, str, str, str | None, str]] = set()

        def claim_content(pair: Mapping[str, Any], *, selector: str) -> str:
            signature = pair_signature(pair)
            digest = content_digest(signature)
            if signature in candidate_content_keys:
                raise ValidationError(
                    "candidate Oracle evidence repeats workload content across plans/selectors: "
                    f"{candidate['candidate_id']} ({selector}, content={digest})"
                )
            owner = content_owners.get(signature)
            if owner is not None and owner != candidate["candidate_id"]:
                raise ValidationError(
                    "candidate Oracle workload content is assigned to multiple candidates: "
                    f"{owner}, {candidate['candidate_id']} (content={digest})"
                )
            candidate_content_keys.add(signature)
            content_owners[signature] = candidate["candidate_id"]
            return digest

        cleanroom = oracle_by_family.get(candidate["cleanroom_oracle_family_id"])
        cleanroom_speedup = None if cleanroom is None else cleanroom["geometric_mean_speedup"]
        official_details: list[dict[str, Any]] = []
        official_content_details: list[dict[str, Any]] = []
        official_pair_values: list[float] = []
        official_family_values: list[float] = []
        official_family_keys: set[str] = set()
        official_pair_keys: set[str] = set()
        official_complete = True
        for reference in candidate["official_oracle_refs"]:
            analysis = resolve(reference)
            plan = oracle_plans[reference["plan_sha256"]]
            if plan["evidence_class"] != "official":
                raise ValidationError("candidate official evidence must reference an official Oracle plan")
            plan_pairs = {pair["pair_id"]: pair for pair in plan["pairs"]}
            family_rows = {row["family"]: row for row in analysis["families"]}
            for family_id in reference["family_ids"]:
                family = family_rows.get(family_id)
                if family is None:
                    raise ValidationError(f"candidate official Oracle reference names unknown family: {family_id}")
                if family_id in official_family_keys:
                    raise ValidationError("candidate official Oracle evidence repeats a family across plans")
                official_family_keys.add(family_id)
                eligible = bool(family["eligible_for_ranking"])
                speedup = family["geometric_mean_speedup"] if eligible else None
                official_details.append(
                    {
                        "plan_sha256": reference["plan_sha256"],
                        "baseline_run_id": reference["baseline_run_id"],
                        "optimized_run_id": reference["optimized_run_id"],
                        "family_id": family_id,
                        "eligible": eligible,
                        "ineligibility_reason": family["ineligibility_reason"],
                        "geometric_mean_speedup": speedup,
                    }
                )
                family_pairs = [
                    row for row in analysis["pairs"] if row["family"] == family_id
                ]
                for pair in family_pairs:
                    if pair["pair_id"] in official_pair_keys:
                        raise ValidationError("candidate official Oracle evidence repeats a workload across plans")
                    official_pair_keys.add(pair["pair_id"])
                    descriptor = plan_pairs[pair["pair_id"]]
                    signature_digest = claim_content(
                        descriptor,
                        selector=f"official family {family_id}",
                    )
                    official_content_details.append(
                        {
                            "plan_sha256": reference["plan_sha256"],
                            "baseline_run_id": reference["baseline_run_id"],
                            "optimized_run_id": reference["optimized_run_id"],
                            "family_id": family_id,
                            "pair_id": pair["pair_id"],
                            "content_signature_sha256": signature_digest,
                            "eligible": pair["eligible_for_ranking"],
                            "ineligibility_reason": pair["ineligibility_reason"],
                            "speedup": pair["speedup"] if pair["eligible_for_ranking"] else None,
                        }
                    )
                if speedup is None:
                    official_complete = False
                    continue
                official_family_values.append(float(speedup))
                eligible_family_pairs = [
                    row for row in family_pairs if row["eligible_for_ranking"]
                ]
                if len(eligible_family_pairs) != family["paired_datasets"]:
                    official_complete = False
                    continue
                for pair in eligible_family_pairs:
                    official_pair_values.append(float(pair["speedup"]))

        official_gm = (
            weighted_geometric_mean((value, 1.0) for value in official_pair_values)
            if official_pair_values and official_complete
            else None
        )
        official_delta = None if official_gm is None else math.log(official_gm)
        maximum_official_family = max(official_family_values, default=None)

        holdout_details: list[dict[str, Any]] = []
        holdout_keys: set[str] = set()
        for reference in candidate["holdout_or_mature_refs"]:
            analysis = resolve(reference)
            plan = oracle_plans[reference["plan_sha256"]]
            if plan["evidence_class"] != "holdout_or_mature":
                raise ValidationError("candidate holdout evidence must reference a holdout_or_mature Oracle plan")
            pairs = {row["pair_id"]: row for row in analysis["pairs"]}
            plan_pairs = {pair["pair_id"]: pair for pair in plan["pairs"]}
            for pair_id in reference["pair_ids"]:
                pair = pairs.get(pair_id)
                if pair is None:
                    raise ValidationError(f"candidate holdout/mature reference names unknown pair: {pair_id}")
                if pair_id in holdout_keys:
                    raise ValidationError("candidate holdout/mature evidence repeats a workload across plans")
                holdout_keys.add(pair_id)
                signature_digest = claim_content(
                    plan_pairs[pair_id],
                    selector=f"holdout/mature pair {pair_id}",
                )
                holdout_details.append(
                    {
                        "plan_sha256": reference["plan_sha256"],
                        "baseline_run_id": reference["baseline_run_id"],
                        "optimized_run_id": reference["optimized_run_id"],
                        "pair_id": pair_id,
                        "content_signature_sha256": signature_digest,
                        "family_id": pair["family"],
                        "baseline_case_id": pair["baseline_case_id"],
                        "optimized_case_id": pair["optimized_case_id"],
                        "eligible": pair["eligible_for_ranking"],
                        "ineligibility_reason": pair["ineligibility_reason"],
                        "speedup": pair["speedup"] if pair["eligible_for_ranking"] else None,
                    }
                )
        official_hits = len({item["family_id"] for item in official_details if item["eligible"]})
        holdout_hits = len({
            item["content_signature_sha256"] for item in holdout_details if item["eligible"]
        })
        if candidate["specification_status"] != "clear":
            priority, reason = "Blocked", "specification_unclear"
        elif candidate["requires_boom_feature"]:
            priority, reason = "Blocked", "requires_unpublished_boom_feature"
        elif candidate["legality_proof_path"] != "clear":
            priority, reason = "P2", "legality_proof_path_unclear"
        elif candidate["risk"] in {"high", "unknown"}:
            priority, reason = "P2", "high_or_unknown_implementation_risk"
        elif official_gm is None:
            priority, reason = "P2", "official_oracle_evidence_missing_or_ineligible"
        elif holdout_hits == 0:
            priority, reason = "P2", "holdout_or_mature_evidence_missing_or_ineligible"
        elif official_gm >= 1.02 and official_hits >= 2 and holdout_hits >= 2:
            priority, reason = "P0", "official_oracle_ge_1.02_two_families_two_holdouts"
        elif (
            (1.005 <= official_gm <= 1.02 or (maximum_official_family or 0.0) >= 1.25)
            and holdout_hits >= 1
        ):
            priority, reason = "P1", "official_oracle_p1_threshold_with_holdout"
        else:
            priority, reason = "P2", "official_evidence_below_p0_p1_thresholds"
        priorities.append({
            "candidate_id": candidate["candidate_id"],
            "cleanroom_oracle_family_id": candidate["cleanroom_oracle_family_id"],
            "cleanroom_oracle_geometric_mean_upper_bound": cleanroom_speedup,
            "official_oracle_geometric_mean": official_gm,
            "official_delta_ln_geometric_mean": official_delta,
            "maximum_official_family_upper_bound": maximum_official_family,
            "official_family_hits": official_hits,
            "holdout_or_mature_hits": holdout_hits,
            "official_evidence": official_details,
            "official_content_evidence": official_content_details,
            "holdout_or_mature_evidence": holdout_details,
            "legality_proof_path": candidate["legality_proof_path"],
            "legality_obligation_ids": candidate["legality_obligation_ids"],
            "implementation_cost": candidate["implementation_cost"],
            "risk": candidate["risk"],
            "priority": priority,
            "priority_reason": reason,
            "rank": None,
        })
    order = {"P0": 0, "P1": 1, "P2": 2, "Blocked": 3}
    cost_order = {"low": 0, "medium": 1, "high": 2, "unknown": 3}
    risk_order = {"low": 0, "medium": 1, "high": 2, "unknown": 3}
    priorities.sort(key=lambda item: (
        order[item["priority"]],
        -(
            item["official_delta_ln_geometric_mean"]
            if item["official_delta_ln_geometric_mean"] is not None
            else -math.inf
        ),
        -item["holdout_or_mature_hits"],
        cost_order[item["implementation_cost"]],
        risk_order[item["risk"]],
        item["candidate_id"],
    ))
    rank = 0
    for item in priorities:
        if item["priority"] != "Blocked":
            rank += 1
            item["rank"] = rank
    return priorities


def _summarize_pass_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    for event in events:
        item = summaries.setdefault(
            event["pass"],
            {
                "pass": event["pass"],
                "pass_summaries": 0,
                "changed_invocations": 0,
                "elapsed_ns": 0,
                "decisions": {"candidate": 0, "applied": 0, "rejected": 0},
                "reasons": {},
                "delta_totals": {},
                "detail_totals": {},
            },
        )
        if event["event_type"] == "pass_summary":
            item["pass_summaries"] += 1
            item["changed_invocations"] += int(event["changed"])
            item["elapsed_ns"] += event["elapsed_ns"]
            for key, value in event["delta"].items():
                item["delta_totals"][key] = item["delta_totals"].get(key, 0) + value
            for key, value in event["details"].items():
                item["detail_totals"][key] = item["detail_totals"].get(key, 0) + value
        else:
            item["decisions"][event["decision"]] += 1
            reason = event["reason"]
            item["reasons"][reason] = item["reasons"].get(reason, 0) + 1
    return [summaries[key] for key in sorted(summaries)]


def _case_measurement_values(case: Mapping[str, Any]) -> dict[str, float]:
    values: dict[str, list[float]] = {}
    for measurement in case["measurements"]:
        if measurement["availability"] == "measured":
            values.setdefault(measurement["metric_id"], []).append(float(measurement["value"]))
    for sample in case["samples"]:
        for measurement in sample["measurements"]:
            if measurement["availability"] == "measured":
                values.setdefault(measurement["metric_id"], []).append(float(measurement["value"]))
    return {metric_id: float(statistics.median(items)) for metric_id, items in values.items()}


def _integer_sample_measurement(
    sample: Mapping[str, Any], metric_id: str, *, strictly_positive: bool = False
) -> int:
    matches = [
        item for item in sample["measurements"] if item["metric_id"] == metric_id
    ]
    if len(matches) != 1:
        raise ValidationError(
            f"cache-hotblock passed sample must contain exactly one {metric_id} measurement"
        )
    measurement = matches[0]
    value = measurement["value"]
    if (
        measurement["availability"] != "measured"
        or measurement["origin"] != "observed"
        or value is None
    ):
        raise ValidationError(
            f"cache-hotblock passed sample lacks observed measured evidence: {metric_id}"
        )
    numeric = float(value)
    if (
        not math.isfinite(numeric)
        or numeric < 0
        or (strictly_positive and numeric <= 0)
        or not numeric.is_integer()
    ):
        qualifier = "positive integer" if strictly_positive else "non-negative integer"
        raise ValidationError(f"cache-hotblock {metric_id} must be a {qualifier}")
    return int(numeric)


def _hotblock_diagnostics(
    runs: Mapping[str, Mapping[str, Any]],
    *,
    allow_terminal_failures: bool = False,
) -> dict[str, Any]:
    run_rows: list[dict[str, Any]] = []
    sample_rows: list[dict[str, Any]] = []
    seen_run_ids: set[str] = set()
    for label, run in sorted(runs.items()):
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,255}", label) is None:
            raise ValidationError(
                "cache-hotblock label must be a portable logical identifier"
            )
        run_id = run["run_id"]
        if run_id in seen_run_ids:
            raise ValidationError(f"cache-hotblock diagnostics repeat run_id: {run_id}")
        seen_run_ids.add(run_id)
        state = run["state"]
        if state not in {"completed", "failed", "interrupted"}:
            raise ValidationError(
                "cache-hotblock diagnostics require terminal run evidence"
                if allow_terminal_failures
                else "cache-hotblock diagnostics require a completed run record"
            )
        if state == "completed" and run["summary"]["pending_cases"] != 0:
            raise ValidationError(
                "completed cache-hotblock diagnostic retains pending cases"
            )
        if state != "completed" and not allow_terminal_failures:
            raise ValidationError("cache-hotblock diagnostics require a completed run record")
        if state == "completed":
            _require_formal_measurement(
                run, require_accela_pipeline=True, allow_metric_superset=True
            )
            _require_hotblock_evidence(run)
        provenance = run["provenance"]
        run_row = {
            "label": label,
            "run_id": run_id,
                "suite_id": run["suite_id"],
                "manifest_sha256": run["manifest_sha256"],
                "repo_commit": provenance["repo_commit"],
                "pipeline_profile_id": provenance["pipeline_profile_id"],
                "pipeline_profile_sha256": provenance["pipeline_profile_sha256"],
                "measurement_protocol_id": provenance["measurement_protocol_id"],
                "measurement_protocol_sha256": provenance[
                    "measurement_protocol_sha256"
                ],
                "total_cases": run["summary"]["total_cases"],
                "passed_cases": run["summary"]["passed_cases"],
                "failed_cases": run["summary"]["failed_cases"],
                "censored_cases": run["summary"]["censored_cases"],
        }
        if allow_terminal_failures:
            run_row.update(
                {
                    "state": state,
                    "failure_classification": (
                        None if state == "completed" else f"run_{state}"
                    ),
                }
            )
        run_rows.append(run_row)
        if state != "completed":
            continue
        for case in run["cases"]:
            if case["status"] != "passed":
                continue
            if not case["samples"]:
                raise ValidationError("cache-hotblock passed case lacks runtime samples")
            for sample in case["samples"]:
                if sample["status"] != "passed":
                    raise ValidationError(
                        "cache-hotblock passed case contains a non-passed sample"
                    )
                dynamic_total = _integer_sample_measurement(
                    sample, "dynamic_instruction_count", strictly_positive=True
                )
                hottest_address = _integer_sample_measurement(
                    sample, "hotblock_hottest_address"
                )
                hottest_executions = _integer_sample_measurement(
                    sample, "hotblock_hottest_executions"
                )
                hottest_dynamic = _integer_sample_measurement(
                    sample, "hotblock_hottest_dynamic_instructions"
                )
                dynamic_loads = _integer_sample_measurement(
                    sample, "dynamic_load_count"
                )
                l1d_misses = _integer_sample_measurement(sample, "l1d_miss_count")
                if hottest_dynamic > dynamic_total:
                    raise ValidationError(
                        "cache-hotblock hottest dynamic instructions exceed the total dynamic instruction count"
                    )
                sample_row = {
                        "label": label,
                        "run_id": run_id,
                        "suite_id": run["suite_id"],
                        "manifest_sha256": run["manifest_sha256"],
                        "data_role": case["data_role"],
                        "pipeline_profile_id": provenance["pipeline_profile_id"],
                        "pipeline_profile_sha256": provenance[
                            "pipeline_profile_sha256"
                        ],
                        "measurement_protocol_id": provenance[
                            "measurement_protocol_id"
                        ],
                        "measurement_protocol_sha256": provenance[
                            "measurement_protocol_sha256"
                        ],
                        "case_id": case["case_id"],
                        "family": case["family"],
                        "source_group": case["source_group"],
                        "target": case["target"],
                        "sample_index": sample["index"],
                        "dynamic_instruction_count": dynamic_total,
                        "hotblock_hottest_address": hottest_address,
                        "hotblock_hottest_address_hex": f"0x{hottest_address:x}",
                        "hotblock_hottest_executions": hottest_executions,
                        "hotblock_hottest_dynamic_instructions": hottest_dynamic,
                        "hotblock_dynamic_instruction_share": (
                            hottest_dynamic / dynamic_total
                        ),
                }
                if allow_terminal_failures:
                    sample_row.update(
                        {
                            "dynamic_load_count": dynamic_loads,
                            "l1d_miss_count": l1d_misses,
                            "l1d_misses_per_1000_dynamic_loads": (
                                None
                                if dynamic_loads == 0
                                else 1000.0 * l1d_misses / dynamic_loads
                            ),
                        }
                    )
                sample_rows.append(sample_row)
    sample_rows.sort(
        key=lambda row: (
            row["suite_id"],
            row["pipeline_profile_id"],
            row["case_id"],
            row["sample_index"],
            row["run_id"],
        )
    )
    return {
        "participates_in_rankings": False,
        "purpose": "diagnostic_only",
        "run_count": len(run_rows),
        "sample_count": len(sample_rows),
        "runs": run_rows,
        "samples": sample_rows,
    }


def _svg_bars(title: str, rows: Sequence[tuple[str, float]], *, unit: str) -> str:
    width = 960
    left = 260
    right = 50
    row_height = 34
    top = 72
    height = max(150, top + row_height * max(1, len(rows)) + 48)
    positive = [(label, value) for label, value in rows if math.isfinite(value) and value > 0]
    maximum = max((value for _, value in positive), default=1.0)
    plot_width = width - left - right
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>text{font-family:system-ui,sans-serif;fill:#172033}.title{font-size:20px;font-weight:700}.label{font-size:13px}.value{font-size:12px}.bar{fill:#3977d4}.grid{stroke:#d8deea;stroke-width:1}</style>",
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text class="title" x="24" y="34">{xml_escape(title)}</text>',
        f'<text class="value" x="24" y="55">单位：{xml_escape(unit)}；删失或无效用例不插补。</text>',
    ]
    if not positive:
        elements.append('<text class="label" x="24" y="100">无可比较的未删失数据。</text>')
    for index, (label, value) in enumerate(positive):
        y = top + index * row_height
        bar_width = max(1.0, plot_width * value / maximum)
        elements.extend(
            [
                f'<text class="label" x="24" y="{y + 18}">{xml_escape(label[:42])}</text>',
                f'<line class="grid" x1="{left}" y1="{y + 24}" x2="{left + plot_width}" y2="{y + 24}"/>',
                f'<rect class="bar" x="{left}" y="{y + 3}" width="{bar_width:.2f}" height="18" rx="3"/>',
                f'<text class="value" x="{min(left + bar_width + 6, width - 90):.2f}" y="{y + 17}">{value:.4g}</text>',
            ]
        )
    elements.append("</svg>")
    return "\n".join(elements) + "\n"


def _svg_ratio_diverging(
    title: str,
    rows: Sequence[tuple[str, float]],
    *,
    evidence_note: str,
) -> str:
    width, left, right, top, row_height = 1120, 300, 160, 88, 36
    height = max(170, top + row_height * max(1, len(rows)) + 54)
    valid = [(label, value) for label, value in rows if math.isfinite(value) and value > 0]
    deltas = [(label, value, 100.0 * math.log(value)) for label, value in valid]
    extent = max((abs(delta) for _, _, delta in deltas), default=1.0)
    extent = max(extent, 0.5)
    center = left + (width - left - right) / 2
    half = (width - left - right) / 2
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fff"/>',
        '<style>text{font-family:system-ui,sans-serif;fill:#172033}.t{font-size:20px;font-weight:700}.l{font-size:12px}.n{font-size:11px}.axis{stroke:#596579;stroke-width:1.4}.grid{stroke:#dce2ec}</style>',
        f'<text class="t" x="24" y="32">{xml_escape(title)}</text>',
        f'<text class="n" x="24" y="54">{xml_escape(evidence_note)}</text>',
        '<text class="n" x="24" y="70">横轴：100 × ln(比值)，0 为 1.0 基准；标签同时给出精确比值。</text>',
        f'<line class="axis" x1="{center:.2f}" y1="{top - 8}" x2="{center:.2f}" y2="{height - 34}"/>',
        f'<text class="n" x="{center - 16:.2f}" y="{height - 12}">0</text>',
    ]
    if not deltas:
        elements.append('<text class="l" x="24" y="118">无可比较的未删失数据／尚未调度。</text>')
    for index, (label, ratio, delta) in enumerate(deltas):
        y = top + index * row_height
        length = half * abs(delta) / extent
        x = center if delta >= 0 else center - length
        color = "#2d72d2" if delta >= 0 else "#e3862b"
        elements.extend([
            f'<text class="l" x="24" y="{y + 18}">{xml_escape(label)}</text>',
            f'<line class="grid" x1="{left}" y1="{y + 25}" x2="{width - right}" y2="{y + 25}"/>',
            f'<rect x="{x:.2f}" y="{y + 3}" width="{max(1.0, length):.2f}" height="20" rx="2" fill="{color}"/>',
            f'<text class="n" x="{width - 24}" y="{y + 18}" text-anchor="end">{ratio:.4f}x ({delta:+.2f})</text>',
        ])
    elements.append("</svg>")
    return "\n".join(elements) + "\n"


def _svg_heatmap(
    title: str,
    rows: Sequence[str],
    columns: Sequence[str],
    values: Mapping[tuple[str, str], float],
) -> str:
    column_width = max(112, min(240, 8 * max((len(item) for item in columns), default=12) + 18))
    width = max(720, 220 + column_width * max(1, len(columns)))
    height = max(200, 112 + 28 * max(1, len(rows)))
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fff"/>',
        '<style>text{font-family:system-ui,sans-serif;fill:#172033}.t{font-size:20px;font-weight:700}.l{font-size:11px}</style>',
        f'<text class="t" x="20" y="30">{xml_escape(title)}</text>',
        '<rect x="20" y="46" width="18" height="12" fill="#e3862b"/><text class="l" x="44" y="57">负贡献</text>',
        '<rect x="112" y="46" width="18" height="12" fill="#f8f8f8"/><text class="l" x="136" y="57">0%</text>',
        '<rect x="178" y="46" width="18" height="12" fill="#2d72d2"/><text class="l" x="202" y="57">正贡献；单元格=100×ln(比值)</text>',
    ]
    if not rows or not columns or not values:
        elements.append('<text class="l" x="20" y="72">无数据／尚未调度。</text>')
    else:
        for column_index, column in enumerate(columns):
            elements.append(
                f'<text class="l" x="{204 + column_index * column_width}" y="82">{xml_escape(column)}</text>'
            )
        for row_index, row in enumerate(rows):
            y = 92 + row_index * 28
            elements.append(f'<text class="l" x="20" y="{y + 18}">{xml_escape(row[:28])}</text>')
            for column_index, column in enumerate(columns):
                value = values.get((row, column))
                x = 200 + column_index * column_width
                if value is None:
                    color, label = "#edf0f5", "n/a"
                else:
                    magnitude = min(1.0, abs(math.log(value)) / math.log(1.5))
                    base = (45, 114, 210) if value >= 1 else (230, 134, 43)
                    color = "rgb(" + ",".join(str(int(248 + (channel - 248) * magnitude)) for channel in base) + ")"
                    label = f"{100 * math.log(value):+.1f}"
                elements.append(f'<rect x="{x}" y="{y}" width="{column_width - 6}" height="23" fill="{color}" rx="2"/>')
                elements.append(f'<text class="l" x="{x + 5}" y="{y + 16}">{label}</text>')
    elements.append("</svg>")
    return "\n".join(elements) + "\n"


def _svg_cache_hotblock(
    title: str,
    rows: Sequence[tuple[str, float | None, float | None]],
) -> str:
    """Render two explicitly unit-separated diagnostic panels."""

    width, height = 1040, max(260, 116 + 42 * max(1, len(rows)))
    left, panel_width = 260, 320
    valid_cache = [value for _, value, _ in rows if value is not None]
    valid_hot = [value for _, _, value in rows if value is not None]
    cache_max = max(valid_cache, default=1.0) or 1.0
    hot_max = max(valid_hot, default=1.0) or 1.0
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fff"/>',
        '<style>text{font-family:system-ui,sans-serif;fill:#172033}.t{font-size:20px;font-weight:700}.l{font-size:11px}.h{font-size:12px;font-weight:600}.g{stroke:#dce2ec}</style>',
        f'<text class="t" x="24" y="32">{xml_escape(title)}</text>',
        '<text class="h" x="260" y="62">L1D misses / 1000 dynamic loads</text>',
        '<text class="h" x="650" y="62">最热基本块动态指令占比（%）</text>',
    ]
    if not rows:
        elements.append('<text class="l" x="24" y="96">无完成的 cache/hotblock 诊断记录。</text>')
    for index, (label, cache_rate, hot_share) in enumerate(rows):
        y = 82 + index * 42
        elements.extend(
            [
                f'<text class="l" x="24" y="{y + 18}">{xml_escape(label[:42])}</text>',
                f'<line class="g" x1="{left}" y1="{y + 25}" x2="{width - 32}" y2="{y + 25}"/>',
            ]
        )
        if cache_rate is None:
            elements.append(f'<text class="l" x="{left}" y="{y + 18}">n/a</text>')
        else:
            bar = panel_width * cache_rate / cache_max
            elements.extend(
                [
                    f'<rect x="{left}" y="{y + 3}" width="{max(1.0, bar):.2f}" height="18" rx="2" fill="#3977d4"/>',
                    f'<text class="l" x="{left + panel_width + 8}" y="{y + 18}">{cache_rate:.4f}</text>',
                ]
            )
        hot_left = 650
        if hot_share is None:
            elements.append(f'<text class="l" x="{hot_left}" y="{y + 18}">n/a</text>')
        else:
            bar = panel_width * hot_share / hot_max
            elements.extend(
                [
                    f'<rect x="{hot_left}" y="{y + 3}" width="{max(1.0, bar):.2f}" height="18" rx="2" fill="#e3862b"/>',
                    f'<text class="l" x="{hot_left + panel_width + 8}" y="{y + 18}">{hot_share:.4f}</text>',
                ]
            )
    elements.extend(
        [
            f'<text class="l" x="24" y="{height - 22}">两面板单位不同，不共享数轴；均值仅覆盖完成且未删失的样本。</text>',
            "</svg>",
        ]
    )
    return "\n".join(elements) + "\n"


def _svg_pareto(
    title: str,
    rows: Sequence[tuple[str, float, float, str]],
) -> str:
    width, height = 900, 520
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fff"/>',
        '<style>text{font-family:system-ui,sans-serif;fill:#172033}.t{font-size:20px;font-weight:700}.l{font-size:11px}.a{stroke:#6d778c}</style>',
        f'<text class="t" x="20" y="30">{xml_escape(title)}</text>',
        '<circle cx="500" cy="48" r="7" fill="#2b8a3e"/><text class="l" x="512" y="52">low</text>',
        '<circle cx="570" cy="48" r="7" fill="#3977d4"/><text class="l" x="582" y="52">medium</text>',
        '<circle cx="666" cy="48" r="7" fill="#d94841"/><text class="l" x="678" y="52">high</text>',
        '<circle cx="744" cy="48" r="7" fill="#7a8496"/><text class="l" x="756" y="52">unknown（颜色编码风险）</text>',
    ]
    valid = [
        row
        for row in rows
        if math.isfinite(row[1])
        and row[1] > 0
        and math.isfinite(row[2])
        and row[2] > 0
        and row[3] in {"low", "medium", "high", "unknown"}
    ]
    if not valid:
        elements.append('<text class="l" x="20" y="72">无可联合展示的收益、静态 text bytes 与风险证据。</text>')
    else:
        left, top, plot_w, plot_h = 80, 70, 760, 380
        min_size = min(row[1] for row in valid)
        max_size = max(row[1] for row in valid)
        min_benefit = min(row[2] for row in valid)
        max_benefit = max(row[2] for row in valid)
        size_span = max_size - min_size or 1.0
        benefit_span = max_benefit - min_benefit or 1.0
        elements.extend([
            f'<line class="a" x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}"/>',
            f'<line class="a" x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}"/>',
            f'<text class="l" x="{left + plot_w - 185}" y="{top + plot_h + 30}">B3-B6 静态 text bytes 合计（越小越好）</text>',
            f'<text class="l" x="{left}" y="{top - 10}">267-case GM（越高越好）</text>',
            f'<text class="l" x="{left}" y="{top + plot_h + 16}">{min_size:.0f}</text>',
            f'<text class="l" x="{left + plot_w - 72}" y="{top + plot_h + 16}">{max_size:.0f} bytes</text>',
            f'<text class="l" x="{left + 5}" y="{top + 14}">{max_benefit:.4f}x</text>',
            f'<text class="l" x="{left + 5}" y="{top + plot_h - 6}">{min_benefit:.4f}x</text>',
        ])
        colors = {
            "low": "#2b8a3e",
            "medium": "#3977d4",
            "high": "#d94841",
            "unknown": "#7a8496",
        }
        for label, code_size, benefit, risk in valid:
            x = left + plot_w * (code_size - min_size) / size_span
            y = top + plot_h * (1 - (benefit - min_benefit) / benefit_span)
            radius = 7.0
            elements.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{radius:.2f}" fill="{colors[risk]}" fill-opacity="0.82"/>')
            text_anchor = "end" if x > left + 0.72 * plot_w else "start"
            text_x = x - radius - 3 if text_anchor == "end" else x + radius + 3
            elements.append(
                f'<text class="l" x="{text_x:.2f}" y="{y + 4:.2f}" text-anchor="{text_anchor}">'
                f'{xml_escape(label[:24])} ({code_size:.0f} B, {benefit:.4f}x)</text>'
            )
    elements.append("</svg>")
    return "\n".join(elements) + "\n"


def _oracle_analysis(
    plan: Mapping[str, Any],
    baseline: Mapping[str, Any],
    optimized: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    if baseline["run_id"] != plan["baseline_run_id"] or optimized["run_id"] != plan["optimized_run_id"]:
        raise ValidationError("oracle run identities do not match oracle-plan.v1")
    for record in (baseline, optimized):
        _require_formal_measurement(record, require_accela_pipeline=True)
        profile = plan["pipeline_profile"]
        if (
            record["provenance"]["pipeline_profile_id"] != profile["profile_id"]
            or record["provenance"]["pipeline_profile_sha256"] != profile["profile_sha256"]
        ):
            raise ValidationError("oracle run pipeline provenance differs from the paired plan")
        if record["configuration"]["evidence_level"] not in {"qemu_proxy", "boom_hardware"}:
            raise ValidationError("oracle ranking requires qemu_proxy or boom_hardware evidence")
    if baseline["configuration"] != optimized["configuration"]:
        raise ValidationError("oracle source legs must use an identical compiler/runtime/metric configuration")
    for key in ("repo_commit", "repo_dirty", "tracked_diff_sha256", "compiler_artifact_sha256"):
        if baseline["provenance"][key] != optimized["provenance"][key]:
            raise ValidationError(f"oracle source legs differ in provenance: {key}")

    left_cases = {case["case_id"]: case for case in baseline["cases"]}
    right_cases = {case["case_id"]: case for case in optimized["cases"]}
    primary = optimized["configuration"]["primary_metric_id"]
    pair_rows: list[dict[str, Any]] = []
    for pair in plan["pairs"]:
        left = left_cases.get(pair["baseline"]["case_id"])
        right = right_cases.get(pair["optimized"]["case_id"])
        reason: str | None = None
        if left is None:
            reason = "baseline_case_missing"
        elif right is None:
            reason = "optimized_case_missing"
        else:
            for case, descriptor, leg in (
                (left, pair["baseline"], "baseline"),
                (right, pair["optimized"], "optimized"),
            ):
                immutable = {
                    "family": pair["family"],
                    "target": pair["target"],
                    "data_role": plan["manifest_data_role"],
                    "source_group": descriptor["source_group"],
                    "source_sha256": descriptor["source_sha256"],
                    "input_sha256": pair["input_sha256"],
                    "expected_output_sha256": pair["expected_output_sha256"],
                }
                if any(case[key] != value for key, value in immutable.items()):
                    raise ValidationError(f"oracle {leg} case content differs from plan: {pair['pair_id']}")
                pairing = case.get("oracle_pair")
                if pairing is None or pairing["pair_id"] != pair["pair_id"] or pairing["leg"] != leg:
                    raise ValidationError(f"oracle {leg} case pairing metadata differs from plan: {pair['pair_id']}")
            statuses = {left["status"], right["status"]}
            if "timeout" in statuses:
                reason = "right_censored"
            elif statuses & {"pending", "cancelled"}:
                reason = "incomplete_run"
            elif statuses != {"passed"}:
                reason = "correctness_failure"
        left_value = case_metric(left, primary) if left is not None and left["status"] == "passed" else None
        right_value = case_metric(right, primary) if right is not None and right["status"] == "passed" else None
        speedup = left_value / right_value if reason is None and left_value is not None and right_value is not None else None
        tags = [] if right is None else right["tags"]
        tiers = [tag.split(":", 1)[1] for tag in tags if tag.startswith("tier:")]
        pair_rows.append(
            {
                "pair_id": pair["pair_id"],
                "family": pair["family"],
                "target": pair["target"],
                "tier": tiers[0] if len(tiers) == 1 else None,
                "baseline_run_id": baseline["run_id"],
                "optimized_run_id": optimized["run_id"],
                "baseline_case_id": pair["baseline"]["case_id"],
                "optimized_case_id": pair["optimized"]["case_id"],
                "eligible_for_ranking": reason is None,
                "ineligibility_reason": reason,
                "baseline_metric_value": left_value,
                "optimized_metric_value": right_value,
                "speedup": speedup,
            }
        )

    family_rows: list[dict[str, Any]] = []
    for family in sorted({row["family"] for row in pair_rows}):
        group = [row for row in pair_rows if row["family"] == family]
        reasons = sorted({row["ineligibility_reason"] for row in group if row["ineligibility_reason"] is not None})
        eligible = not reasons and bool(group)
        family_rows.append(
            {
                "family": family,
                "paired_datasets": len(group),
                "eligible_for_ranking": eligible,
                "ineligibility_reason": None if eligible else ",".join(reasons) or "no_pairs",
                "geometric_mean_speedup": (
                    weighted_geometric_mean((float(row["speedup"]), 1.0) for row in group)
                    if eligible else None
                ),
                "baseline_run_id": baseline["run_id"],
                "optimized_run_id": optimized["run_id"],
                "rank": None,
            }
        )
    family_rows.sort(key=lambda row: (not row["eligible_for_ranking"], -(row["geometric_mean_speedup"] or -math.inf), row["family"]))
    rank = 0
    for row in family_rows:
        if row["eligible_for_ranking"]:
            rank += 1
            row["rank"] = rank
    return {"pairs": pair_rows, "families": family_rows}


_CANDIDATE_SCREENING_FAMILIES: tuple[tuple[str, str, str], ...] = (
    (
        "bitset",
        "标量／向量位压缩",
        "本轮 Blocked；不得以未冻结的位集合同进入实现队列。",
    ),
    (
        "boom_ilp",
        "整数 runtime partial unroll 与 reduction expansion",
        "排除浮点重排；independent chains 归入 fusion，不重复申报。",
    ),
    (
        "closed_form",
        "扩展现有 SCEV／Affine 递推总结",
        "必须复用现有递推分析，禁止另建递推框架。",
    ),
    (
        "dp_storage",
        "有界依赖的滚动存储与临时表收缩",
        "必须统一证明依赖距离、索引边界与覆盖关系。",
    ),
    (
        "finite_state",
        "小常量闭合状态域的 jump table／binary lifting",
        "必须证明状态闭包、成本，以及零次／负次数语义。",
    ),
    (
        "fusion",
        "严格同域 loop fusion／temporary contraction",
        "优先复用 dependence、alias 与 IV 分析。",
    ),
    (
        "linear_transition",
        "小维度整数 affine state transition",
        "保持精确 i32 模语义，禁止浮点重排。",
    ),
    (
        "memoization",
        "现有 RRT 未覆盖的 RRT2：复合 rank、可达域、按需 memo",
        "不得重复申报普通 Fibonacci tabulation。",
    ),
    (
        "prefix_scan",
        "重复前缀聚合的增量化复用",
        "必须证明无别名、无副作用与空域语义。",
    ),
    (
        "recursion_worklist",
        "混杂 tail／divide-conquer／DFS lowering",
        "家族没有统一 matcher／transform／proof，整族拒绝且不拆票。",
    ),
    (
        "structured_kernel",
        "GEMM、stencil、transpose 等混杂变换",
        "家族没有统一 matcher／transform／proof，整族拒绝且不拆票。",
    ),
)

_CANDIDATE_SCREENING_REJECTION_TEXT = {
    "duplicate_candidate": "与另一候选重复",
    "overlaps_existing_pipeline": "与现有流水线能力重复",
    "no_complete_oracle_structure": "没有 small／medium／large 全部完整的 Oracle 结构",
    "oracle_structure_below_1_10": "允许结构的 Oracle GM 未达到 1.10",
    "unclear_legality": "合法性证明路径不清晰",
    "unclear_specification": "候选规格不清晰",
    "unsupported_ir_or_backend": "当前 IR 或后端不支持所需能力",
    "requires_unavailable_boom_feature": "依赖当前不可用的 BOOM 特性",
    "blocked_locked_bitset_capability_gap": "锁定的 bitset 能力存在缺口，本轮冻结为 Blocked",
    "mixed_family_no_unified_transform": "混杂家族无法形成统一 matcher／transform／proof",
}


def _screening_ids(values: Sequence[str]) -> str:
    if not values:
        return "无"
    return "、".join(f"`{_markdown_cell(value)}`" for value in values)


def _screening_oracle_refs(values: Sequence[Mapping[str, Any]]) -> str:
    if not values:
        return "无"
    return "、".join(
        "`"
        + _markdown_cell(
            f"{value['oracle_family_id']}/{value['structure_id']}"
        )
        + "`"
        for value in values
    )


def _screening_rejections(values: Sequence[str]) -> str:
    if not values:
        return "无"
    unknown = sorted(set(values) - set(_CANDIDATE_SCREENING_REJECTION_TEXT))
    if unknown:
        raise ValidationError(
            "candidate screening report encountered unknown rejection reasons: "
            + ", ".join(unknown)
        )
    return "；".join(
        f"`{_markdown_cell(value)}`（{_CANDIDATE_SCREENING_REJECTION_TEXT[value]}）"
        for value in values
    )


def _screening_tier_cell(tier: Mapping[str, Any]) -> str:
    if tier["eligible_for_ranking"]:
        speedup = tier["speedup"]
        if speedup is None:
            raise ValidationError("complete candidate Oracle tier lacks a speedup")
        return f"完整；{_format_number(float(speedup))}x"
    reason = tier["ineligibility_reason"]
    if reason is None:
        raise ValidationError("incomplete candidate Oracle tier lacks an exact reason")
    return f"不完整；`{_markdown_cell(reason)}`"


_SCREENING_QUEUE_ORDINAL = {"low": 0, "medium": 1, "high": 2, "unknown": 3}


def _candidate_implementation_queue(
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return the qualified-only Oracle queue; this is not a Pass ranking."""

    queue: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate["qualification_status"] != "qualified":
            continue
        implementation_id = candidate["implementation_candidate_id"]
        if implementation_id is None:
            raise ValidationError(
                "qualified candidate lacks an implementation candidate id"
            )
        complete_allowed = [
            float(structure["geometric_mean_speedup"])
            for structure in candidate["oracle_structures"]
            if structure["eligible_for_candidate_screening"]
            and structure["eligible_for_ranking"]
            and structure["geometric_mean_speedup"] is not None
        ]
        if not complete_allowed:
            raise ValidationError(
                "qualified candidate lacks a complete allowed Oracle structure"
            )
        risk = candidate["risk"]
        implementation_cost = candidate["implementation_cost"]
        if risk not in _SCREENING_QUEUE_ORDINAL or (
            implementation_cost not in _SCREENING_QUEUE_ORDINAL
        ):
            raise ValidationError(
                "candidate implementation queue encountered an unknown risk or cost"
            )
        queue.append(
            {
                "candidate_id": candidate["candidate_id"],
                "implementation_candidate_id": implementation_id,
                "best_complete_allowed_oracle_geometric_mean": max(
                    complete_allowed
                ),
                "risk": risk,
                "implementation_cost": implementation_cost,
            }
        )
    queue.sort(
        key=lambda item: (
            -item["best_complete_allowed_oracle_geometric_mean"],
            _SCREENING_QUEUE_ORDINAL[item["risk"]],
            _SCREENING_QUEUE_ORDINAL[item["implementation_cost"]],
            item["implementation_candidate_id"].encode("utf-8"),
        )
    )
    return queue


def build_candidate_screening_report(
    *,
    screening: Path | str | Mapping[str, Any],
    output_directory: Path,
) -> dict[str, Path]:
    """Emit the deterministic first-stage report for the locked 11 families.

    The report deliberately consumes only the normalized screening artifact.  It
    cannot infer implementation performance from an Oracle source-pair upper
    bound, and therefore leaves the later Pass capture rate explicitly pending.
    """

    if isinstance(screening, Mapping):
        document = validate_document(dict(screening))
        if document["schema_version"] != "candidate-screening.v1":
            raise ValidationError(
                "candidate screening report requires candidate-screening.v1"
            )
    else:
        document = _load_version(Path(screening), "candidate-screening.v1")

    locked_ids = [item[0] for item in _CANDIDATE_SCREENING_FAMILIES]
    by_family = {item["candidate_id"]: item for item in document["candidates"]}
    if set(by_family) != set(locked_ids):
        missing = sorted(set(locked_ids) - set(by_family))
        unexpected = sorted(set(by_family) - set(locked_ids))
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unexpected:
            details.append("unexpected=" + ",".join(unexpected))
        raise ValidationError(
            "candidate screening report requires the exact locked 11 families: "
            + "; ".join(details)
        )

    qualified_ids = [
        family_id
        for family_id in locked_ids
        if by_family[family_id]["qualification_status"] == "qualified"
    ]
    implementation_queue = _candidate_implementation_queue(
        [by_family[family_id] for family_id in locked_ids]
    )
    lines = [
        "# ACCELA 候选算法筛选报告",
        "",
        "> **Oracle 上界声明：** 本报告中的 GM 来自同一 FULL artifact 的 baseline／optimized Oracle 源码腿，是结构级动态指令收益上界，不是候选 Pass 的实测速率，也不是 BOOM 硬件收益。被静态合同排除的结构只作诊断，不得用于判定合格。",
        "",
        "## 筛选终点",
        "",
    ]
    if qualified_ids:
        lines.append(
            f"共有 {len(qualified_ids)} 个家族合格：{_screening_ids(qualified_ids)}。"
            "未触发“无合格项停止”条件；后续只能实现这些家族对应的显式候选。"
        )
    else:
        lines.append(
            "**无合格项。按锁定协议在本报告处停止实现，不降低 1.10 门槛，"
            "不以不完整、被排除或诊断性 Oracle 结构补票。**"
        )
    lines.extend(
        [
            "",
            "## 合格项实施队列",
            "",
            "该队列只决定合格项的实施先后，不是候选 Pass 实测排名。顺序固定为：最佳完整允许 Oracle GM 降序、风险升序、实现成本升序、稳定 implementation candidate ID UTF-8 字节序。",
            "",
            "| 顺序 | 家族 | 实现候选 | 最佳完整允许 Oracle GM | 风险 | 实现成本 |",
            "|---:|---|---|---:|---|---|",
        ]
    )
    if implementation_queue:
        for index, item in enumerate(implementation_queue, 1):
            lines.append(
                f"| {index} | `{_markdown_cell(item['candidate_id'])}` | "
                f"`{_markdown_cell(item['implementation_candidate_id'])}` | "
                f"{_format_ratio(item['best_complete_allowed_oracle_geometric_mean'])} | "
                f"`{item['risk']}` | `{item['implementation_cost']}` |"
            )
    else:
        lines.append("| — | 无合格项 | — | — | — | — |")
    lines.extend(
        [
            "",
            "## 证据身份与判定规则",
            "",
            f"- Screening ID：`{_markdown_cell(document['screening_id'])}`",
            f"- 生成时间：`{_markdown_cell(document['generated_at'])}`",
            f"- Candidate evidence SHA-256：`{document['candidate_evidence_sha256']}`",
            f"- Screening spec SHA-256：`{document['screening_spec_sha256']}`",
            f"- Oracle capture SHA-256：`{document['oracle_capture_sha256']}`",
            f"- Screening 声明的基线 PassRegistry SHA-256（`pass_registry_sha256`）：`{document['pass_registry_sha256']}`",
            f"- 筛选基线 PassRegistry artifact canonical / physical SHA-256：`{document['base_pass_registry']['canonical_sha256']}` / `{document['base_pass_registry']['physical_sha256']}`。报告不记录其本地路径。",
            f"- 固定门槛：允许结构的 small／medium／large 三档全部正确完整，且三档等权动态指令 GM `>= {_format_number(float(document['oracle_threshold']), 2)}`。",
            "- 同时必须不存在能力重复，并具备统一 matcher／profitability／transform／proof、完整 legality obligations、精确 i32 语义且不改变 runtime／ABI。静态拒绝条件不会被 Oracle 数值覆盖。",
            "",
            "## 十一家族总览",
            "",
            "| 家族 | 锁定能力 | 实现候选 | 判定 | 允许结构 | 达标结构 | 现有能力重叠 | Proof | 成本 | 风险 | 精确原因 |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for family_id, capability, _ in _CANDIDATE_SCREENING_FAMILIES:
        item = by_family[family_id]
        lines.append(
            f"| `{family_id}` | {_markdown_cell(capability)} | "
            f"{('`' + _markdown_cell(item['implementation_candidate_id']) + '`') if item['implementation_candidate_id'] is not None else '无'} | "
            f"`{item['qualification_status']}` | {_screening_oracle_refs(item['eligible_oracle_structure_refs'])} | "
            f"{_screening_oracle_refs(item['qualifying_oracle_structure_refs'])} | "
            f"{_screening_ids(item['overlaps_existing_pass_ids'])} | "
            f"`{item['legality_proof_path']}` | `{item['implementation_cost']}` | "
            f"`{item['risk']}` | {_screening_rejections(item['rejection_reasons'])} |"
        )

    lines.extend(
        [
            "",
            "## 全部 Oracle 结构与三档完整性",
            "",
            "“允许”由锁定静态结构集合决定；不允许的结构即使 GM 较高也只能诊断。GM 仅在三档全部可排名时存在，且三档等权。",
            "",
            "| 候选家族 | Oracle 来源 | 结构 | 静态范围 | small | medium | large | 三档完整 | Oracle GM 上界 | 门槛／用途 |",
            "|---|---|---|---|---|---|---|---|---:|---|",
        ]
    )
    for family_id, _, _ in _CANDIDATE_SCREENING_FAMILIES:
        item = by_family[family_id]
        for structure in sorted(
            item["oracle_structures"],
            key=lambda value: (
                value["oracle_family_id"], value["structure_id"]
            ),
        ):
            if not structure["eligible_for_candidate_screening"]:
                outcome = "诊断；静态合同排除"
            elif structure["meets_threshold"]:
                outcome = "达标；可支撑家族合格"
            elif not structure["eligible_for_ranking"]:
                outcome = (
                    "不完整；`"
                    + _markdown_cell(structure["ineligibility_reason"])
                    + "`"
                )
            else:
                outcome = "完整；GM 低于 1.10"
            lines.append(
                f"| `{family_id}` | `{_markdown_cell(structure['oracle_family_id'])}` | "
                f"`{_markdown_cell(structure['structure_id'])}` | "
                f"{('允许' if structure['eligible_for_candidate_screening'] else '排除')} | "
                f"{_screening_tier_cell(structure['sizes']['small'])} | "
                f"{_screening_tier_cell(structure['sizes']['medium'])} | "
                f"{_screening_tier_cell(structure['sizes']['large'])} | "
                f"{('是' if structure['eligible_for_ranking'] else '否')} | "
                f"{_format_ratio(structure['geometric_mean_speedup'])} | {outcome} |"
            )

    lines.extend(
        [
            "",
            "## Oracle 捕获率边界",
            "",
            "本阶段只能给出“允许结构中有多少达到上界门槛”，不能给出 Pass 捕获率。后续捕获率固定定义为 `B3 候选实测 GM / 该候选最佳允许 Oracle GM 上界`；在冻结候选 Pass 完成 B3 前一律为 `n/a`，不得把达标结构占比冒充捕获率。",
            "",
            "| 家族 | 允许结构 | 达标结构 | 达标结构覆盖 | 最佳允许 Oracle GM 上界 | Pass 捕获率 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for family_id, _, _ in _CANDIDATE_SCREENING_FAMILIES:
        item = by_family[family_id]
        allowed = [
            structure
            for structure in item["oracle_structures"]
            if structure["eligible_for_candidate_screening"]
        ]
        qualifying = [structure for structure in allowed if structure["meets_threshold"]]
        complete_upper_bounds = [
            float(structure["geometric_mean_speedup"])
            for structure in allowed
            if structure["eligible_for_ranking"]
            and structure["geometric_mean_speedup"] is not None
        ]
        coverage = (
            f"{len(qualifying) / len(allowed):.2%}" if allowed else "n/a（静态阻断）"
        )
        best = max(complete_upper_bounds) if complete_upper_bounds else None
        lines.append(
            f"| `{family_id}` | {len(allowed)} | {len(qualifying)} | {coverage} | "
            f"{_format_ratio(best)} | n/a（B3 未实测） |"
        )

    lines.extend(["", "## 家族级实现与拒绝依据", ""])
    for family_id, capability, static_handling in _CANDIDATE_SCREENING_FAMILIES:
        item = by_family[family_id]
        complete_allowed = [
            structure
            for structure in item["oracle_structures"]
            if structure["eligible_for_candidate_screening"]
            and structure["eligible_for_ranking"]
            and structure["geometric_mean_speedup"] is not None
        ]
        best = max(
            (float(structure["geometric_mean_speedup"]) for structure in complete_allowed),
            default=None,
        )
        lines.extend(
            [
                f"### `{family_id}`",
                "",
                f"- 锁定能力：{capability}。",
                f"- 静态处理：{static_handling}",
                f"- 允许／达标结构：{_screening_oracle_refs(item['eligible_oracle_structure_refs'])} / {_screening_oracle_refs(item['qualifying_oracle_structure_refs'])}；最佳完整允许 Oracle GM 上界 {_format_ratio(best)}。",
                f"- 现有能力重叠：{_screening_ids(item['overlaps_existing_pass_ids'])}；duplicate_of：{('`' + _markdown_cell(item['duplicate_of']) + '`') if item['duplicate_of'] is not None else '无'}。",
                f"- Legality：proof path `{item['legality_proof_path']}`；obligations {_screening_ids(item['legality_obligation_ids'])}。",
                f"- 工程代价／风险：`{item['implementation_cost']}` / `{item['risk']}`；规格 `{item['specification_status']}`；BOOM 特性依赖 `{str(item['requires_boom_feature']).lower()}`。",
                f"- 最终判定：`{item['qualification_status']}`；精确原因：{_screening_rejections(item['rejection_reasons'])}。",
                "",
            ]
        )

    lines.extend(["## 重复候选审计", ""])
    if document["duplicate_groups"]:
        for group in document["duplicate_groups"]:
            lines.append(
                f"- canonical `{_markdown_cell(group['canonical_candidate_id'])}`："
                f"{_screening_ids(group['duplicate_candidate_ids'])}。"
            )
    else:
        lines.append("没有声明重复候选组。")
    lines.extend(
        [
            "",
            "## 结论边界",
            "",
            "排序若存在，仅用于合格项实施队列；Oracle GM 是上界而非 Pass 实测。"
            "本报告不构成候选默认启用、BOOM v3 收益或竞赛最终加速声明。",
            "",
        ]
    )

    output = _prepare_report_output_directory(
        output_directory, label="candidate screening report output directory"
    )
    report_path = output / "CANDIDATE_SCREENING_REPORT.zh-CN.md"
    atomic_write_text(report_path, "\n".join(lines))
    return {"CANDIDATE_SCREENING_REPORT.zh-CN.md": report_path}


_CANDIDATE_REPORT_SVG_FILES = (
    "candidate-single-gain.svg",
    "candidate-per-suite.svg",
    "candidate-combined-ranking.svg",
    "candidate-interaction-heatmap.svg",
    "candidate-oracle-capture.svg",
    "candidate-cache-hotblock.svg",
    "candidate-pareto.svg",
)

_CANDIDATE_REPORT_CSV_FIELDS: dict[str, tuple[str, ...]] = {
    "candidate-screening.csv": (
        "candidate_id",
        "implementation_candidate_id",
        "qualification_status",
        "eligible_oracle_structure_refs",
        "qualifying_oracle_structure_refs",
        "best_eligible_oracle_geometric_mean_upper_bound",
        "overlaps_existing_pass_ids",
        "legality_proof_path",
        "legality_obligation_ids",
        "implementation_cost",
        "risk",
        "rejection_reasons",
    ),
    "candidate-b1-correctness.csv": (
        "candidate_id",
        "evidence_present",
        "run_id",
        "expected_case_count",
        "state",
        "passed_cases",
        "failed_cases",
        "pending_cases",
        "censored_cases",
        "all_correct",
        "failure_classification",
    ),
    "candidate-b2-tuning.csv": (
        "candidate_id",
        "evidence_present",
        "run_id",
        "expected_case_count",
        "eligible_for_analysis",
        "failure_classification",
        "comparable_cases",
        "comparable_source_groups",
        "correctness_failures",
        "censored_cases",
        "excluded_cases",
        "case_geometric_mean_speedup",
        "confidence_interval_95_low",
        "confidence_interval_95_high",
        "used_for_elimination",
    ),
    "candidate-suite-results.csv": (
        "candidate_id",
        "data_role",
        "evidence_present",
        "run_id",
        "expected_case_count",
        "eligible_for_ranking",
        "failure_classification",
        "comparable_cases",
        "comparable_source_groups",
        "correctness_failures",
        "censored_cases",
        "excluded_cases",
        "case_geometric_mean_speedup",
        "confidence_interval_95_low",
        "confidence_interval_95_high",
        "static_text_bytes_full",
        "static_text_bytes_full_plus_candidate",
        "static_text_ratio",
    ),
    "candidate-ranking.csv": (
        "rank",
        "candidate_id",
        "combined_case_geometric_mean_speedup",
        "b3_case_geometric_mean_speedup",
        "combined_static_text_bytes_full_plus_candidate",
        "combined_static_text_ratio",
        "stable_id_tiebreak",
        "implementation_cost",
        "risk",
    ),
    "candidate-oracle-capture.csv": (
        "candidate_id",
        "oracle_upper_bound",
        "b3_measured_speedup",
        "oracle_capture_ratio",
    ),
    "candidate-interactions.csv": (
        "task_id",
        "run_id",
        "run_sha256",
        "configuration_sha256",
        "state",
        "terminal_failure_classification",
        "left_candidate_id",
        "right_candidate_id",
        "eligible_for_interpretation",
        "failure_classification",
        "comparable_cases",
        "correctness_failures",
        "censored_cases",
        "excluded_cases",
        "pair_case_geometric_mean_speedup",
        "expected_multiplicative_speedup",
        "delta_ln_geometric_mean",
    ),
    "candidate-toolchains.csv": (
        "label",
        "reference_run_id",
        "state",
        "failure_classification",
        "full_run_id",
        "winner_run_id",
        "failed_cases",
        "pending_cases",
        "comparable_cases",
        "correctness_failures",
        "censored_cases",
        "excluded_cases",
        "reference_over_full_geometric_mean",
        "reference_over_full_confidence_interval_95_low",
        "reference_over_full_confidence_interval_95_high",
        "reference_over_winner_geometric_mean",
        "reference_over_winner_confidence_interval_95_low",
        "reference_over_winner_confidence_interval_95_high",
    ),
    "candidate-cache-hotblock.csv": (
        "label",
        "run_id",
        "enabled_candidate_ids",
        "state",
        "failure_classification",
        "total_cases",
        "passed_cases",
        "failed_cases",
        "censored_cases",
        "sample_count",
        "mean_l1d_misses_per_1000_dynamic_loads",
        "mean_hottest_block_dynamic_instruction_share",
    ),
}

_CANDIDATE_STAGE_CASE_COUNTS = {
    "B1": 140,
    "B2": 20,
    "B3": 60,
    "B4": 59,
    "B5": 60,
    "B6": 88,
}


def _candidate_outcome_row(
    candidate_id: str,
    data_role: str,
    outcome: Mapping[str, Any] | None,
) -> dict[str, Any]:
    interval = None if outcome is None else outcome["confidence_interval_95"]
    return {
        "candidate_id": candidate_id,
        "data_role": data_role,
        "evidence_present": outcome is not None,
        "run_id": None if outcome is None else outcome["run_id"],
        "expected_case_count": _CANDIDATE_STAGE_CASE_COUNTS[data_role],
        "eligible_for_ranking": (
            False if outcome is None else outcome["eligible_for_ranking"]
        ),
        "failure_classification": (
            "not_run" if outcome is None else outcome["ineligibility_reason"]
        ),
        "comparable_cases": 0 if outcome is None else outcome["comparable_cases"],
        "comparable_source_groups": (
            0 if outcome is None else outcome["comparable_source_groups"]
        ),
        "correctness_failures": (
            0 if outcome is None else outcome["correctness_failures"]
        ),
        "censored_cases": 0 if outcome is None else outcome["censored_cases"],
        "excluded_cases": 0 if outcome is None else outcome["excluded_cases"],
        "case_geometric_mean_speedup": (
            None if outcome is None else outcome["case_geometric_mean_speedup"]
        ),
        "confidence_interval_95_low": (
            None if interval is None else interval["low"]
        ),
        "confidence_interval_95_high": (
            None if interval is None else interval["high"]
        ),
        "static_text_bytes_full": (
            None if outcome is None else outcome["static_text_bytes_full"]
        ),
        "static_text_bytes_full_plus_candidate": (
            None
            if outcome is None
            else outcome["static_text_bytes_full_plus_candidate"]
        ),
        "static_text_ratio": None if outcome is None else outcome["static_text_ratio"],
    }


def _candidate_ci_cell(row: Mapping[str, Any]) -> str:
    low = row["confidence_interval_95_low"]
    high = row["confidence_interval_95_high"]
    if low is None or high is None:
        return "n/a"
    return f"[{_format_number(float(low))}, {_format_number(float(high))}]"


def _verify_r7_physical_bindings(
    frozen: Mapping[str, Any],
    *,
    workspace_root: Path,
    campaign_root: Path,
    runs_root: Path,
) -> int:
    """Rehash every artifact enumerated by the frozen r7 namespace contract."""

    roots = {
        "repository": resolve_without_symlinks(
            workspace_root, label="r7 repository root"
        ),
        "campaign": resolve_without_symlinks(
            campaign_root, label="r7 campaign root"
        ),
        "runs": resolve_without_symlinks(runs_root, label="r7 runs root"),
    }

    def resolve_artifact(artifact_key: str) -> Path:
        parts = artifact_key.split("/")
        if len(parts) < 2 or parts[0] not in roots or any(
            part in {"", ".", ".."} for part in parts[1:]
        ):
            raise ValidationError(f"r7 artifact key is not portable: {artifact_key}")
        root = roots[parts[0]]
        resolved = resolve_without_symlinks(
            root.joinpath(*parts[1:]), label=f"r7 artifact {artifact_key}"
        )
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValidationError(
                f"r7 artifact key escapes its frozen namespace: {artifact_key}"
            )
        return resolved

    bindings = frozen["bindings"]
    artifacts = [
        bindings["campaign_plan"],
        *bindings["measurement_protocols"],
        *bindings["controllers"],
        bindings["run_evidence_registry"],
        *bindings["status_ledger"]["entries"],
        *frozen["registered_terminal_runs"],
        frozen["unregistered_partial_run"],
    ]
    keys = [item["artifact_key"] for item in artifacts]
    if len(keys) != 56 or len(set(keys)) != 56:
        raise ValidationError("r7 freeze must enumerate exactly 56 distinct bindings")
    for item in artifacts:
        artifact_key = item["artifact_key"]
        path = resolve_artifact(artifact_key)
        if sha256_file(path) != item["physical_sha256"]:
            raise ValidationError(f"r7 physical artifact hash differs: {artifact_key}")
        canonical = item.get("canonical_sha256")
        if canonical is not None and sha256_json(read_json(path)) != canonical:
            raise ValidationError(f"r7 canonical artifact hash differs: {artifact_key}")
    return len(artifacts)


def build_candidate_report(
    *,
    candidate_final_path: Path,
    campaign_plan_path: Path,
    completed_campaign_status_path: Path,
    completed_status_ledger_paths: Sequence[Path],
    screening_path: Path,
    output_directory: Path,
    b1_full_run_path: Path,
    full_run_path: Path,
    r7_freeze_path: Path,
    workspace_root: Path,
    r7_campaign_root: Path,
    r7_runs_root: Path,
    diagnostic_study_path: Path | None = None,
    winner_run_path: Path | None = None,
    comparison_paths: Mapping[str, Path] | None = None,
    hotblock_run_paths: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    """Emit deterministic, reader-facing candidate campaign artifacts.

    Diagnostics and external toolchains remain explicit context and never enter
    the 267-case single-candidate ranking already sealed in candidate-final.v1.
    """

    campaign_completion = validate_candidate_final_completion(
        campaign_plan_path=campaign_plan_path,
        candidate_final_path=candidate_final_path,
        completed_status_path=completed_campaign_status_path,
        status_ledger_paths=completed_status_ledger_paths,
        workspace_root=workspace_root,
    )
    final = _load_version(candidate_final_path, "candidate-final.v1")
    screening = _load_version(screening_path, "candidate-screening.v1")
    frozen_screening = final["freeze"]["screening"]
    screening_base_registry = screening["base_pass_registry"]
    frozen_screening_base_registry = final["freeze"][
        "screening_base_pass_registry"
    ]
    frozen_executable_registry = final["freeze"]["executable_pass_registry"]
    executable_registry_sha256 = final["executable_pass_registry_sha256"]
    if (
        final["screening_sha256"] != sha256_json(screening)
        or frozen_screening["canonical_sha256"] != sha256_json(screening)
        or frozen_screening["physical_sha256"] != sha256_file(screening_path)
        or screening["pass_registry_sha256"]
        != screening_base_registry["canonical_sha256"]
        or screening_base_registry != frozen_screening_base_registry
        or executable_registry_sha256
        != frozen_executable_registry["canonical_sha256"]
        or screening_base_registry["canonical_sha256"]
        == frozen_executable_registry["canonical_sha256"]
    ):
        raise ValidationError(
            "candidate report screening/executable PassRegistry binding differs"
        )
    campaign_runs_by_run_id = {
        item["run_id"]: item for item in final["campaign"]["run_records"]
    }

    def require_final_raw_run(
        run: Mapping[str, Any], path: Path, *, label: str
    ) -> None:
        binding = campaign_runs_by_run_id.get(run["run_id"])
        if (
            binding is None
            or binding["run_sha256"] != sha256_json(run)
            or binding["run_physical_sha256"] != sha256_file(path)
            or binding["state"] != run["state"]
        ):
            raise ValidationError(
                f"candidate report {label} differs from final campaign evidence"
            )
    diagnostic = (
        _load_version(diagnostic_study_path, "candidate-study.v1")
        if diagnostic_study_path is not None
        else None
    )
    final_diagnostic_study = final["diagnostics"]["study"]
    if diagnostic is None:
        if final_diagnostic_study is not None:
            raise ValidationError(
                "candidate report omits the final-bound diagnostic study"
            )
    elif (
        diagnostic_study_path is None
        or final_diagnostic_study is None
        or diagnostic["study_id"] != final_diagnostic_study["study_id"]
        or sha256_json(diagnostic)
        != final_diagnostic_study["canonical_sha256"]
        or sha256_file(diagnostic_study_path)
        != final_diagnostic_study["physical_sha256"]
    ):
        raise ValidationError(
            "candidate diagnostic study dual-hash differs from final evidence"
        )
    if diagnostic is not None:
        if (
            diagnostic["data_role"] != "B3"
            or diagnostic["candidate_registry_sha256"]
            != final["candidate_registry_sha256"]
        ):
            raise ValidationError(
                "candidate report diagnostics must be bound B3 candidate evidence"
            )
        frozen_top3_ids = final["diagnostics"]["top3_candidate_ids"]
        if [item["candidate_id"] for item in diagnostic["candidates"]] != frozen_top3_ids:
            raise ValidationError(
                "candidate diagnostic study must cover the exact ordered B3 Top3 singles"
            )
        b3_reference = final["studies"]["B3"]
        if (
            diagnostic["study_id"] == b3_reference["study_id"]
            or diagnostic["suite_id"] != b3_reference["suite_id"]
            or diagnostic["manifest_sha256"] != b3_reference["manifest_sha256"]
            or diagnostic["baseline"]["run_id"]
            != b3_reference["baseline_run_id"]
            or diagnostic["baseline"]["run_sha256"]
            != b3_reference["baseline_run_sha256"]
            or diagnostic["baseline"]["configuration_sha256"]
            != b3_reference["baseline_configuration_sha256"]
        ):
            raise ValidationError(
                "candidate diagnostic study must be a distinct study bound to the exact B3 baseline"
            )
        final_b3 = {
            item["implementation_candidate_id"]: item["suite_outcomes"]["B3"]
            for item in final["candidates"]
            if item["implementation_candidate_id"] in set(frozen_top3_ids)
        }
        for item in diagnostic["candidates"]:
            expected = final_b3[item["candidate_id"]]
            assert expected is not None
            if (
                item["run_id"] != expected["run_id"]
                or item["run_sha256"] != expected["run_sha256"]
                or item["configuration_sha256"]
                != expected["configuration_sha256"]
            ):
                raise ValidationError(
                    "candidate diagnostic study single-run binding differs from formal B3"
                )

    diagnostic_tasks = final["diagnostics"]["tasks"]
    pair_task_by_candidates = {
        tuple(sorted(task["candidate_ids"])): task
        for task in diagnostic_tasks
        if task["kind"] == "pair"
    }
    cache_task_by_candidates = {
        tuple(task["candidate_ids"]): task
        for task in diagnostic_tasks
        if task["kind"] == "cache_hotblock"
    }
    if diagnostic is not None:
        for interaction in diagnostic["interactions"]:
            candidate_ids = tuple(sorted(interaction["candidate_ids"]))
            task = pair_task_by_candidates.get(candidate_ids)
            expected_task_id = f"diagnostic.pair.{'+'.join(candidate_ids)}"
            if (
                task is None
                or task["task_id"] != expected_task_id
                or interaction["run_id"] != task["run_id"]
                or interaction["run_sha256"] != task["evidence_sha256"]
                or interaction["configuration_sha256"]
                != task["configuration_sha256"]
                or task["status"] not in {"completed", "failed", "interrupted"}
            ):
                raise ValidationError(
                    "candidate Top3 pair run identity differs from final diagnostic evidence"
                )
            if task["status"] != "completed" and (
                interaction["eligible_for_ranking"]
                or interaction["pair_case_geometric_mean_speedup"] is not None
                or interaction["expected_multiplicative_speedup"] is not None
                or interaction["delta_ln_geometric_mean"] is not None
            ):
                raise ValidationError(
                    "failed candidate Top3 pair diagnostic carries fabricated metrics"
                )

    b1_full_run = _load_version(b1_full_run_path, "run-record.v1")
    full_run = _load_version(full_run_path, "run-record.v1")
    require_final_raw_run(b1_full_run, b1_full_run_path, label="B1 FULL run")
    require_final_raw_run(full_run, full_run_path, label="B3 FULL run")
    b3_study = final["studies"]["B3"]
    if (
        full_run["run_id"] != b3_study["baseline_run_id"]
        or sha256_json(full_run) != b3_study["baseline_run_sha256"]
        or full_run["configuration_sha256"]
        != b3_study["baseline_configuration_sha256"]
    ):
        raise ValidationError("candidate B3 FULL run differs from final evidence")
    _require_formal_measurement(full_run, require_accela_pipeline=True)
    implemented_b1 = [
        item["b1_correctness"]
        for item in final["candidates"]
        if item["b1_correctness"] is not None
    ]
    if not implemented_b1:
        raise ValidationError("candidate report cannot bind B1 FULL without candidate B1 evidence")
    frozen_b1_full = final["b1_full_correctness"]
    first_b1 = implemented_b1[0]
    if (
        b1_full_run["run_id"] != frozen_b1_full["run_id"]
        or sha256_json(b1_full_run) != frozen_b1_full["run_sha256"]
        or b1_full_run["configuration_sha256"]
        != frozen_b1_full["configuration_sha256"]
        or b1_full_run["state"] != frozen_b1_full["state"]
        or b1_full_run["summary"]["passed_cases"]
        != frozen_b1_full["passed_cases"]
        or b1_full_run["summary"]["failed_cases"]
        != frozen_b1_full["failed_cases"]
        or b1_full_run["summary"]["pending_cases"]
        != frozen_b1_full["pending_cases"]
        or b1_full_run["summary"]["censored_cases"]
        != frozen_b1_full["censored_cases"]
        or frozen_b1_full["suite_id"] != first_b1["suite_id"]
        or frozen_b1_full["manifest_sha256"] != first_b1["manifest_sha256"]
        or frozen_b1_full["case_count"] != 140
        or frozen_b1_full["evidence_level"] != "qemu_correctness"
        or b1_full_run["state"] not in {"completed", "failed", "interrupted"}
        or b1_full_run["suite_id"] != first_b1["suite_id"]
        or b1_full_run["manifest_sha256"] != first_b1["manifest_sha256"]
        or b1_full_run["configuration"]["evidence_level"] != "qemu_correctness"
        or b1_full_run["provenance"]["pipeline_profile_id"] != "candidate-empty"
        or b1_full_run["configuration"].get("enabled_candidate_ids", [])
        or {case["data_role"] for case in b1_full_run["cases"]} != {"B1"}
        or b1_full_run["summary"]["total_cases"] != 140
    ):
        raise ValidationError("candidate report B1 FULL baseline identity differs")
    winner_run = (
        _load_version(winner_run_path, "run-record.v1")
        if winner_run_path is not None
        else None
    )
    reference_labels = ("gcc-13.3-o2", "clang-18-o3")
    supplied_reference_paths = comparison_paths or {}
    if set(supplied_reference_paths) != set(reference_labels):
        raise ValidationError(
            "candidate report requires exact gcc-13.3-o2 and clang-18-o3 B3 references"
        )
    reference_runs = {
        label: _load_version(supplied_reference_paths[label], "run-record.v1")
        for label in reference_labels
    }
    campaign_runs = {
        item["task_id"]: item for item in final["campaign"]["run_records"]
    }
    reference_tasks = {
        "gcc-13.3-o2": "run.B3.gcc",
        "clang-18-o3": "run.B3.clang",
    }
    toolchain_context: list[dict[str, Any]] = []
    winner_id = final["winner_candidate_id"]
    if (winner_id is None) != (winner_run is None):
        raise ValidationError(
            "candidate report winner identity and winner B3 run must be supplied together"
        )
    if winner_run is not None:
        assert winner_run_path is not None
        require_final_raw_run(
            winner_run, winner_run_path, label="winner B3 run"
        )
        if winner_id is None:
            raise ValidationError("candidate report cannot bind a winner run without a winner")
        winner = next(
            item
            for item in final["candidates"]
            if item["implementation_candidate_id"] == winner_id
        )
        b3 = winner["suite_outcomes"]["B3"]
        assert b3 is not None
        if (
            winner_run["run_id"] != b3["run_id"]
            or sha256_json(winner_run) != b3["run_sha256"]
        ):
            raise ValidationError("candidate winner run differs from final B3 evidence")
        _require_formal_measurement(winner_run, require_accela_pipeline=True)
    for label, reference in reference_runs.items():
        require_final_raw_run(
            reference,
            supplied_reference_paths[label],
            label=f"{label} reference run",
        )
        reference_state = reference["state"]
        campaign_reference = campaign_runs.get(reference_tasks[label])
        if (
            campaign_reference is None
            or campaign_reference["run_id"] != reference["run_id"]
            or campaign_reference["run_sha256"] != sha256_json(reference)
            or campaign_reference["state"] != reference_state
            or reference_state not in {"completed", "failed", "interrupted"}
            or reference["provenance"]["pipeline_profile_id"] != label
            or reference["suite_id"] != full_run["suite_id"]
            or reference["manifest_sha256"] != full_run["manifest_sha256"]
            or {case["data_role"] for case in reference["cases"]} != {"B3"}
        ):
            raise ValidationError(
                f"candidate report reference run identity differs from {label}"
            )
        full_comparison = None
        winner_comparison = None
        full_interval = None
        if reference_state == "completed":
            _require_formal_measurement(reference)
            full_comparison = compare_runs(
                reference, full_run, mode="cross_toolchain"
            )
            full_interval = bootstrap_geometric_mean_ci(
                full_comparison.pairs, samples=10_000, seed=20260809
            )
            winner_comparison = (
                None
                if winner_run is None
                else compare_runs(reference, winner_run, mode="cross_toolchain")
            )
        winner_interval = (
            None
            if winner_comparison is None
            else bootstrap_geometric_mean_ci(
                winner_comparison.pairs, samples=10_000, seed=20260809
            )
        )
        toolchain_context.append(
            {
                "label": label,
                "reference_run_id": reference["run_id"],
                "state": reference_state,
                "failure_classification": (
                    None
                    if reference_state == "completed"
                    else f"run_{reference_state}"
                ),
                "full_run_id": full_run["run_id"],
                "winner_run_id": None if winner_run is None else winner_run["run_id"],
                "failed_cases": reference["summary"]["failed_cases"],
                "pending_cases": reference["summary"]["pending_cases"],
                "comparable_cases": (
                    0 if full_comparison is None else len(full_comparison.pairs)
                ),
                "correctness_failures": (
                    sum(
                        case["status"] in {"wrong_output", "runtime_error"}
                        for case in reference["cases"]
                    )
                    if full_comparison is None
                    else full_comparison.correctness_failures
                ),
                "censored_cases": (
                    reference["summary"]["censored_cases"]
                    if full_comparison is None
                    else full_comparison.censored_cases
                ),
                "excluded_cases": (
                    reference["summary"]["failed_cases"]
                    if full_comparison is None
                    else full_comparison.excluded_cases
                ),
                "reference_over_full_geometric_mean": (
                    None
                    if full_comparison is None
                    else full_comparison.geometric_mean_speedup
                ),
                "reference_over_full_confidence_interval_95_low": (
                    None if full_interval is None else full_interval[0]
                ),
                "reference_over_full_confidence_interval_95_high": (
                    None if full_interval is None else full_interval[1]
                ),
                "reference_over_winner_geometric_mean": (
                    None
                    if winner_comparison is None
                    else winner_comparison.geometric_mean_speedup
                ),
                "reference_over_winner_confidence_interval_95_low": (
                    None if winner_interval is None else winner_interval[0]
                ),
                "reference_over_winner_confidence_interval_95_high": (
                    None if winner_interval is None else winner_interval[1]
                ),
            }
        )

    hotblock_runs = {
        label: _load_version(path, "run-record.v1")
        for label, path in sorted((hotblock_run_paths or {}).items())
    }
    hotblock = _hotblock_diagnostics(
        hotblock_runs, allow_terminal_failures=True
    )
    b3_ranked = sorted(
        (
            item
            for item in final["candidates"]
            if item["suite_outcomes"]["B3"] is not None
            and item["suite_outcomes"]["B3"]["eligible_for_ranking"]
        ),
        key=lambda item: (
            -float(item["suite_outcomes"]["B3"]["case_geometric_mean_speedup"]),
            item["implementation_candidate_id"],
        ),
    )
    top3_ids = [item["implementation_candidate_id"] for item in b3_ranked[:3]]
    if top3_ids != final["diagnostics"]["top3_candidate_ids"]:
        raise ValidationError(
            "candidate report B3 Top3 order differs from final diagnostics"
        )
    expected_pairs = {
        tuple(sorted(pair))
        for pair in combinations(top3_ids, 2)
    }
    actual_pairs = (
        set()
        if diagnostic is None
        else {
            tuple(sorted(item["candidate_ids"]))
            for item in diagnostic["interactions"]
        }
    )
    if actual_pairs != expected_pairs or len(actual_pairs) > 3:
        raise ValidationError(
            "candidate diagnostic study must contain all and only the at-most-three Top3 pairs"
        )
    if expected_pairs and diagnostic is None:
        raise ValidationError("candidate report lacks the required Top3 pair study")
    if not expected_pairs and diagnostic is not None:
        raise ValidationError(
            "candidate report must not bind an empty Top3 pair study"
        )

    expected_hotblock_sets = [(), *((candidate_id,) for candidate_id in top3_ids)]
    hotblock_by_candidates: dict[tuple[str, ...], tuple[str, Mapping[str, Any]]] = {}
    frozen_report_contract = final["freeze"]
    for label, run in hotblock_runs.items():
        require_final_raw_run(
            run,
            (hotblock_run_paths or {})[label],
            label=f"{label} cache/hotblock run",
        )
        enabled = tuple(run["configuration"].get("enabled_candidate_ids", []))
        if enabled not in expected_hotblock_sets:
            raise ValidationError(
                "cache/hotblock diagnostics may contain only candidate-empty and B3 Top3 singles"
            )
        if enabled in hotblock_by_candidates:
            raise ValidationError(
                "cache/hotblock diagnostics repeat an enabled-candidate profile"
            )
        if (
            run["suite_id"] != full_run["suite_id"]
            or run["manifest_sha256"] != full_run["manifest_sha256"]
            or {case["data_role"] for case in run["cases"]} != {"B3"}
            or run["configuration"].get("candidate_registry_sha256")
            != final["candidate_registry_sha256"]
            or run["configuration"].get("candidate_pass_registry_sha256")
            != frozen_report_contract["executable_pass_registry"][
                "canonical_sha256"
            ]
            or run["provenance"]["compiler_artifact_sha256"]
            != frozen_report_contract["compiler_artifact"]["physical_sha256"]
            or run["provenance"]["measurement_protocol_sha256"]
            != frozen_report_contract["hotblock_measurement_protocol"][
                "canonical_sha256"
            ]
        ):
            raise ValidationError(
                "cache/hotblock diagnostics must use the exact frozen B3 suite"
            )
        task = cache_task_by_candidates.get(enabled)
        expected_task_id = (
            "diagnostic.cache.full"
            if not enabled
            else f"diagnostic.cache.{enabled[0]}"
        )
        if (
            task is None
            or task["task_id"] != expected_task_id
            or run["run_id"] != task["run_id"]
            or sha256_json(run) != task["evidence_sha256"]
            or sha256_file((hotblock_run_paths or {})[label])
            != task["evidence_physical_sha256"]
            or run["configuration_sha256"] != task["configuration_sha256"]
            or run["state"] != task["status"]
        ):
            raise ValidationError(
                "cache/hotblock run identity differs from final diagnostic evidence"
            )
        hotblock_by_candidates[enabled] = (label, run)
    if set(hotblock_by_candidates) != set(expected_hotblock_sets):
        raise ValidationError(
            "candidate report requires cache/hotblock evidence for FULL and every B3 Top3 single"
        )

    r7: dict[str, Any] | None = None
    if r7_freeze_path is not None:
        resolved_r7_freeze = resolve_without_symlinks(
            r7_freeze_path, label="r7 diagnostic freeze"
        )
        frozen = read_json(resolved_r7_freeze)
        eligibility = frozen.get("eligibility", {})
        if (
            frozen.get("schema_version") != "diagnostic-freeze.v1"
            or frozen.get("classification")
            != "diagnostic_aborted_direction_mismatch"
            or eligibility.get("diagnostic_only") is not True
            or eligibility.get("ranking_eligible") is not False
            or eligibility.get("promotion_eligible") is not False
            or eligibility.get("auto_resume_allowed") is not False
            or eligibility.get("manual_resume_allowed") is not False
        ):
            raise ValidationError(
                "r7 appendix requires the immutable diagnostic-only direction-mismatch freeze"
            )
        registry = frozen["bindings"]["run_evidence_registry"]
        partial = frozen["unregistered_partial_run"]
        limitation_codes = {
            item["code"] for item in frozen.get("provenance_limitations", [])
        }
        enumerated_binding_count = _verify_r7_physical_bindings(
            frozen,
            workspace_root=workspace_root,
            campaign_root=r7_campaign_root,
            runs_root=r7_runs_root,
        )
        terminal_states = [
            item["state"] for item in frozen["registered_terminal_runs"]
        ]
        if (
            registry["completed_count"] != 20
            or registry["failed_count"] != 1
            or terminal_states.count("completed") != 20
            or terminal_states.count("failed") != 1
            or partial["summary"]["passed_cases"] != 16
            or partial["summary"]["pending_cases"] != 4
            or partial["resume_allowed"] is not False
            or enumerated_binding_count != 56
            or "source_to_wsl_ignored_tree_full_hash_not_completed"
            not in limitation_codes
        ):
            raise ValidationError("r7 diagnostic freeze counts or provenance boundary drifted")
        r7 = {
            "freeze_id": frozen["freeze_id"],
            "freeze_canonical_sha256": sha256_json(frozen),
            "freeze_physical_sha256": sha256_file(resolved_r7_freeze),
            "classification": frozen["classification"],
            "completed_count": registry["completed_count"],
            "failed_count": registry["failed_count"],
            "partial_passed_count": partial["summary"]["passed_cases"],
            "partial_pending_count": partial["summary"]["pending_cases"],
            "resume_allowed": partial["resume_allowed"],
            "ranking_eligible": eligibility["ranking_eligible"],
            "enumerated_bindings_rehashed": enumerated_binding_count,
            "whole_ignored_tree_equivalence_verified": False,
            "migration_source_equivalence_verified": False,
            "provenance_limitation_code": (
                "source_to_wsl_ignored_tree_full_hash_not_completed"
            ),
        }
    else:
        raise ValidationError("candidate final report requires the frozen r7 appendix")

    screening_by_implementation = {
        item["implementation_candidate_id"]: item
        for item in screening["candidates"]
        if item["implementation_candidate_id"] is not None
    }
    screening_rows: list[dict[str, Any]] = []
    for item in screening["candidates"]:
        allowed_bounds = [
            structure["geometric_mean_speedup"]
            for structure in item["oracle_structures"]
            if structure["eligible_for_candidate_screening"]
            and structure["eligible_for_ranking"]
            and structure["geometric_mean_speedup"] is not None
        ]
        screening_rows.append(
            {
                "candidate_id": item["candidate_id"],
                "implementation_candidate_id": item["implementation_candidate_id"],
                "qualification_status": item["qualification_status"],
                "eligible_oracle_structure_refs": list(
                    item["eligible_oracle_structure_refs"]
                ),
                "qualifying_oracle_structure_refs": list(
                    item["qualifying_oracle_structure_refs"]
                ),
                "best_eligible_oracle_geometric_mean_upper_bound": (
                    max(allowed_bounds) if allowed_bounds else None
                ),
                "overlaps_existing_pass_ids": list(
                    item["overlaps_existing_pass_ids"]
                ),
                "legality_proof_path": item["legality_proof_path"],
                "legality_obligation_ids": list(item["legality_obligation_ids"]),
                "implementation_cost": item["implementation_cost"],
                "risk": item["risk"],
                "rejection_reasons": list(item["rejection_reasons"]),
            }
        )

    b1_full_summary = b1_full_run["summary"]
    b1_full_all_correct = (
        b1_full_run["state"] == "completed"
        and b1_full_summary["passed_cases"] == 140
        and b1_full_summary["failed_cases"] == 0
        and b1_full_summary["pending_cases"] == 0
        and b1_full_summary["censored_cases"] == 0
    )
    b1_full_baseline = {
        "candidate_id": "FULL",
        "evidence_present": True,
        "run_id": b1_full_run["run_id"],
        "expected_case_count": _CANDIDATE_STAGE_CASE_COUNTS["B1"],
        "state": b1_full_run["state"],
        "passed_cases": b1_full_summary["passed_cases"],
        "failed_cases": b1_full_summary["failed_cases"],
        "pending_cases": b1_full_summary["pending_cases"],
        "censored_cases": b1_full_summary["censored_cases"],
        "all_correct": b1_full_all_correct,
        "failure_classification": (
            None
            if b1_full_all_correct
            else (
                f"run_{b1_full_run['state']}"
                if b1_full_run["state"] != "completed"
                else "correctness_failure"
            )
        ),
    }
    b1_rows: list[dict[str, Any]] = []
    b2_rows: list[dict[str, Any]] = []
    suite_rows: list[dict[str, Any]] = []
    for item in final["candidates"]:
        implementation_id = item["implementation_candidate_id"]
        if (
            implementation_id is None
            or screening_by_implementation[implementation_id][
                "qualification_status"
            ]
            != "qualified"
        ):
            continue
        b1 = item["b1_correctness"]
        b1_rows.append(
            {
                "candidate_id": implementation_id,
                "evidence_present": b1 is not None,
                "run_id": None if b1 is None else b1["run_id"],
                "expected_case_count": _CANDIDATE_STAGE_CASE_COUNTS["B1"],
                "state": None if b1 is None else b1["state"],
                "passed_cases": 0 if b1 is None else b1["passed_cases"],
                "failed_cases": 0 if b1 is None else b1["failed_cases"],
                "pending_cases": 140 if b1 is None else b1["pending_cases"],
                "censored_cases": 0 if b1 is None else b1["censored_cases"],
                "all_correct": False if b1 is None else b1["all_correct"],
                "failure_classification": (
                    "not_run"
                    if b1 is None
                    else (None if b1["all_correct"] else b1["failure_reason"])
                ),
            }
        )
        b2_outcome = item["b2_tuning"]
        b2_base = _candidate_outcome_row(implementation_id, "B2", b2_outcome)
        b2_rows.append(
            {
                "candidate_id": implementation_id,
                "evidence_present": b2_base["evidence_present"],
                "run_id": b2_base["run_id"],
                "expected_case_count": b2_base["expected_case_count"],
                "eligible_for_analysis": b2_base["eligible_for_ranking"],
                "failure_classification": b2_base["failure_classification"],
                "comparable_cases": b2_base["comparable_cases"],
                "comparable_source_groups": b2_base["comparable_source_groups"],
                "correctness_failures": b2_base["correctness_failures"],
                "censored_cases": b2_base["censored_cases"],
                "excluded_cases": b2_base["excluded_cases"],
                "case_geometric_mean_speedup": b2_base[
                    "case_geometric_mean_speedup"
                ],
                "confidence_interval_95_low": b2_base[
                    "confidence_interval_95_low"
                ],
                "confidence_interval_95_high": b2_base[
                    "confidence_interval_95_high"
                ],
                "used_for_elimination": False,
            }
        )
        for role in ("B3", "B4", "B5", "B6"):
            outcome = item["suite_outcomes"][role]
            suite_rows.append(
                _candidate_outcome_row(implementation_id, role, outcome)
            )

    interaction_rows: list[dict[str, Any]] = []
    if diagnostic is not None:
        for item in diagnostic["interactions"]:
            left_candidate_id, right_candidate_id = sorted(item["candidate_ids"])
            task = pair_task_by_candidates[
                (left_candidate_id, right_candidate_id)
            ]
            interaction_rows.append(
                {
                    "task_id": task["task_id"],
                    "run_id": item["run_id"],
                    "run_sha256": item["run_sha256"],
                    "configuration_sha256": item[
                        "configuration_sha256"
                    ],
                    "state": task["status"],
                    "terminal_failure_classification": task[
                        "failure_reason"
                    ],
                    "left_candidate_id": left_candidate_id,
                    "right_candidate_id": right_candidate_id,
                    "eligible_for_interpretation": item[
                        "eligible_for_ranking"
                    ],
                    "failure_classification": item["ineligibility_reason"],
                    "comparable_cases": item["comparable_cases"],
                    "correctness_failures": item["correctness_failures"],
                    "censored_cases": item["censored_cases"],
                    "excluded_cases": item["excluded_cases"],
                    "pair_case_geometric_mean_speedup": item[
                        "pair_case_geometric_mean_speedup"
                    ],
                    "expected_multiplicative_speedup": item[
                        "expected_multiplicative_speedup"
                    ],
                    "delta_ln_geometric_mean": item[
                        "delta_ln_geometric_mean"
                    ],
                }
            )
    interaction_rows.sort(
        key=lambda row: (row["left_candidate_id"], row["right_candidate_id"])
    )
    ranking_rows = [
        {
            **dict(item),
            "implementation_cost": screening_by_implementation[
                item["candidate_id"]
            ]["implementation_cost"],
            "risk": screening_by_implementation[item["candidate_id"]]["risk"],
        }
        for item in final["ranking"]
    ]
    oracle_by_implementation = {
        row["implementation_candidate_id"]: row[
            "best_eligible_oracle_geometric_mean_upper_bound"
        ]
        for row in screening_rows
        if row["implementation_candidate_id"] is not None
    }
    capture_rows: list[dict[str, Any]] = []
    for item in final["candidates"]:
        implementation_id = item["implementation_candidate_id"]
        if (
            implementation_id is None
            or screening_by_implementation[implementation_id][
                "qualification_status"
            ]
            != "qualified"
        ):
            continue
        oracle_upper = oracle_by_implementation.get(implementation_id)
        b3 = item["suite_outcomes"]["B3"]
        measured = (
            None if b3 is None else b3["case_geometric_mean_speedup"]
        )
        capture_rows.append(
            {
                "candidate_id": implementation_id,
                "oracle_upper_bound": oracle_upper,
                "b3_measured_speedup": measured,
                "oracle_capture_ratio": (
                    measured / oracle_upper
                    if measured is not None
                    and oracle_upper is not None
                    and oracle_upper > 0
                    else None
                ),
            }
        )

    hotblock_rows: list[dict[str, Any]] = []
    for enabled in expected_hotblock_sets:
        label, run = hotblock_by_candidates[enabled]
        task = cache_task_by_candidates[enabled]
        run_samples = [
            item for item in hotblock["samples"] if item["run_id"] == run["run_id"]
        ]
        cache_rates = [
            float(item["l1d_misses_per_1000_dynamic_loads"])
            for item in run_samples
            if item["l1d_misses_per_1000_dynamic_loads"] is not None
        ]
        hot_shares = [
            float(item["hotblock_dynamic_instruction_share"])
            for item in run_samples
        ]
        run_record = next(
            item for item in hotblock["runs"] if item["run_id"] == run["run_id"]
        )
        hotblock_rows.append(
            {
                "label": label,
                "run_id": run["run_id"],
                "enabled_candidate_ids": list(enabled),
                "state": run_record["state"],
                "failure_classification": task["failure_reason"],
                "total_cases": run_record["total_cases"],
                "passed_cases": run_record["passed_cases"],
                "failed_cases": run_record["failed_cases"],
                "censored_cases": run_record["censored_cases"],
                "sample_count": len(run_samples),
                "mean_l1d_misses_per_1000_dynamic_loads": (
                    statistics.fmean(cache_rates) if cache_rates else None
                ),
                "mean_hottest_block_dynamic_instruction_share": (
                    statistics.fmean(hot_shares) if hot_shares else None
                ),
            }
        )

    assert r7 is not None

    def document_binding(path: Path, document: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "canonical_sha256": sha256_json(document),
            "physical_sha256": sha256_file(path),
        }

    def frozen_artifact_identity(artifact: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "canonical_sha256": artifact["canonical_sha256"],
            "physical_sha256": artifact["physical_sha256"],
        }

    frozen = final["freeze"]
    reference_toolchain = frozen["reference_toolchain"]
    reference_baselines = {
        item["profile_id"]: item for item in reference_toolchain["baselines"]
    }
    if set(reference_baselines) != set(reference_labels):
        raise ValidationError(
            "candidate final frozen reference toolchain differs from GCC/Clang report contexts"
        )
    frozen_context = {
        "freeze_id": frozen["freeze_id"],
        "freeze_sha256": frozen["freeze_sha256"],
        "campaign_id": frozen["campaign_id"],
        "repository_commit": frozen["repo_commit"],
        "repository_tree": frozen["repo_tree"],
        "compiler_artifact_physical_sha256": frozen["compiler_artifact"][
            "physical_sha256"
        ],
        "candidate_empty_profile_id": "candidate-empty",
        "candidate_empty_profile": frozen_artifact_identity(
            frozen["base_pipeline_profile"]
        ),
        "standard_measurement_protocol": frozen_artifact_identity(
            frozen["standard_measurement_protocol"]
        ),
        "hotblock_measurement_protocol": frozen_artifact_identity(
            frozen["hotblock_measurement_protocol"]
        ),
        "reference_toolchain": {
            "snapshot": frozen_artifact_identity(reference_toolchain["snapshot"]),
            "common_tool_versions": dict(
                reference_toolchain["common_tool_versions"]
            ),
            "accela_jdk_version": reference_toolchain["accela_jdk_version"],
            "baselines": [
                {
                    key: reference_baselines[label][key]
                    for key in (
                        "compiler_baseline",
                        "profile_id",
                        "profile_sha256",
                        "tool",
                        "version",
                        "optimization",
                        "compiler_command_sha256",
                        "compiler_argv_sha256",
                    )
                }
                for label in reference_labels
            ],
        },
    }

    summary = validate_document(
        {
            "schema_version": "candidate-report.v1",
            "generated_at": final["generated_at"],
            "final_id": final["final_id"],
            "bindings": {
                "pass_registries": {
                    "screening_base": {
                        "declared_sha256": screening["pass_registry_sha256"],
                        "artifact": {
                            "canonical_sha256": frozen_screening_base_registry[
                                "canonical_sha256"
                            ],
                            "physical_sha256": frozen_screening_base_registry[
                                "physical_sha256"
                            ],
                        },
                    },
                    "executable": {
                        "declared_sha256": executable_registry_sha256,
                        "artifact": {
                            "canonical_sha256": frozen_executable_registry[
                                "canonical_sha256"
                            ],
                            "physical_sha256": frozen_executable_registry[
                                "physical_sha256"
                            ],
                        },
                    },
                },
                "campaign_completion": campaign_completion,
                "candidate_final": document_binding(candidate_final_path, final),
                "screening": document_binding(screening_path, screening),
                "b1_full_run": {
                    **document_binding(b1_full_run_path, b1_full_run),
                    "run_id": b1_full_run["run_id"],
                    "configuration_sha256": b1_full_run[
                        "configuration_sha256"
                    ],
                },
                "b3_full_run": {
                    **document_binding(full_run_path, full_run),
                    "run_id": full_run["run_id"],
                    "configuration_sha256": full_run["configuration_sha256"],
                },
                "diagnostic_study": (
                    None
                    if diagnostic is None or diagnostic_study_path is None
                    else {
                        **document_binding(diagnostic_study_path, diagnostic),
                        "study_id": diagnostic["study_id"],
                    }
                ),
                "winner_run": (
                    None
                    if winner_run is None or winner_run_path is None
                    else {
                        **document_binding(winner_run_path, winner_run),
                        "run_id": winner_run["run_id"],
                        "configuration_sha256": winner_run[
                            "configuration_sha256"
                        ],
                    }
                ),
                "reference_runs": [
                    {
                        "label": label,
                        "run_id": reference["run_id"],
                        "configuration_sha256": reference["configuration_sha256"],
                        **document_binding((comparison_paths or {})[label], reference),
                    }
                    for label, reference in reference_runs.items()
                ],
                "hotblock_runs": [
                    {
                        "label": label,
                        "run_id": run["run_id"],
                        "configuration_sha256": run["configuration_sha256"],
                        **document_binding((hotblock_run_paths or {})[label], run),
                    }
                    for label, run in hotblock_runs.items()
                ],
                "r7_freeze": {
                    "canonical_sha256": r7["freeze_canonical_sha256"],
                    "physical_sha256": r7["freeze_physical_sha256"],
                },
            },
            "frozen_context": frozen_context,
            "evidence_class": "qemu_proxy",
            "boom_hardware_verified": False,
            "conclusion": {
                "winner_candidate_id": final["winner_candidate_id"],
                "winner_reason": final["winner_reason"],
                "claim": (
                    "no_winner"
                    if final["winner_candidate_id"] is None
                    else "qemu_proxy_best_candidate"
                ),
            },
            "expected_combined_case_count": final[
                "expected_combined_case_count"
            ],
            "ranking_rule": [
                "combined_geometric_mean_desc",
                "b3_geometric_mean_desc",
                "static_text_bytes_asc",
                "stable_candidate_id_asc",
            ],
            "screening": screening_rows,
            "b1_full_baseline": b1_full_baseline,
            "b1_correctness": b1_rows,
            "b2_tuning": b2_rows,
            "suite_results": suite_rows,
            "ranking": ranking_rows,
            "oracle_capture": capture_rows,
            "interactions": interaction_rows,
            "toolchain_context": toolchain_context,
            "hotblock_diagnostics": hotblock_rows,
            "r7_diagnostic_appendix": r7,
            "artifacts": {
                "markdown_file": "FINAL_CANDIDATE_REPORT.zh-CN.md",
                "json_file": "candidate-report.v1.json",
                "csv_files": list(_CANDIDATE_REPORT_CSV_FIELDS),
                "svg_files": list(_CANDIDATE_REPORT_SVG_FILES),
            },
        }
    )
    output = _prepare_report_output_directory(
        output_directory, label="candidate final report output directory"
    )
    atomic_write_json(output / "candidate-report.v1.json", summary)

    def write_csv(name: str, rows: Sequence[Mapping[str, Any]]) -> None:
        buffer = io.StringIO(newline="")
        fields = _CANDIDATE_REPORT_CSV_FIELDS[name]
        writer = csv.DictWriter(
            buffer,
            fieldnames=fields,
            lineterminator="\n",
            extrasaction="raise",
        )
        writer.writeheader()
        for row in rows:
            def list_item(value: Any) -> str:
                if isinstance(value, Mapping) and set(value) == {
                    "oracle_family_id",
                    "structure_id",
                }:
                    return f"{value['oracle_family_id']}/{value['structure_id']}"
                return str(value)

            flattened = {
                field: (
                    ";".join(list_item(item) for item in row[field])
                    if isinstance(row[field], list)
                    else row[field]
                )
                for field in fields
            }
            writer.writerow(flattened)
        atomic_write_text(output / name, buffer.getvalue())

    write_csv("candidate-screening.csv", screening_rows)
    write_csv("candidate-b1-correctness.csv", [b1_full_baseline, *b1_rows])
    write_csv("candidate-b2-tuning.csv", b2_rows)
    write_csv("candidate-suite-results.csv", suite_rows)
    write_csv("candidate-ranking.csv", ranking_rows)
    write_csv("candidate-oracle-capture.csv", capture_rows)
    write_csv("candidate-interactions.csv", interaction_rows)
    write_csv("candidate-toolchains.csv", toolchain_context)
    write_csv("candidate-cache-hotblock.csv", hotblock_rows)

    lines = [
        "# ACCELA 候选算法最终报告",
        "",
        "## 技术摘要",
        "",
        f"- Final ID：`{_markdown_cell(final['final_id'])}`。",
        f"- 结论：{('QEMU 代理下最佳候选 `' + _markdown_cell(final['winner_candidate_id']) + '`') if final['winner_candidate_id'] is not None else '**无优胜项**'}。",
        f"- 冠军榜固定覆盖 B3/B4/B5/B6 共 {final['expected_combined_case_count']} 个 case；B1 是正确性门，B2 只用于调优且不按收益淘汰。",
        "- 证据等级是 `qemu_proxy`，未取得 BOOM v3 硬件证据；因此结论不等同竞赛硬件加速，也不授权默认启用候选。",
        "",
        "## 口径、基线与证据边界",
        "",
        "- 动态指令 GM 定义为同 case 的 `FULL / FULL+candidate` 指令数比值的等权几何平均；大于 1 表示候选减少动态指令。95% CI 使用固定 seed `20260809` 的 10,000 次 bootstrap。",
        "- B3 基线是冻结 artifact 上的 candidate-empty FULL；B4/B5/B6 各自仍以同阶段 FULL 为直接基线。GCC/Clang、Top3 pair、cache/hotblock 都是诊断上下文，不进入冠军榜。",
        "- 本报告只在 candidate-final 已登记到 `completed` campaign 终态后生成；完整 status ledger 的 canonical/physical 身份链与 final 双哈希均已闭合。",
        f"- Terminal raw-evidence registry canonical / physical SHA-256：`{campaign_completion['raw_evidence_registry_sha256']}` / `{campaign_completion['raw_evidence_registry_physical_sha256']}`；registry 所列 run journal、attempt raw files 与 remarks 已由中央校验器重新回放。",
        f"- 筛选基线 PassRegistry canonical / physical SHA-256：`{frozen_screening_base_registry['canonical_sha256']}` / `{frozen_screening_base_registry['physical_sha256']}`；它描述实现前的 production pass 集。",
        f"- 可执行 PassRegistry canonical / physical SHA-256：`{frozen_executable_registry['canonical_sha256']}` / `{frozen_executable_registry['physical_sha256']}`；它在筛选后加入实现候选，因此与筛选基线 hash 预期不同。",
        "- Oracle 是 baseline/optimized 源码腿的结构级上界，不是候选 Pass 实测速率；捕获率定义为 `B3 GM / 最佳允许 Oracle GM`。",
        "- Tie-break 固定为 combined GM、B3 GM、较小的 B3-B6 candidate 静态 text bytes 合计、稳定候选 ID。",
        "",
        "## 冻结复现上下文",
        "",
        f"- Freeze / campaign：`{_markdown_cell(frozen_context['freeze_id'])}` / `{_markdown_cell(frozen_context['campaign_id'])}`；freeze canonical SHA-256 `{frozen_context['freeze_sha256']}`。",
        f"- Repository commit / tree：`{frozen_context['repository_commit']}` / `{frozen_context['repository_tree']}`。",
        f"- Compiler artifact physical SHA-256：`{frozen_context['compiler_artifact_physical_sha256']}`。",
        f"- Candidate-empty FULL profile：`{frozen_context['candidate_empty_profile_id']}`；canonical / physical SHA-256 `{frozen_context['candidate_empty_profile']['canonical_sha256']}` / `{frozen_context['candidate_empty_profile']['physical_sha256']}`。",
        f"- Standard protocol canonical / physical SHA-256：`{frozen_context['standard_measurement_protocol']['canonical_sha256']}` / `{frozen_context['standard_measurement_protocol']['physical_sha256']}`；hotblock protocol：`{frozen_context['hotblock_measurement_protocol']['canonical_sha256']}` / `{frozen_context['hotblock_measurement_protocol']['physical_sha256']}`。",
        f"- Reference toolchain snapshot canonical / physical SHA-256：`{frozen_context['reference_toolchain']['snapshot']['canonical_sha256']}` / `{frozen_context['reference_toolchain']['snapshot']['physical_sha256']}`；ACCELA JDK `{_markdown_cell(frozen_context['reference_toolchain']['accela_jdk_version'])}`。",
        "",
        "| 冻结公共工具 | 版本 |",
        "|---|---|",
    ]
    common_tool_versions = frozen_context["reference_toolchain"][
        "common_tool_versions"
    ]
    for tool in sorted(common_tool_versions):
        version = common_tool_versions[tool]
        lines.append(
            f"| `{_markdown_cell(tool)}` | `{_markdown_cell(version)}` |"
        )
    lines.extend(
        [
            "",
            "| 参考编译器 | Profile | 工具／版本 | 优化 | Profile SHA-256 | Command / argv SHA-256 |",
            "|---|---|---|---|---|---|",
        ]
    )
    for baseline in frozen_context["reference_toolchain"]["baselines"]:
        lines.append(
            f"| `{baseline['compiler_baseline']}` | `{baseline['profile_id']}` | "
            f"`{baseline['tool']}` / `{baseline['version']}` | `{baseline['optimization']}` | "
            f"`{baseline['profile_sha256']}` | `{baseline['compiler_command_sha256']}` / `{baseline['compiler_argv_sha256']}` |"
        )
    lines.extend(
        [
            "",
            "限制：本节只展开冻结、无路径的复现身份；它证明报告所消费对象与冻结合同一致，不证明本地主机或 BOOM 硬件行为等价。",
            "",
            "## 十一家族筛选",
            "",
            "| 家族 | 实现候选 | 结论 | 允许结构 | 达标结构 | 最佳 Oracle 上界 | 成本 | 风险 | 原因 |",
            "|---|---|---|---|---|---:|---|---|---|",
        ]
    )
    for row in screening_rows:
        lines.append(
            f"| {_markdown_cell(row['candidate_id'])} | {_markdown_cell(row['implementation_candidate_id'] or '—')} | "
            f"{row['qualification_status']} | {_screening_oracle_refs(row['eligible_oracle_structure_refs'])} | "
            f"{_screening_oracle_refs(row['qualifying_oracle_structure_refs'])} | "
            f"{_format_ratio(row['best_eligible_oracle_geometric_mean_upper_bound'])} | "
            f"{row['implementation_cost']} | {row['risk']} | {_screening_rejections(row['rejection_reasons'])} |"
        )
    lines.extend(
        [
            "",
            "> Oracle 是结构级动态指令上界，不是编译器 Pass 实测速率；被结构合同排除的样本仅作诊断。",
            "",
            "### Oracle 捕获率",
            "",
            "| 候选 | 最佳允许 Oracle GM | B3 实测 GM | 捕获率 |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in capture_rows:
        lines.append(
            f"| `{_markdown_cell(row['candidate_id'])}` | "
            f"{_format_ratio(row['oracle_upper_bound'])} | "
            f"{_format_ratio(row['b3_measured_speedup'])} | "
            f"{_format_number(None if row['oracle_capture_ratio'] is None else 100.0 * row['oracle_capture_ratio'], 2)}% |"
        )
    lines.extend(
        [
            "",
            "![Oracle 上界与捕获率](candidate-oracle-capture.svg)",
            "",
            "解读：图中并列给出允许结构的最佳 Oracle 上界和候选 Pass 的 B3 实测 GM；标签中的 capture 是两者之比。",
            "",
            "限制：Oracle 源码腿绕过了 matcher、proof 和变换成本，因此捕获率只描述上界实现程度，不是硬件效率，也不应超过证据精度作外推。",
            "",
            "## 267-case QEMU 代理冠军榜",
            "",
            "| 排名 | 候选 | Combined GM | B3 GM | 静态 text bytes | text 比值 | 实现代价 | 风险 |",
            "|---:|---|---:|---:|---:|---:|---|---|",
        ]
    )
    for row in ranking_rows:
        lines.append(
            f"| {row['rank']} | {_markdown_cell(row['candidate_id'])} | "
            f"{_format_ratio(row['combined_case_geometric_mean_speedup'])} | "
            f"{_format_ratio(row['b3_case_geometric_mean_speedup'])} | "
            f"{_format_number(row['combined_static_text_bytes_full_plus_candidate'], 0)} | "
            f"{_format_ratio(row['combined_static_text_ratio'])} | "
            f"{row['implementation_cost']} | {row['risk']} |"
        )
    if not ranking_rows:
        lines.append("| — | 无优胜项 | — | — | — | — | — | — |")
    lines.extend(
        [
            "",
            "排序固定为 combined GM、B3 GM、较小静态 text bytes、稳定候选 ID；诊断 pair、工具链和热点不入榜。",
            "",
            "![267-case Combined 排名](candidate-combined-ranking.svg)",
            "",
            "解读：Combined 图只展示满足 B3 晋级且 B4/B5/B6 完整可排名的候选，横轴以 `100×ln(GM)` 对 1.0 对称显示。",
            "",
            "限制：267-case 是四套 QEMU 动态指令证据的 case 等权组合，不代表真实工作负载频率，也不包含工具链或交互诊断。",
            "",
            "![收益、静态 text bytes 与风险 Pareto](candidate-pareto.svg)",
            "",
            "解读：Pareto 横轴是真实 B3-B6 `FULL+candidate` 静态 text bytes 合计（越小越好），纵轴是 Combined GM（越高越好），颜色按表中的风险类别编码；实现成本只保留在上表。",
            "",
            "限制：静态 text bytes 是代码规模代理，不是编译耗时或 BOOM 前端压力的完整模型；风险是筛选期类别判断，不是概率。",
            "",
            "## B1 正确性门",
            "",
            "| 候选 | 状态 | passed / failed / pending / censored | 全部正确 | 失败分类 |",
            "|---|---|---:|---|---|",
        ]
    )
    for row in [b1_full_baseline, *b1_rows]:
        lines.append(
            f"| `{_markdown_cell(row['candidate_id'])}` | "
            f"{_markdown_cell(row['state'] or 'not_run')} | "
            f"{row['passed_cases']} / {row['failed_cases']} / {row['pending_cases']} / {row['censored_cases']} | "
            f"{('是' if row['all_correct'] else '否')} | "
            f"{_markdown_cell(row['failure_classification'] or 'none')} |"
        )
    lines.extend(
        [
            "",
            "B1 先运行一次 candidate-empty FULL，再为每个已实现候选运行 140 个 `qemu_correctness` case；任何候选失败只淘汰该候选，不阻断其他候选。",
            "",
            "## B2 调优证据（不按收益淘汰）",
            "",
            "| 候选 | GM | 95% CI | 可比较 | correctness / censored / excluded | 分析资格／原因 |",
            "|---|---:|---:|---:|---:|---|",
        ]
    )
    for row in b2_rows:
        lines.append(
            f"| `{_markdown_cell(row['candidate_id'])}` | "
            f"{_format_ratio(row['case_geometric_mean_speedup'])} | "
            f"{_candidate_ci_cell(row)} | {row['comparable_cases']} | "
            f"{row['correctness_failures']} / {row['censored_cases']} / {row['excluded_cases']} | "
            f"{('可分析' if row['eligible_for_analysis'] else _markdown_cell(row['failure_classification'] or 'ineligible'))} |"
        )
    lines.extend(
        [
            "",
            "B2 仅用于调优最终实现；即使 GM 不高也不以收益淘汰，任何代码变更必须使用新 trial 身份，冻结后由最终 artifact 重跑正式证据。",
            "",
            "## B3-B6 分套件证据",
            "",
            "| 候选 | 阶段 | GM | 95% CI | 可比较 / 预期 | correctness / censored / excluded | 资格／失败分类 |",
            "|---|---|---:|---:|---:|---:|---|",
        ]
    )
    for row in suite_rows:
        lines.append(
            f"| `{_markdown_cell(row['candidate_id'])}` | {row['data_role']} | "
            f"{_format_ratio(row['case_geometric_mean_speedup'])} | "
            f"{_candidate_ci_cell(row)} | {row['comparable_cases']} / {row['expected_case_count']} | "
            f"{row['correctness_failures']} / {row['censored_cases']} / {row['excluded_cases']} | "
            f"{('可排名' if row['eligible_for_ranking'] else _markdown_cell(row['failure_classification'] or 'ineligible'))} |"
        )
    lines.extend(
        [
            "",
            "![候选分套件 GM](candidate-per-suite.svg)",
            "",
            "解读：分套件图保留 B3/B4/B5/B6 身份，展示每项完成且可排名证据的 GM；表中同时给出 95% CI 和精确失败计数。",
            "",
            "限制：未调度、失败、删失或缺少候选 observation 的阶段保持 n/a，不做零值或成功值插补。",
            "",
            "### B3 单项晋级视图",
            "",
            "![B3 单候选收益](candidate-single-gain.svg)",
            "",
            "解读：B3 单项图是晋级门视图，只有 GM 严格大于 1 且证据完整的候选才能进入 B4/B5/B6。",
            "",
            "限制：B3 只有 60 case，且仍是 QEMU 动态指令代理；图中的正收益不能替代其余 207 case 或硬件门。",
            "",
            "## Top3 交互诊断",
            "",
            "交互量固定为 `ΔlnGM = ln(S_AB) - ln(S_A) - ln(S_B)`；等价交互因子为 `exp(ΔlnGM)`。Top3 按 B3 单项 GM 选取，最多三个 pair，全部不入冠军榜。",
            "",
        ]
    )
    if interaction_rows:
        lines.extend(
            [
                "| 左候选 | 右候选 | Pair GM | 独立乘积 | ΔlnGM | 终态／资格／原因 |",
                "|---|---|---:|---:|---:|---|",
            ]
        )
        for item in interaction_rows:
            lines.append(
                f"| {_markdown_cell(item['left_candidate_id'])} | {_markdown_cell(item['right_candidate_id'])} | "
                f"{_format_ratio(item['pair_case_geometric_mean_speedup'])} | "
                f"{_format_ratio(item['expected_multiplicative_speedup'])} | "
                f"{_format_number(item['delta_ln_geometric_mean'], 6)} | "
                f"{item['state']} / "
                f"{_markdown_cell(item['terminal_failure_classification'] or 'none')} / "
                f"{('可解释' if item['eligible_for_interpretation'] else item['failure_classification'])} |"
            )
    else:
        lines.append("Top3 少于两项，没有可调度 pair；未合成交互值。")
    lines.extend(
        [
            "",
            "![Top3 交互热力图](candidate-interaction-heatmap.svg)",
            "",
            "解读：热力图单元格是 `100×ΔlnGM`；正值表示 pair 超过单项独立乘积，负值表示相互侵蚀。",
            "",
            "限制：交互只覆盖 B3 Top3 的至多三个 pair；失败或不完整 pair 标为 n/a，不能推断未运行组合或三项组合。",
        ]
    )
    lines.extend(["", "## GCC／Clang 上下文", ""])
    lines.extend(
        [
            "| 参考 | 终态／失败分类 | Reference/FULL GM (95% CI) | Reference/Winner GM (95% CI) | comparable | failed / pending / censored |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for item in toolchain_context:
        full_ci = (
            "n/a"
            if item["reference_over_full_confidence_interval_95_low"] is None
            else f"[{_format_number(item['reference_over_full_confidence_interval_95_low'])}, {_format_number(item['reference_over_full_confidence_interval_95_high'])}]"
        )
        winner_ci = (
            "n/a"
            if item["reference_over_winner_confidence_interval_95_low"] is None
            else f"[{_format_number(item['reference_over_winner_confidence_interval_95_low'])}, {_format_number(item['reference_over_winner_confidence_interval_95_high'])}]"
        )
        lines.append(
            f"| `{_markdown_cell(item['label'])}` | "
            f"{item['state']} / {_markdown_cell(item['failure_classification'] or 'none')} | "
            f"{_format_ratio(item['reference_over_full_geometric_mean'])} {full_ci} | "
            f"{_format_ratio(item['reference_over_winner_geometric_mean'])} {winner_ci} | "
            f"{item['comparable_cases']} | {item['failed_cases']} / "
            f"{item['pending_cases']} / {item['censored_cases']} |"
        )
    lines.extend(
        [
            "",
            "Reference/FULL 在无优胜项时仍完整报告；只有显式提供 hash-bound winner B3 run 时才填 Reference/Winner。比值大于 1 表示 ACCELA 对应运行使用更少动态指令。",
            "",
            "限制：GCC 13.3 `-O2` 与 Clang 18 `-O3` 是冻结 SysY adapter/C++17 合同下的参考上下文，优化级别不同，且不参与 ACCELA 候选排序。",
        ]
    )
    lines.extend(["", "## Cache／Hotblock 诊断", ""])
    lines.extend(
        [
            "| Profile | 终态／失败分类 | passed / total | samples | mean L1D misses / 1000 dynamic loads | mean hottest-block share |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for item in hotblock_rows:
        profile = (
            "FULL"
            if not item["enabled_candidate_ids"]
            else item["enabled_candidate_ids"][0]
        )
        lines.append(
            f"| `{_markdown_cell(profile)}` | {item['state']} / {_markdown_cell(item['failure_classification'] or 'none')} | "
            f"{item['passed_cases']} / {item['total_cases']} | "
            f"{item['sample_count']} | "
            f"{_format_number(item['mean_l1d_misses_per_1000_dynamic_loads'])} | "
            f"{_format_number(None if item['mean_hottest_block_dynamic_instruction_share'] is None else 100.0 * item['mean_hottest_block_dynamic_instruction_share'], 2)}% |"
        )
    lines.extend(
        [
            "",
            "![Cache 与 Hotblock 诊断](candidate-cache-hotblock.svg)",
            "",
            "解读：左右两面板分别显示 L1D misses/1000 dynamic loads 与最热基本块动态指令占比；只运行 FULL 和 B3 Top3 单项。",
            "",
            "限制：两指标单位和量纲不同，图中不共享数轴；它们解释指令收益可能来自哪里，但不进入冠军榜，也不是 BOOM cache 实测。",
        ]
    )
    lines.extend(["", "## r7 隔离诊断附录", ""])
    lines.extend(
        [
            f"- Freeze：`{_markdown_cell(r7['freeze_id'])}`。",
            f"- 已登记终态：{r7['completed_count']} completed / {r7['failed_count']} failed；未登记 partial：{r7['partial_passed_count']} passed / {r7['partial_pending_count']} pending。",
            f"- 已重新校验 freeze 枚举的 {r7['enumerated_bindings_rehashed']} 个 artifact binding；这些枚举 binding 的 hash 校验不等于整个 ignored evidence tree 的证明。",
            "- 明确限制：整个 ignored-tree 与迁移 source 的等价性仍未证明（`source_to_wsl_ignored_tree_full_hash_not_completed`）。",
            "- r7 永久不可恢复、不可补写、不可排名、不可导入新 campaign、不可晋级；本节只保留诊断历史。",
        ]
    )
    lines.extend(
        [
            "",
            "## BOOM v3 硬件验收建议",
            "",
            (
                "- 仅对本报告 winner 的冻结实现建立新的 `boom_hardware` 证据链，与同一代码、"
                "同一工具链下显式禁用该候选的 FULL 基线配对；不得将 QEMU 主机墙钟当成替代证据。"
                if final["winner_candidate_id"] is not None
                else "- 本轮无 winner，不生成默认启用草案；候选需以新 trial 身份完成改进、B1 和 B3–B6 后，才能进入 BOOM 验收。"
            ),
            "- 硬件运行必须继续绑定 source revision、pipeline profile、benchmark hash、LP64D/`medany` 产物、工具版本及输出正确性；任一身份漂移都应终止比较。",
            "- 在运行前固定样本数、频率／热状态控制、周期与 retired-instruction 主指标、cache／分支计数器及置信区间；超时、工具失败和不完整样本保持不可变终态。",
            "- 只有 BOOM 硬件收益、代码大小风险和完整正确性同时通过后，才复核默认 production 启用；启用后仍须按计划重跑 judge B1 与开发基线 B3。",
            "",
            "## 待回答问题",
            "",
            "- QEMU 动态指令 GM 能否转化为 BOOM v3 周期收益，以及转化比在四个 suite 间是否稳定？",
            "- 静态 text bytes 变化是否引入真实的前端取指／I-cache 代价，与 QEMU cache 模型的结论是否一致？",
            "- Top3 交互量与 hotblock 变化在硬件上是否可复现，或者只是代理模型的结构效应？",
            "",
            "## 结论边界",
            "",
            "若存在 winner，本报告最多称其为“QEMU 代理下最佳候选”；若没有正收益 winner，则结论明确为“无优胜项”。在完成 BOOM v3 硬件证据与独立人工验收前，不得默认启用任何候选。",
            "",
        ]
    )
    atomic_write_text(
        output / "FINAL_CANDIDATE_REPORT.zh-CN.md", "\n".join(lines)
    )

    atomic_write_text(
        output / "candidate-single-gain.svg",
        _svg_ratio_diverging(
            "B3 单候选动态指令 GM",
            [
                (
                    item["implementation_candidate_id"],
                    item["suite_outcomes"]["B3"]["case_geometric_mean_speedup"],
                )
                for item in b3_ranked
            ],
            evidence_note="仅正确性完整、无删失的 B3 单项；QEMU 代理。",
        ),
    )
    atomic_write_text(
        output / "candidate-per-suite.svg",
        _svg_ratio_diverging(
            "候选分套件动态指令 GM",
            [
                (f"{row['candidate_id']}:{row['data_role']}", row["case_geometric_mean_speedup"])
                for row in suite_rows
                if row["eligible_for_ranking"]
                and row["case_geometric_mean_speedup"] is not None
            ],
            evidence_note="B3/B4/B5/B6 分套件证据；失败、删失和未选择项不插补。",
        ),
    )
    atomic_write_text(
        output / "candidate-combined-ranking.svg",
        _svg_ratio_diverging(
            "267-case Combined GM",
            [
                (row["candidate_id"], row["combined_case_geometric_mean_speedup"])
                for row in ranking_rows
            ],
            evidence_note="全部 267 case 等权；固定四级 tie-break。",
        ),
    )
    interaction_ids = sorted(
        {
            candidate_id
            for item in interaction_rows
            for candidate_id in (
                item["left_candidate_id"],
                item["right_candidate_id"],
            )
        }
    )
    interaction_values = {
        (item["left_candidate_id"], item["right_candidate_id"]): math.exp(
            item["delta_ln_geometric_mean"]
        )
        for item in interaction_rows
        if item["eligible_for_interpretation"]
        and item["delta_ln_geometric_mean"] is not None
    }
    interaction_values.update(
        {(right, left): value for (left, right), value in list(interaction_values.items())}
    )
    atomic_write_text(
        output / "candidate-interaction-heatmap.svg",
        _svg_heatmap(
            "Top3 交互 100×ΔlnGM",
            interaction_ids,
            interaction_ids,
            interaction_values,
        ),
    )
    atomic_write_text(
        output / "candidate-oracle-capture.svg",
        _svg_ratio_diverging(
            "Oracle 上界与 B3 捕获",
            [
                (f"{row['candidate_id']}:oracle", row["oracle_upper_bound"])
                for row in capture_rows
                if row["oracle_upper_bound"] is not None
            ]
            + [
                (
                    f"{row['candidate_id']}:B3:capture="
                    + (
                        "n/a"
                        if row["oracle_capture_ratio"] is None
                        else f"{100.0 * row['oracle_capture_ratio']:.2f}%"
                    ),
                    row["b3_measured_speedup"],
                )
                for row in capture_rows
                if row["b3_measured_speedup"] is not None
            ],
            evidence_note="Oracle 是上界；B3 是实际 Pass 的 QEMU 代理结果。",
        ),
    )
    atomic_write_text(
        output / "candidate-cache-hotblock.svg",
        _svg_cache_hotblock(
            "B3 FULL 与 Top3 Cache／Hotblock 诊断",
            [
                (
                    "FULL"
                    if not row["enabled_candidate_ids"]
                    else row["enabled_candidate_ids"][0],
                    row["mean_l1d_misses_per_1000_dynamic_loads"],
                    (
                        None
                        if row["mean_hottest_block_dynamic_instruction_share"]
                        is None
                        else 100.0
                        * row["mean_hottest_block_dynamic_instruction_share"]
                    ),
                )
                for row in hotblock_rows
            ],
        ),
    )
    atomic_write_text(
        output / "candidate-pareto.svg",
        _svg_pareto(
            "候选收益—静态 text bytes—风险 Pareto",
            [
                (
                    row["candidate_id"],
                    float(
                        row[
                            "combined_static_text_bytes_full_plus_candidate"
                        ]
                    ),
                    float(row["combined_case_geometric_mean_speedup"]),
                    row["risk"],
                )
                for row in ranking_rows
            ],
        ),
    )
    return summary


def build_report(
    *,
    run_path: Path,
    output_directory: Path,
    baseline_path: Path | None = None,
    baseline_mode: str = "pipeline_ablation",
    comparison_paths: Mapping[str, Path] | None = None,
    oracle_plan_path: Path | None = None,
    candidate_evidence_path: Path | None = None,
    candidate_plan_paths: Sequence[Path] = (),
    candidate_run_paths: Sequence[Path] = (),
    remark_paths: Mapping[str, Path] | None = None,
    ablation_paths: Sequence[Path] = (),
    hotblock_run_paths: Mapping[str, Path] | None = None,
    bootstrap_samples: int = 10_000,
    seed: int = 20260809,
) -> dict[str, Any]:
    run = _load_version(run_path, "run-record.v1")
    baseline = _load_version(baseline_path, "run-record.v1") if baseline_path is not None else None
    oracle_plan = _load_version(oracle_plan_path, "oracle-plan.v1") if oracle_plan_path is not None else None
    candidate_evidence = (
        _load_version(candidate_evidence_path, "candidate-evidence.v1")
        if candidate_evidence_path is not None else None
    )
    if oracle_plan is not None and baseline is None:
        raise ValidationError("--oracle-plan requires the baseline source-leg run")
    supplied_plans = [_load_version(path, "oracle-plan.v1") for path in candidate_plan_paths]
    if oracle_plan is not None:
        supplied_plans.append(oracle_plan)
    oracle_plan_registry: dict[str, dict[str, Any]] = {}
    for item in supplied_plans:
        digest = sha256_json(item)
        previous = oracle_plan_registry.get(digest)
        if previous is not None and previous != item:
            raise ValidationError("candidate Oracle plan digest collision")
        oracle_plan_registry[digest] = item
    supplied_runs = [_load_version(path, "run-record.v1") for path in candidate_run_paths]
    supplied_runs.append(run)
    if baseline is not None:
        supplied_runs.append(baseline)
    candidate_run_registry: dict[str, dict[str, Any]] = {}
    for item in supplied_runs:
        previous = candidate_run_registry.get(item["run_id"])
        if previous is not None and sha256_json(previous) != sha256_json(item):
            raise ValidationError(f"conflicting candidate run records share run_id: {item['run_id']}")
        candidate_run_registry[item["run_id"]] = item
    comparison_runs = {
        label: _load_version(path, "run-record.v1")
        for label, path in sorted((comparison_paths or {}).items())
    }
    hotblock_runs = {
        label: _load_version(path, "run-record.v1")
        for label, path in sorted((hotblock_run_paths or {}).items())
    }
    for item in hotblock_runs.values():
        previous = candidate_run_registry.get(item["run_id"])
        if previous is not None and sha256_json(previous) != sha256_json(item):
            raise ValidationError(
                f"conflicting report inputs share run_id: {item['run_id']}"
            )
    hotblock_diagnostics = _hotblock_diagnostics(hotblock_runs)
    run_cases = {case["case_id"]: case for case in run["cases"]}
    pass_events: list[dict[str, Any]] = []
    for case_id, path in sorted((remark_paths or {}).items()):
        case = run_cases.get(case_id)
        if case is None:
            raise ValidationError(f"optimization remarks reference an unknown run case: {case_id}")
        if case["remarks_sha256"] is None or sha256_file(path.resolve(strict=True)) != case["remarks_sha256"]:
            raise ValidationError(f"optimization remarks content hash does not match run evidence: {case_id}")
        pass_events.extend(load_and_validate_jsonl(path))
    pass_remarks = _summarize_pass_events(pass_events)
    ablations = [_load_version(path, "ablation-study.v1") for path in ablation_paths]
    output_directory = output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)

    oracle_result = (
        _oracle_analysis(oracle_plan, baseline, run)
        if oracle_plan is not None and baseline is not None
        else {"pairs": [], "families": []}
    )
    if oracle_plan is not None:
        oracle_baseline_id = {
            item["optimized_case_id"]: item["baseline_case_id"] for item in oracle_result["pairs"]
        }
        raw_baseline_by_id = {case["case_id"]: case for case in baseline["cases"]} if baseline is not None else {}
        baseline_by_id = {
            optimized_id: raw_baseline_by_id[baseline_id]
            for optimized_id, baseline_id in oracle_baseline_id.items()
            if baseline_id in raw_baseline_by_id
        }
    else:
        baseline_by_id = {} if baseline is None else {case["case_id"]: case for case in baseline["cases"]}
    primary_metric_id = run["configuration"]["primary_metric_id"]
    if baseline is not None and oracle_plan is None:
        _require_formal_measurement(baseline)
        _require_formal_measurement(run)
        comparison = compare_runs(baseline, run, mode=baseline_mode)
        interval = bootstrap_geometric_mean_ci(comparison.pairs, samples=bootstrap_samples, seed=seed)
        comparison_summary: dict[str, Any] | None = {
            "baseline_run_id": baseline["run_id"],
            "comparable_cases": len(comparison.pairs),
            "correctness_failures": comparison.correctness_failures,
            "censored_cases": comparison.censored_cases,
            "excluded_cases": comparison.excluded_cases,
            "geometric_mean_speedup": comparison.geometric_mean_speedup,
            "source_group_geometric_mean_speedup": comparison.source_group_geometric_mean_speedup,
            "confidence_interval_95": None if interval is None else {"low": interval[0], "high": interval[1]},
            "families": family_geometric_means(comparison.pairs),
            "targets": target_geometric_means(comparison.pairs),
        }
        speedups = {pair.case_id: pair.speedup for pair in comparison.pairs}
    else:
        comparison_summary = None
        speedups = {
            item["optimized_case_id"]: item["speedup"]
            for item in oracle_result["pairs"] if item["speedup"] is not None
        }

    labeled_comparisons: list[dict[str, Any]] = []
    for label, reference in comparison_runs.items():
        _require_formal_measurement(reference)
        _require_formal_measurement(run)
        compared = compare_runs(reference, run, mode="cross_toolchain")
        labeled_comparisons.append(
            {
                "label": label,
                "run_id": reference["run_id"],
                "case_geometric_mean_speedup": compared.geometric_mean_speedup,
                "source_group_geometric_mean_speedup": compared.source_group_geometric_mean_speedup,
                "correctness_failures": compared.correctness_failures,
                "censored_cases": compared.censored_cases,
                "excluded_cases": compared.excluded_cases,
                "families": family_geometric_means(compared.pairs),
            }
        )

    case_rows: list[dict[str, Any]] = []
    for case in run["cases"]:
        value = case_metric(case, primary_metric_id)
        baseline_case = baseline_by_id.get(case["case_id"])
        baseline_value = (
            case_metric(baseline_case, primary_metric_id) if baseline_case is not None else None
        )
        unavailable = sorted(
            {
                f"{item['metric_id']}:{item['reason']}"
                for item in case["measurements"]
                if item["availability"] == "unavailable"
            }
        )
        row = {
                "run_id": run["run_id"],
                "case_id": case["case_id"],
                "family": case["family"],
                "source_group": case["source_group"],
                "target": case["target"],
                "weight": case["weight"],
                "status": case["status"],
                "cache_hit": case["cache_hit"],
                "sample_count": len(case["samples"]),
                "metric_value": value,
                "metric_unit": metric_spec(run)["unit"],
                "baseline_metric_value": baseline_value,
                "speedup": speedups.get(case["case_id"]),
                "diagnostic": case["diagnostic"],
                "unavailable_metrics": ",".join(unavailable),
            }
        for metric_id, metric_value in sorted(_case_measurement_values(case).items()):
            row[f"measurement.{metric_id}"] = metric_value
        case_rows.append(row)

    passed_by_source_group: dict[str, list[dict[str, Any]]] = {}
    for row in case_rows:
        if row["metric_value"] is not None:
            passed_by_source_group.setdefault(row["source_group"], []).append(row)
    source_group_metrics = [
        weighted_geometric_mean((row["metric_value"], row["weight"]) for row in rows)
        for _, rows in sorted(passed_by_source_group.items())
    ]
    benefit_groups = _optimization_rankings_by_suite(ablations)
    primary_benefit_group = _primary_optimization_group(benefit_groups, run)
    ranking = [] if primary_benefit_group is None else list(primary_benefit_group["ranking"])
    interaction_rows = (
        [] if primary_benefit_group is None else list(primary_benefit_group["interactions"])
    )
    oracle_ranking = oracle_result["families"]
    implementation_priority = _implementation_priorities(
        candidate_evidence,
        oracle_ranking,
        oracle_plans=oracle_plan_registry,
        runs=candidate_run_registry,
    )
    summary: dict[str, Any] = {
        "schema_version": "benchmark-report.v1",
        "generated_at": utc_now(),
        "run_id": run["run_id"],
        "suite_id": run["suite_id"],
        "state": run["state"],
        "environment_label": run["configuration"]["environment_label"],
        "provenance": run["provenance"],
        "tool_versions": run["configuration"]["tool_versions"],
        "primary_metric_id": primary_metric_id,
        "metrics": run["configuration"]["metrics"],
        "case_summary": run["summary"],
        "source_group_count": len({case["source_group"] for case in run["cases"]}),
        "geometric_mean_metric": (
            weighted_geometric_mean((value, 1.0) for value in source_group_metrics)
            if source_group_metrics
            else None
        ),
        "comparison": comparison_summary,
        "labeled_comparisons": labeled_comparisons,
        "oracle_pairs": oracle_result["pairs"],
        "optimization_ranking": ranking,
        "optimization_evidence_by_suite": benefit_groups,
        "ablation_interactions": interaction_rows,
        "rankings": {
            "benefit": ranking,
            "benefit_by_suite": benefit_groups,
            "oracle": oracle_ranking,
            "implementation_priority": implementation_priority,
        },
        "hotblock_diagnostics": hotblock_diagnostics,
        "pass_remarks": pass_remarks,
        "cases": case_rows,
    }
    atomic_write_json(output_directory / "summary.json", summary)

    buffer = io.StringIO(newline="")
    fieldnames = sorted({field for row in case_rows for field in row}) if case_rows else []
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    if case_rows:
        writer.writeheader()
        writer.writerows(case_rows)
    atomic_write_text(output_directory / "cases.csv", buffer.getvalue())

    if hotblock_runs:
        hotblock_buffer = io.StringIO(newline="")
        hotblock_fieldnames = [
            "label",
            "run_id",
            "suite_id",
            "manifest_sha256",
            "data_role",
            "pipeline_profile_id",
            "pipeline_profile_sha256",
            "measurement_protocol_id",
            "measurement_protocol_sha256",
            "case_id",
            "family",
            "source_group",
            "target",
            "sample_index",
            "dynamic_instruction_count",
            "hotblock_hottest_address",
            "hotblock_hottest_address_hex",
            "hotblock_hottest_executions",
            "hotblock_hottest_dynamic_instructions",
            "hotblock_dynamic_instruction_share",
        ]
        hotblock_writer = csv.DictWriter(
            hotblock_buffer, fieldnames=hotblock_fieldnames
        )
        hotblock_writer.writeheader()
        hotblock_writer.writerows(hotblock_diagnostics["samples"])
        atomic_write_text(
            output_directory / "hotblocks.csv", hotblock_buffer.getvalue()
        )
    else:
        (output_directory / "hotblocks.csv").unlink(missing_ok=True)

    lines = [
        "# ACCELA 基准分析报告",
        "",
        f"- 运行 ID：`{_markdown_cell(run['run_id'])}`",
        f"- 测试套件：`{_markdown_cell(run['suite_id'])}`",
        f"- 状态：`{run['state']}`",
        f"- 证据环境：`{run['configuration']['environment_label']}` / `{run['configuration']['evidence_level']}`",
        f"- 仓库提交：`{run['provenance']['repo_commit']}`（dirty：`{str(run['provenance']['repo_dirty']).lower()}`）",
        f"- 流水线 profile：`{run['provenance']['pipeline_profile_id']}` / `{run['provenance']['pipeline_profile_sha256']}`",
        f"- 编译器产物 SHA-256：`{run['provenance']['compiler_artifact_sha256']}`",
        f"- 通过／总数：{run['summary']['passed_cases']} / {run['summary']['total_cases']}",
        f"- 唯一源码哈希组：{summary['source_group_count']}",
        f"- 右删失超时用例：{run['summary']['censored_cases']}",
        f"- 10% 三次一致性检查：{run['summary']['consistency_passed_cases']} / {run['summary']['consistency_selected_cases']}",
        "",
    ]
    if run["configuration"]["environment_label"] != "official":
        lines.extend(
            [
                "> 本次结果仅属于参考／代理证据，不代表官方比赛环境，也不能据此宣称 BOOM 硬件加速收益。",
                "",
            ]
        )
    versions = run["configuration"]["tool_versions"]
    if versions:
        lines.extend(
            [
                "## 工具链证据",
                "",
                "| 工具 | 实测版本 | 官方期望 | 对比 |",
                "|---|---:|---:|---|",
            ]
        )
        for version in versions:
            lines.append(
                f"| {_markdown_cell(version['tool'])} | {_markdown_cell(version['actual'])} | "
                f"{_markdown_cell(version['official_expected'] or '未记录')} | {version['comparison']} |"
            )
        lines.append("")
    if comparison_summary is not None:
        lines.extend(
            [
                "## 成对对比",
                "",
                f"- 官方用例权重几何平均加速：{_format_ratio(comparison_summary['geometric_mean_speedup'])}",
                f"- 按源码哈希去重后的几何平均加速：{_format_ratio(comparison_summary['source_group_geometric_mean_speedup'])}",
                f"- 可比较用例：{comparison_summary['comparable_cases']}",
                f"- 正确性失败：{comparison_summary['correctness_failures']}",
                f"- 删失用例：{comparison_summary['censored_cases']}",
                "",
            ]
        )
    if labeled_comparisons:
        lines.extend([
            "## GCC／Clang 工具链差距",
            "",
            "仅完整 rv64gc-qemu-v1 代理协议进入此表；QEMU 主机墙钟时间不得作为差距排名。",
            "",
            "| 标签 | 参考运行 | ACCELA 运行 | 官方用例 GM | 源码去重 GM | 正确性失败 | 删失 |",
            "|---|---|---|---:|---:|---:|---:|",
        ])
        for item in labeled_comparisons:
            lines.append(
                f"| {_markdown_cell(item['label'])} | {_markdown_cell(item['run_id'])} | "
                f"{_markdown_cell(run['run_id'])} | {_format_ratio(item['case_geometric_mean_speedup'])} | "
                f"{_format_ratio(item['source_group_geometric_mean_speedup'])} | "
                f"{item['correctness_failures']} | {item['censored_cases']} |"
            )
        lines.append("")
    if pass_remarks:
        lines.extend(
            [
                "## 优化 Pass 事件",
                "",
                "| Pass | 汇总次数 | 实际修改 | 耗时 ns | 候选 | 应用 | 拒绝 |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for item in pass_remarks:
            lines.append(
                f"| {_markdown_cell(item['pass'])} | {item['pass_summaries']} | "
                f"{item['changed_invocations']} | {item['elapsed_ns']} | "
                f"{item['decisions']['candidate']} | {item['decisions']['applied']} | "
                f"{item['decisions']['rejected']} |"
            )
        lines.append("")
    if primary_benefit_group is not None:
        ordered_groups = [primary_benefit_group] + [
            group for group in benefit_groups if group is not primary_benefit_group
        ]
        for group_index, group in enumerate(ordered_groups):
            is_official = group["data_role"] == "B3"
            heading = (
                "## 已实现优化实测收益主榜（B3 官方代理）"
                if group_index == 0 and is_official
                else "## 已实现优化实测收益主榜（无 B3 时的代理套件）"
                if group_index == 0
                else f"### {group['data_role']} 独立消融证据"
            )
            if group_index == 1:
                lines.extend(["## 其他套件的独立消融证据", ""])
            lines.extend(
                [
                    heading,
                    "",
                    f"- suite：`{_markdown_cell(group['suite_id'])}`",
                    f"- data role：`{group['data_role']}`",
                    f"- manifest SHA-256：`{group['manifest_sha256']}`",
                    f"- study：{', '.join(f'`{_markdown_cell(item)}`' for item in group['study_ids'])}",
                    "",
                    "仅正确性完整、无删失且测量协议完整的 profile 在本套件内部排名；不同套件的同名优化不会合并或互相占用名次。",
                    "",
                    "| 排名 | 优化 | 用例权重 GM 贡献 | 源码去重 GM | 源码组 | 资格 | 正确性失败 | 删失 | 可比较 | FULL 运行 | without 运行 |",
                    "|---:|---|---:|---:|---:|---|---:|---:|---:|---|---|",
                ]
            )
            for row in group["ranking"]:
                lines.append(
                    f"| {row['rank'] if row['rank'] is not None else '—'} | {_markdown_cell(row['optimization_id'])} | "
                    f"{_format_ratio(row['case_geometric_mean_contribution'])} | "
                    f"{_format_ratio(row['source_group_geometric_mean_contribution'])} | "
                    f"{row['comparable_source_groups']} | "
                    f"{('可排名' if row['eligible_for_ranking'] else row['ineligibility_reason'])} | "
                    f"{row['correctness_failures']} | {row['censored_cases']} | {row['comparable_cases']} | "
                    f"{_markdown_cell(row['baseline_run_id'])} | {_markdown_cell(row['variant_run_id'])} |"
                )
            family_rows = [
                (row, family)
                for row in group["ranking"]
                for family in row["families"]
            ]
            if family_rows:
                lines.extend(
                    [
                        "",
                        "#### Family 分组证据",
                        "",
                        "| 优化 | Family | 可比较用例 | GM 贡献 | FULL 运行 | without 运行 |",
                        "|---|---|---:|---:|---|---|",
                    ]
                )
                for row, family in family_rows:
                    lines.append(
                        f"| {_markdown_cell(row['optimization_id'])} | {_markdown_cell(family['family'])} | "
                        f"{family['comparable_cases']} | {_format_ratio(family['geometric_mean_speedup'])} | "
                        f"{_markdown_cell(row['baseline_run_id'])} | {_markdown_cell(row['variant_run_id'])} |"
                    )
            lines.append("")
    interaction_groups = (
        ([primary_benefit_group] if primary_benefit_group is not None and primary_benefit_group["interactions"] else [])
        + [
            group for group in benefit_groups
            if group is not primary_benefit_group and group["interactions"]
        ]
    )
    for group in interaction_groups:
        lines.extend([
            (
                "## Top5 双消融交互（主榜套件）"
                if group is primary_benefit_group
                else f"## {group['data_role']} 双消融交互（独立证据）"
            ),
            "",
            f"suite：`{_markdown_cell(group['suite_id'])}`；manifest：`{group['manifest_sha256']}`。",
            "",
            "ΔlnGM = ln(双消融实测贡献 / 两个单消融贡献乘积)；正值仅表示协同候选，负值仅表示重叠／冗余候选，仍需结合热点与合法性事件判断，不能据此推断执行顺序。",
            "",
            "| 左家族 | 右家族 | ΔlnGM | 交互因子 | 资格／原因 | FULL 运行 | 双消融运行 |",
            "|---|---|---:|---:|---|---|---|",
        ])
        for item in group["interactions"]:
            lines.append(
                f"| {_markdown_cell(item['left'])} | {_markdown_cell(item['right'])} | "
                f"{_format_number(item['delta_ln_geometric_mean'], 6)} | "
                f"{_format_number(item['interaction_factor'], 6)} | "
                f"{('可解释' if item['eligible_for_ranking'] else item['ineligibility_reason'])} | "
                f"{_markdown_cell(item['baseline_run_id'])} | {_markdown_cell(item['run_id'])} |"
            )
        lines.append("")
    if oracle_ranking:
        lines.extend([
            "## 未实现候选 Oracle 上界榜",
            "",
            "Oracle 两条源码腿必须由同一 ACCELA 流水线编译；缺腿、失败或删失项不进入排名。",
            "",
            "| 排名 | 家族 | 成对数据集 | 几何平均上界 | 资格／原因 | 基线运行 | 优化运行 |",
            "|---:|---|---:|---:|---|---|---|",
        ])
        for row in oracle_ranking:
            lines.append(
                f"| {row['rank'] if row['rank'] is not None else '—'} | {_markdown_cell(row['family'])} | "
                f"{row['paired_datasets']} | {_format_ratio(row['geometric_mean_speedup'])} | "
                f"{('可排名' if row['eligible_for_ranking'] else row['ineligibility_reason'])} | "
                f"{_markdown_cell(row['baseline_run_id'])} | {_markdown_cell(row['optimized_run_id'])} |"
            )
        lines.append("")
    if implementation_priority:
        lines.extend([
            "## 实施优先级",
            "",
            "第三榜只使用可回链到 oracle-plan 与 run-record 的官方 Oracle、holdout／成熟用例证据；clean-room 上界单独展示，不冒充官方收益。P0/P1/P2/Blocked 使用显式门槛，不从已实现 Pass remarks 或手填收益推断。",
            "",
            "| 排名 | 优先级 | 候选 | clean-room 上界 | 官方 Oracle GM | 官方 family | Holdout/成熟 | 合法性证明路径 | 成本 | 风险 | 依据／阻塞原因 |",
            "|---:|---|---|---:|---:|---:|---:|---|---|---|---|",
        ])
        for item in implementation_priority:
            lines.append(
                f"| {item['rank'] if item['rank'] is not None else '—'} | {item['priority']} | "
                f"{_markdown_cell(item['candidate_id'])} | {_format_ratio(item['cleanroom_oracle_geometric_mean_upper_bound'])} | "
                f"{_format_ratio(item['official_oracle_geometric_mean'])} | {item['official_family_hits']} | "
                f"{item['holdout_or_mature_hits']} | {item['legality_proof_path']} | "
                f"{item['implementation_cost']} | {item['risk']} | {_markdown_cell(item['priority_reason'])} |"
            )
        lines.append("")
    if hotblock_runs:
        lines.extend(
            [
                "## 热点诊断（不参与收益排名）",
                "",
                "以下数据仅用于定位优化机会；不会进入已实现收益、Oracle 上界或实施优先级的任何几何平均与排序。每行对应一个已通过正确性校验的动态样本，缺失值不插补。",
                "",
            ]
        )
        rows_by_run = {
            item["run_id"]: [
                row
                for row in hotblock_diagnostics["samples"]
                if row["run_id"] == item["run_id"]
            ]
            for item in hotblock_diagnostics["runs"]
        }
        for item in hotblock_diagnostics["runs"]:
            lines.extend(
                [
                    f"### {_markdown_cell(item['label'])}",
                    "",
                    f"- 运行 ID：`{_markdown_cell(item['run_id'])}`",
                    f"- suite：`{_markdown_cell(item['suite_id'])}`",
                    f"- manifest SHA-256：`{item['manifest_sha256']}`",
                    f"- profile：`{_markdown_cell(item['pipeline_profile_id'])}` / `{item['pipeline_profile_sha256']}`",
                    f"- 测量协议：`{_markdown_cell(item['measurement_protocol_id'])}` / `{item['measurement_protocol_sha256']}`",
                    f"- 通过／总数：{item['passed_cases']} / {item['total_cases']}；失败：{item['failed_cases']}；删失：{item['censored_cases']}",
                    "",
                ]
            )
            run_sample_rows = rows_by_run[item["run_id"]]
            if not run_sample_rows:
                lines.extend(["该运行没有可展示的 passed sample；未生成或插补热点值。", ""])
                continue
            lines.extend(
                [
                    "| 数据角色 | 用例 | Family | 样本 | 最热块地址 | 执行次数 | 最热块动态指令 | 总动态指令 | 占比 |",
                    "|---|---|---|---:|---:|---:|---:|---:|---:|",
                ]
            )
            for row in run_sample_rows:
                lines.append(
                    f"| {row['data_role']} | {_markdown_cell(row['case_id'])} | "
                    f"{_markdown_cell(row['family'])} | {row['sample_index']} | "
                    f"`{row['hotblock_hottest_address_hex']}` | "
                    f"{row['hotblock_hottest_executions']} | "
                    f"{row['hotblock_hottest_dynamic_instructions']} | "
                    f"{row['dynamic_instruction_count']} | "
                    f"{_format_number(row['hotblock_dynamic_instruction_share'] * 100, 4)}% |"
                )
            lines.append("")
    lines.extend(
        [
            "> ELF 节大小与静态指令分类包含各工具链共用的 proxy runtime／启动代码；严格成对差异可比较，但绝对值不代表用户程序独占成本。",
            "",
            "## 用例明细",
            "",
            "| 用例 | 家族 | 目标 | 状态 | 指标 | 加速比 |",
            "|---|---|---|---|---:|---:|",
        ]
    )
    for row in case_rows:
        lines.append(
            f"| {_markdown_cell(row['case_id'])} | {_markdown_cell(row['family'])} | "
            f"{_markdown_cell(row['target'])} | {row['status']} | {_format_number(row['metric_value'])} | "
            f"{_format_number(row['speedup'])} |"
        )
    lines.extend(
        [
            "",
            "超时观测统一记录为右删失；计算几何平均时绝不以超时上界代替实际指标。",
            "",
        ]
    )
    atomic_write_text(output_directory / "report.md", "\n".join(lines))

    if ranking:
        chart_rows = [
            (row["optimization_id"], row["case_geometric_mean_contribution"])
            for row in ranking
            if row["eligible_for_ranking"] and row["case_geometric_mean_contribution"] is not None
        ]
        chart_title = "优化贡献几何平均（without/FULL）"
        ratio_chart = True
    elif comparison_summary is not None:
        chart_rows = [
            (item["family"], item["geometric_mean_speedup"])
            for item in comparison_summary["families"]
            if item["geometric_mean_speedup"] is not None
        ]
        chart_title = "家族几何平均加速比"
        ratio_chart = True
    else:
        chart_rows = [
            (row["case_id"], row["metric_value"])
            for row in case_rows
            if row["metric_value"] is not None
        ][:40]
        chart_title = "逐用例中位数指标"
        chart_unit = metric_spec(run)["unit"]
        ratio_chart = False
    atomic_write_text(
        output_directory / "speedups.svg",
        (
            _svg_ratio_diverging(
                chart_title,
                chart_rows,
                evidence_note=f"样本={len(chart_rows)}；正确性失败与右删失不插补。",
            )
            if ratio_chart
            else _svg_bars(chart_title, chart_rows, unit=chart_unit)
        ),
    )
    atomic_write_text(
        output_directory / "ablation-waterfall.svg",
        _svg_ratio_diverging(
            (
                "消融瀑布图：B3 官方代理用例权重贡献"
                if primary_benefit_group is not None
                and primary_benefit_group["data_role"] == "B3"
                else "消融瀑布图：主榜套件用例权重贡献"
            ),
            [
                (row["optimization_id"], row["case_geometric_mean_contribution"])
                for row in ranking
                if row["eligible_for_ranking"] and row["case_geometric_mean_contribution"] is not None
            ],
            evidence_note="100 × ln(without/FULL)；仅显示具有收益排名资格的 profile。",
        ),
    )
    primary_studies = (
        []
        if primary_benefit_group is None
        else [
            study for study in ablations
            if study["suite_id"] == primary_benefit_group["suite_id"]
            and study["data_role"] == primary_benefit_group["data_role"]
            and study["manifest_sha256"] == primary_benefit_group["manifest_sha256"]
        ]
    )
    heatmap_rows = sorted(
        {
            item["family"]
            for study in primary_studies
            for variant in study["variants"]
            for item in variant["families"]
        }
    )
    heatmap_columns = [row["optimization_id"] for row in ranking if row["eligible_for_ranking"]]
    heatmap_values: dict[tuple[str, str], float] = {}
    for study in primary_studies:
        for variant in study["variants"]:
            if not variant["eligible_for_ranking"]:
                continue
            for family in variant["families"]:
                value = family["geometric_mean_speedup"]
                if value is not None:
                    heatmap_values[(family["family"], variant["optimization_id"])] = value
    atomic_write_text(
        output_directory / "family-pass-heatmap.svg",
        _svg_heatmap("家族 × Pass 贡献热力图（中心=1.0/0%）", heatmap_rows, heatmap_columns, heatmap_values),
    )
    toolchain_rows = [
        (f"{comparison['label']}:{item['family']}", item["geometric_mean_speedup"])
        for comparison in labeled_comparisons
        for item in comparison["families"]
        if item["geometric_mean_speedup"] is not None
    ]
    atomic_write_text(
        output_directory / "toolchain-gap.svg",
        _svg_ratio_diverging(
            "ACCELA 与 GCC／Clang 差距（严格成对输入）",
            toolchain_rows,
            evidence_note=f"比较组={len(labeled_comparisons)}；比值=参考工具链/ACCELA，运行与指标路径锁定。",
        ),
    )
    tier_order = {"small": 0, "medium": 1, "large": 2}
    oracle_scaling_rows = [
        (f"{row['family']}:{row['tier'] or '未标注'}", row["speedup"])
        for row in sorted(
            oracle_result["pairs"],
            key=lambda item: (item["family"], tier_order.get(item["tier"], 99), item["pair_id"]),
        )
        if row["eligible_for_ranking"] and row["speedup"] is not None
    ]
    atomic_write_text(
        output_directory / "oracle-scaling.svg",
        _svg_ratio_diverging(
            "Oracle 复杂度分层：baseline/optimized",
            oracle_scaling_rows,
            evidence_note=f"有效配对={len(oracle_scaling_rows)}；按 small→medium→large 排列，缺腿/失败/删失不插补。",
        ),
    )
    atomic_write_text(
        output_directory / "benefit-cost-risk-pareto.svg",
        _svg_pareto(
            "收益—成本—风险 Pareto",
            [
                (
                    item["candidate_id"],
                    float(
                        item["official_oracle_geometric_mean"]
                        if item["official_oracle_geometric_mean"] is not None
                        else item["cleanroom_oracle_geometric_mean_upper_bound"]
                    ),
                    float({"low": 1, "medium": 2, "high": 3}[item["implementation_cost"]]),
                    float({"low": 0.2, "medium": 0.5, "high": 0.8}[item["risk"]]),
                )
                for item in implementation_priority
                if (
                    item["official_oracle_geometric_mean"] is not None
                    or item["cleanroom_oracle_geometric_mean_upper_bound"] is not None
                )
                and item["implementation_cost"] != "unknown"
                and item["risk"] != "unknown"
            ],
        ),
    )
    return summary
