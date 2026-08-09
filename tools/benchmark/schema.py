from __future__ import annotations

import json
import math
import re
import statistics
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from .errors import ValidationError
from .metrics import ANALYZER_METRICS, UNAVAILABLE_REASONS, cache_hotblock_metrics_v1
from .util import read_json, resolve_manifest_path, sha256_file, sha256_json, validate_relative_path

_SCHEMA_FILES = {
    "benchmark-manifest.v1": "benchmark-manifest.v1.json",
    "run-record.v1": "run-record.v1.json",
    "optimization-remark.v1": "optimization-remark.v1.json",
    "ablation-study.v1": "ablation-study.v1.json",
    "binary-analysis.v1": "binary-analysis.v1.json",
    "pass-registry.v1": "pass-registry.v1.json",
    "ablation-matrix.v1": "ablation-matrix.v1.json",
    "oracle-plan.v1": "oracle-plan.v1.json",
    "cross-suite-audit.v1": "cross-suite-audit.v1.json",
    "campaign-plan.v1": "campaign-plan.v1.json",
    "campaign-status.v1": "campaign-status.v1.json",
    "candidate-evidence.v1": "candidate-evidence.v1.json",
    "measurement-protocol.v1": "measurement-protocol.v1.json",
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
    elif version == "optimization-remark.v1":
        _validate_optimization_event_semantics(document)
    elif version == "ablation-study.v1":
        _validate_ablation_semantics(document)
    elif version == "binary-analysis.v1":
        _validate_binary_analysis_semantics(document)
    elif version == "pass-registry.v1":
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
    return document


def load_and_validate(path: Path, *, suite_root: Path | None = None, verify_files: bool = False) -> dict[str, Any]:
    return validate_document(read_json(path), suite_root=suite_root, verify_files=verify_files)


def load_and_validate_jsonl(path: Path) -> list[dict[str, Any]]:
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
                if event["schema_version"] != "optimization-remark.v1":
                    raise ValidationError(f"JSONL line {line_number} is not optimization-remark.v1")
                events.append(event)
    except OSError as exc:
        raise ValidationError("cannot read optimization remark JSONL") from exc
    if not events:
        raise ValidationError("optimization remark JSONL is empty")
    for expected, event in enumerate(events, 1):
        if event["sequence"] != expected:
            raise ValidationError("optimization remark JSONL sequence must be contiguous and start at 1")
    return events


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
        if case["status"] not in {"pending", "cancelled"} and not has_attempt_start:
            raise ValidationError(
                f"case {case['case_id']} completed attempt lacks its start/configuration binding"
            )
        if not has_attempt_start and (
            case["cache_hit"]
            or any(
                case[field] is not None
                for field in (
                    "artifact_sha256",
                    "binary_sha256",
                    "remarks_sha256",
                    "remarks_event_count",
                    "analysis_sha256",
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
            if attempt["failure_summary"] != _ATTEMPT_FAILURE_SUMMARIES[attempt["status"]]:
                raise ValidationError(
                    f"case {case['case_id']} archived attempt summary does not match its failure status"
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
            attempt_cancelled_stage = any(
                phase is not None and phase["status"] == "cancelled"
                for phase in (attempt["compile"], attempt["link"], attempt["analyze"])
            ) or any(sample["status"] == "cancelled" for sample in attempt["samples"])
            if attempt_cancelled_stage and attempt["status"] != "cancelled":
                raise ValidationError(
                    f"case {case['case_id']} attempt {attempt['attempt_index']} "
                    "scheduler cancellation is misclassified"
                )
        for index, sample in enumerate(case["compile_samples"]):
            _validate_compile_sample_evidence(
                sample,
                remarks_configured=remarks_configured,
                label=f"case {case['case_id']} cold sample {index}",
            )
        cancelled_stage = any(
            phase is not None and phase["status"] == "cancelled"
            for phase in (case["compile"], case["link"], case["analyze"])
        ) or any(sample["status"] == "cancelled" for sample in case["samples"])
        if cancelled_stage and case["status"] != "cancelled":
            raise ValidationError(
                f"case {case['case_id']} scheduler cancellation is misclassified as {case['status']}"
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
            {"compiler_baseline": baseline_id, "flags": [optimization]}
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
