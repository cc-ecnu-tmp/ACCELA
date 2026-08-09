from __future__ import annotations

import csv
import io
import math
import re
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence
from xml.sax.saxutils import escape as xml_escape

from .errors import ValidationError
from .ablation import _require_formal_measurement
from .campaign import _require_hotblock_evidence
from .schema import load_and_validate, load_and_validate_jsonl
from .stats import (
    bootstrap_geometric_mean_ci,
    case_metric,
    compare_runs,
    family_geometric_means,
    target_geometric_means,
    weighted_geometric_mean,
    metric_spec,
)
from .util import atomic_write_json, atomic_write_text, sha256_file, sha256_json, utc_now


def _load_version(path: Path, version: str) -> dict[str, Any]:
    document = load_and_validate(path)
    if document["schema_version"] != version:
        raise ValidationError(f"expected {version}, got {document['schema_version']}")
    return document


def _format_number(value: float | None, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


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
        if run["state"] != "completed" or run["summary"]["pending_cases"] != 0:
            raise ValidationError("cache-hotblock diagnostics require a completed run record")
        _require_formal_measurement(
            run, require_accela_pipeline=True, allow_metric_superset=True
        )
        _require_hotblock_evidence(run)
        provenance = run["provenance"]
        run_rows.append(
            {
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
        )
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
                if hottest_dynamic > dynamic_total:
                    raise ValidationError(
                        "cache-hotblock hottest dynamic instructions exceed the total dynamic instruction count"
                    )
                sample_rows.append(
                    {
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
                )
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
    width, left, right, top, row_height = 1040, 300, 70, 88, 36
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
            f'<text class="n" x="{width - right + 8}" y="{y + 18}">{ratio:.4f}x ({delta:+.2f})</text>',
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


def _svg_pareto(title: str, rows: Sequence[tuple[str, float, float, float]]) -> str:
    width, height = 900, 520
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fff"/>',
        '<style>text{font-family:system-ui,sans-serif;fill:#172033}.t{font-size:20px;font-weight:700}.l{font-size:11px}.a{stroke:#6d778c}</style>',
        f'<text class="t" x="20" y="30">{xml_escape(title)}</text>',
    ]
    valid = [row for row in rows if all(math.isfinite(value) and value >= 0 for value in row[1:])]
    if not valid:
        elements.append('<text class="l" x="20" y="72">无数据／收益、成本、风险证据尚未联合调度。</text>')
    else:
        left, top, plot_w, plot_h = 80, 70, 760, 380
        max_cost = max(row[2] for row in valid) or 1.0
        max_benefit = max(row[1] for row in valid) or 1.0
        elements.extend([
            f'<line class="a" x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}"/>',
            f'<line class="a" x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}"/>',
            f'<text class="l" x="{left + plot_w - 80}" y="{top + plot_h + 30}">成本（elapsed ns）</text>',
            f'<text class="l" x="10" y="{top + 10}">收益</text>',
        ])
        for label, benefit, cost, risk in valid:
            x = left + plot_w * cost / max_cost
            y = top + plot_h * (1 - benefit / max_benefit)
            radius = 5 + min(14, 14 * risk)
            elements.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{radius:.2f}" fill="#3977d4" fill-opacity="0.72"/>')
            elements.append(f'<text class="l" x="{x + radius + 3:.2f}" y="{y + 4:.2f}">{xml_escape(label[:24])}</text>')
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
                f"- 官方用例权重几何平均加速：{_format_number(comparison_summary['geometric_mean_speedup'])}x",
                f"- 按源码哈希去重后的几何平均加速：{_format_number(comparison_summary['source_group_geometric_mean_speedup'])}x",
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
                f"{_markdown_cell(run['run_id'])} | {_format_number(item['case_geometric_mean_speedup'])}x | "
                f"{_format_number(item['source_group_geometric_mean_speedup'])}x | "
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
                    f"{_format_number(row['case_geometric_mean_contribution'])}x | "
                    f"{_format_number(row['source_group_geometric_mean_contribution'])}x | "
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
                        f"{family['comparable_cases']} | {_format_number(family['geometric_mean_speedup'])}x | "
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
                f"{row['paired_datasets']} | {_format_number(row['geometric_mean_speedup'])}x | "
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
                f"{_markdown_cell(item['candidate_id'])} | {_format_number(item['cleanroom_oracle_geometric_mean_upper_bound'])}x | "
                f"{_format_number(item['official_oracle_geometric_mean'])}x | {item['official_family_hits']} | "
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
