from __future__ import annotations

import json
import math
import re
import statistics
from datetime import datetime
from importlib.resources import files
from itertools import combinations
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from .cache import compile_storage_contract
from .errors import ValidationError
from .metrics import ANALYZER_METRICS, UNAVAILABLE_REASONS, cache_hotblock_metrics_v1
from .util import (
    raw_attempt_identity_sha256,
    read_json,
    resolve_manifest_path,
    sha256_bytes,
    sha256_file,
    sha256_json,
    validate_relative_path,
)

_SCHEMA_FILES = {
    "benchmark-manifest.v1": "benchmark-manifest.v1.json",
    "run-record.v1": "run-record.v1.json",
    "optimization-remark.v1": "optimization-remark.v1.json",
    "optimization-remark.v2": "optimization-remark.v2.json",
    "ablation-study.v1": "ablation-study.v1.json",
    "binary-analysis.v1": "binary-analysis.v1.json",
    "pass-registry.v1": "pass-registry.v1.json",
    "pass-registry.v2": "pass-registry.v2.json",
    "ablation-matrix.v1": "ablation-matrix.v1.json",
    "oracle-plan.v1": "oracle-plan.v1.json",
    "cross-suite-audit.v1": "cross-suite-audit.v1.json",
    "campaign-plan.v1": "campaign-plan.v1.json",
    "campaign-status.v1": "campaign-status.v1.json",
    "candidate-evidence.v1": "candidate-evidence.v1.json",
    "candidate-catalog.v1": "candidate-catalog.v1.json",
    "candidate-profile-matrix.v1": "candidate-profile-matrix.v1.json",
    "candidate-study.v1": "candidate-study.v1.json",
    "candidate-oracle-capture.v1": "candidate-oracle-capture.v1.json",
    "candidate-campaign-plan.v1": "candidate-campaign-plan.v1.json",
    "candidate-campaign-status.v1": "candidate-campaign-status.v1.json",
    "candidate-raw-evidence.v1": "candidate-raw-evidence.v1.json",
    "candidate-screening-spec.v1": "candidate-screening-spec.v1.json",
    "candidate-screening.v1": "candidate-screening.v1.json",
    "candidate-freeze.v1": "candidate-freeze.v1.json",
    "candidate-final.v1": "candidate-final.v1.json",
    "candidate-report.v1": "candidate-report.v1.json",
    "measurement-protocol.v1": "measurement-protocol.v1.json",
    "pipeline-profile.v2": "pipeline-profile.v2.json",
}

_WINDOWS_LOCAL_PATH = re.compile(r"(?i)(?<![A-Za-z0-9])(?:[a-z]:[\\/]|\\\\[^\\/\s]+[\\/])")
_POSIX_LOCAL_PATH = re.compile(r"(?<![A-Za-z0-9/])/(?!/)")
_FILE_URI = re.compile(r"(?i)\bfile://")

_ATTEMPT_FAILURE_SUMMARIES = {
    "compile_error": "compiler_stage_failed",
    "link_error": "linker_stage_failed",
    "analyze_error": "analyzer_stage_failed",
    "runtime_error": "runtime_execution_failed",
    "wrong_output": "correctness_mismatch",
    "timeout": "runtime_timeout",
    "measurement_inconsistent": "deterministic_measurement_mismatch",
    "cancelled": "scheduler_cancelled",
}

# The candidate study is intentionally closed over these eleven conceptual
# families.  Keeping this contract in the semantic validator prevents a
# self-consistent but substituted screening spec from changing the research
# question or cherry-picking Oracle structures.
_LOCKED_CANDIDATE_SCREENING_CONTRACT = (
    (
        "bitset",
        "bitset",
        "bitset",
        "blocked",
        "blocked_locked_bitset_capability_gap",
        (),
    ),
    (
        "boom_ilp",
        "boom_ilp",
        "standard",
        "eligible",
        None,
        (("boom_ilp", "dot_unroll4"), ("boom_ilp", "reduction_multi_acc")),
    ),
    (
        "closed_form",
        "closed_form",
        "standard",
        "eligible",
        None,
        (
            ("closed_form", "linear_sum"),
            ("closed_form", "quadratic_sum"),
            ("closed_form", "triangular_recurrence"),
        ),
    ),
    (
        "dp_storage",
        "dp_storage",
        "standard",
        "eligible",
        None,
        (
            ("dp_storage", "reverse_single_row"),
            ("dp_storage", "three_row"),
            ("dp_storage", "two_row"),
        ),
    ),
    (
        "finite_state",
        "finite_state",
        "standard",
        "eligible",
        None,
        (
            ("finite_state", "affine_mod97"),
            ("finite_state", "branch_mod53"),
            ("finite_state", "quadratic_mod31"),
        ),
    ),
    (
        "fusion",
        "fusion",
        "standard",
        "eligible",
        None,
        (
            ("fusion", "single_temporary"),
            ("fusion", "stencil_producer"),
            ("fusion", "two_temporaries"),
            ("boom_ilp", "independent_chains"),
        ),
    ),
    (
        "linear_transition",
        "linear_transition",
        "standard",
        "eligible",
        None,
        (
            ("linear_transition", "affine_2d"),
            ("linear_transition", "affine_scalar"),
            ("linear_transition", "fibonacci_2d"),
        ),
    ),
    (
        "memoization",
        "memoization",
        "standard",
        "eligible",
        None,
        (("memoization", "binomial"), ("memoization", "grid_paths")),
    ),
    (
        "prefix_scan",
        "prefix_scan",
        "standard",
        "eligible",
        None,
        (
            ("prefix_scan", "forward_prefix"),
            ("prefix_scan", "reverse_suffix"),
            ("prefix_scan", "weighted_prefix"),
        ),
    ),
    (
        "recursion_worklist",
        "recursion_worklist",
        "mixed",
        "rejected",
        "mixed_family_no_unified_transform",
        (),
    ),
    (
        "structured_kernel",
        "structured_kernel",
        "mixed",
        "rejected",
        "mixed_family_no_unified_transform",
        (),
    ),
)

_LOCKED_CANDIDATE_IDS = tuple(
    item[0] for item in _LOCKED_CANDIDATE_SCREENING_CONTRACT
)
_LOCKED_CANDIDATE_FAMILIES = {
    item[0]: item[1] for item in _LOCKED_CANDIDATE_SCREENING_CONTRACT
}


def _reject_local_absolute_paths(value: Any, location: str = "$") -> None:
    """Reject path-bearing normalized records, including path-like logical IDs.

    Persisted benchmark evidence is intentionally portable and privacy-safe.
    Relative corpus paths are allowed; host, WSL, UNC, and file-URI paths are not.
    """

    if isinstance(value, dict):
        for key, item in value.items():
            _reject_local_absolute_paths(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_local_absolute_paths(item, f"{location}[{index}]")
    elif isinstance(value, str) and (
        _WINDOWS_LOCAL_PATH.search(value)
        or _POSIX_LOCAL_PATH.search(value)
        or _FILE_URI.search(value)
    ):
        raise ValidationError(f"{location}: local absolute paths are forbidden in persisted records")


def _load_schema(version: str) -> dict[str, Any]:
    if version not in _SCHEMA_FILES:
        raise ValidationError(f"unsupported schema_version: {version!r}")
    resource = files("tools.benchmark").joinpath("schemas", _SCHEMA_FILES[version])
    import json

    schema = json.loads(resource.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def schema_sha256(version: str) -> str:
    """Return the canonical physical digest of a bundled schema resource."""
    filename = _SCHEMA_FILES.get(version)
    if filename is None:
        raise ValidationError(f"unsupported schema_version: {version!r}")
    resource = files("tools.benchmark").joinpath("schemas", filename)
    return sha256_bytes(resource.read_bytes())


def _format_error(error: JsonSchemaValidationError) -> str:
    path = "$"
    for item in error.absolute_path:
        path += f"[{item}]" if isinstance(item, int) else f".{item}"
    return f"{path}: {error.message}"


def validate_document(document: Any, *, suite_root: Path | None = None, verify_files: bool = False) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValidationError("document root must be a JSON object")
    version = document.get("schema_version")
    if not isinstance(version, str):
        raise ValidationError("document must declare string schema_version")
    _reject_local_absolute_paths(document)
    validator = Draft202012Validator(_load_schema(version), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    if errors:
        rendered = "; ".join(_format_error(error) for error in errors[:10])
        if len(errors) > 10:
            rendered += f"; and {len(errors) - 10} more"
        raise ValidationError(rendered)

    if version == "benchmark-manifest.v1":
        _validate_manifest_semantics(document, suite_root=suite_root, verify_files=verify_files)
    elif version == "run-record.v1":
        _validate_run_semantics(document)
    elif version in {"optimization-remark.v1", "optimization-remark.v2"}:
        _validate_optimization_event_semantics(document)
    elif version == "ablation-study.v1":
        _validate_ablation_semantics(document)
    elif version == "binary-analysis.v1":
        _validate_binary_analysis_semantics(document)
    elif version in {"pass-registry.v1", "pass-registry.v2"}:
        _validate_pass_registry_semantics(document)
    elif version == "ablation-matrix.v1":
        _validate_ablation_matrix_semantics(document)
    elif version == "oracle-plan.v1":
        _validate_oracle_plan_semantics(document)
    elif version == "cross-suite-audit.v1":
        _validate_cross_suite_audit_semantics(document)
    elif version == "campaign-plan.v1":
        _validate_campaign_plan_semantics(document)
    elif version == "campaign-status.v1":
        _validate_campaign_status_semantics(document)
    elif version == "candidate-evidence.v1":
        _validate_candidate_evidence_semantics(document)
    elif version == "candidate-catalog.v1":
        _validate_candidate_catalog_semantics(document)
    elif version == "candidate-profile-matrix.v1":
        _validate_candidate_profile_matrix_semantics(document)
    elif version == "candidate-study.v1":
        _validate_candidate_study_semantics(document)
    elif version == "candidate-oracle-capture.v1":
        _validate_candidate_oracle_capture_semantics(document)
    elif version == "candidate-campaign-plan.v1":
        _validate_candidate_campaign_plan_semantics(document)
    elif version == "candidate-campaign-status.v1":
        _validate_candidate_campaign_status_semantics(document)
    elif version == "candidate-raw-evidence.v1":
        _validate_candidate_raw_evidence_semantics(document)
    elif version == "candidate-screening-spec.v1":
        _validate_candidate_screening_spec_semantics(document)
    elif version == "candidate-screening.v1":
        _validate_candidate_screening_semantics(document)
    elif version == "candidate-freeze.v1":
        _validate_candidate_freeze_semantics(document)
    elif version == "candidate-final.v1":
        _validate_candidate_final_semantics(document)
    elif version == "candidate-report.v1":
        _validate_candidate_report_semantics(document)
    return document


def load_and_validate(path: Path, *, suite_root: Path | None = None, verify_files: bool = False) -> dict[str, Any]:
    return validate_document(read_json(path), suite_root=suite_root, verify_files=verify_files)


def validate_pipeline_profile_v2(
    document: Any,
    *,
    candidate_order: list[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValidationError("PipelineProfile v2 root must be a JSON object")
    _reject_local_absolute_paths(document)
    validator = Draft202012Validator(_load_schema("pipeline-profile.v2"))
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    if errors:
        rendered = "; ".join(_format_error(error) for error in errors[:10])
        raise ValidationError(rendered)
    enabled = document["enable_candidates"]
    if candidate_order is not None:
        unknown = sorted(set(enabled) - set(candidate_order))
        if unknown:
            raise ValidationError(
                "PipelineProfile v2 enables unknown candidates: " + ", ".join(unknown)
            )
        canonical = [item for item in candidate_order if item in set(enabled)]
        if enabled != canonical:
            raise ValidationError(
                "PipelineProfile v2 candidate order differs from PassRegistry v2 candidates()"
            )
    return document


def load_pipeline_profile_v2(
    path: Path,
    *,
    candidate_order: list[str] | None = None,
) -> dict[str, Any]:
    return validate_pipeline_profile_v2(read_json(path), candidate_order=candidate_order)


def load_and_validate_jsonl(
    path: Path,
    *,
    required_version: str | None = None,
) -> list[dict[str, Any]]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON numeric constant: {value}")

    events: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    raise ValidationError(f"JSONL line {line_number} is blank")
                try:
                    value = json.loads(line, parse_constant=reject_constant)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise ValidationError(f"JSONL line {line_number} is not strict JSON: {exc}") from exc
                event = validate_document(value)
                if event["schema_version"] not in {
                    "optimization-remark.v1",
                    "optimization-remark.v2",
                }:
                    raise ValidationError(
                        f"JSONL line {line_number} is not an optimization remark"
                    )
                if required_version is not None and event["schema_version"] != required_version:
                    raise ValidationError(
                        f"JSONL line {line_number} must use {required_version}"
                    )
                events.append(event)
    except OSError as exc:
        raise ValidationError("cannot read optimization remark JSONL") from exc
    if not events:
        raise ValidationError("optimization remark JSONL is empty")
    for expected, event in enumerate(events, 1):
        if event["sequence"] != expected:
            raise ValidationError("optimization remark JSONL sequence must be contiguous and start at 1")
    versions = {event["schema_version"] for event in events}
    if len(versions) != 1:
        raise ValidationError("optimization remark JSONL cannot mix schema versions")
    return events


def validate_candidate_remark_jsonl(
    path: Path,
    *,
    catalog: Mapping[str, Any],
    pass_registry: Mapping[str, Any],
    enabled_candidate_ids: list[str],
    candidate_registry_sha256: str,
    pipeline_profile_id: str,
    pipeline_profile_sha256: str,
    require_candidate_observation: bool = False,
) -> dict[str, Any]:
    """Validate v2 candidate lifecycle pairs under an already-bound run profile.

    Registry/profile identities live in the immutable run configuration rather than
    every JSONL line.  Callers must verify that binding before invoking this helper;
    the parameters make that evidence chain explicit and reject malformed identities.
    """

    if catalog.get("schema_version") != "candidate-catalog.v1":
        raise ValidationError("candidate remark validation requires candidate-catalog.v1")
    if pass_registry.get("schema_version") != "pass-registry.v2":
        raise ValidationError("candidate remark validation requires pass-registry.v2")
    if candidate_registry_sha256 != sha256_json(catalog):
        raise ValidationError("candidate remark validation registry digest differs")
    if catalog["pass_registry_sha256"] != sha256_json(pass_registry):
        raise ValidationError("candidate remark validation pass registry digest differs")
    if not re.fullmatch(r"[0-9a-f]{64}", candidate_registry_sha256) or not re.fullmatch(
        r"[0-9a-f]{64}", pipeline_profile_sha256
    ):
        raise ValidationError("candidate remark validation requires registry/profile SHA-256")
    if not pipeline_profile_id:
        raise ValidationError("candidate remark validation requires a pipeline profile id")
    events = load_and_validate_jsonl(
        path, required_version="optimization-remark.v2"
    )
    catalog_candidates = {item["candidate_id"]: item for item in catalog["candidates"]}
    registered = {item["id"]: item for item in pass_registry["passes"]}
    canonical = [
        item["id"]
        for item in pass_registry["passes"]
        if item["lifecycle"] == "candidate" and item["id"] in catalog_candidates
    ]
    unknown_enabled = sorted(set(enabled_candidate_ids) - set(catalog_candidates))
    if unknown_enabled:
        raise ValidationError(
            "candidate remark validation enables unknown candidates: "
            + ", ".join(unknown_enabled)
        )
    expected_enabled = [item for item in canonical if item in set(enabled_candidate_ids)]
    if enabled_candidate_ids != expected_enabled:
        raise ValidationError(
            "candidate remark validation enablement differs from PassRegistry v2 order"
        )
    enabled = set(enabled_candidate_ids)
    summaries: dict[
        str, dict[tuple[int, str, str, str], Mapping[str, Any]]
    ] = {candidate_id: {} for candidate_id in enabled_candidate_ids}
    pending: dict[str, set[tuple[int, str, str, str]]] = {
        candidate_id: set() for candidate_id in enabled_candidate_ids
    }
    counts = {
        candidate_id: {"paired_candidate_count": 0, "applied_count": 0, "rejected_count": 0}
        for candidate_id in enabled_candidate_ids
    }
    for event in events:
        pass_id = event["pass"]
        descriptor = registered.get(pass_id)
        if descriptor is None:
            raise ValidationError("optimization remark references a pass absent from PassRegistry v2")
        if descriptor["lifecycle"] != "candidate":
            continue
        if pass_id not in catalog_candidates:
            raise ValidationError(
                "optimization remark references an unqualified candidate pass"
            )
        if pass_id not in enabled:
            raise ValidationError("optimization remark was emitted for a disabled candidate")
        candidate = catalog_candidates[pass_id]
        if event["stage"] != candidate["stage"] or event["occurrence"] != 1:
            raise ValidationError("candidate v2 remark stage/occurrence differs from registry")
        key = (
            event["occurrence"],
            event["stage"],
            event["target_kind"],
            event["target_name"],
        )
        if event["event_type"] == "pass_summary":
            if key in summaries[pass_id]:
                raise ValidationError(
                    "candidate v2 remark repeats a pass summary for the same target"
                )
            if event["decision_observability"] != "available":
                raise ValidationError("candidate v2 pass summary lacks decision observability")
            summaries[pass_id][key] = event
            continue
        decision = event["decision"]
        if decision == "candidate":
            if key in pending[pass_id]:
                raise ValidationError("candidate v2 remark repeats an open lifecycle match")
            pending[pass_id].add(key)
            continue
        if key not in pending[pass_id]:
            raise ValidationError("candidate v2 terminal remark has no matched predecessor")
        pending[pass_id].remove(key)
        counts[pass_id]["paired_candidate_count"] += 1
        if decision == "applied":
            counts[pass_id]["applied_count"] += 1
        else:
            counts[pass_id]["rejected_count"] += 1
            obligation = event["legality_obligation_id"]
            obligations = {
                item["obligation_id"] for item in candidate["legality_obligations"]
            }
            if obligation is not None and obligation not in obligations:
                raise ValidationError(
                    "candidate v2 remark references an undeclared legality obligation"
                )
    if any(pending.values()):
        raise ValidationError("candidate v2 remark contains unmatched candidate lifecycles")
    for candidate_id in enabled_candidate_ids:
        if not summaries[candidate_id]:
            raise ValidationError(
                "candidate v2 remarks require at least one summary per enabled candidate"
            )
        changed = any(
            summary["changed"] for summary in summaries[candidate_id].values()
        )
        applied = counts[candidate_id]["applied_count"]
        if changed != (applied > 0):
            raise ValidationError(
                "candidate v2 aggregate summary changed flag differs from applied decisions"
            )
        if require_candidate_observation and counts[candidate_id][
            "paired_candidate_count"
        ] == 0:
            raise ValidationError("enabled candidate has no matched lifecycle observation")
    return {
        "event_count": len(events),
        "paired_candidate_count": sum(
            item["paired_candidate_count"] for item in counts.values()
        ),
        "applied_count": sum(item["applied_count"] for item in counts.values()),
        "rejected_count": sum(item["rejected_count"] for item in counts.values()),
        "by_candidate": counts,
        "summary_count": sum(len(items) for items in summaries.values()),
    }


def _validate_manifest_semantics(document: dict[str, Any], *, suite_root: Path | None, verify_files: bool) -> None:
    provenance = document["provenance"]
    if (provenance["data_role"] == "B2") != (provenance["derived_from"] is not None):
        raise ValidationError("B2 provenance must, and only B2 provenance may, bind a parent manifest")
    if provenance["derived_from"] is not None and provenance["origin"]["snapshot_sha256"] != provenance["derived_from"]["manifest_sha256"]:
        raise ValidationError("derived B2 origin snapshot must equal its parent manifest SHA-256")
    case_ids: set[str] = set()
    for case in document["cases"]:
        case_id = case["id"]
        if case_id in case_ids:
            raise ValidationError(f"duplicate case id: {case_id}")
        case_ids.add(case_id)
        case_provenance = case["provenance"]
        for field in ("data_role", "origin", "license", "derived_from"):
            if case_provenance[field] != provenance[field]:
                raise ValidationError(f"case {case_id} provenance differs from manifest provenance: {field}")
        if case_provenance["validity"] != {"status": "included", "reason": "verified"}:
            raise ValidationError(f"included manifest case has invalid case-level validity: {case_id}")
        expected_source_group = f"sg-{case['source']['sha256']}"
        if case["source_group"] != expected_source_group:
            raise ValidationError(f"case {case_id} source_group must be derived from its source SHA-256")
        for field in ("source", "input", "expected_output"):
            reference = case[field]
            if reference is None:
                continue
            validate_relative_path(reference["path"], label=f"{case_id}.{field}.path")
            if verify_files:
                if suite_root is None:
                    raise ValidationError("suite_root is required when verify_files is enabled")
                path = resolve_manifest_path(suite_root, reference["path"])
                stat = path.stat()
                if stat.st_size != reference["size_bytes"]:
                    raise ValidationError(f"size mismatch for {case_id}.{field}")
                if sha256_file(path) != reference["sha256"]:
                    raise ValidationError(f"sha256 mismatch for {case_id}.{field}")
    by_case_id = {case["id"]: case for case in document["cases"]}
    paired_cases = [case for case in document["cases"] if "oracle_pair" in case]
    if paired_cases and document["provenance"]["data_role"] != "oracle":
        raise ValidationError("oracle_pair metadata is only valid for data_role=oracle")
    for case in paired_cases:
        pairing = case["oracle_pair"]
        counterpart = by_case_id.get(pairing["counterpart_case_id"])
        if counterpart is None:
            # Single-leg derivative manifests are valid for execution, while
            # oracle planning requires both legs and checks that separately.
            continue
        other = counterpart.get("oracle_pair")
        if (
            other is None
            or other["pair_id"] != pairing["pair_id"]
            or other["leg"] == pairing["leg"]
            or other["counterpart_case_id"] != case["id"]
        ):
            raise ValidationError(f"oracle pair metadata is not reciprocal: {case['id']}")
        if case["input"] != counterpart["input"] or case["expected_output"] != counterpart["expected_output"]:
            raise ValidationError(f"oracle pair legs do not share exact input/output bytes: {pairing['pair_id']}")
    quality = document["data_quality"]
    if quality["orphan_count"] != len(quality["orphan_sidecars"]):
        raise ValidationError("data_quality orphan_count does not match orphan_sidecars")
    if quality["duplicate_group_count"] != len(quality["duplicate_file_groups"]):
        raise ValidationError("data_quality duplicate_group_count does not match duplicate_file_groups")
    if quality["source_group_count"] != len(quality["source_groups"]):
        raise ValidationError("data_quality source_group_count does not match source_groups")
    for index, item in enumerate(quality["orphan_sidecars"]):
        validate_relative_path(item["logical_id"], label=f"data_quality.orphan_sidecars[{index}].logical_id")
        if item["role"] != "orphan" or item["suffix"] not in {".in", ".out"}:
            raise ValidationError("orphan sidecars must have orphan role and .in/.out suffix")
        validity = item["provenance"]["validity"]
        if validity != {"status": "excluded", "reason": "packaging_defect"}:
            raise ValidationError("orphan sidecars must be excluded as packaging_defect")
        if verify_files:
            if suite_root is None:
                raise ValidationError("suite_root is required when verify_files is enabled")
            orphan_path = resolve_manifest_path(suite_root, item["logical_id"] + item["suffix"])
            if orphan_path.stat().st_size != item["size_bytes"] or sha256_file(orphan_path) != item["sha256"]:
                raise ValidationError(f"orphan sidecar integrity mismatch: {item['logical_id']}{item['suffix']}")
    for group_index, group in enumerate(quality["duplicate_file_groups"]):
        seen_members: set[tuple[str, str, str]] = set()
        for member_index, member in enumerate(group["members"]):
            validate_relative_path(
                member["logical_id"],
                label=f"data_quality.duplicate_file_groups[{group_index}].members[{member_index}].logical_id",
            )
            key = (member["logical_id"], member["suffix"], member["role"])
            if key in seen_members:
                raise ValidationError("duplicate file group repeats a logical member")
            seen_members.add(key)
            if member["sha256"] != group["sha256"] or member["size_bytes"] != group["size_bytes"]:
                raise ValidationError("duplicate file group member digest/size does not match its group")
    cases_by_group: dict[str, list[dict[str, Any]]] = {}
    for case in document["cases"]:
        cases_by_group.setdefault(case["source_group"], []).append(case)
    declared_groups = {group["source_group"]: group for group in quality["source_groups"]}
    if declared_groups.keys() != cases_by_group.keys():
        raise ValidationError("data_quality source_groups do not cover the exact case source groups")
    for source_group, cases in cases_by_group.items():
        declared = declared_groups[source_group]
        expected_members = sorted(case["id"] for case in cases)
        if declared["members"] != expected_members or declared["case_count"] != len(cases):
            raise ValidationError(f"source group member/count mismatch: {source_group}")
        hashes = {case["source"]["sha256"] for case in cases}
        if hashes != {declared["source_sha256"]}:
            raise ValidationError(f"source group hash mismatch: {source_group}")
        if declared["families"] != sorted({case["family"] for case in cases}):
            raise ValidationError(f"source group family summary mismatch: {source_group}")
    expected_family_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for case in document["cases"]:
        expected_family_groups.setdefault((case["family"], case["target"]), []).append(case)
    declared_family_groups = {(group["family"], group["target"]): group for group in quality["family_groups"]}
    if declared_family_groups.keys() != expected_family_groups.keys():
        raise ValidationError("data_quality family_groups do not cover the exact case families")
    for key, cases in expected_family_groups.items():
        declared = declared_family_groups[key]
        if declared["case_count"] != len(cases) or declared["distinct_source_groups"] != len(
            {case["source_group"] for case in cases}
        ):
            raise ValidationError(f"family group summary mismatch: {key[0]}")


def _validate_compile_sample_evidence(
    sample: dict[str, Any],
    *,
    remarks_configured: bool,
    label: str,
) -> None:
    has_artifact = sample["artifact_sha256"] is not None
    if has_artifact != (sample["artifact_size_bytes"] is not None):
        raise ValidationError(f"{label} artifact hash/size evidence is inconsistent")
    has_remarks = sample["remarks_sha256"] is not None
    if has_remarks != (sample["remarks_event_count"] is not None):
        raise ValidationError(f"{label} optimization-remark hash/count evidence is inconsistent")
    if sample["status"] == "ok" and not has_artifact:
        raise ValidationError(f"{label} successful cold compile lacks artifact evidence")
    if sample["status"] == "ok" and remarks_configured and (
        not has_remarks or sample["remarks_event_count"] <= 0
    ):
        raise ValidationError(f"{label} successful cold compile lacks non-empty remark evidence")
    if not remarks_configured and has_remarks:
        raise ValidationError(f"{label} carries remarks without a configured remarks output")


def _validate_candidate_remark_summary_binding(
    summary: Mapping[str, Any] | None,
    *,
    remarks_event_count: int | None,
    enabled_candidate_ids: list[str],
    candidate_contract_bound: bool,
    label: str,
) -> None:
    should_have_summary = candidate_contract_bound and remarks_event_count is not None
    if (summary is not None) != should_have_summary:
        raise ValidationError(
            f"{label} candidate remark summary presence differs from its run contract"
        )
    if summary is None:
        return
    candidates = summary["candidates"]
    if [item["candidate_id"] for item in candidates] != enabled_candidate_ids:
        raise ValidationError(f"{label} candidate remark summary order differs")
    paired = sum(item["paired_candidate_count"] for item in candidates)
    applied = sum(item["applied_count"] for item in candidates)
    rejected = sum(item["rejected_count"] for item in candidates)
    if (
        summary["event_count"] != remarks_event_count
        or summary["paired_candidate_count"] != paired
        or summary["applied_count"] != applied
        or summary["rejected_count"] != rejected
        or applied + rejected != paired
        or any(
            item["applied_count"] + item["rejected_count"]
            != item["paired_candidate_count"]
            for item in candidates
        )
    ):
        raise ValidationError(f"{label} candidate remark summary totals differ")


def _attempt_has_execution_evidence(attempt: Mapping[str, Any]) -> bool:
    return bool(
        attempt["cache_hit"]
        or any(
            attempt.get(field) is not None
            for field in (
                "artifact_sha256",
                "binary_sha256",
                "remarks_sha256",
                "remarks_event_count",
                "candidate_remark_summary",
                "analysis_sha256",
                "compile",
                "compile_statistics",
                "link",
                "analyze",
            )
        )
        or attempt["compile_samples"]
        or attempt["measurements"]
        or attempt["samples"]
    )


def _typed_cancellation_observed(attempt: Mapping[str, Any]) -> bool:
    return any(
        phase is not None and phase["status"] == "cancelled"
        for phase in (attempt["compile"], attempt["link"], attempt["analyze"])
    ) or any(sample["status"] == "cancelled" for sample in attempt["compile_samples"]) or any(
        sample["status"] == "cancelled" for sample in attempt["samples"]
    )


def validate_attempt_cancellation_state(
    attempt: Mapping[str, Any],
    *,
    label: str,
) -> None:
    """Validate one normalized attempt's typed cancellation state machine.

    This semantic contract is intentionally shared by schema validation and the
    formal-ranking gate.  Journal validation independently proves the physical
    event sequence; normalized evidence must still describe exactly one logical
    terminal cancellation, with successful predecessors and no later stage.
    A cancelled compile aggregate and its final cancelled cold sample are one
    logical compile terminal because the aggregate is derived from that sample.
    """
    cancelled = attempt["status"] == "cancelled"
    reason = attempt["cancellation_reason"]
    if cancelled != (reason is not None):
        raise ValidationError(f"{label} cancellation status/reason binding is inconsistent")
    typed_observation = _typed_cancellation_observed(attempt)
    if not cancelled:
        if typed_observation:
            raise ValidationError(
                f"{label} non-cancelled attempt contains a typed cancelled stage"
            )
        return
    has_evidence = _attempt_has_execution_evidence(attempt)
    if reason == "execution_interrupted" and has_evidence:
        raise ValidationError(
            f"{label} pre-commit interruption cannot carry normalized execution evidence"
        )
    if reason == "execution_interrupted":
        return

    compile_phase = attempt["compile"]
    compile_samples = attempt["compile_samples"]
    link_phase = attempt["link"]
    analyze_phase = attempt["analyze"]
    run_samples = attempt["samples"]

    if reason == "infrastructure_failure":
        if typed_observation:
            raise ValidationError(
                f"{label} infrastructure failure cannot masquerade as a typed scheduler cancellation"
            )
        phase_statuses = {
            phase["status"]
            for phase in (attempt["compile"], attempt["link"], attempt["analyze"])
            if phase is not None
        }
        compile_statuses = {sample["status"] for sample in attempt["compile_samples"]}
        sample_statuses = {sample["status"] for sample in attempt["samples"]}
        if (
            phase_statuses - {"ok"}
            or compile_statuses - {"ok"}
            or sample_statuses - {"passed"}
        ):
            raise ValidationError(
                f"{label} infrastructure failure prefix contains a real stage failure"
            )
        if compile_phase is None and has_evidence:
            raise ValidationError(
                f"{label} infrastructure failure prefix has evidence without a committed compile stage"
            )
        if not attempt["diagnostic"]:
            raise ValidationError(
                f"{label} infrastructure failure lacks its original diagnostic"
            )
        return

    if reason != "scheduler_cancelled":
        raise ValidationError(f"{label} has an unknown cancellation reason")

    phase_statuses = {
        phase["status"]
        for phase in (compile_phase, link_phase, analyze_phase)
        if phase is not None
    }
    compile_statuses = [sample["status"] for sample in compile_samples]
    run_statuses = [sample["status"] for sample in run_samples]
    if (
        phase_statuses - {"ok", "cancelled"}
        or set(compile_statuses) - {"ok", "cancelled"}
        or set(run_statuses) - {"passed", "cancelled"}
    ):
        raise ValidationError(
            f"{label} scheduler cancellation contains a real stage failure"
        )

    if compile_phase is None:
        if has_evidence:
            raise ValidationError(
                f"{label} scheduler cancellation has evidence without a committed compile stage"
            )
        return

    compile_status = compile_phase["status"]
    if compile_status == "cancelled":
        if (
            not compile_statuses
            or compile_statuses[-1] != "cancelled"
            or any(status != "ok" for status in compile_statuses[:-1])
        ):
            raise ValidationError(
                f"{label} cancelled compile aggregate differs from its terminal cold sample"
            )
        if (
            link_phase is not None
            or analyze_phase is not None
            or run_samples
            or attempt["binary_sha256"] is not None
            or attempt["analysis_sha256"] is not None
        ):
            raise ValidationError(
                f"{label} scheduler cancellation has evidence after its compile terminal"
            )
        return

    if compile_status != "ok":
        raise ValidationError(
            f"{label} scheduler cancellation contains a real compile failure"
        )
    if not compile_statuses or any(status != "ok" for status in compile_statuses):
        raise ValidationError(
            f"{label} scheduler cancellation lacks an all-successful cold-compile prefix"
        )

    if link_phase is not None:
        if link_phase["status"] == "cancelled":
            if (
                analyze_phase is not None
                or run_samples
                or attempt["binary_sha256"] is not None
                or attempt["analysis_sha256"] is not None
            ):
                raise ValidationError(
                    f"{label} scheduler cancellation has evidence after its link terminal"
                )
            return
        if link_phase["status"] != "ok":
            raise ValidationError(
                f"{label} scheduler cancellation contains a real link failure"
            )

    if analyze_phase is not None:
        if analyze_phase["status"] == "cancelled":
            if run_samples or attempt["analysis_sha256"] is not None:
                raise ValidationError(
                    f"{label} scheduler cancellation has evidence after its analyzer terminal"
                )
            return
        if analyze_phase["status"] != "ok":
            raise ValidationError(
                f"{label} scheduler cancellation contains a real analyzer failure"
            )

    if run_samples:
        if run_statuses[-1] != "cancelled" or any(
            status != "passed" for status in run_statuses[:-1]
        ):
            raise ValidationError(
                f"{label} scheduler cancellation runtime terminal is not the unique final sample"
            )
        return

    if has_evidence:
        raise ValidationError(
            f"{label} scheduler cancellation with execution evidence lacks a typed terminal stage"
        )


def _validate_run_semantics(document: dict[str, Any]) -> None:
    if document["configuration_sha256"] != sha256_json(
        {"configuration": document["configuration"], "provenance": document["provenance"]}
    ):
        raise ValidationError("run configuration_sha256 does not cover configuration and provenance")
    provenance = document["provenance"]
    if not provenance["repo_dirty"] and provenance["tracked_diff_sha256"] is not None:
        raise ValidationError("clean repository provenance cannot contain a tracked diff digest")
    has_protocol_id = provenance["measurement_protocol_id"] is not None
    has_protocol_hash = provenance["measurement_protocol_sha256"] is not None
    if has_protocol_id != has_protocol_hash:
        raise ValidationError("measurement protocol id/hash must be present together")
    case_ids = [case["case_id"] for case in document["cases"]]
    if len(case_ids) != len(set(case_ids)):
        raise ValidationError("run record contains duplicate case ids")
    if document["manifest_case_count"] != len(case_ids):
        raise ValidationError("run case count differs from its manifest commitment")
    if document["manifest_case_ids_sha256"] != sha256_json(case_ids):
        raise ValidationError("run case sequence differs from its manifest commitment")
    summary = document["summary"]
    if summary["total_cases"] != len(case_ids):
        raise ValidationError("run summary total_cases does not match case records")
    classified = summary["passed_cases"] + summary["failed_cases"] + summary["pending_cases"]
    if classified != summary["total_cases"]:
        raise ValidationError("run summary counts do not add up")
    censored = sum(case["status"] == "timeout" for case in document["cases"])
    if summary["censored_cases"] != censored:
        raise ValidationError("run summary censored_cases does not match timeout cases")
    actual_passed = sum(case["status"] == "passed" for case in document["cases"])
    actual_pending = sum(case["status"] == "pending" for case in document["cases"])
    if summary["passed_cases"] != actual_passed or summary["pending_cases"] != actual_pending:
        raise ValidationError("run summary status counts do not match case records")
    state = document["state"]
    completed_at = document["completed_at"]
    if state == "completed" and (
        completed_at is None
        or summary["passed_cases"] != summary["total_cases"]
        or summary["failed_cases"] != 0
        or summary["pending_cases"] != 0
    ):
        raise ValidationError("completed run requires all cases passed and a completion timestamp")
    if state == "failed" and (
        completed_at is None or summary["failed_cases"] == 0 or summary["pending_cases"] != 0
    ):
        raise ValidationError("failed run requires failures, no pending cases, and a completion timestamp")
    if state in {"running", "interrupted"} and completed_at is not None:
        raise ValidationError("running/interrupted run must not contain a completion timestamp")
    configuration = document["configuration"]
    if configuration["compile_storage_contract"] != compile_storage_contract(
        configuration["reuse_compile_cache"]
    ):
        raise ValidationError(
            "compile storage contract disagrees with reuse_compile_cache"
        )
    specs = configuration["metrics"]
    metric_ids = [spec["metric_id"] for spec in specs]
    if len(metric_ids) != len(set(metric_ids)):
        raise ValidationError("run configuration contains duplicate metric ids")
    if configuration["primary_metric_id"] not in set(metric_ids):
        raise ValidationError("primary_metric_id is not declared in configuration.metrics")
    by_metric = {spec["metric_id"]: spec for spec in specs}
    regex_sources = {"stdout", "stderr", "file", "compile_stdout", "compile_stderr", "link_stdout", "link_stderr"}
    for spec in specs:
        if (spec["source"] in regex_sources) != (spec["pattern_sha256"] is not None):
            raise ValidationError(f"metric {spec['metric_id']} has inconsistent pattern/source configuration")
    primary = by_metric[configuration["primary_metric_id"]]
    if primary["source"] not in {"wall_time", "stdout", "stderr", "file"}:
        raise ValidationError("primary metric must be collected for every run sample")
    uses_metric_file = any(spec["source"] == "file" for spec in specs)
    if uses_metric_file != (configuration["metric_file_sha256"] is not None):
        raise ValidationError("metric_file_sha256 must be present exactly when file metrics are configured")
    uses_analyzer = any(spec["source"] == "analyzer" for spec in specs)
    if uses_analyzer != (configuration["analyzer"] is not None):
        raise ValidationError("analyzer stage must be present exactly when analyzer metrics are configured")
    if uses_analyzer != (configuration["analysis_file_sha256"] is not None):
        raise ValidationError("analysis_file_sha256 must be present exactly when analyzer metrics are configured")
    uses_result_file = configuration["output_contract"] == "result_file"
    if uses_result_file != (configuration["result_file_sha256"] is not None):
        raise ValidationError("result_file_sha256 must be present exactly for the result_file contract")
    uses_baseline_timeout = configuration["timeout_policy"] == "baseline_derived"
    if uses_baseline_timeout != (configuration["baseline_timeout_run_sha256"] is not None):
        raise ValidationError("baseline timeout digest must be present exactly for baseline_derived policy")
    if uses_baseline_timeout != (configuration["baseline_timeout_run_id"] is not None):
        raise ValidationError("baseline timeout run id must be present exactly for baseline_derived policy")
    if configuration["timeout_minimum_seconds"] > configuration["timeout_cap_seconds"]:
        raise ValidationError("timeout minimum exceeds timeout cap")
    if configuration["timeout_policy"] in {"initial", "baseline_derived"} and any(
        not math.isclose(configuration[field], expected, rel_tol=0, abs_tol=1e-12)
        for field, expected in (
            ("run_timeout_seconds", 1800.0),
            ("timeout_minimum_seconds", 120.0),
            ("timeout_multiplier", 3.0),
            ("timeout_cap_seconds", 1800.0),
        )
    ):
        raise ValidationError(
            "initial/baseline-derived timeout policy must use run=1800, minimum=120, multiplier=3, cap=1800"
        )
    profile_file_sha256 = configuration.get("pipeline_profile_file_sha256")
    if profile_file_sha256 is not None and profile_file_sha256 != provenance["pipeline_profile_sha256"]:
        raise ValidationError("pipeline profile file digest differs from provenance")
    candidate_registry_sha256 = configuration.get("candidate_registry_sha256")
    candidate_pass_registry_sha256 = configuration.get(
        "candidate_pass_registry_sha256"
    )
    enabled_candidate_ids = configuration.get("enabled_candidate_ids", [])
    if (candidate_registry_sha256 is None) != (
        candidate_pass_registry_sha256 is None
    ):
        raise ValidationError(
            "candidate catalog and PassRegistry v2 digests must be present together"
        )
    if candidate_registry_sha256 is None and enabled_candidate_ids:
        raise ValidationError("enabled candidates require a bound candidate registry")
    if configuration["environment_label"] == "official" and configuration["evidence_level"] != "boom_hardware":
        raise ValidationError("official performance evidence must use boom_hardware level")
    if configuration["evidence_level"] == "boom_hardware" and (
        configuration["environment_label"] != "official" or configuration["runner"]["kind"] != "boom"
    ):
        raise ValidationError("boom_hardware evidence requires official environment and BOOM runner")
    if configuration["evidence_level"] in {"qemu_correctness", "qemu_proxy"} and configuration["runner"]["kind"] != "qemu":
        raise ValidationError("QEMU evidence requires a QEMU runner")
    if configuration["evidence_level"] == "qemu_proxy" and not has_protocol_id:
        raise ValidationError("qemu_proxy evidence requires a versioned measurement protocol snapshot")
    if configuration["evidence_level"] != "compile_only" and configuration["output_contract"] == "raw_stdout":
        raise ValidationError("correctness/performance evidence must validate main return uint8")
    expected_selected = math.ceil(len(document["cases"]) * configuration["consistency_fraction"])
    selected = sum(case["consistency_selected"] for case in document["cases"])
    passed_consistency = sum(case["consistency_passed"] is True for case in document["cases"])
    if selected != expected_selected:
        raise ValidationError("consistency-selected case count is not exactly 10 percent rounded up")
    expected_selected_ids = {
        case["case_id"]
        for case in sorted(
            document["cases"],
            key=lambda case: sha256_json(
                {"seed": configuration["seed"], "case_id": case["case_id"]}
            ),
        )[:expected_selected]
    }
    actual_selected_ids = {
        case["case_id"] for case in document["cases"] if case["consistency_selected"]
    }
    if actual_selected_ids != expected_selected_ids:
        raise ValidationError("consistency-selected cases do not match the fixed seed protocol")
    if summary["consistency_selected_cases"] != selected:
        raise ValidationError("summary consistency_selected_cases does not match cases")
    if summary["consistency_passed_cases"] != passed_consistency:
        raise ValidationError("summary consistency_passed_cases does not match cases")
    remarks_configured = configuration["remarks_file_sha256"] is not None
    for case in document["cases"]:
        if [attempt["attempt_index"] for attempt in case["attempts"]] != list(range(len(case["attempts"]))):
            raise ValidationError(f"case {case['case_id']} attempt indexes are not contiguous")
        if case["attempt_index"] != len(case["attempts"]):
            raise ValidationError(f"case {case['case_id']} current attempt index is not contiguous")
        has_attempt_start = case["attempt_started_at"] is not None
        has_attempt_configuration = case["attempt_configuration_sha256"] is not None
        if has_attempt_start != has_attempt_configuration:
            raise ValidationError(
                f"case {case['case_id']} attempt start/configuration binding is inconsistent"
            )
        if has_attempt_configuration and case["attempt_configuration_sha256"] != document[
            "configuration_sha256"
        ]:
            raise ValidationError(
                f"case {case['case_id']} current attempt configuration differs from the run"
            )
        has_journal_sha256 = case["attempt_journal_sha256"] is not None
        has_journal_count = case["attempt_journal_event_count"] is not None
        if has_journal_sha256 != has_journal_count:
            raise ValidationError(
                f"case {case['case_id']} journal hash/count binding is inconsistent"
            )
        if not has_attempt_start and has_journal_sha256:
            raise ValidationError(f"case {case['case_id']} unstarted attempt carries a journal")
        if case["status"] == "pending" and has_journal_sha256:
            raise ValidationError(f"case {case['case_id']} pending attempt carries a terminal journal")
        if case["status"] not in {"pending", "cancelled"} and not has_journal_sha256:
            raise ValidationError(
                f"case {case['case_id']} terminal attempt lacks its journal commitment"
            )
        if case["status"] == "cancelled" and has_attempt_start and not has_journal_sha256:
            raise ValidationError(
                f"case {case['case_id']} started cancellation lacks its journal commitment"
            )
        if case["status"] not in {"pending", "cancelled"} and not has_attempt_start:
            raise ValidationError(
                f"case {case['case_id']} completed attempt lacks its start/configuration binding"
            )
        if not has_attempt_start and (
            case["cache_hit"]
            or any(
                case.get(field) is not None
                for field in (
                    "artifact_sha256",
                    "binary_sha256",
                    "remarks_sha256",
                    "remarks_event_count",
                    "candidate_remark_summary",
                    "analysis_sha256",
                    "attempt_journal_sha256",
                    "attempt_journal_event_count",
                    "compile",
                    "compile_statistics",
                    "link",
                    "analyze",
                )
            )
            or case["compile_samples"]
            or case["measurements"]
            or case["samples"]
        ):
            raise ValidationError(
                f"case {case['case_id']} unstarted attempt carries execution evidence"
            )
        for attempt in case["attempts"]:
            if attempt["configuration_sha256"] != document["configuration_sha256"]:
                raise ValidationError(
                    f"case {case['case_id']} archived attempt configuration differs from the run"
                )
            if attempt["failure_summary"] != _ATTEMPT_FAILURE_SUMMARIES[attempt["status"]]:
                raise ValidationError(
                    f"case {case['case_id']} archived attempt summary does not match its failure status"
                )
            expected_raw_identity_sha256 = raw_attempt_identity_sha256(
                run_id=document["run_id"],
                manifest_sha256=document["manifest_sha256"],
                case_id=case["case_id"],
                attempt_index=attempt["attempt_index"],
                started_at=attempt["started_at"],
                configuration_sha256=attempt["configuration_sha256"],
            )
            if attempt["raw_attempt_identity_sha256"] != expected_raw_identity_sha256:
                raise ValidationError(
                    f"case {case['case_id']} attempt {attempt['attempt_index']} raw identity digest differs"
                )
            for index, sample in enumerate(attempt["compile_samples"]):
                _validate_compile_sample_evidence(
                    sample,
                    remarks_configured=remarks_configured,
                    label=(
                        f"case {case['case_id']} attempt {attempt['attempt_index']} "
                        f"cold sample {index}"
                    ),
                )
            _validate_candidate_remark_summary_binding(
                attempt.get("candidate_remark_summary"),
                remarks_event_count=attempt["remarks_event_count"],
                enabled_candidate_ids=enabled_candidate_ids,
                candidate_contract_bound=candidate_registry_sha256 is not None,
                label=(
                    f"case {case['case_id']} attempt {attempt['attempt_index']}"
                ),
            )
            validate_attempt_cancellation_state(
                attempt,
                label=f"case {case['case_id']} attempt {attempt['attempt_index']}",
            )
        for index, sample in enumerate(case["compile_samples"]):
            _validate_compile_sample_evidence(
                sample,
                remarks_configured=remarks_configured,
                label=f"case {case['case_id']} cold sample {index}",
            )
        validate_attempt_cancellation_state(
            case,
            label=f"case {case['case_id']} current attempt",
        )
        if case["source_group"] != f"sg-{case['source_sha256']}":
            raise ValidationError(f"case {case['case_id']} has inconsistent source_group")
        if configuration["timeout_policy"] == "fixed" and not math.isclose(
            case["effective_timeout_seconds"], configuration["run_timeout_seconds"], rel_tol=0, abs_tol=1e-12
        ):
            raise ValidationError("fixed timeout case bound differs from configuration")
        if configuration["timeout_policy"] == "initial" and not math.isclose(
            case["effective_timeout_seconds"], configuration["timeout_cap_seconds"], rel_tol=0, abs_tol=1e-12
        ):
            raise ValidationError("initial timeout case bound differs from protocol cap")
        timeout_derivation = case.get("timeout_derivation")
        if configuration["timeout_policy"] == "baseline_derived":
            if timeout_derivation is None:
                raise ValidationError("baseline-derived case lacks its recorded timeout derivation")
            if (
                timeout_derivation["baseline_run_id"] != configuration["baseline_timeout_run_id"]
                or timeout_derivation["baseline_run_sha256"]
                != configuration["baseline_timeout_run_sha256"]
            ):
                raise ValidationError("case timeout derivation does not bind the configured baseline run")
            baseline_median_ns = timeout_derivation["baseline_median_duration_ns"]
            if timeout_derivation["baseline_case_status"] == "timeout":
                if baseline_median_ns is not None:
                    raise ValidationError("timed-out baseline case cannot declare a median duration")
                expected_timeout = 1800.0
            else:
                if baseline_median_ns is None:
                    raise ValidationError("passed baseline case must declare its median duration")
                expected_timeout = min(
                    1800.0,
                    max(120.0, 3.0 * float(baseline_median_ns) / 1_000_000_000),
                )
            if not math.isclose(
                case["effective_timeout_seconds"], expected_timeout, rel_tol=0, abs_tol=1e-12
            ):
                raise ValidationError("derived timeout case bound differs from its recorded 120/3x/1800 derivation")
        elif timeout_derivation is not None:
            raise ValidationError("non-derived timeout policy cannot carry baseline derivation evidence")
        if case["cache_hit"] and not configuration["reuse_compile_cache"]:
            raise ValidationError("cache_hit is impossible when reuse_compile_cache is disabled")
        if case["status"] == "passed" and (case["artifact_sha256"] is None or case["binary_sha256"] is None):
            raise ValidationError("passed case lacks emitted assembly/artifact or linked binary content hash")
        if (case["remarks_sha256"] is not None) != (
            configuration["remarks_file_sha256"] is not None and case["artifact_sha256"] is not None
        ):
            raise ValidationError("case optimization-remark content hash is inconsistent with configuration/compile state")
        if "remarks_event_count" in case and (
            (case["remarks_event_count"] is not None) != (case["remarks_sha256"] is not None)
        ):
            raise ValidationError("case optimization-remark event count is inconsistent with its content hash")
        _validate_candidate_remark_summary_binding(
            case.get("candidate_remark_summary"),
            remarks_event_count=case["remarks_event_count"],
            enabled_candidate_ids=enabled_candidate_ids,
            candidate_contract_bound=candidate_registry_sha256 is not None,
            label=f"case {case['case_id']}",
        )
        if (case["analysis_sha256"] is not None) != (
            uses_analyzer and case["analyze"] is not None and case["analyze"]["status"] == "ok"
        ):
            raise ValidationError("case binary-analysis content hash is inconsistent with analyzer state")
        if case["compile"] is not None and case["compile"]["status"] == "ok":
            if len(case["compile_samples"]) != configuration["compile_repetitions"]:
                raise ValidationError(f"case {case['case_id']} lacks the configured cold compile samples")
            if any(sample["status"] != "ok" for sample in case["compile_samples"]):
                raise ValidationError("successful compile contains a failed cold sample")
            compile_statistics = case["compile_statistics"]
            if compile_statistics is None:
                raise ValidationError("successful compile lacks median/MAD statistics")
            durations = [float(sample["duration_ns"]) for sample in case["compile_samples"]]
            median = float(statistics.median(durations))
            mad = float(statistics.median(abs(value - median) for value in durations))
            if compile_statistics["sample_count"] != len(durations) or not math.isclose(
                compile_statistics["median_duration_ns"], median, rel_tol=0, abs_tol=1e-9
            ) or not math.isclose(compile_statistics["mad_duration_ns"], mad, rel_tol=0, abs_tol=1e-9):
                raise ValidationError("compile median/MAD statistics do not match cold samples")
        if not case["consistency_selected"] and case["consistency_passed"] is not None:
            raise ValidationError("non-selected case must not declare a consistency result")
        if not case["consistency_selected"] and case["consistency_mismatched_metrics"]:
            raise ValidationError("non-selected case must not declare consistency mismatches")
        if case["status"] == "passed" and case["consistency_selected"]:
            if case["consistency_passed"] is not True:
                raise ValidationError("passed selected case must pass consistency validation")
            if len(case["samples"]) < configuration["consistency_repetitions"]:
                raise ValidationError("consistency-selected case has fewer than three samples")
        if case["status"] == "passed":
            expected_samples = max(
                configuration["repetitions"],
                configuration["consistency_repetitions"] if case["consistency_selected"] else 0,
            )
            if len(case["samples"]) != expected_samples:
                raise ValidationError("passed case sample count differs from the configured protocol")
        _validate_measurements(case["measurements"], by_metric, f"case {case['case_id']}")
        if uses_analyzer and case["status"] == "passed":
            if case["analyze"] is None or case["analyze"]["status"] != "ok":
                raise ValidationError("passed case lacks a successful post-link analyzer phase")
            measured_ids = {item["metric_id"] for item in case["measurements"]}
            expected_analyzer_ids = {spec["metric_id"] for spec in specs if spec["source"] == "analyzer"}
            if not expected_analyzer_ids.issubset(measured_ids):
                raise ValidationError("passed case lacks configured analyzer measurements")
        expected_indexes = list(range(len(case["samples"])))
        if [sample["index"] for sample in case["samples"]] != expected_indexes:
            raise ValidationError(f"case {case['case_id']} sample indexes are not contiguous")
        for sample in case["samples"]:
            _validate_measurements(sample["measurements"], by_metric, f"case {case['case_id']} sample")
            values = {item["metric_id"]: item["value"] for item in sample["measurements"]}
            if sample["status"] == "passed":
                value = values.get(configuration["primary_metric_id"])
                if value is None or value <= 0:
                    raise ValidationError(f"passed case {case['case_id']} sample lacks positive primary metric")
                if sample["censoring"] != "none" or any(
                    sample[field] is not None for field in ("censor_bound", "censor_unit", "censor_metric_id")
                ):
                    raise ValidationError("uncensored sample has censor metadata")
                if configuration["output_contract"] == "raw_stdout":
                    if sample["expected_return_uint8"] is not None or sample["observed_return_uint8"] is not None:
                        raise ValidationError("raw_stdout samples must not record a main return value")
                elif sample["expected_return_uint8"] is None or sample["observed_return_uint8"] != sample["expected_return_uint8"]:
                    raise ValidationError("passed sample lacks an independently matching uint8 return value")
            elif sample["status"] == "timeout":
                if sample["censoring"] != "right" or any(
                    sample[field] is None for field in ("censor_bound", "censor_unit", "censor_metric_id")
                ):
                    raise ValidationError("timeout sample must carry a right-censoring bound")
                expected_bound = case["effective_timeout_seconds"] * 1_000_000_000
                if not math.isclose(sample["censor_bound"], expected_bound, rel_tol=0, abs_tol=1e-6):
                    raise ValidationError("timeout censor bound differs from effective per-case timeout")
        if (
            case["consistency_selected"]
            and len(case["samples"]) >= configuration["consistency_repetitions"]
            and all(sample["status"] == "passed" for sample in case["samples"])
        ):
            deterministic_ids = {
                spec["metric_id"] for spec in specs if spec["source"] in {"stdout", "stderr", "file"}
            }
            mismatched: list[str] = []
            if deterministic_ids:
                for metric_id in sorted(deterministic_ids):
                    observations = [
                        {item["metric_id"]: item["value"] for item in sample["measurements"]}.get(metric_id)
                        for sample in case["samples"]
                    ]
                    if observations[0] is None or any(value != observations[0] for value in observations[1:]):
                        mismatched.append(metric_id)
            else:
                hashes = [
                    (sample["program_stdout"]["sha256"], sample["observed_return_uint8"])
                    for sample in case["samples"]
                ]
                if any(value != hashes[0] for value in hashes[1:]):
                    mismatched.append("program_output")
            if case["consistency_mismatched_metrics"] != mismatched:
                raise ValidationError("consistency_mismatched_metrics does not match sample observations")
            if mismatched:
                if case["consistency_passed"] is not False or case["status"] != "measurement_inconsistent":
                    raise ValidationError("inconsistent deterministic measurements must fail the case")
            elif case["consistency_passed"] is not True:
                raise ValidationError("consistent deterministic measurements must pass consistency validation")


def _validate_measurements(
    measurements: list[dict[str, Any]],
    specs: dict[str, dict[str, Any]],
    label: str,
) -> None:
    identifiers = [item["metric_id"] for item in measurements]
    if len(identifiers) != len(set(identifiers)):
        raise ValidationError(f"{label} contains duplicate measurements")
    for item in measurements:
        spec = specs.get(item["metric_id"])
        if spec is None:
            raise ValidationError(f"{label} references an undeclared metric: {item['metric_id']}")
        if item["unit"] != spec["unit"]:
            raise ValidationError(f"{label} metric unit does not match configuration: {item['metric_id']}")
        if item["availability"] == "measured":
            if item["value"] is None or not math.isfinite(item["value"]):
                raise ValidationError(f"{label} contains an invalid measured metric: {item['metric_id']}")
            if item["reason"] is not None:
                raise ValidationError("measured metrics must not carry an unavailable reason")
        elif item["value"] is not None or item["reason"] is None:
            raise ValidationError("unavailable metrics require a null value and non-empty reason")


def _validate_optimization_event_semantics(document: dict[str, Any]) -> None:
    if document["event_type"] == "pass_summary":
        before = document["before"]
        after = document["after"]
        keys = set(before) | set(after)
        expected_delta = {
            key: after.get(key, 0) - before.get(key, 0)
            for key in keys
            if after.get(key, 0) - before.get(key, 0) != 0
        }
        if document["delta"] != expected_delta:
            raise ValidationError("pass_summary delta does not exactly match before/after counters")
        return
    expected_decisions = {
        "candidate_matched": "candidate",
        "applied_profitable": "applied",
        "applied_canonicalization": "applied",
        "rejected_legality": "rejected",
        "rejected_profitability": "rejected",
        "rejected_unsupported_shape": "rejected",
        "rejected_resource_limit": "rejected",
        "rejected_no_benefit": "rejected",
    }
    if expected_decisions[document["reason"]] != document["decision"]:
        raise ValidationError("decision reason is incompatible with decision status")
    if document["schema_version"] == "optimization-remark.v2":
        obligation = document["legality_obligation_id"]
        if (document["reason"] == "rejected_legality") != (obligation is not None):
            raise ValidationError(
                "v2 legality obligation must be present exactly for rejected_legality"
            )


def _validate_binary_analysis_semantics(document: dict[str, Any]) -> None:
    identifiers = [item["metric_id"] for item in document["measurements"]]
    if len(identifiers) != len(set(identifiers)):
        raise ValidationError("binary analysis contains duplicate metric ids")
    for item in document["measurements"]:
        expected_unit = ANALYZER_METRICS.get(item["metric_id"])
        if expected_unit is None or item["unit"] != expected_unit:
            raise ValidationError("binary analysis uses an unknown metric id or non-canonical unit")
        if item["availability"] == "measured":
            if item["value"] is None or not math.isfinite(item["value"]):
                raise ValidationError("measured binary analysis values must be finite")
            if item["reason"] is not None:
                raise ValidationError("measured binary analysis values must not carry a reason")
        elif item["value"] is not None or item["reason"] not in UNAVAILABLE_REASONS:
            raise ValidationError("unavailable binary analysis values require a reason and null value")


def _validate_pass_registry_semantics(document: dict[str, Any]) -> None:
    identifiers = [item["id"] for item in document["passes"]]
    if len(identifiers) != len(set(identifiers)):
        raise ValidationError("pass registry contains duplicate pass ids")
    if document["schema_version"] == "pass-registry.v1":
        return
    by_id = {item["id"]: item for item in document["passes"]}
    for descriptor in document["passes"]:
        is_candidate = descriptor["lifecycle"] == "candidate"
        if is_candidate:
            if (
                not descriptor["id"].startswith("candidate.")
                or not descriptor["logical_family_id"].startswith("candidate.")
                or not descriptor["decision_observable"]
                or descriptor["candidate_anchor"] is None
                or descriptor["full_pipeline_occurrences"] != 1
                or not descriptor["legality_obligation_ids"]
            ):
                raise ValidationError("candidate pass registry descriptor is incomplete")
            if any(
                not obligation.startswith(descriptor["id"] + ".")
                for obligation in descriptor["legality_obligation_ids"]
            ):
                raise ValidationError("candidate legality obligation is not pass-scoped")
            anchor = descriptor["candidate_anchor"]
            anchored = by_id.get(anchor["pass"])
            if (
                anchored is None
                or anchored["lifecycle"] == "candidate"
                or anchored["stage"] != descriptor["stage"]
                or anchor["occurrence"] > anchored["full_pipeline_occurrences"]
            ):
                raise ValidationError("candidate anchor is unknown, recursive, or out of range")
        elif descriptor["candidate_anchor"] is not None or descriptor[
            "legality_obligation_ids"
        ]:
            raise ValidationError("non-candidate pass cannot carry candidate contracts")


def _validate_ablation_matrix_semantics(document: dict[str, Any]) -> None:
    profile_ids = [item["profile_id"] for item in document["profiles"]]
    if len(profile_ids) != len(set(profile_ids)):
        raise ValidationError("ablation matrix contains duplicate profile ids")
    paths = [item["path"] for item in document["profiles"]]
    if len(paths) != len(set(paths)):
        raise ValidationError("ablation matrix contains duplicate profile paths")
    for path in paths:
        validate_relative_path(path, label="ablation profile path")
    known = set(profile_ids)
    if profile_ids.count("full") != 1 or profile_ids.count("mandatory") != 1:
        raise ValidationError("ablation matrix requires exactly one full and mandatory profile")
    scheduled: set[str] = set()
    for item in document["schedule"]:
        if item["baseline_profile_id"] != "full":
            raise ValidationError("ablation schedule baseline must be full")
        candidate = item["candidate_profile_id"]
        if candidate not in known:
            raise ValidationError("ablation schedule references an unknown profile")
        if candidate in scheduled:
            raise ValidationError("ablation schedule repeats a candidate profile")
        scheduled.add(candidate)


def _validate_oracle_plan_semantics(document: dict[str, Any]) -> None:
    expected_class = {
        "oracle": "cleanroom",
        "B3": "official",
        "B4": "holdout_or_mature",
        "B6": "holdout_or_mature",
    }[document["manifest_data_role"]]
    if document["evidence_class"] != expected_class:
        raise ValidationError("oracle evidence class does not match its bound manifest data role")
    pair_ids = [item["pair_id"] for item in document["pairs"]]
    case_ids = [item[leg]["case_id"] for item in document["pairs"] for leg in ("baseline", "optimized")]
    if len(pair_ids) != len(set(pair_ids)) or len(case_ids) != len(set(case_ids)):
        raise ValidationError("oracle plan contains duplicate pair/source-leg case ids")
    if document["baseline_run_id"] == document["optimized_run_id"]:
        raise ValidationError("oracle source legs require distinct run ids")
    for item in document["pairs"]:
        for leg in ("baseline", "optimized"):
            descriptor = item[leg]
            if descriptor["source_group"] != f"sg-{descriptor['source_sha256']}":
                raise ValidationError("oracle plan source_group does not match source_sha256")
        if item["baseline"]["case_id"] == item["optimized"]["case_id"]:
            raise ValidationError("oracle pair must contain distinct source-leg cases")


def _validate_cross_suite_audit_semantics(document: dict[str, Any]) -> None:
    counts = document["counts"]
    left_total = sum(len(item["left_case_ids"]) for item in document["mappings"])
    right_total = sum(len(item["right_case_ids"]) for item in document["mappings"])
    if left_total != counts["left_cases"] or right_total != counts["right_cases"]:
        raise ValidationError("cross-suite mapping counts do not cover both manifests")
    if counts["matched_cases"] + counts["left_only_cases"] != counts["left_cases"]:
        raise ValidationError("cross-suite left matched/unmatched counts do not add up")
    if counts["matched_cases"] + counts["right_only_cases"] != counts["right_cases"]:
        raise ValidationError("cross-suite right matched/unmatched counts do not add up")
    if counts["shared_content_groups"] != sum(item["matched_count"] > 0 for item in document["mappings"]):
        raise ValidationError("cross-suite shared content group count is inconsistent")
    for item in document["mappings"]:
        left_count = len(item["left_case_ids"])
        right_count = len(item["right_case_ids"])
        matched = min(left_count, right_count)
        if (
            item["matched_count"] != matched
            or item["left_only_count"] != left_count - matched
            or item["right_only_count"] != right_count - matched
        ):
            raise ValidationError("cross-suite per-signature multiplicity counts are inconsistent")
        expected = (
            "identical" if left_count == right_count and left_count > 0
            else "multiplicity_mismatch" if left_count and right_count
            else "left_only" if left_count else "right_only"
        )
        if item["status"] != expected:
            raise ValidationError("cross-suite mapping status is inconsistent with membership")


def _validate_campaign_plan_semantics(document: dict[str, Any]) -> None:
    if document["run_record_schema_sha256"] != schema_sha256("run-record.v1"):
        raise ValidationError(
            "campaign run-record schema binding differs from the active benchmark schema"
        )
    finalized = document["parent_plan_sha256"] is not None
    if finalized != (document["promotion_status_sha256"] is not None):
        raise ValidationError("campaign parent-plan and promotion-status hashes must be present together")
    if not finalized and document["matrix_sha256"] != document["initial_matrix_sha256"]:
        raise ValidationError("initial campaign matrix hash must equal matrix_sha256")
    if finalized != bool(document["final_pair_families"]):
        raise ValidationError("only a finalized campaign may declare final pair families")
    if finalized and len(document["final_pair_families"]) != 5:
        raise ValidationError("finalized campaign must declare exactly five measured pair families")
    if sum(item["budget_seconds"] for item in document["phases"]) != document["total_budget_seconds"]:
        raise ValidationError("campaign phase budgets must add up to the 72-hour total")
    phase_ids = [item["phase_id"] for item in document["phases"]]
    expected_phases = ["baseline_validation", "singleton_b2", "promotion_b3", "final_validation"]
    expected_budgets = [43_200, 86_400, 86_400, 43_200]
    if phase_ids != expected_phases or [item["budget_seconds"] for item in document["phases"]] != expected_budgets:
        raise ValidationError("campaign phases must be the fixed 12h/24h/24h/12h protocol")
    for item in document["phases"]:
        expected_destination = None if item["phase_id"] == "final_validation" else "final_validation"
        if item["unused_budget_destination"] != expected_destination:
            raise ValidationError("unused campaign phase budget must transfer only to final_validation")
    suite_roles = [item["data_role"] for item in document["suites"]]
    if suite_roles != ["B1", "B2", "B3", "B4", "B5", "B6", "oracle"]:
        raise ValidationError("campaign suites must contain ordered B1..B6 and oracle manifests")
    suites = {item["data_role"]: item for item in document["suites"]}
    protocols = document["measurement_protocols"]
    if (
        protocols["standard_proxy"]["measurement_mode"] != "standard_proxy"
        or protocols["cache_hotblock"]["measurement_mode"] != "cache_hotblock"
        or protocols["standard_proxy"]["protocol_sha256"]
        == protocols["cache_hotblock"]["protocol_sha256"]
        or protocols["standard_proxy"]["runner_command_sha256"]
        == protocols["cache_hotblock"]["runner_command_sha256"]
    ):
        raise ValidationError("campaign measurement protocol modes are not independently bound")
    for field in (
        "profile_plugin_sha256",
        "cache_plugin_sha256",
        "hotblocks_plugin_sha256",
        "cache_model_sha256",
    ):
        if protocols["standard_proxy"][field] != protocols["cache_hotblock"][field]:
            raise ValidationError(f"campaign protocol physical model drift: {field}")
    reference_baselines = {
        item["compiler_baseline"]: item
        for item in document["reference_toolchain"]["baselines"]
    }
    if set(reference_baselines) != {"gcc_13_3_o2", "clang_18_o3"}:
        raise ValidationError("campaign reference toolchain must bind exact GCC and Clang baselines")
    expected_reference = {
        "gcc_13_3_o2": ("gcc-13.3-o2", "riscv-gcc", "13.3.0", "-O2"),
        "clang_18_o3": ("clang-18-o3", "clang", "18.1.3", "-O3"),
    }
    for baseline_id, (profile_id, tool, version, optimization) in expected_reference.items():
        item = reference_baselines[baseline_id]
        expected_profile_sha256 = sha256_json(
            {
                "schema": "reference-frontend-profile.v1",
                "compiler_baseline": baseline_id,
                "compiler_argv_sha256": item["compiler_argv_sha256"],
                "source_adapter_sha256": document["reference_toolchain"]["source_adapter_sha256"],
                "builtin_header_sha256": document["reference_toolchain"]["builtin_header_sha256"],
                "image_id": document["reference_toolchain"]["image_id"],
            }
        )
        if (
            item["profile_id"] != profile_id
            or item["tool"] != tool
            or item["version"] != version
            or item["optimization"] != optimization
            or item["profile_sha256"] != expected_profile_sha256
        ):
            raise ValidationError(f"campaign reference baseline identity drift: {baseline_id}")
    oracle = document["oracle_plan"]
    if (
        oracle["suite_id"] != suites["oracle"]["suite_id"]
        or oracle["manifest_sha256"] != suites["oracle"]["manifest_sha256"]
        or oracle["baseline_run_id"] == oracle["optimized_run_id"]
    ):
        raise ValidationError("campaign Oracle plan registry is inconsistent")
    task_ids = [item["task_id"] for item in document["tasks"]]
    run_ids = [item["run_id"] for item in document["tasks"]]
    if len(task_ids) != len(set(task_ids)) or len(run_ids) != len(set(run_ids)):
        raise ValidationError("campaign plan contains duplicate task/run ids")
    known = set(task_ids)
    for task in document["tasks"]:
        if task["profile_path"] is not None:
            validate_relative_path(task["profile_path"], label="campaign profile path")
        if (task["kind"] == "toolchain_baseline") != (task["profile_path"] is None):
            raise ValidationError("only toolchain baseline tasks may omit a pipeline profile path")
        if any(dependency not in known or dependency == task["task_id"] for dependency in task["dependencies"]):
            raise ValidationError("campaign task dependency is missing or self-referential")
        suite = suites[task["suite_role"]]
        if task["oracle_leg"] is None and (
            task["suite_id"] != suite["suite_id"] or task["manifest_sha256"] != suite["manifest_sha256"]
        ):
            raise ValidationError("campaign task suite identity differs from its suite registry")
        if task["suite_role"] == "B1" and task["required_evidence_level"] != "qemu_correctness":
            raise ValidationError("B1 campaign tasks are correctness evidence only")
        if task["suite_role"] != "B1" and task["required_evidence_level"] != "qemu_proxy":
            raise ValidationError("performance campaign tasks require qemu_proxy evidence")
        if task["measurement_mode"] == "cache_hotblock" and (
            task["phase_id"] != "final_validation" or task["selection_rule"] != "confirmation_top5"
        ):
            raise ValidationError("cache/hotblock probes are final Top5 diagnostics only")
        contract = task["measurement_contract"]
        if task["measurement_mode"] == "correctness":
            if task["suite_role"] != "B1" or contract != {
                "metric_profile_id": None,
                "compile_repetitions": 5,
                "reuse_compile_cache": False,
                "additional_metric_specs": [],
            }:
                raise ValidationError("campaign correctness measurement contract is inconsistent")
        else:
            if (
                task["suite_role"] == "B1"
                or contract["metric_profile_id"] != "rv64gc-qemu-v1"
                or contract["compile_repetitions"] != 5
                or contract["reuse_compile_cache"]
            ):
                raise ValidationError("campaign proxy measurement contract is inconsistent")
            expected_extra_specs = (
                [
                    {
                        "metric_id": item["metric_id"],
                        "source": item["source"],
                        "pattern_sha256": sha256_json(item["pattern"]),
                        "unit": item["unit"],
                    }
                    for item in cache_hotblock_metrics_v1()
                ]
                if task["measurement_mode"] == "cache_hotblock"
                else []
            )
            if contract["additional_metric_specs"] != expected_extra_specs:
                raise ValidationError("campaign mode has an inconsistent diagnostic metric contract")
        reference = task["reference_compiler_contract"]
        expected_contract = reference_baselines.get(task["compiler_baseline"])
        if reference != expected_contract:
            raise ValidationError("campaign task reference compiler contract is inconsistent")
    full = [
        item for item in document["tasks"]
        if item["phase_id"] == "baseline_validation" and item["kind"] == "full"
    ]
    if {item["suite_role"] for item in full} != {"B1", "B3"} or any(item["dependencies"] for item in full):
        raise ValidationError("first 12h must contain only dependency-free B1/B3 ACCELA FULL baselines")
    b3_baselines = {
        item["compiler_baseline"] for item in document["tasks"]
        if item["phase_id"] == "baseline_validation" and item["suite_role"] == "B3"
    }
    if b3_baselines != {"accela_full", "accela_mandatory", "gcc_13_3_o2", "clang_18_o3"}:
        raise ValidationError("official B3 baseline phase must schedule the exact four compiler baselines")
    if any(
        item["suite_role"] in {"B4", "B5", "B6", "oracle"}
        and item["phase_id"] != "final_validation"
        for item in document["tasks"]
    ):
        raise ValidationError("holdout, mature, and Oracle tasks belong only to final_validation")
    oracle_tasks = [item for item in document["tasks"] if item["oracle_leg"] is not None]
    if (
        {item["oracle_leg"] for item in oracle_tasks} != {"baseline", "optimized"}
        or len(oracle_tasks) != 2
        or any(item["compiler_baseline"] != "oracle_accela_full" for item in oracle_tasks)
        or {item["run_id"] for item in oracle_tasks}
        != {oracle["baseline_run_id"], oracle["optimized_run_id"]}
    ):
        raise ValidationError("campaign must schedule the two same-FULL Oracle source legs exactly once")
    pair_tasks = [item for item in document["tasks"] if item["kind"] == "pair_ablation"]
    expected_pair_count = document["promotion"]["required_pair_profiles"] if finalized else 0
    if len(pair_tasks) != expected_pair_count:
        raise ValidationError("campaign pair task count is inconsistent with finalization state")
    if any(
        item["phase_id"] != "final_validation"
        or item["suite_role"] != "B3"
        or item["selection_rule"] != "pair_of_confirmation_top5"
        for item in pair_tasks
    ):
        raise ValidationError("pair profiles must be conditional final B3 tasks")
    if finalized:
        declared_families = set(document["final_pair_families"])
        actual_pairs = {tuple(sorted(item["logical_families"])) for item in pair_tasks}
        expected_pairs = {
            tuple(sorted((left, right)))
            for index, left in enumerate(document["final_pair_families"])
            for right in document["final_pair_families"][index + 1 :]
        }
        if actual_pairs != expected_pairs or any(
            len(item["logical_families"]) != 2
            or not set(item["logical_families"]).issubset(declared_families)
            for item in pair_tasks
        ):
            raise ValidationError("campaign pair tasks do not cover the exact measured Top5 combinations")


def _validate_campaign_status_semantics(document: dict[str, Any]) -> None:
    if document["elapsed_wall_clock_seconds"] + document["remaining_wall_clock_seconds"] > 259_200 + 1e-9:
        raise ValidationError("campaign elapsed/remaining wall-clock exceeds 72 hours")
    phase_ids = [item["phase_id"] for item in document["phases"]]
    if phase_ids != ["baseline_validation", "singleton_b2", "promotion_b3", "final_validation"]:
        raise ValidationError("campaign status phases are not in protocol order")
    task_ids = [item["task_id"] for item in document["tasks"]]
    if len(task_ids) != len(set(task_ids)):
        raise ValidationError("campaign status contains duplicate tasks")
    for task in document["tasks"]:
        has_run = task["run_record_sha256"] is not None
        has_times = task["started_at"] is not None
        if task["status"] in {"running", "completed", "failed", "interrupted"} and not has_run:
            raise ValidationError("campaign task run provenance is inconsistent with its status")
        if task["status"] in {"pending", "not_selected"} and has_run:
            raise ValidationError("pending/unselected campaign task cannot have run evidence")
        if has_run != has_times:
            raise ValidationError("campaign task run timestamps are inconsistent with run provenance")
        if task["status"] == "completed" and task["completed_at"] is None:
            raise ValidationError("completed campaign task lacks completed_at")
        if task["status"] == "running" and task["completed_at"] is not None:
            raise ValidationError("running campaign task cannot have completed_at")
        if task["status"] == "completed" and task["missing_reason"] is not None:
            raise ValidationError("completed campaign task must not have a missing reason")
        if task["status"] in {"pending", "not_selected", "budget_exhausted", "failed", "interrupted"} and task["missing_reason"] is None:
            raise ValidationError("unexecuted campaign task requires a missing reason")
        if task["selection_state"] == "not_selected" and task["status"] != "not_selected":
            raise ValidationError("unselected campaign task has an executable status")
        if task["selection_state"] == "awaiting_evidence" and (
            (
                task["status"] == "pending"
                and task["missing_reason"] == "promotion_evidence_missing"
            )
            or (
                task["status"] == "budget_exhausted"
                and task["missing_reason"] == "not_scheduled"
            )
        ) is False:
            raise ValidationError("campaign task awaiting promotion evidence is inconsistently classified")
        if task["status"] == "failed" and task["missing_reason"] not in {
            "timeout", "tool_failure", "correctness_failure"
        }:
            raise ValidationError("failed campaign task lacks a terminal failure category")
        if task["status"] == "interrupted" and task["missing_reason"] != "tool_failure":
            raise ValidationError("interrupted campaign task must be classified as tool_failure")
        if task["status"] == "budget_exhausted" and task["missing_reason"] not in {
            "not_scheduled", "timeout"
        }:
            raise ValidationError("budget-exhausted campaign task lacks a deadline category")
    decisions = document["promotion_decisions"]
    selected_smoke = sorted(item["profile_id"] for item in decisions["smoke"] if item["selected"])
    if selected_smoke != sorted(decisions["promoted_profile_ids"]):
        raise ValidationError("campaign promoted profile summary differs from smoke decisions")
    if decisions["minimum_top8_satisfied"] != (len(selected_smoke) >= 8):
        raise ValidationError("campaign Top8 satisfaction flag is inconsistent")
    if len(decisions["final_profile_ids"]) > 5:
        raise ValidationError("campaign confirmation selected more than Top5")


def _validate_candidate_evidence_semantics(document: dict[str, Any]) -> None:
    identifiers = [item["candidate_id"] for item in document["candidates"]]
    if len(identifiers) != len(set(identifiers)):
        raise ValidationError("candidate evidence contains duplicate candidate ids")
    for candidate in document["candidates"]:
        if (
            candidate["candidate_id"] in _LOCKED_CANDIDATE_FAMILIES
            and candidate["cleanroom_oracle_family_id"]
            != _LOCKED_CANDIDATE_FAMILIES[candidate["candidate_id"]]
        ):
            raise ValidationError(
                "candidate evidence changes a locked Oracle family identity"
            )
        if not candidate["legality_obligation_ids"]:
            raise ValidationError(
                "candidate evidence requires nonempty legality obligations"
            )
        official_selectors: set[str] = set()
        for reference in candidate["official_oracle_refs"]:
            if reference["baseline_run_id"] == reference["optimized_run_id"]:
                raise ValidationError("candidate official Oracle reference must bind distinct run ids")
            for family in reference["family_ids"]:
                if family in official_selectors:
                    raise ValidationError("candidate repeats an official Oracle family across plans")
                official_selectors.add(family)
        holdout_selectors: set[str] = set()
        for reference in candidate["holdout_or_mature_refs"]:
            if reference["baseline_run_id"] == reference["optimized_run_id"]:
                raise ValidationError("candidate holdout reference must bind distinct run ids")
            for pair_id in reference["pair_ids"]:
                if pair_id in holdout_selectors:
                    raise ValidationError("candidate repeats a holdout/mature workload across plans")
                holdout_selectors.add(pair_id)


def _validate_candidate_catalog_semantics(document: dict[str, Any]) -> None:
    candidate_ids = [item["candidate_id"] for item in document["candidates"]]
    remark_pass_ids = [item["remark_pass_id"] for item in document["candidates"]]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValidationError("candidate registry contains duplicate candidate ids")
    if len(remark_pass_ids) != len(set(remark_pass_ids)):
        raise ValidationError("candidate registry contains ambiguous remark pass ids")
    for candidate in document["candidates"]:
        if candidate["candidate_id"] != candidate["remark_pass_id"]:
            raise ValidationError(
                "executable candidate id must equal its PassRegistry v2 candidate pass id"
            )
        obligation_ids = [
            item["obligation_id"] for item in candidate["legality_obligations"]
        ]
        if len(obligation_ids) != len(set(obligation_ids)):
            raise ValidationError(
                f"candidate repeats a legality obligation: {candidate['candidate_id']}"
            )


def _validate_candidate_screening_spec_semantics(document: dict[str, Any]) -> None:
    candidates = document["candidates"]
    identifiers = [item["candidate_id"] for item in candidates]
    if len(identifiers) != len(set(identifiers)):
        raise ValidationError("candidate screening spec repeats a candidate")
    known = set(identifiers)
    oracle_families = [item["oracle_family_id"] for item in candidates]
    implementation_ids = [
        item["implementation_candidate_id"]
        for item in candidates
        if item["implementation_candidate_id"] is not None
    ]
    if len(oracle_families) != len(set(oracle_families)):
        raise ValidationError("candidate screening spec repeats an Oracle family")
    if len(implementation_ids) != len(set(implementation_ids)):
        raise ValidationError("candidate screening spec repeats an implementation candidate id")
    eligible_refs = [
        (ref["oracle_family_id"], ref["structure_id"])
        for item in candidates
        for ref in item["eligible_oracle_structure_refs"]
    ]
    if (
        len(eligible_refs) != len(set(eligible_refs))
        or any(family not in set(oracle_families) for family, _ in eligible_refs)
    ):
        raise ValidationError(
            "candidate screening spec has duplicate or unknown fully qualified Oracle structure refs"
        )
    locked_observed = tuple(
        (
            item["candidate_id"],
            item["oracle_family_id"],
            item["family_kind"],
            item["structural_disposition"],
            item["structural_reason"],
            tuple(
                (ref["oracle_family_id"], ref["structure_id"])
                for ref in item["eligible_oracle_structure_refs"]
            ),
        )
        for item in candidates
    )
    if locked_observed != _LOCKED_CANDIDATE_SCREENING_CONTRACT:
        raise ValidationError(
            "candidate screening spec differs from the locked eleven-family structure contract"
        )
    for item in candidates:
        duplicate = item["duplicate_of"]
        if duplicate == item["candidate_id"] or (
            duplicate is not None and duplicate not in known
        ):
            raise ValidationError("candidate screening duplicate relation is invalid")
        if (item["structural_disposition"] == "eligible") != (
            item["structural_reason"] is None
        ):
            raise ValidationError(
                "candidate structural disposition/reason must be both eligible/null or blocked"
            )
        if (item["structural_disposition"] == "eligible") != bool(
            item["eligible_oracle_structure_refs"]
        ):
            raise ValidationError(
                "eligible candidate families require structures; blocked/rejected families forbid them"
            )
        if (
            item["structural_disposition"] == "eligible"
            and item["implementation_candidate_id"] is None
        ):
            raise ValidationError(
                "eligible candidate family requires an implementation pass id"
            )
        if (duplicate is not None) != (
            item["structural_reason"] == "duplicate_candidate"
        ):
            raise ValidationError("candidate duplicate relation/reason is inconsistent")


def _validate_candidate_screening_semantics(document: dict[str, Any]) -> None:
    base_registry = document["base_pass_registry"]
    sources = document["sources"]
    source_bindings = (
        (
            sources["candidate_evidence"],
            document["candidate_evidence_sha256"],
            "candidate evidence",
        ),
        (
            sources["screening_spec"],
            document["screening_spec_sha256"],
            "screening spec",
        ),
        (
            sources["oracle_capture"],
            document["oracle_capture_sha256"],
            "Oracle capture",
        ),
        (base_registry, document["pass_registry_sha256"], "base PassRegistry"),
    )
    for artifact, declared_sha256, label in source_bindings:
        validate_relative_path(
            artifact["path"], label=f"candidate screening {label} path"
        )
        if artifact["canonical_sha256"] != declared_sha256:
            raise ValidationError(
                f"candidate screening {label} binding differs"
            )
    if len({artifact["path"] for artifact, _, _ in source_bindings}) != len(
        source_bindings
    ):
        raise ValidationError(
            "candidate screening source artifact paths must be distinct"
        )
    candidates = document["candidates"]
    identifiers = [item["candidate_id"] for item in candidates]
    if len(identifiers) != len(set(identifiers)):
        raise ValidationError("candidate screening repeats a candidate")
    known = set(identifiers)
    oracle_families = [item["oracle_family_id"] for item in candidates]
    implementation_ids = [
        item["implementation_candidate_id"]
        for item in candidates
        if item["implementation_candidate_id"] is not None
    ]
    if len(oracle_families) != len(set(oracle_families)):
        raise ValidationError("candidate screening repeats an Oracle family")
    if len(implementation_ids) != len(set(implementation_ids)):
        raise ValidationError("candidate screening repeats an implementation candidate id")
    if tuple(identifiers) != _LOCKED_CANDIDATE_IDS:
        raise ValidationError(
            "candidate screening must contain the locked eleven families in canonical order"
        )
    locked_by_id = {
        item[0]: item for item in _LOCKED_CANDIDATE_SCREENING_CONTRACT
    }
    expected_duplicates: dict[str, list[str]] = {}
    for item in candidates:
        locked = locked_by_id[item["candidate_id"]]
        if (
            item["oracle_family_id"] != locked[1]
            or item["family_kind"] != locked[2]
            or tuple(
                (ref["oracle_family_id"], ref["structure_id"])
                for ref in item["eligible_oracle_structure_refs"]
            )
            != locked[5]
        ):
            raise ValidationError(
                "candidate screening differs from the locked family/structure contract"
            )
        if locked[3] == "blocked" and item["qualification_status"] != "blocked":
            raise ValidationError("the locked bitset family must remain blocked")
        if locked[3] == "rejected" and item["qualification_status"] != "rejected":
            raise ValidationError("locked mixed families must remain rejected")
        duplicate = item["duplicate_of"]
        if duplicate is not None:
            if duplicate not in known or duplicate == item["candidate_id"]:
                raise ValidationError("candidate screening duplicate relation is invalid")
            expected_duplicates.setdefault(duplicate, []).append(item["candidate_id"])
        structures = item["oracle_structures"]
        structure_refs = [
            (structure["oracle_family_id"], structure["structure_id"])
            for structure in structures
        ]
        if len(structure_refs) != len(set(structure_refs)):
            raise ValidationError("candidate screening repeats an Oracle structure")
        primary_family = item["oracle_family_id"]
        expected_cross_refs = [
            ref for ref in locked[5] if ref[0] != primary_family
        ]
        if (
            len(structure_refs) != 3 + len(expected_cross_refs)
            or any(family != primary_family for family, _ in structure_refs[:3])
            or structure_refs[3:] != expected_cross_refs
        ):
            raise ValidationError(
                "candidate screening Oracle source structure set/order differs"
            )
        eligible_refs = [
            (ref["oracle_family_id"], ref["structure_id"])
            for ref in item["eligible_oracle_structure_refs"]
        ]
        if eligible_refs != [
            structure_ref
            for structure_ref in structure_refs
            if structure_ref in set(eligible_refs)
        ]:
            raise ValidationError(
                "candidate eligible structures must be an ordered Oracle structure subset"
            )
        expected_qualifying: list[dict[str, str]] = []
        for structure in structures:
            _validate_candidate_oracle_structure(
                structure,
                upper_bound_key="geometric_mean_speedup",
                expected_family=structure["oracle_family_id"],
            )
            structure_ref = (
                structure["oracle_family_id"],
                structure["structure_id"],
            )
            expected_threshold = (
                structure["eligible_for_candidate_screening"]
                and
                structure["eligible_for_ranking"]
                and structure["geometric_mean_speedup"] is not None
                and structure["geometric_mean_speedup"] >= 1.1
            )
            if structure["eligible_for_candidate_screening"] != (
                structure_ref in set(eligible_refs)
            ):
                raise ValidationError(
                    "candidate Oracle structure screening eligibility differs from the spec"
                )
            if structure["meets_threshold"] != expected_threshold:
                raise ValidationError(
                    "candidate Oracle structure >=1.10 screening flag is inconsistent"
                )
            if expected_threshold:
                expected_qualifying.append(
                    {
                        "oracle_family_id": structure_ref[0],
                        "structure_id": structure_ref[1],
                    }
                )
        if item["qualifying_oracle_structure_refs"] != expected_qualifying:
            raise ValidationError(
                "candidate qualifying Oracle structures differ from structure evidence"
            )
        reasons = item["rejection_reasons"]
        if (item["qualification_status"] == "qualified") != (not reasons):
            raise ValidationError("candidate qualification and rejection reasons are inconsistent")
        if (
            item["qualification_status"] == "qualified"
            and item["implementation_candidate_id"] is None
        ):
            raise ValidationError(
                "qualified conceptual family lacks an implementation candidate id"
            )
    declared_duplicates = {
        item["canonical_candidate_id"]: item["duplicate_candidate_ids"]
        for item in document["duplicate_groups"]
    }
    if declared_duplicates != {
        key: sorted(value) for key, value in sorted(expected_duplicates.items())
    }:
        raise ValidationError("candidate screening duplicate groups do not match candidates")


def _validate_candidate_profile_matrix_semantics(document: dict[str, Any]) -> None:
    profiles = document["profiles"]
    profile_ids = [item["profile_id"] for item in profiles]
    if len(profile_ids) != len(set(profile_ids)):
        raise ValidationError("candidate matrix contains duplicate logical profiles")
    paths = [item["path"] for item in profiles]
    hashes = [item["profile_sha256"] for item in profiles]
    if len(paths) != len(set(paths)) or len(hashes) != len(set(hashes)):
        raise ValidationError("candidate matrix profile paths/hashes must be distinct")
    for path in paths:
        validate_relative_path(path, label="candidate profile path")
    full = [item for item in profiles if item["kind"] == "candidate_empty"]
    if (
        len(full) != 1
        or full[0]["profile_id"] != "candidate-empty"
        or full[0]["candidate_ids"]
        or full[0]["enabled_candidate_ids"]
    ):
        raise ValidationError("candidate matrix requires one exact candidate-empty profile")
    candidate_profiles = [
        item for item in profiles if item["kind"] in {"single", "pair"}
    ]
    for profile in candidate_profiles:
        expected_count = 1 if profile["kind"] == "single" else 2
        if (
            len(profile["candidate_ids"]) != expected_count
            or profile["enabled_candidate_ids"] != profile["candidate_ids"]
        ):
            raise ValidationError(
                "candidate logical profile kind/ids/enablement are inconsistent"
            )
    candidate_sets = [tuple(item["candidate_ids"]) for item in candidate_profiles]
    if len(candidate_sets) != len(set(candidate_sets)):
        raise ValidationError("candidate matrix repeats an enabled candidate set")
    by_profile = {item["profile_id"]: item for item in profiles}
    scheduled_profiles: set[str] = set()
    for item in document["schedule"]:
        candidate_profile = by_profile.get(item["candidate_profile_id"])
        if (
            item["baseline_profile_id"] != "candidate-empty"
            or candidate_profile is None
            or candidate_profile["kind"] != item["kind"]
            or candidate_profile["candidate_ids"] != item["candidate_ids"]
        ):
            raise ValidationError(
                "candidate schedule must compare FULL to the matching FULL+candidate profile"
            )
        if item["candidate_profile_id"] in scheduled_profiles:
            raise ValidationError("candidate matrix schedules a profile more than once")
        scheduled_profiles.add(item["candidate_profile_id"])
    if scheduled_profiles != {item["profile_id"] for item in candidate_profiles}:
        raise ValidationError("candidate matrix schedule must cover every variant profile")
    if document["base_pipeline_profile"] != {
        "profile_id": full[0]["profile_id"],
        "profile_sha256": full[0]["profile_sha256"],
    }:
        raise ValidationError("candidate matrix base profile does not bind candidate-empty")


def _validate_candidate_study_semantics(document: dict[str, Any]) -> None:
    expected_case_count = {
        "B2": 20,
        "B3": 60,
        "B4": 59,
        "B5": 60,
        "B6": 88,
    }[document["data_role"]]
    identifiers = [item["candidate_id"] for item in document["candidates"]]
    run_ids = [item["run_id"] for item in document["candidates"]]
    run_hashes = [item["run_sha256"] for item in document["candidates"]]
    if len(identifiers) != len(set(identifiers)):
        raise ValidationError("candidate study contains duplicate candidate ids")
    if len(run_ids) != len(set(run_ids)) or len(run_hashes) != len(set(run_hashes)):
        raise ValidationError("candidate study must bind distinct physical candidate runs")
    if document["baseline"]["run_id"] in set(run_ids) or document["baseline"][
        "run_sha256"
    ] in set(run_hashes):
        raise ValidationError("candidate study baseline and candidate runs must be distinct")
    raw = document["raw_evidence"]
    if (
        raw["baseline"]["run_id"] != document["baseline"]["run_id"]
        or raw["baseline"]["run_canonical_sha256"]
        != document["baseline"]["run_sha256"]
        or [item["candidate_id"] for item in raw["candidates"]] != identifiers
        or any(
            raw_item["run"]["run_id"] != result["run_id"]
            or raw_item["run"]["run_canonical_sha256"] != result["run_sha256"]
            for raw_item, result in zip(raw["candidates"], document["candidates"])
        )
    ):
        raise ValidationError("candidate study raw-evidence single-run binding differs")
    raw_physical = [
        raw["baseline"]["run_physical_sha256"],
        *[item["run"]["run_physical_sha256"] for item in raw["candidates"]],
        *[item["run"]["run_physical_sha256"] for item in raw["interactions"]],
    ]
    if len(raw_physical) != len(set(raw_physical)):
        raise ValidationError("candidate study raw run bytes must be distinct")
    for result in document["candidates"]:
        candidate_id = result["candidate_id"]
        if result["enabled_candidate_ids"] != [candidate_id]:
            raise ValidationError("candidate study result enables more than its candidate")
        per_cases = result["per_cases"]
        case_ids = [item["case_id"] for item in per_cases]
        if len(case_ids) != len(set(case_ids)) or len(case_ids) != result[
            "comparable_cases"
        ]:
            raise ValidationError(
                "candidate per_cases must cover each comparable case exactly once"
            )
        for item in per_cases:
            expected = item["metric_full"] / item["metric_full_plus_candidate"]
            if not math.isclose(
                item["speedup"], expected, rel_tol=1e-12, abs_tol=1e-12
            ):
                raise ValidationError(
                    "candidate speedup direction must be FULL / FULL+candidate"
                )
        geometric_mean = result["case_geometric_mean_speedup"]
        if per_cases:
            recomputed = math.exp(
                sum(item["weight"] * math.log(item["speedup"]) for item in per_cases)
                / sum(item["weight"] for item in per_cases)
            )
            if geometric_mean is None or not math.isclose(
                geometric_mean, recomputed, rel_tol=1e-12, abs_tol=1e-12
            ):
                raise ValidationError(
                    "candidate geometric mean does not match direct per-case speedups"
                )
        elif geometric_mean is not None:
            raise ValidationError(
                "candidate study without comparable cases cannot report a geometric mean"
            )
        remarks = result["remarks"]
        if remarks["paired_candidate_count"] != (
            remarks["applied_count"] + remarks["rejected_count"]
        ):
            raise ValidationError(
                "candidate matched/applied/rejected remark counts are not paired"
            )
        static_values = (
            result["static_text_bytes_full"],
            result["static_text_bytes_full_plus_candidate"],
            result["static_text_ratio"],
        )
        if any(value is None for value in static_values) != all(
            value is None for value in static_values
        ):
            raise ValidationError("candidate static text evidence must be all null or complete")
        if static_values[0] is not None and not math.isclose(
            float(static_values[2]),
            float(static_values[0]) / float(static_values[1]),
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValidationError("candidate static text ratio must be FULL / FULL+candidate")
        reason = None
        if result["correctness_failures"]:
            reason = "correctness_failure"
        elif result["excluded_cases"]:
            reason = "incomplete_profile"
        elif result["censored_cases"]:
            reason = "right_censored"
        elif result["comparable_cases"] == 0:
            reason = "no_comparable_cases"
        elif remarks["paired_candidate_count"] == 0:
            reason = "no_candidate_observation"
        if result["ineligibility_reason"] != reason or result[
            "eligible_for_ranking"
        ] != (reason is None):
            raise ValidationError(
                "candidate ranking eligibility/reason is inconsistent with evidence"
            )
        if (
            result["comparable_cases"]
            + result["censored_cases"]
            + result["excluded_cases"]
            != expected_case_count
        ):
            raise ValidationError(
                "candidate study result does not cover the exact suite manifest count"
            )

    interactions = document["interactions"]
    if interactions and document["data_role"] != "B3":
        raise ValidationError("candidate interactions are diagnostic B3 evidence only")
    eligible_singles = sorted(
        (item for item in document["candidates"] if item["eligible_for_ranking"]),
        key=lambda item: (
            -float(item["case_geometric_mean_speedup"]),
            item["candidate_id"],
        ),
    )
    top_ids = [item["candidate_id"] for item in eligible_singles[:3]]
    expected_pairs = {
        frozenset(pair) for pair in combinations(top_ids, 2)
    }
    actual_pairs = [frozenset(item["candidate_ids"]) for item in interactions]
    raw_interaction_pairs = [
        frozenset(item["candidate_ids"]) for item in raw["interactions"]
    ]
    if (
        raw_interaction_pairs != actual_pairs
        or any(
            raw_item["run"]["run_id"] != interaction["run_id"]
            or raw_item["run"]["run_canonical_sha256"]
            != interaction["run_sha256"]
            for raw_item, interaction in zip(raw["interactions"], interactions)
        )
    ):
        raise ValidationError("candidate study raw-evidence interaction binding differs")
    if interactions and (
        len(actual_pairs) != len(set(actual_pairs))
        or set(actual_pairs) != expected_pairs
    ):
        raise ValidationError(
            "candidate interactions must cover exactly the B3 single-GM Top3 pairs"
        )
    candidate_by_id = {
        item["candidate_id"]: item for item in document["candidates"]
    }
    physical_run_ids = set(run_ids) | {document["baseline"]["run_id"]}
    physical_run_hashes = set(run_hashes) | {document["baseline"]["run_sha256"]}
    for interaction in interactions:
        pair = interaction["candidate_ids"]
        if interaction["enabled_candidate_ids"] != pair:
            raise ValidationError(
                "candidate interaction enablement differs from its pair"
            )
        observations = interaction["candidate_observations"]
        if [item["candidate_id"] for item in observations] != pair:
            raise ValidationError(
                "candidate interaction observations do not cover its pair in order"
            )
        if (
            interaction["run_id"] in physical_run_ids
            or interaction["run_sha256"] in physical_run_hashes
        ):
            raise ValidationError(
                "candidate interaction must bind a distinct physical run"
            )
        physical_run_ids.add(interaction["run_id"])
        physical_run_hashes.add(interaction["run_sha256"])
        per_cases = interaction["per_cases"]
        if (
            len({item["case_id"] for item in per_cases}) != len(per_cases)
            or len(per_cases) != interaction["comparable_cases"]
        ):
            raise ValidationError(
                "candidate interaction per_cases must cover comparable cases exactly once"
            )
        for item in per_cases:
            expected_speedup = item["metric_full"] / item[
                "metric_full_plus_candidate"
            ]
            if not math.isclose(
                item["speedup"], expected_speedup, rel_tol=1e-12, abs_tol=1e-12
            ):
                raise ValidationError(
                    "candidate interaction speedup must be FULL / FULL+pair"
                )
        reason = None
        if interaction["correctness_failures"]:
            reason = "correctness_failure"
        elif interaction["excluded_cases"]:
            reason = "incomplete_profile"
        elif interaction["censored_cases"]:
            reason = "right_censored"
        elif interaction["comparable_cases"] == 0:
            reason = "no_comparable_cases"
        elif any(item["paired_candidate_count"] == 0 for item in observations):
            reason = "no_candidate_observation"
        elif any(not candidate_by_id[item]["eligible_for_ranking"] for item in pair):
            reason = "constituent_ineligible"
        if interaction["ineligibility_reason"] != reason or interaction[
            "eligible_for_ranking"
        ] != (reason is None):
            raise ValidationError(
                "candidate interaction eligibility differs from physical evidence"
            )
        if (
            interaction["comparable_cases"]
            + interaction["censored_cases"]
            + interaction["excluded_cases"]
            != expected_case_count
        ):
            raise ValidationError(
                "candidate interaction does not cover the exact B3 manifest count"
            )
        inference = (
            interaction["pair_case_geometric_mean_speedup"],
            interaction["expected_multiplicative_speedup"],
            interaction["delta_ln_geometric_mean"],
        )
        if reason is not None:
            if any(value is not None for value in inference):
                raise ValidationError(
                    "ineligible candidate interaction cannot report inferred metrics"
                )
            continue
        recomputed_pair = math.exp(
            sum(item["weight"] * math.log(item["speedup"]) for item in per_cases)
            / sum(item["weight"] for item in per_cases)
        )
        expected_multiplicative = math.prod(
            float(candidate_by_id[item]["case_geometric_mean_speedup"])
            for item in pair
        )
        expected_delta = math.log(recomputed_pair) - sum(
            math.log(float(candidate_by_id[item]["case_geometric_mean_speedup"]))
            for item in pair
        )
        if (
            not math.isclose(
                interaction["pair_case_geometric_mean_speedup"],
                recomputed_pair,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
            or not math.isclose(
                interaction["expected_multiplicative_speedup"],
                expected_multiplicative,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
            or not math.isclose(
                interaction["delta_ln_geometric_mean"],
                expected_delta,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
        ):
            raise ValidationError(
                "candidate interaction must equal ln(S_AB)-ln(S_A)-ln(S_B)"
            )


def _validate_candidate_oracle_capture_semantics(document: dict[str, Any]) -> None:
    sources = document["sources"]
    validate_relative_path(
        sources["candidate_evidence"]["path"],
        label="candidate Oracle evidence source path",
    )
    validate_relative_path(
        sources["oracle_plan"]["path"],
        label="candidate Oracle plan source path",
    )
    validate_relative_path(
        document["raw_state_root"], label="candidate Oracle raw state root"
    )
    if (
        sources["candidate_evidence"]["canonical_sha256"]
        != document["candidate_evidence_sha256"]
        or sources["oracle_plan"]["canonical_sha256"]
        != document["oracle_plan_sha256"]
    ):
        raise ValidationError("candidate Oracle source artifact binding differs")
    if document["baseline"]["run_id"] == document["optimized"]["run_id"] or document[
        "baseline"
    ]["run_sha256"] == document["optimized"]["run_sha256"]:
        raise ValidationError("candidate Oracle capture requires distinct source-leg runs")
    raw = document["raw_evidence"]
    validate_relative_path(
        raw["baseline"]["run_record_path"],
        label="candidate Oracle baseline run-record path",
    )
    validate_relative_path(
        raw["optimized"]["run_record_path"],
        label="candidate Oracle optimized run-record path",
    )
    if (
        raw["baseline"]["run_id"] != document["baseline"]["run_id"]
        or raw["baseline"]["run_canonical_sha256"]
        != document["baseline"]["run_sha256"]
        or raw["optimized"]["run_id"] != document["optimized"]["run_id"]
        or raw["optimized"]["run_canonical_sha256"]
        != document["optimized"]["run_sha256"]
        or raw["baseline"]["run_physical_sha256"]
        == raw["optimized"]["run_physical_sha256"]
    ):
        raise ValidationError("candidate Oracle raw-evidence run binding differs")
    identifiers = [item["candidate_id"] for item in document["candidates"]]
    families = [item["oracle_family_id"] for item in document["candidates"]]
    if len(identifiers) != len(set(identifiers)):
        raise ValidationError("candidate Oracle capture contains duplicate candidates")
    if len(families) != len(set(families)):
        raise ValidationError("candidate Oracle capture contains duplicate Oracle families")
    all_pair_ids: list[str] = []
    for candidate in document["candidates"]:
        structures = candidate["structures"]
        structure_ids = [item["structure_id"] for item in structures]
        if len(structure_ids) != len(set(structure_ids)):
            raise ValidationError("candidate Oracle capture repeats a structure")
        for structure in structures:
            _validate_candidate_oracle_structure(
                structure,
                upper_bound_key="geometric_mean_upper_bound",
                expected_family=candidate["oracle_family_id"],
            )
            all_pair_ids.extend(
                structure["sizes"][size]["pair_id"]
                for size in ("small", "medium", "large")
            )
    if len(all_pair_ids) != document["pair_count"] or len(all_pair_ids) != len(
        set(all_pair_ids)
    ):
        raise ValidationError("candidate Oracle capture must bind 99 distinct physical pairs")


def _validate_candidate_oracle_structure(
    structure: Mapping[str, Any],
    *,
    upper_bound_key: str,
    expected_family: str | None = None,
) -> None:
    pairs = structure["sizes"]
    pair_ids: list[str] = []
    ordered: list[Mapping[str, Any]] = []
    for size in ("small", "medium", "large"):
        pair = pairs[size]
        ordered.append(pair)
        pair_ids.append(pair["pair_id"])
        if (
            pair["size"] != size
            or pair["structure_id"] != structure["structure_id"]
            or (expected_family is not None and pair["family"] != expected_family)
            or pair["pair_id"]
            != f"{pair['family']}:{pair['structure_id']}:{pair['size']}"
        ):
            raise ValidationError(
                "candidate Oracle pair identity differs from family/structure/size"
            )
        values = (
            pair["baseline_metric_value"],
            pair["optimized_metric_value"],
            pair["speedup"],
        )
        if pair["eligible_for_ranking"]:
            if pair["ineligibility_reason"] is not None or any(
                value is None for value in values
            ):
                raise ValidationError("eligible Oracle pair lacks complete metrics")
            assert all(value is not None for value in values)
            if not math.isclose(
                float(values[2]),
                float(values[0]) / float(values[1]),
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                raise ValidationError(
                    "candidate Oracle speedup must be baseline / optimized"
                )
        elif pair["ineligibility_reason"] is None or pair["speedup"] is not None:
            raise ValidationError("ineligible Oracle pair has inconsistent evidence")
    if len(pair_ids) != len(set(pair_ids)) or structure["paired_datasets"] != 3:
        raise ValidationError("candidate Oracle structure must bind three distinct sizes")
    reason = (
        None
        if all(item["eligible_for_ranking"] for item in ordered)
        else "incomplete_or_incorrect_oracle"
    )
    if structure["ineligibility_reason"] != reason or structure[
        "eligible_for_ranking"
    ] != (reason is None):
        raise ValidationError("candidate Oracle structure eligibility is inconsistent")
    upper_bound = structure[upper_bound_key]
    if reason is not None and upper_bound is not None:
        raise ValidationError("ineligible candidate Oracle structure reports an upper bound")
    if reason is None:
        recomputed = math.exp(
            sum(math.log(float(item["speedup"])) for item in ordered) / 3.0
        )
        if upper_bound is None or not math.isclose(
            upper_bound, recomputed, rel_tol=1e-12, abs_tol=1e-12
        ):
            raise ValidationError(
                "candidate Oracle structure upper bound differs from its three sizes"
            )


def _validate_candidate_campaign_plan_semantics(document: dict[str, Any]) -> None:
    if document["run_record_schema_sha256"] != schema_sha256("run-record.v1"):
        raise ValidationError("candidate campaign run-record schema binding is stale")
    if document["candidate_study_schema_sha256"] != schema_sha256(
        "candidate-study.v1"
    ):
        raise ValidationError("candidate campaign study schema binding is stale")
    if document["candidate_freeze_schema_sha256"] != schema_sha256(
        "candidate-freeze.v1"
    ):
        raise ValidationError("candidate campaign freeze schema binding is stale")
    if document["candidate_raw_evidence_schema_sha256"] != schema_sha256(
        "candidate-raw-evidence.v1"
    ):
        raise ValidationError("candidate campaign raw-evidence schema binding is stale")
    if document["run_namespace"] != f"{document['campaign_id']}:":
        raise ValidationError("candidate campaign run namespace differs")
    expected_counts = {"B1": 140, "B2": 20, "B3": 60, "B4": 59, "B5": 60, "B6": 88}
    suites = document["suites"]
    if [item["data_role"] for item in suites] != list(expected_counts) or any(
        item["case_count"] != expected_counts[item["data_role"]]
        for item in suites
    ):
        raise ValidationError("candidate campaign suites/counts are not canonical B1-B6")
    artifacts = [
        *document["artifacts"].values(),
        document["measurement_protocol"]["artifact"],
    ]
    artifacts.extend(item["manifest"] for item in suites)
    for artifact in artifacts:
        validate_relative_path(artifact["path"], label="candidate campaign artifact path")
    executable_registry = document["artifacts"]["executable_pass_registry"]
    base_registry = document["artifacts"]["screening_base_pass_registry"]
    if (
        executable_registry["canonical_sha256"]
        == base_registry["canonical_sha256"]
        or executable_registry["path"] == base_registry["path"]
    ):
        raise ValidationError(
            "candidate campaign executable/base PassRegistry artifacts must be distinct"
        )
    validate_relative_path(
        document["repository"]["compiler_artifact"]["path"],
        label="candidate campaign compiler artifact path",
    )
    validate_relative_path(
        document["raw_state_root"],
        label="candidate campaign raw state root",
    )
    protocol = document["measurement_protocol"]
    if protocol["protocol_sha256"] != protocol["artifact"]["canonical_sha256"]:
        raise ValidationError("candidate campaign protocol canonical identity differs")
    validate_relative_path(
        document["base_pipeline_profile"]["path"],
        label="candidate campaign base profile path",
    )
    candidates = document["qualified_candidate_ids"]
    if len(document["study_ids"].values()) != len(set(document["study_ids"].values())):
        raise ValidationError("candidate campaign study ids must be distinct")
    tasks = document["tasks"]
    task_ids = [item["task_id"] for item in tasks]
    run_ids = [item["run_id"] for item in tasks if item["run_id"] is not None]
    if len(task_ids) != len(set(task_ids)) or len(run_ids) != len(set(run_ids)):
        raise ValidationError("candidate campaign contains duplicate task/run ids")
    expected_ids = ["run.B1.full", *[f"run.B1.{item}" for item in candidates]]
    expected_ids.extend(
        ["run.B2.full", *[f"run.B2.{item}" for item in candidates], "study.B2", "freeze"]
    )
    expected_ids.extend(
        [
            "run.B3.full", "run.B3.gcc", "run.B3.clang",
            *[f"run.B3.{item}" for item in candidates], "study.B3",
        ]
    )
    for role in ("B4", "B5", "B6"):
        expected_ids.extend(
            [f"run.{role}.full", *[f"run.{role}.{item}" for item in candidates], f"study.{role}"]
        )
    expected_ids.append("final")
    if task_ids != expected_ids:
        raise ValidationError("candidate campaign task order differs from the authoritative DAG")
    known: set[str] = set()
    for task in tasks:
        dependencies = [*task["dependencies"], *task["terminal_dependencies"]]
        if len(dependencies) != len(set(dependencies)) or not set(dependencies).issubset(known):
            raise ValidationError("candidate campaign dependencies must be unique earlier tasks")
        known.add(task["task_id"])
        gate = task["gate"]
        if (gate["kind"] in {"b1_completed", "b3_promoted"}) != (
            gate["candidate_id"] is not None
        ):
            raise ValidationError("candidate campaign gate/candidate binding differs")
        if gate["candidate_id"] is not None and gate["candidate_id"] not in candidates:
            raise ValidationError("candidate campaign gate references an unknown candidate")
        if task["task_type"] == "run":
            if task["run_id"] != f"{document['run_namespace']}{task['task_id']}":
                raise ValidationError("candidate campaign run id escapes its namespace")
            if task["data_role"] != task["stage"]:
                raise ValidationError("candidate campaign run stage/data role differs")
            if task["kind"] == "reference":
                if (
                    task["reference_profile_id"] is None
                    or task["candidate_ids"]
                    or task["logical_profile_id"] is not None
                    or task["candidate_profile_sha256"] is not None
                    or task["candidate_profile_path"] is not None
                    or task["ranking_evidence"]
                ):
                    raise ValidationError("candidate reference task contract differs")
            else:
                if (
                    task["reference_profile_id"] is not None
                    or task["logical_profile_id"] is None
                    or task["candidate_profile_sha256"] is None
                    or task["candidate_profile_path"] is None
                ):
                    raise ValidationError("candidate profile task lacks its exact profile")
                validate_relative_path(
                    task["candidate_profile_path"],
                    label="candidate campaign profile path",
                )
                expected_candidates = 1 if task["kind"] == "single" else 0
                if len(task["candidate_ids"]) != expected_candidates:
                    raise ValidationError("candidate task/profile arity differs")
        elif any(
            value is not None
            for value in (
                task["run_id"], task["logical_profile_id"],
                task["candidate_profile_sha256"], task["candidate_profile_path"],
                task["reference_profile_id"],
            )
        ) or task["candidate_ids"] or task["ranking_evidence"]:
            raise ValidationError("candidate pseudo-task carries run/profile evidence")
    by_id = {item["task_id"]: item for item in tasks}
    if by_id["run.B1.full"]["dependencies"] or by_id["run.B1.full"]["terminal_dependencies"]:
        raise ValidationError("candidate B1 FULL must be the sole initial task")
    previous = None
    for candidate in candidates:
        task = by_id[f"run.B1.{candidate}"]
        if task["dependencies"] != ["run.B1.full"] or task["terminal_dependencies"] != (
            [] if previous is None else [previous]
        ):
            raise ValidationError("candidate B1 profiles must be serial after FULL")
        previous = task["task_id"]
    if by_id["run.B2.full"]["terminal_dependencies"] != [
        f"run.B1.{item}" for item in candidates
    ]:
        raise ValidationError("candidate B2 FULL must wait for all terminal B1 candidates")
    previous = None
    for candidate in candidates:
        task = by_id[f"run.B2.{candidate}"]
        expected_terminal = [f"run.B1.{candidate}", *([] if previous is None else [previous])]
        if (
            task["dependencies"] != ["run.B2.full"]
            or task["terminal_dependencies"] != expected_terminal
            or task["gate"] != {"kind": "b1_completed", "candidate_id": candidate}
        ):
            raise ValidationError("candidate B2 selection/serialization differs")
        previous = task["task_id"]
    if (
        by_id["freeze"]["dependencies"] != ["study.B2"]
        or by_id["freeze"]["gate"]
        != {"kind": "b2_formal_complete", "candidate_id": None}
    ):
        raise ValidationError("candidate freeze must follow the exact B2 study")
    if by_id["run.B3.full"]["dependencies"] != ["freeze"]:
        raise ValidationError("candidate B3 FULL must depend on freeze")
    previous_validation_study = "study.B3"
    for role in ("B4", "B5", "B6"):
        if (
            by_id[f"run.{role}.full"]["gate"]["kind"] != "b3_has_promoted"
            or by_id[f"run.{role}.full"]["dependencies"]
            != [previous_validation_study]
        ):
            raise ValidationError("candidate validation FULL gate differs")
        for candidate in candidates:
            if by_id[f"run.{role}.{candidate}"]["gate"] != {
                "kind": "b3_promoted", "candidate_id": candidate
            }:
                raise ValidationError("candidate B4-B6 promotion gate differs")
        previous_validation_study = f"study.{role}"
    if by_id["final"]["dependencies"] != ["study.B3"] or by_id["final"]["terminal_dependencies"] != [
        "study.B4", "study.B5", "study.B6"
    ]:
        raise ValidationError("candidate final task dependencies differ")


def _validate_candidate_campaign_status_semantics(document: dict[str, Any]) -> None:
    validate_relative_path(
        document["raw_evidence_registry"]["path"],
        label="candidate campaign raw evidence registry path",
    )
    tasks = document["tasks"]
    task_ids = [item["task_id"] for item in tasks]
    if len(task_ids) != len(set(task_ids)):
        raise ValidationError("candidate campaign status contains duplicate tasks")
    if len(document["ready_tasks"]) > 1:
        raise ValidationError("candidate campaign may expose only one serial profile/pseudo-task")
    by_id = {item["task_id"]: item for item in tasks}
    for item in tasks:
        status = item["status"]
        has_evidence = item["evidence_sha256"] is not None
        has_physical_evidence = item["evidence_physical_sha256"] is not None
        if has_evidence != has_physical_evidence or has_evidence != (status not in {"pending", "ineligible"}) or (
            has_evidence != (item["evidence_kind"] is not None)
        ):
            raise ValidationError("candidate campaign task evidence binding is inconsistent")
        if status == "pending":
            if item["started_at"] is not None or item["completed_at"] is not None:
                raise ValidationError("pending candidate campaign task carries timestamps")
        elif status == "ineligible":
            if item["started_at"] is not None or item["completed_at"] is None:
                raise ValidationError("candidate gate decision timestamp binding differs")
        elif item["started_at"] is None:
            raise ValidationError("started candidate campaign task lacks started_at")
        if (status in {"completed", "failed", "interrupted", "ineligible"}) != (
            item["completed_at"] is not None
        ):
            raise ValidationError("candidate campaign terminal timestamp binding differs")
        if (status == "ineligible") != (item["ineligibility_reason"] is not None):
            raise ValidationError("candidate campaign ineligibility reason binding differs")
    diagnostic = document["diagnostic_plan"]
    diagnostic_by_id: dict[str, dict[str, Any]] = {}
    if diagnostic is not None:
        validate_relative_path(
            diagnostic["matrix"]["path"],
            label="candidate diagnostic matrix path",
        )
        top = diagnostic["top3_candidate_ids"]
        expected_pair_sets = [frozenset(pair) for pair in combinations(top, 2)]
        pair_tasks = [item for item in diagnostic["tasks"] if item["kind"] == "pair"]
        if [frozenset(item["candidate_ids"]) for item in pair_tasks] != expected_pair_sets:
            raise ValidationError("candidate diagnostic plan differs from exact Top3 pair set")
        if any(
            item["task_id"]
            != f"diagnostic.pair.{'+'.join(sorted(item['candidate_ids']))}"
            for item in pair_tasks
        ):
            raise ValidationError("candidate diagnostic pair task id is not canonical")
        expected_cache_specs = [
            (
                "diagnostic.cache.full" if candidate is None else f"diagnostic.cache.{candidate}",
                "cache_hotblock",
                [] if candidate is None else [candidate],
            )
            for candidate in [None, *top]
        ]
        actual_cache_specs = [
            (item["task_id"], item["kind"], item["candidate_ids"])
            for item in diagnostic["tasks"]
            if item["kind"] == "cache_hotblock"
        ]
        if (
            [item["kind"] for item in diagnostic["tasks"]]
            != ["pair"] * len(pair_tasks)
            + ["cache_hotblock"] * len(expected_cache_specs)
            or actual_cache_specs != expected_cache_specs
        ):
            raise ValidationError("candidate diagnostic plan differs from exact Top3 pairs/cache set")
        dependency = "study.B3"
        for task in diagnostic["tasks"]:
            if task["dependencies"] != [dependency] or task["ranking_evidence"]:
                raise ValidationError("candidate diagnostic profiles must be serial and non-ranking")
            if task["run_id"] != f"{document['campaign_id']}:{task['task_id']}":
                raise ValidationError("candidate diagnostic run namespace differs")
            validate_relative_path(
                task["profile_path"],
                label=f"candidate diagnostic profile path {task['task_id']}",
            )
            expected_mode = (
                "standard_proxy" if task["kind"] == "pair" else "cache_hotblock"
            )
            if task["measurement_mode"] != expected_mode:
                raise ValidationError("candidate diagnostic measurement mode differs")
            if task["kind"] == "pair" and len(task["candidate_ids"]) != 2:
                raise ValidationError("candidate diagnostic pair must enable exactly two candidates")
            if task["kind"] == "cache_hotblock" and len(task["candidate_ids"]) > 1:
                raise ValidationError("candidate cache/hotblock task enables too many candidates")
            status = task["status"]
            has_evidence = task["evidence_sha256"] is not None
            has_physical = task["evidence_physical_sha256"] is not None
            has_configuration = task["configuration_sha256"] is not None
            if (
                has_evidence != has_physical
                or has_evidence != has_configuration
                or has_evidence != (status != "pending")
            ):
                raise ValidationError("candidate diagnostic evidence binding is inconsistent")
            if status == "pending":
                if (
                    task["started_at"] is not None
                    or task["completed_at"] is not None
                    or task["failure_reason"] is not None
                ):
                    raise ValidationError("pending candidate diagnostic task carries evidence state")
            elif task["started_at"] is None:
                raise ValidationError("started candidate diagnostic task lacks started_at")
            if (status in {"completed", "failed", "interrupted"}) != (
                task["completed_at"] is not None
            ):
                raise ValidationError("candidate diagnostic terminal timestamp binding differs")
            if (status in {"failed", "interrupted"}) != (
                task["failure_reason"] is not None
            ):
                raise ValidationError("candidate diagnostic failure classification differs")
            dependency = task["task_id"]
        study_task = diagnostic["study"]
        if study_task["dependencies"] != [dependency]:
            raise ValidationError("candidate diagnostic study dependency differs")
        study_status = study_task["status"]
        study_has_evidence = study_task["evidence_sha256"] is not None
        study_has_physical = study_task["evidence_physical_sha256"] is not None
        if study_has_evidence != study_has_physical or study_has_evidence != (
            study_status == "completed"
        ):
            raise ValidationError("candidate diagnostic study evidence binding differs")
        if (study_status == "completed") != (
            study_task["evidence_kind"] == "candidate-study.v1"
        ):
            raise ValidationError("candidate diagnostic study evidence kind differs")
        if study_status == "pending":
            if any(
                study_task[key] is not None
                for key in (
                    "started_at",
                    "completed_at",
                    "ineligibility_reason",
                )
            ):
                raise ValidationError("pending candidate diagnostic study carries state")
        elif study_status == "completed":
            if (
                study_task["started_at"] is None
                or study_task["completed_at"] is None
                or study_task["ineligibility_reason"] is not None
            ):
                raise ValidationError("completed candidate diagnostic study state differs")
        elif (
            study_task["started_at"] is not None
            or study_task["completed_at"] is None
            or study_task["ineligibility_reason"] is None
        ):
            raise ValidationError("ineligible candidate diagnostic study state differs")
        diagnostic_by_id = {
            item["task_id"]: item for item in diagnostic["tasks"]
        }
        diagnostic_by_id[study_task["task_id"]] = study_task
    all_ready_ids = set(task_ids) | set(diagnostic_by_id)
    if not set(document["ready_tasks"]).issubset(all_ready_ids):
        raise ValidationError("candidate campaign ready_tasks references an unknown task")
    if any(
        (by_id.get(item) or diagnostic_by_id[item])["status"] != "pending"
        for item in document["ready_tasks"]
    ):
        raise ValidationError("candidate campaign ready task is not pending")
    if document["state"] == "completed" and by_id.get("final", {}).get("status") != "completed":
        raise ValidationError("completed candidate campaign lacks candidate-final evidence")
    if document["state"] in {"completed", "failed", "interrupted"} and document[
        "ready_tasks"
    ]:
        raise ValidationError("terminal candidate campaign cannot expose ready tasks")
    started = datetime.fromisoformat(document["started_at"].replace("Z", "+00:00"))
    observed = datetime.fromisoformat(document["as_of"].replace("Z", "+00:00"))
    if started > observed:
        raise ValidationError("candidate campaign started_at follows as_of")
    for item in tasks:
        task_started = (
            None
            if item["started_at"] is None
            else datetime.fromisoformat(item["started_at"].replace("Z", "+00:00"))
        )
        task_completed = (
            None
            if item["completed_at"] is None
            else datetime.fromisoformat(item["completed_at"].replace("Z", "+00:00"))
        )
        if task_started is not None and not (started <= task_started <= observed):
            raise ValidationError("candidate campaign task start lies outside ledger time")
        if task_completed is not None and (
            task_completed > observed
            or (task_started is not None and task_completed < task_started)
        ):
            raise ValidationError("candidate campaign task completion violates ledger time")
    for item in diagnostic_by_id.values():
        task_started = (
            None
            if item["started_at"] is None
            else datetime.fromisoformat(item["started_at"].replace("Z", "+00:00"))
        )
        task_completed = (
            None
            if item["completed_at"] is None
            else datetime.fromisoformat(item["completed_at"].replace("Z", "+00:00"))
        )
        if task_started is not None and not (started <= task_started <= observed):
            raise ValidationError("candidate diagnostic task start lies outside ledger time")
        if task_completed is not None and (
            task_completed > observed
            or (task_started is not None and task_completed < task_started)
        ):
            raise ValidationError("candidate diagnostic task completion violates ledger time")


def _validate_candidate_raw_evidence_semantics(document: dict[str, Any]) -> None:
    validate_relative_path(
        document["raw_state_root"], label="candidate raw evidence state root"
    )
    runs = document["runs"]
    expected_registry_id = (
        f"{document['campaign_id']}:raw:{sha256_json(runs)[:32]}"
    )
    if document["registry_id"] != expected_registry_id:
        raise ValidationError("candidate raw evidence registry id is not content-derived")
    task_ids = [item["task_id"] for item in runs]
    if task_ids != sorted(task_ids) or len(task_ids) != len(set(task_ids)):
        raise ValidationError(
            "candidate raw evidence run tasks must be unique and lexically ordered"
        )
    run_ids: set[str] = set()
    for item in runs:
        artifact = item["run_record"]
        verification = item["verification"]
        validate_relative_path(
            artifact["path"], label=f"candidate raw run {item['task_id']} path"
        )
        if (
            verification["run_id"] in run_ids
            or artifact["canonical_sha256"]
            != verification["run_canonical_sha256"]
            or artifact["physical_sha256"]
            != verification["run_physical_sha256"]
        ):
            raise ValidationError("candidate raw run identity/hash binding differs")
        run_ids.add(verification["run_id"])
        cases = verification["cases"]
        case_ids = [case["case_id"] for case in cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValidationError("candidate raw evidence contains duplicate cases")
        attempts = [attempt for case in cases for attempt in case["attempts"]]
        if (
            verification["attempt_count"] != len(attempts)
            or verification["terminal_attempt_count"] != len(attempts)
        ):
            raise ValidationError("candidate raw evidence attempt counts differ")
        for case in cases:
            indexes = [attempt["attempt_index"] for attempt in case["attempts"]]
            if indexes != list(range(len(indexes))) or case[
                "current_attempt_index"
            ] not in {None, indexes[-1] if indexes else None}:
                raise ValidationError(
                    "candidate raw evidence attempt ordering/current binding differs"
                )
        commitment = {
            key: value
            for key, value in verification.items()
            if key != "raw_evidence_sha256"
        }
        if verification["raw_evidence_sha256"] != sha256_json(
            {
                "schema_version": "benchmark-run-raw-evidence-commitment.v1",
                "document": commitment,
            }
        ):
            raise ValidationError("candidate raw evidence commitment differs")


def _validate_candidate_freeze_semantics(document: dict[str, Any]) -> None:
    expected_counts = {
        "B1": 140,
        "B2": 20,
        "B3": 60,
        "B4": 59,
        "B5": 60,
        "B6": 88,
    }
    suites = document["suites"]
    validate_relative_path(
        document["repository"]["compiler_artifact"]["path"],
        label="candidate freeze compiler artifact path",
    )
    validate_relative_path(
        document["b2_campaign"]["raw_evidence_registry"]["path"],
        label="candidate freeze raw evidence registry path",
    )
    if [item["data_role"] for item in suites] != list(expected_counts):
        raise ValidationError(
            "candidate freeze suites must be canonical B1 through B6"
        )
    if any(
        item["case_count"] != expected_counts[item["data_role"]]
        for item in suites
    ):
        raise ValidationError("candidate freeze suite case counts differ from protocol")
    if len(
        {item["manifest"]["canonical_sha256"] for item in suites}
    ) != len(suites):
        raise ValidationError("candidate freeze manifests must be physically distinct")
    if document["run_namespace"] != f"{document['campaign_id']}:":
        raise ValidationError("candidate freeze run namespace differs from campaign id")
    protocols = document["measurement_protocols"]
    for mode in ("standard_proxy", "cache_hotblock"):
        if protocols[mode]["measurement_mode"] != mode:
            raise ValidationError(
                "candidate freeze protocol key/mode binding is inconsistent"
            )
    if (
        protocols["standard_proxy"]["protocol_sha256"]
        == protocols["cache_hotblock"]["protocol_sha256"]
        or protocols["standard_proxy"]["runner_command_sha256"]
        == protocols["cache_hotblock"]["runner_command_sha256"]
    ):
        raise ValidationError(
            "candidate freeze requires distinct standard and cache protocols"
        )
    baselines = document["reference_toolchain"]["baselines"]
    if [item["compiler_baseline"] for item in baselines] != [
        "gcc_13_3_o2",
        "clang_18_o3",
    ]:
        raise ValidationError(
            "candidate freeze toolchain baselines must be GCC then Clang"
        )
    snapshots = document["snapshots"]
    if (
        snapshots["run_record_schema"]["physical_sha256"]
        != schema_sha256("run-record.v1")
        or snapshots["run_record_schema"]["canonical_sha256"]
        != sha256_json(_load_schema("run-record.v1"))
    ):
        raise ValidationError("candidate freeze run-record schema binding is stale")
    if (
        snapshots["candidate_study_schema"]["physical_sha256"]
        != schema_sha256("candidate-study.v1")
        or snapshots["candidate_study_schema"]["canonical_sha256"]
        != sha256_json(_load_schema("candidate-study.v1"))
    ):
        raise ValidationError("candidate freeze study schema binding is stale")
    artifact_rows = [
        snapshots[key]
        for key in (
            "candidate_registry",
            "executable_pass_registry",
            "screening_base_pass_registry",
            "matrix",
            "screening",
            "oracle_capture",
            "run_record_schema",
            "candidate_study_schema",
        )
    ]
    if (
        snapshots["executable_pass_registry"]["canonical_sha256"]
        == snapshots["screening_base_pass_registry"]["canonical_sha256"]
        or snapshots["executable_pass_registry"]["path"]
        == snapshots["screening_base_pass_registry"]["path"]
    ):
        raise ValidationError(
            "candidate freeze executable/base PassRegistry artifacts must be distinct"
        )
    artifact_rows.extend(item["manifest"] for item in suites)
    artifact_rows.append(document["base_pipeline_profile"]["artifact"])
    artifact_rows.append(document["reference_toolchain"]["snapshot"])
    for mode in ("standard_proxy", "cache_hotblock"):
        protocol = protocols[mode]
        artifact_rows.append(
            {
                "path": protocol["path"],
                "canonical_sha256": protocol["protocol_sha256"],
                "physical_sha256": protocol["physical_sha256"],
            }
        )
    for artifact in artifact_rows:
        validate_relative_path(
            artifact["path"], label="candidate freeze artifact path"
        )
    if document["frozen_candidate_ids_sha256"] != sha256_json(
        document["frozen_candidate_ids"]
    ):
        raise ValidationError("candidate freeze candidate identity hash is inconsistent")
    b2 = document["b2_campaign"]
    if b2["status_ledger_head_sha256"] != b2["status_sha256"]:
        raise ValidationError("candidate freeze status ledger head differs from status")
    passed = b2["b1_passed_candidate_ids"]
    failed = b2["b1_failed_candidate_ids"]
    if (
        set(passed) & set(failed)
        or set(passed) | set(failed) != set(document["frozen_candidate_ids"])
        or passed != [item for item in document["frozen_candidate_ids"] if item in set(passed)]
        or failed != [item for item in document["frozen_candidate_ids"] if item in set(failed)]
    ):
        raise ValidationError("candidate freeze B1 pass/fail partition differs from implemented candidates")


def _validate_candidate_final_semantics(document: dict[str, Any]) -> None:
    candidates = document["candidates"]
    identifiers = [item["candidate_id"] for item in candidates]
    if len(identifiers) != len(set(identifiers)):
        raise ValidationError("candidate final repeats a candidate")
    oracle_families = [item["oracle_family_id"] for item in candidates]
    implementation_ids = [
        item["implementation_candidate_id"]
        for item in candidates
        if item["implementation_candidate_id"] is not None
    ]
    if len(oracle_families) != len(set(oracle_families)) or len(
        implementation_ids
    ) != len(set(implementation_ids)):
        raise ValidationError("candidate final repeats an Oracle or implementation identity")
    if tuple(identifiers) != _LOCKED_CANDIDATE_IDS or any(
        item["oracle_family_id"]
        != _LOCKED_CANDIDATE_FAMILIES[item["candidate_id"]]
        for item in candidates
    ):
        raise ValidationError("candidate final differs from the locked eleven families")
    by_id = {
        item["implementation_candidate_id"]: item
        for item in candidates
        if item["implementation_candidate_id"] is not None
    }
    eligible_ids: list[str] = []
    qualified_ids: list[str] = []
    freeze = document["freeze"]
    validate_relative_path(
        document["campaign"]["raw_evidence_registry"]["path"],
        label="candidate final campaign raw evidence registry path",
    )
    validate_relative_path(
        freeze["raw_evidence_registry"]["path"],
        label="candidate final freeze raw evidence registry path",
    )
    validate_relative_path(
        freeze["artifact"]["path"],
        label="candidate final freeze artifact path",
    )
    if freeze["artifact"]["canonical_sha256"] != freeze["freeze_sha256"]:
        raise ValidationError("candidate final freeze artifact identity differs")
    for role, study in document["studies"].items():
        if study is not None:
            validate_relative_path(
                study["path"],
                label=f"candidate final {role} study path",
            )
    campaign_runs = document["campaign"]["run_records"]
    if document["campaign"]["status_ledger_head_sha256"] != document["campaign"]["status_sha256"]:
        raise ValidationError("candidate final status ledger head differs from status")
    if freeze["freeze_status_ledger_entry_count"] > document["campaign"]["status_ledger_entry_count"]:
        raise ValidationError("candidate final ledger is shorter than its pre-B3 freeze ledger")
    if (
        len({item["task_id"] for item in campaign_runs}) != len(campaign_runs)
        or len({item["run_id"] for item in campaign_runs}) != len(campaign_runs)
        or len({item["run_sha256"] for item in campaign_runs}) != len(campaign_runs)
        or len({item["run_physical_sha256"] for item in campaign_runs}) != len(campaign_runs)
    ):
        raise ValidationError("candidate final campaign repeats a raw run identity")
    b1_full = document["b1_full_correctness"]
    if (
        not b1_full["all_correct"]
        or b1_full["run_sha256"] != freeze["b1_full_run_sha256"]
        or b1_full["manifest_sha256"]
        != freeze["suite_manifests"]["B1"]["canonical_sha256"]
        or not any(
            item["task_id"] == "run.B1.full"
            and item["run_id"] == b1_full["run_id"]
            and item["run_sha256"] == b1_full["run_sha256"]
            for item in campaign_runs
        )
    ):
        raise ValidationError("candidate final B1 FULL identity/classification differs")
    expected_suite_counts = {"B2": 20, "B3": 60, "B4": 59, "B5": 60, "B6": 88}
    for item in candidates:
        outcomes = item["suite_outcomes"]
        b3 = outcomes["B3"]
        for role in ("B3", "B4", "B5", "B6"):
            outcome = outcomes[role]
            if outcome is None:
                continue
            study = document["studies"][role]
            if study is None or (
                outcome["study_id"] != study["study_id"]
                or outcome["suite_id"] != study["suite_id"]
                or outcome["manifest_sha256"] != study["manifest_sha256"]
                or outcome["expected_case_count"] != expected_suite_counts[role]
            ):
                raise ValidationError(
                    "candidate suite outcome differs from its bound study/manifest"
                )
            expected_reason = None
            if outcome["correctness_failures"]:
                expected_reason = "correctness_failure"
            elif outcome["excluded_cases"]:
                expected_reason = "incomplete_profile"
            elif outcome["censored_cases"]:
                expected_reason = "right_censored"
            elif outcome["comparable_cases"] == 0:
                expected_reason = "no_comparable_cases"
            if outcome["ineligibility_reason"] not in {
                expected_reason,
                "no_candidate_observation" if expected_reason is None else expected_reason,
            } or outcome["eligible_for_ranking"] != (
                outcome["ineligibility_reason"] is None
            ):
                raise ValidationError(
                    "candidate suite failure classification is inconsistent"
                )
            inferred = (
                outcome["case_geometric_mean_speedup"],
                outcome["source_group_geometric_mean_speedup"],
                outcome["confidence_interval_95"],
            )
            if outcome["eligible_for_ranking"] != all(
                value is not None for value in inferred
            ):
                raise ValidationError(
                    "candidate suite GM/CI availability differs from eligibility"
                )
        if item["screening_status"] == "qualified":
            implementation_id = item["implementation_candidate_id"]
            if implementation_id is None:
                raise ValidationError(
                    "qualified final candidate lacks an implementation identity"
                )
            qualified_ids.append(implementation_id)
            if item["b1_correctness"] is None:
                raise ValidationError(
                    "implemented candidate final lacks B1 correctness evidence"
                )
            b1 = item["b1_correctness"]
            if (
                b1["manifest_sha256"]
                != freeze["suite_manifests"]["B1"]["canonical_sha256"]
                or
                b1["passed_cases"]
                + b1["failed_cases"]
                + b1["pending_cases"]
                != b1["case_count"]
                or b1["all_correct"]
                != (
                    b1["state"] == "completed"
                    and b1["passed_cases"] == b1["case_count"]
                    and b1["failed_cases"] == 0
                    and b1["pending_cases"] == 0
                    and b1["censored_cases"] == 0
                )
                or (b1["state"] == "completed") != (b1["failure_reason"] is None)
            ):
                raise ValidationError(
                    "candidate B1 correctness classification is inconsistent"
                )
            if b1["all_correct"] != (item["b2_tuning"] is not None):
                raise ValidationError(
                    "B2 must contain every and only B1-correct candidate"
                )
            b2 = item["b2_tuning"]
            if b2 is not None:
                study = document["studies"]["B2"]
                if (
                    b2["study_id"] != study["study_id"]
                    or b2["suite_id"] != study["suite_id"]
                    or b2["manifest_sha256"] != study["manifest_sha256"]
                    or b2["expected_case_count"] != expected_suite_counts["B2"]
                ):
                    raise ValidationError(
                        "candidate B2 tuning outcome differs from its bound study"
                    )
        elif (
            item["b1_correctness"] is not None
            or item["b2_tuning"] is not None
            or any(outcome is not None for outcome in outcomes.values())
        ):
            raise ValidationError("screening-ineligible candidate cannot carry final studies")
        if (
            b3 is not None
            and b3["eligible_for_ranking"]
            and b3["case_geometric_mean_speedup"] is not None
            and b3["case_geometric_mean_speedup"] <= 1.0
            and any(outcomes[role] is not None for role in ("B4", "B5", "B6"))
        ):
            raise ValidationError("B3 GM<=1 candidate must not enter B4-B6")
        reasons = item["final_ineligibility_reasons"]
        b1 = item["b1_correctness"]
        if item["screening_status"] == "qualified" and b1 is not None:
            if (not b1["all_correct"]) != ("b1_not_correct" in reasons):
                raise ValidationError(
                    "candidate B1 outcome and final ineligibility reason differ"
                )
        complete_metrics = all(
            item[field] is not None
            for field in (
                "combined_case_geometric_mean_speedup",
                "b3_case_geometric_mean_speedup",
                "combined_static_text_bytes_full_plus_candidate",
                "combined_static_text_ratio",
            )
        )
        if item["eligible_for_final"] != (not reasons):
            raise ValidationError("candidate final eligibility/reasons are inconsistent")
        if item["eligible_for_final"]:
            if (
                item["combined_case_count"] != 267
                or not complete_metrics
                or item["b3_case_geometric_mean_speedup"] <= 1.0
                or item["rank"] is None
            ):
                raise ValidationError("eligible candidate final lacks the fixed final evidence")
            assert item["implementation_candidate_id"] is not None
            eligible_ids.append(item["implementation_candidate_id"])
        elif item["rank"] is not None:
            raise ValidationError("ineligible candidate final cannot have a rank")
    diagnostics = document["diagnostics"]
    if diagnostics["study"] is not None:
        validate_relative_path(
            diagnostics["study"]["path"],
            label="candidate final diagnostic study path",
        )
    if (
        diagnostics["source_freeze_sha256"] != freeze["freeze_sha256"]
        or diagnostics["source_study_sha256"]
        != document["studies"]["B3"]["study_sha256"]
    ):
        raise ValidationError("candidate final diagnostics differ from freeze/B3 evidence")
    validate_relative_path(
        diagnostics["matrix"]["path"],
        label="candidate final diagnostic matrix path",
    )
    expected_top3 = [
        item["implementation_candidate_id"]
        for item in sorted(
            (
                item
                for item in candidates
                if item["implementation_candidate_id"] is not None
                and item["suite_outcomes"]["B3"] is not None
                and item["suite_outcomes"]["B3"]["eligible_for_ranking"]
            ),
            key=lambda item: (
                -float(
                    item["suite_outcomes"]["B3"][
                        "case_geometric_mean_speedup"
                    ]
                ),
                item["implementation_candidate_id"],
            ),
        )[:3]
    ]
    if diagnostics["top3_candidate_ids"] != expected_top3:
        raise ValidationError("candidate final diagnostic Top3 differs from B3")
    diagnostic_tasks = diagnostics["tasks"]
    pair_tasks = [item for item in diagnostic_tasks if item["kind"] == "pair"]
    expected_pairs = [frozenset(pair) for pair in combinations(expected_top3, 2)]
    if (
        [frozenset(item["candidate_ids"]) for item in pair_tasks] != expected_pairs
        or any(
            item["task_id"]
            != f"diagnostic.pair.{'+'.join(sorted(item['candidate_ids']))}"
            or item["measurement_mode"] != "standard_proxy"
            for item in pair_tasks
        )
    ):
        raise ValidationError("candidate final diagnostics omit an exact Top3 pair")
    cache_tasks = [
        item for item in diagnostic_tasks if item["kind"] == "cache_hotblock"
    ]
    if [item["candidate_ids"] for item in cache_tasks] != [
        [], *[[candidate_id] for candidate_id in expected_top3]
    ] or any(
        item["measurement_mode"] != "cache_hotblock" for item in cache_tasks
    ) or [item["kind"] for item in diagnostic_tasks] != [
        "pair"
    ] * len(expected_pairs) + ["cache_hotblock"] * (len(expected_top3) + 1):
        raise ValidationError("candidate final cache/hotblock diagnostic set differs")
    campaign_run_by_task = {item["task_id"]: item for item in campaign_runs}
    dependency = "study.B3"
    final_generated_at = datetime.fromisoformat(
        document["generated_at"].replace("Z", "+00:00")
    )
    for task in diagnostic_tasks:
        if (
            task["dependencies"] != [dependency]
            or task["ranking_evidence"]
            or task["run_id"] != f"{freeze['run_namespace']}{task['task_id']}"
            or campaign_run_by_task.get(task["task_id"])
            != {
                "task_id": task["task_id"],
                "run_id": task["run_id"],
                "run_sha256": task["evidence_sha256"],
                "run_physical_sha256": task["evidence_physical_sha256"],
                "state": (
                    "completed"
                    if task["status"] == "completed"
                    else task["status"]
                ),
            }
        ):
            raise ValidationError("candidate final diagnostic/raw-run identity differs")
        if (task["status"] == "completed") != (task["failure_reason"] is None):
            raise ValidationError("candidate final diagnostic failure class differs")
        validate_relative_path(
            task["profile_path"],
            label=f"candidate final diagnostic profile path {task['task_id']}",
        )
        task_started = datetime.fromisoformat(
            task["started_at"].replace("Z", "+00:00")
        )
        task_completed = datetime.fromisoformat(
            task["completed_at"].replace("Z", "+00:00")
        )
        if not task_started <= task_completed <= final_generated_at:
            raise ValidationError("candidate final diagnostic timestamp differs")
        dependency = task["task_id"]
    expected_study_reason = "fewer_than_two_top3" if not pair_tasks else None
    if (
        diagnostics["study_status"]
        != ("ineligible" if expected_study_reason is not None else "completed")
        or diagnostics["study_ineligibility_reason"] != expected_study_reason
        or (diagnostics["study"] is None) != (expected_study_reason is not None)
    ):
        raise ValidationError("candidate final diagnostic study classification differs")
    ranking = document["ranking"]
    if [item["rank"] for item in ranking] != list(range(1, len(ranking) + 1)):
        raise ValidationError("candidate final ranking is not contiguous")
    if set(item["candidate_id"] for item in ranking) != set(eligible_ids):
        raise ValidationError("candidate final ranking differs from eligible candidate order")
    for row in ranking:
        candidate = by_id[row["candidate_id"]]
        if (
            row["stable_id_tiebreak"] != row["candidate_id"]
            or row["rank"] != candidate["rank"]
            or row["combined_case_geometric_mean_speedup"]
            != candidate["combined_case_geometric_mean_speedup"]
            or row["b3_case_geometric_mean_speedup"]
            != candidate["b3_case_geometric_mean_speedup"]
            or row["combined_static_text_bytes_full_plus_candidate"]
            != candidate["combined_static_text_bytes_full_plus_candidate"]
            or row["combined_static_text_ratio"]
            != candidate["combined_static_text_ratio"]
        ):
            raise ValidationError("candidate final ranking row differs from candidate evidence")
    expected = sorted(
        ranking,
        key=lambda row: (
            -row["combined_case_geometric_mean_speedup"],
            -row["b3_case_geometric_mean_speedup"],
            row["combined_static_text_bytes_full_plus_candidate"],
            row["stable_id_tiebreak"],
        ),
    )
    if ranking != expected:
        raise ValidationError("candidate final tie-break order is not canonical")
    if (
        freeze["candidate_registry"]["canonical_sha256"]
        != document["candidate_registry_sha256"]
        or freeze["matrix"]["canonical_sha256"] != document["matrix_sha256"]
        or freeze["screening"]["canonical_sha256"]
        != document["screening_sha256"]
        or freeze["b2_study_sha256"] != document["studies"]["B2"]["study_sha256"]
        or freeze["frozen_candidate_ids_sha256"] != sha256_json(qualified_ids)
        or freeze["run_namespace"] != f"{freeze['campaign_id']}:"
        or freeze["combined_case_count"]
        != document["expected_combined_case_count"]
        or freeze["ranking_rule"]
        != [
            "combined_geometric_mean_desc",
            "b3_geometric_mean_desc",
            "static_text_bytes_asc",
            "stable_candidate_id_asc",
        ]
        or any(
            study is not None
            and freeze["suite_manifests"][role]["canonical_sha256"]
            != study["manifest_sha256"]
            for role, study in document["studies"].items()
        )
    ):
        raise ValidationError("candidate final B2 freeze identity is inconsistent")
    promoted = any(
        item["suite_outcomes"]["B3"] is not None
        and item["suite_outcomes"]["B3"]["eligible_for_ranking"]
        and item["suite_outcomes"]["B3"]["case_geometric_mean_speedup"] > 1.0
        for item in candidates
    )
    if any(
        (document["studies"][role] is not None) != promoted
        for role in ("B4", "B5", "B6")
    ):
        raise ValidationError("candidate final validation studies differ from B3 promotion")
    if (
        set(freeze["b1_passed_candidate_ids"]) | set(freeze["b1_failed_candidate_ids"])
        != set(qualified_ids)
        or set(freeze["b1_passed_candidate_ids"])
        & set(freeze["b1_failed_candidate_ids"])
    ):
        raise ValidationError("candidate final frozen B1 partition differs")
    validate_relative_path(
        freeze["compiler_artifact"]["path"],
        label="candidate final frozen compiler artifact path",
    )
    for artifact in (
        freeze["candidate_registry"],
        freeze["executable_pass_registry"],
        freeze["screening_base_pass_registry"],
        freeze["matrix"],
        freeze["screening"],
        freeze["oracle_capture"],
        freeze["run_record_schema"],
        freeze["candidate_study_schema"],
        freeze["base_pipeline_profile"],
        freeze["standard_measurement_protocol"],
        freeze["hotblock_measurement_protocol"],
        freeze["reference_toolchain"]["snapshot"],
        *freeze["suite_manifests"].values(),
    ):
        validate_relative_path(
            artifact["path"], label="candidate final frozen artifact path"
        )
    if (
        document["executable_pass_registry_sha256"]
        != freeze["executable_pass_registry"]["canonical_sha256"]
        or freeze["executable_pass_registry"]["canonical_sha256"]
        == freeze["screening_base_pass_registry"]["canonical_sha256"]
        or freeze["executable_pass_registry"]["path"]
        == freeze["screening_base_pass_registry"]["path"]
    ):
        raise ValidationError(
            "candidate final executable/base PassRegistry binding differs"
        )
    expected_winner = (
        ranking[0]["candidate_id"]
        if ranking and ranking[0]["combined_case_geometric_mean_speedup"] > 1.0
        else None
    )
    if document["winner_candidate_id"] != expected_winner or document[
        "winner_reason"
    ] != (
        "top_combined_gm_above_one"
        if expected_winner is not None
        else "no_winning_candidate"
    ):
        raise ValidationError("candidate final winner gate is inconsistent")


def _validate_ablation_semantics(document: dict[str, Any]) -> None:
    identifiers = [variant["optimization_id"] for variant in document["variants"]]
    if len(identifiers) != len(set(identifiers)):
        raise ValidationError("optimization remark contains duplicate variant ids")
    known = set(identifiers)
    eligible_variants = {
        variant["optimization_id"]: variant["eligible_for_ranking"]
        for variant in document["variants"]
    }
    pairs: set[tuple[str, str]] = set()
    for interaction in document["interactions"]:
        left = interaction["left"]
        right = interaction["right"]
        if left not in known or right not in known:
            raise ValidationError("interaction references an unknown variant")
        if left == right:
            raise ValidationError("interaction variants must be distinct")
        pair = tuple(sorted((left, right)))
        if pair in pairs:
            raise ValidationError(f"duplicate interaction pair: {left}+{right}")
        pairs.add(pair)
        factor = interaction["interaction_factor"]
        delta = interaction["delta_ln_geometric_mean"]
        expected_reason = None
        if interaction["correctness_failures"]:
            expected_reason = "correctness_failure"
        elif interaction["excluded_cases"]:
            expected_reason = "incomplete_profile"
        elif interaction["censored_cases"]:
            expected_reason = "right_censored"
        elif interaction["comparable_cases"] == 0:
            expected_reason = "no_comparable_cases"
        elif not eligible_variants[left] or not eligible_variants[right]:
            expected_reason = "constituent_ineligible"
        if (
            interaction["ineligibility_reason"] != expected_reason
            or interaction["eligible_for_ranking"] != (expected_reason is None)
        ):
            raise ValidationError("interaction ranking eligibility/reason is inconsistent with evidence counts")
        inference_fields = (
            "observed_case_geometric_mean_contribution",
            "expected_multiplicative_contribution",
            "interaction_factor",
            "delta_ln_geometric_mean",
        )
        if expected_reason is not None and any(interaction[field] is not None for field in inference_fields):
            raise ValidationError("ineligible interaction must not report inferred contribution metrics")
        if (factor is None) != (delta is None):
            raise ValidationError("interaction factor and delta_ln_geometric_mean must both be null or non-null")
        if factor is not None and not math.isclose(delta, math.log(factor), rel_tol=1e-12, abs_tol=1e-12):
            raise ValidationError("interaction delta_ln_geometric_mean must equal ln(interaction_factor)")
    for variant in document["variants"]:
        full = variant["case_geometric_mean_contribution"]
        per_case_ids = [item["case_id"] for item in variant["per_cases"]]
        if len(per_case_ids) != len(set(per_case_ids)) or len(per_case_ids) != variant["comparable_cases"]:
            raise ValidationError("ablation per_cases must cover each comparable case exactly once")
        for item in variant["per_cases"]:
            if not math.isclose(
                item["contribution_ratio"],
                item["metric_without"] / item["metric_full"],
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                raise ValidationError("per-case contribution must equal metric_without / metric_full")
        if variant["per_cases"]:
            recomputed = math.exp(
                sum(item["weight"] * math.log(item["contribution_ratio"]) for item in variant["per_cases"])
                / sum(item["weight"] for item in variant["per_cases"])
            )
            if full is None or not math.isclose(full, recomputed, rel_tol=1e-12, abs_tol=1e-12):
                raise ValidationError("case geometric mean does not match per-case contribution evidence")
        elif full is not None:
            raise ValidationError("empty per-case evidence requires a null case geometric mean")
        reason = variant["ineligibility_reason"]
        expected_reason = None
        if variant["correctness_failures"]:
            expected_reason = "correctness_failure"
        elif variant["excluded_cases"]:
            expected_reason = "incomplete_profile"
        elif variant["censored_cases"]:
            expected_reason = "right_censored"
        elif variant["comparable_cases"] == 0:
            expected_reason = "no_comparable_cases"
        if reason != expected_reason or variant["eligible_for_ranking"] != (expected_reason is None):
            raise ValidationError("ablation ranking eligibility/reason is inconsistent with evidence counts")
        for contribution in variant["leave_one_family_out"]:
            if contribution["metric_full"] != full:
                raise ValidationError("leave-one-family-out metric_full must match variant geometric mean")
            without = contribution["metric_without"]
            ratio = contribution["contribution_ratio"]
            if full is None or without is None:
                if ratio is not None:
                    raise ValidationError("leave-one-family-out ratio must be null when a metric is unavailable")
            elif ratio is None or not math.isclose(ratio, without / full, rel_tol=1e-12, abs_tol=1e-12):
                raise ValidationError("leave-one-family-out contribution direction must be metric_without / metric_full")


def _validate_candidate_report_semantics(document: dict[str, Any]) -> None:
    """Validate normalized report relationships without reopening source files."""

    def unique(rows: list[dict[str, Any]], key: str, label: str) -> None:
        values = [row[key] for row in rows]
        if len(values) != len(set(values)):
            raise ValidationError(f"candidate report repeats {label}")

    def close(left: float, right: float) -> bool:
        return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)

    registries = document["bindings"]["pass_registries"]
    screening_registry = registries["screening_base"]
    executable_registry = registries["executable"]
    if (
        screening_registry["declared_sha256"]
        != screening_registry["artifact"]["canonical_sha256"]
        or executable_registry["declared_sha256"]
        != executable_registry["artifact"]["canonical_sha256"]
        or screening_registry["artifact"]["canonical_sha256"]
        == executable_registry["artifact"]["canonical_sha256"]
    ):
        raise ValidationError("candidate report PassRegistry binding differs")

    completion = document["bindings"]["campaign_completion"]
    final_binding = document["bindings"]["candidate_final"]
    if (
        completion["candidate_final_sha256"]
        != final_binding["canonical_sha256"]
        or completion["candidate_final_physical_sha256"]
        != final_binding["physical_sha256"]
        or completion["completed_status_sha256"]
        != completion["status_ledger_head_sha256"]
    ):
        raise ValidationError(
            "candidate report terminal campaign closure binding differs"
        )

    frozen_context = document["frozen_context"]
    if frozen_context["campaign_id"] != completion["campaign_id"]:
        raise ValidationError(
            "candidate report frozen campaign differs from terminal closure"
        )
    expected_reference_baselines = [
        (
            "gcc_13_3_o2",
            "gcc-13.3-o2",
            "riscv-gcc",
            "13.3.0",
            "-O2",
        ),
        (
            "clang_18_o3",
            "clang-18-o3",
            "clang",
            "18.1.3",
            "-O3",
        ),
    ]
    observed_reference_baselines = [
        (
            row["compiler_baseline"],
            row["profile_id"],
            row["tool"],
            row["version"],
            row["optimization"],
        )
        for row in frozen_context["reference_toolchain"]["baselines"]
    ]
    if observed_reference_baselines != expected_reference_baselines:
        raise ValidationError(
            "candidate report frozen GCC/Clang toolchain contexts differ"
        )

    screening = document["screening"]
    if tuple(row["candidate_id"] for row in screening) != _LOCKED_CANDIDATE_IDS:
        raise ValidationError(
            "candidate report screening differs from the locked eleven families"
        )
    implementation_ids = [
        row["implementation_candidate_id"]
        for row in screening
        if row["implementation_candidate_id"] is not None
    ]
    if len(implementation_ids) != len(set(implementation_ids)):
        raise ValidationError("candidate report repeats an implementation candidate")
    screening_by_implementation: dict[str, dict[str, Any]] = {}
    locked_screening = {
        item[0]: item for item in _LOCKED_CANDIDATE_SCREENING_CONTRACT
    }
    for row in screening:
        eligible = [
            (ref["oracle_family_id"], ref["structure_id"])
            for ref in row["eligible_oracle_structure_refs"]
        ]
        qualifying = [
            (ref["oracle_family_id"], ref["structure_id"])
            for ref in row["qualifying_oracle_structure_refs"]
        ]
        locked = locked_screening[row["candidate_id"]]
        if (
            eligible != list(locked[5])
            or qualifying
            != [item for item in eligible if item in set(qualifying)]
        ):
            raise ValidationError(
                "candidate report qualifying structures are not an ordered eligible subset"
            )
        qualified = row["qualification_status"] == "qualified"
        implementation_id = row["implementation_candidate_id"]
        if (locked[3] == "eligible") != (implementation_id is not None):
            raise ValidationError(
                "candidate report implementation identity differs from the locked screening slice"
            )
        if qualified:
            if (
                implementation_id is None
                or not qualifying
                or row["best_eligible_oracle_geometric_mean_upper_bound"] is None
                or row["best_eligible_oracle_geometric_mean_upper_bound"] < 1.1
                or row["legality_proof_path"] != "clear"
                or not row["legality_obligation_ids"]
                or row["overlaps_existing_pass_ids"]
                or row["rejection_reasons"]
            ):
                raise ValidationError(
                    "candidate report qualified screening row lacks its locked obligations"
                )
            screening_by_implementation[implementation_id] = row
        elif not row["rejection_reasons"]:
            raise ValidationError(
                "candidate report rejected screening row lacks an exact reason"
            )
    implemented = list(screening_by_implementation)

    def validate_b1(row: dict[str, Any], *, full: bool) -> None:
        if row["candidate_id"] == "FULL" and not full:
            raise ValidationError("candidate report candidate B1 row uses FULL identity")
        if not row["evidence_present"]:
            if (
                full
                or row["run_id"] is not None
                or row["state"] is not None
                or row["passed_cases"] != 0
                or row["failed_cases"] != 0
                or row["pending_cases"] != 140
                or row["censored_cases"] != 0
                or row["all_correct"]
                or row["failure_classification"] != "not_run"
            ):
                raise ValidationError("candidate report absent B1 evidence is inconsistent")
            return
        if row["run_id"] is None or row["state"] is None:
            raise ValidationError("candidate report present B1 evidence lacks identity")
        if (
            row["passed_cases"]
            + row["failed_cases"]
            + row["pending_cases"]
            != 140
        ):
            raise ValidationError("candidate report B1 counts do not cover 140 cases")
        expected_correct = (
            row["state"] == "completed"
            and row["passed_cases"] == 140
            and row["failed_cases"] == 0
            and row["pending_cases"] == 0
            and row["censored_cases"] == 0
        )
        if row["all_correct"] != expected_correct or (
            row["failure_classification"] is None
        ) != expected_correct:
            raise ValidationError("candidate report B1 classification is inconsistent")

    b1_full = document["b1_full_baseline"]
    if b1_full["candidate_id"] != "FULL":
        raise ValidationError("candidate report lacks the exact B1 FULL baseline")
    validate_b1(b1_full, full=True)
    if not b1_full["all_correct"] or (
        b1_full["run_id"] != document["bindings"]["b1_full_run"]["run_id"]
    ):
        raise ValidationError("candidate report B1 FULL binding/outcome differs")

    b1_rows = document["b1_correctness"]
    if [row["candidate_id"] for row in b1_rows] != implemented:
        raise ValidationError("candidate report B1 rows differ from implemented candidates")
    for row in b1_rows:
        validate_b1(row, full=False)
        if not row["evidence_present"]:
            raise ValidationError("candidate report implemented candidate lacks B1 evidence")
    b1_by_id = {row["candidate_id"]: row for row in b1_rows}

    def validate_outcome(
        row: dict[str, Any],
        *,
        eligibility_key: str,
        include_static: bool,
    ) -> None:
        metric_keys = [
            "case_geometric_mean_speedup",
            "confidence_interval_95_low",
            "confidence_interval_95_high",
        ]
        if not row["evidence_present"]:
            absent = (
                row["run_id"] is None
                and not row[eligibility_key]
                and row["failure_classification"] == "not_run"
                and row["comparable_cases"] == 0
                and row["comparable_source_groups"] == 0
                and row["correctness_failures"] == 0
                and row["censored_cases"] == 0
                and row["excluded_cases"] == 0
                and all(row[key] is None for key in metric_keys)
            )
            if include_static:
                absent = absent and all(
                    row[key] is None
                    for key in (
                        "static_text_bytes_full",
                        "static_text_bytes_full_plus_candidate",
                        "static_text_ratio",
                    )
                )
            if not absent:
                raise ValidationError(
                    "candidate report absent suite evidence is inconsistent"
                )
            return
        if row["run_id"] is None or row[eligibility_key] != (
            row["failure_classification"] is None
        ):
            raise ValidationError("candidate report suite eligibility is inconsistent")
        metrics_present = all(row[key] is not None for key in metric_keys)
        if row[eligibility_key] != metrics_present:
            raise ValidationError("candidate report suite GM/CI availability differs")
        if metrics_present:
            if (
                row["confidence_interval_95_low"]
                > row["confidence_interval_95_high"]
                or row["comparable_cases"] != row["expected_case_count"]
                or row["correctness_failures"]
                or row["censored_cases"]
                or row["excluded_cases"]
            ):
                raise ValidationError(
                    "candidate report eligible suite evidence is incomplete"
                )
            if include_static and any(
                row[key] is None
                for key in (
                    "static_text_bytes_full",
                    "static_text_bytes_full_plus_candidate",
                    "static_text_ratio",
                )
            ):
                raise ValidationError(
                    "candidate report eligible suite lacks static text evidence"
                )

    b2_rows = document["b2_tuning"]
    if [row["candidate_id"] for row in b2_rows] != implemented:
        raise ValidationError("candidate report B2 rows differ from implemented candidates")
    for row in b2_rows:
        validate_outcome(
            row, eligibility_key="eligible_for_analysis", include_static=False
        )
        if row["used_for_elimination"]:
            raise ValidationError("candidate report illegally uses B2 for elimination")
        if row["evidence_present"] != b1_by_id[row["candidate_id"]]["all_correct"]:
            raise ValidationError("candidate report B2/B1 gate differs")

    roles = ("B3", "B4", "B5", "B6")
    expected_counts = {"B3": 60, "B4": 59, "B5": 60, "B6": 88}
    suite_rows = document["suite_results"]
    expected_suite_order = [
        (candidate_id, role) for candidate_id in implemented for role in roles
    ]
    if [
        (row["candidate_id"], row["data_role"]) for row in suite_rows
    ] != expected_suite_order:
        raise ValidationError("candidate report suite rows are not the exact B3-B6 matrix")
    suite_by_key = {
        (row["candidate_id"], row["data_role"]): row for row in suite_rows
    }
    for row in suite_rows:
        if row["expected_case_count"] != expected_counts[row["data_role"]]:
            raise ValidationError("candidate report suite case count differs")
        validate_outcome(
            row, eligibility_key="eligible_for_ranking", include_static=True
        )
    for candidate_id in implemented:
        b3 = suite_by_key[(candidate_id, "B3")]
        if b3["evidence_present"] != b1_by_id[candidate_id]["all_correct"]:
            raise ValidationError("candidate report B3/B1 gate differs")
        promoted = (
            b3["eligible_for_ranking"]
            and b3["case_geometric_mean_speedup"] is not None
            and b3["case_geometric_mean_speedup"] > 1.0
        )
        if any(
            suite_by_key[(candidate_id, role)]["evidence_present"] != promoted
            for role in ("B4", "B5", "B6")
        ):
            raise ValidationError("candidate report B3 promotion gate differs")

    ranking = document["ranking"]
    unique(ranking, "candidate_id", "a ranked candidate")
    rankable: list[str] = []
    for candidate_id in implemented:
        rows = [suite_by_key[(candidate_id, role)] for role in roles]
        if all(row["eligible_for_ranking"] for row in rows) and all(
            row["static_text_bytes_full"] is not None
            and row["static_text_bytes_full_plus_candidate"] is not None
            for row in rows
        ):
            rankable.append(candidate_id)
    if set(row["candidate_id"] for row in ranking) != set(rankable):
        raise ValidationError("candidate report ranking differs from complete 267-case evidence")
    for row in ranking:
        candidate_id = row["candidate_id"]
        evidence = [suite_by_key[(candidate_id, role)] for role in roles]
        combined = math.exp(
            sum(
                expected_counts[item["data_role"]]
                * math.log(item["case_geometric_mean_speedup"])
                for item in evidence
            )
            / 267
        )
        static_full = sum(item["static_text_bytes_full"] for item in evidence)
        static_candidate = sum(
            item["static_text_bytes_full_plus_candidate"] for item in evidence
        )
        screened = screening_by_implementation[candidate_id]
        if (
            row["stable_id_tiebreak"] != candidate_id
            or not close(row["combined_case_geometric_mean_speedup"], combined)
            or not close(
                row["b3_case_geometric_mean_speedup"],
                suite_by_key[(candidate_id, "B3")][
                    "case_geometric_mean_speedup"
                ],
            )
            or not close(
                row["combined_static_text_bytes_full_plus_candidate"],
                static_candidate,
            )
            or not close(row["combined_static_text_ratio"], static_full / static_candidate)
            or row["implementation_cost"] != screened["implementation_cost"]
            or row["risk"] != screened["risk"]
        ):
            raise ValidationError("candidate report ranking row differs from suite evidence")
    expected_ranking = sorted(
        ranking,
        key=lambda row: (
            -row["combined_case_geometric_mean_speedup"],
            -row["b3_case_geometric_mean_speedup"],
            row["combined_static_text_bytes_full_plus_candidate"],
            row["stable_id_tiebreak"],
        ),
    )
    if ranking != expected_ranking or [row["rank"] for row in ranking] != list(
        range(1, len(ranking) + 1)
    ):
        raise ValidationError("candidate report ranking/tie-break order differs")

    expected_winner = (
        ranking[0]["candidate_id"]
        if ranking and ranking[0]["combined_case_geometric_mean_speedup"] > 1.0
        else None
    )
    conclusion = document["conclusion"]
    if (
        conclusion["winner_candidate_id"] != expected_winner
        or conclusion["winner_reason"]
        != (
            "top_combined_gm_above_one"
            if expected_winner is not None
            else "no_winning_candidate"
        )
        or conclusion["claim"]
        != (
            "qemu_proxy_best_candidate"
            if expected_winner is not None
            else "no_winner"
        )
    ):
        raise ValidationError("candidate report winner gate differs")
    winner_binding = document["bindings"]["winner_run"]
    if (expected_winner is None) != (winner_binding is None):
        raise ValidationError(
            "candidate report winner identity and winner run binding differ"
        )
    if winner_binding is not None and winner_binding["run_id"] != suite_by_key[
        (expected_winner, "B3")
    ]["run_id"]:
        raise ValidationError("candidate report winner run differs from B3")

    capture = document["oracle_capture"]
    if [row["candidate_id"] for row in capture] != implemented:
        raise ValidationError("candidate report Oracle capture rows differ")
    for row in capture:
        screened = screening_by_implementation[row["candidate_id"]]
        measured = suite_by_key[(row["candidate_id"], "B3")][
            "case_geometric_mean_speedup"
        ]
        upper = screened["best_eligible_oracle_geometric_mean_upper_bound"]
        expected_capture = (
            None
            if measured is None or upper is None
            else measured / upper
        )
        if (
            row["oracle_upper_bound"] != upper
            or row["b3_measured_speedup"] != measured
            or (
                expected_capture is None
                and row["oracle_capture_ratio"] is not None
            )
            or (
                expected_capture is not None
                and not close(row["oracle_capture_ratio"], expected_capture)
            )
        ):
            raise ValidationError("candidate report Oracle capture differs")

    b3_ranked = sorted(
        (
            suite_by_key[(candidate_id, "B3")]
            for candidate_id in implemented
            if suite_by_key[(candidate_id, "B3")]["eligible_for_ranking"]
        ),
        key=lambda row: (
            -row["case_geometric_mean_speedup"], row["candidate_id"]
        ),
    )[:3]
    top3 = [row["candidate_id"] for row in b3_ranked]
    expected_pairs = [
        tuple(sorted(pair)) for pair in combinations(top3, 2)
    ]
    interactions = document["interactions"]
    if [
        (row["left_candidate_id"], row["right_candidate_id"])
        for row in interactions
    ] != sorted(expected_pairs):
        raise ValidationError("candidate report interactions differ from exact Top3 pairs")
    if (document["bindings"]["diagnostic_study"] is not None) != bool(
        expected_pairs
    ):
        raise ValidationError("candidate report diagnostic study binding differs")
    unique(interactions, "task_id", "a diagnostic task")
    unique(interactions, "run_id", "a diagnostic run")
    for row in interactions:
        pair = (row["left_candidate_id"], row["right_candidate_id"])
        terminal = row["terminal_failure_classification"]
        if row["task_id"] != f"diagnostic.pair.{'+'.join(pair)}" or (
            row["state"] == "completed"
        ) != (terminal is None):
            raise ValidationError("candidate report pair terminal identity differs")
        eligible = row["eligible_for_interpretation"]
        metrics = (
            row["pair_case_geometric_mean_speedup"],
            row["expected_multiplicative_speedup"],
            row["delta_ln_geometric_mean"],
        )
        if eligible != (row["failure_classification"] is None) or eligible != all(
            value is not None for value in metrics
        ):
            raise ValidationError("candidate report pair metric availability differs")
        if row["state"] != "completed" and eligible:
            raise ValidationError("failed candidate pair cannot be interpreted")
        if eligible:
            left = suite_by_key[(pair[0], "B3")][
                "case_geometric_mean_speedup"
            ]
            right = suite_by_key[(pair[1], "B3")][
                "case_geometric_mean_speedup"
            ]
            expected_multiplicative = left * right
            expected_delta = (
                math.log(row["pair_case_geometric_mean_speedup"])
                - math.log(left)
                - math.log(right)
            )
            if not close(
                row["expected_multiplicative_speedup"], expected_multiplicative
            ) or not close(row["delta_ln_geometric_mean"], expected_delta):
                raise ValidationError("candidate report interaction formula differs")

    toolchains = document["toolchain_context"]
    reference_bindings = document["bindings"]["reference_runs"]
    labels = ["gcc-13.3-o2", "clang-18-o3"]
    if [row["label"] for row in toolchains] != labels or [
        row["label"] for row in reference_bindings
    ] != labels:
        raise ValidationError("candidate report toolchain order differs")
    full_run_id = document["bindings"]["b3_full_run"]["run_id"]
    winner_run_id = None if winner_binding is None else winner_binding["run_id"]
    full_metric_keys = (
        "reference_over_full_geometric_mean",
        "reference_over_full_confidence_interval_95_low",
        "reference_over_full_confidence_interval_95_high",
    )
    winner_metric_keys = (
        "reference_over_winner_geometric_mean",
        "reference_over_winner_confidence_interval_95_low",
        "reference_over_winner_confidence_interval_95_high",
    )
    for row, binding in zip(toolchains, reference_bindings, strict=True):
        if (
            row["reference_run_id"] != binding["run_id"]
            or row["full_run_id"] != full_run_id
            or row["winner_run_id"] != winner_run_id
        ):
            raise ValidationError("candidate report toolchain run binding differs")
        if row["state"] == "completed":
            if row["failure_classification"] is not None or not all(
                row[key] is not None for key in full_metric_keys
            ):
                raise ValidationError("completed toolchain diagnostic lacks metrics")
            if (winner_binding is not None) != all(
                row[key] is not None for key in winner_metric_keys
            ):
                raise ValidationError("toolchain winner comparison availability differs")
        elif row["failure_classification"] != f"run_{row['state']}" or any(
            row[key] is not None for key in (*full_metric_keys, *winner_metric_keys)
        ):
            raise ValidationError("failed toolchain diagnostic carries fabricated metrics")

    hotblocks = document["hotblock_diagnostics"]
    expected_hotblock_profiles = [[], *[[candidate_id] for candidate_id in top3]]
    if [row["enabled_candidate_ids"] for row in hotblocks] != expected_hotblock_profiles:
        raise ValidationError("candidate report hotblock profiles differ from FULL/Top3")
    hotblock_bindings = document["bindings"]["hotblock_runs"]
    binding_by_label = {row["label"]: row for row in hotblock_bindings}
    if len(binding_by_label) != len(hotblock_bindings) or set(binding_by_label) != {
        row["label"] for row in hotblocks
    }:
        raise ValidationError("candidate report hotblock bindings differ")
    for row in hotblocks:
        if row["run_id"] != binding_by_label[row["label"]]["run_id"]:
            raise ValidationError("candidate report hotblock run binding differs")
        diagnostic_metrics = (
            row["mean_l1d_misses_per_1000_dynamic_loads"],
            row["mean_hottest_block_dynamic_instruction_share"],
        )
        if row["state"] == "completed":
            if (
                row["failure_classification"] is not None
                or row["sample_count"] == 0
                or row["mean_hottest_block_dynamic_instruction_share"] is None
            ):
                raise ValidationError("completed hotblock diagnostic lacks samples")
        elif (
            row["failure_classification"] is None
            or row["sample_count"] != 0
            or any(value is not None for value in diagnostic_metrics)
        ):
            raise ValidationError("failed hotblock diagnostic carries fabricated metrics")
