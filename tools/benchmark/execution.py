from __future__ import annotations

import re
import shutil
import statistics
import string
import threading
import math
from copy import deepcopy
from concurrent.futures import FIRST_COMPLETED, CancelledError, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .adapters import CommandRenderer, StageSpec, WslPathMapper
from .cache import (
    CompileBuild,
    CompileCache,
    compile_storage_contract,
)
from .errors import ConfigurationError, ExecutionError, ValidationError
from .journal import (
    AttemptJournal,
    JournalSnapshot,
    RunTerminalJournal,
    RunTerminalSnapshot,
    durable_create_json,
)
from .lease import ExclusiveFileLease, output_lease_path, path_identity
from .metrics import ANALYZER_METRICS, rv64gc_qemu_v1
from .process import ProcessResult, extract_metric, first_mismatch_offset, run_process
from .protocol import resolve_measurement_protocol_assets, verify_measurement_protocol
from .schema import (
    load_and_validate,
    load_and_validate_jsonl,
    load_pipeline_profile_v2,
    validate_candidate_remark_jsonl,
    validate_document,
)
from .util import (
    atomic_write_json,
    executable_label,
    raw_attempt_identity,
    raw_attempt_identity_sha256,
    read_json,
    resolve_without_symlinks,
    resolve_manifest_path,
    safe_slug,
    sanitize_text,
    sha256_bytes,
    sha256_file,
    sha256_json,
    utc_now,
    validate_relative_path,
)


def _stream_record(payload: bytes) -> dict[str, Any]:
    return {"sha256": sha256_bytes(payload), "size_bytes": len(payload)}


def _split_return_trailer(payload: bytes, *, label: str) -> tuple[bytes, int]:
    if not payload.endswith(b"\n"):
        raise ExecutionError(f"{label} must end with an LF-delimited uint8 return trailer")
    previous_lf = payload.rfind(b"\n", 0, len(payload) - 1)
    trailer_start = previous_lf + 1
    trailer = payload[trailer_start:-1]
    if not trailer or any(value < ord("0") or value > ord("9") for value in trailer):
        raise ExecutionError(f"{label} return trailer is not an unsigned decimal integer")
    value = int(trailer)
    if not 0 <= value <= 255:
        raise ExecutionError(f"{label} return trailer is outside uint8 range")
    return payload[:trailer_start], value


def _first_mismatch_bytes(expected: bytes, actual: bytes) -> int | None:
    common = min(len(expected), len(actual))
    for index in range(common):
        if expected[index] != actual[index]:
            return index
    return None if len(expected) == len(actual) else common


def _read_result_uint8(path: Path) -> int:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ExecutionError("runner did not produce the configured result file") from exc
    if payload.endswith(b"\n"):
        payload = payload[:-1]
    if not payload or b"\n" in payload or any(value < ord("0") or value > ord("9") for value in payload):
        raise ExecutionError("result file must contain one decimal uint8 value with optional final LF")
    value = int(payload)
    if not 0 <= value <= 255:
        raise ExecutionError("result file return value is outside uint8 range")
    return value


@dataclass(frozen=True)
class ToolVersion:
    tool: str
    actual: str
    official_expected: str | None = None

    def as_record(self) -> dict[str, str | None]:
        if self.official_expected is None:
            comparison = "unknown"
        elif self.actual == self.official_expected:
            comparison = "exact"
        else:
            comparison = "mismatch"
        return {
            "tool": self.tool,
            "actual": self.actual,
            "official_expected": self.official_expected,
            "comparison": comparison,
        }


@dataclass(frozen=True)
class MeasurementSpec:
    metric_id: str
    source: str
    unit: str
    pattern: str | None = None


@dataclass(frozen=True)
class RunProvenance:
    repo_commit: str
    repo_dirty: bool
    pipeline_profile_id: str
    pipeline_profile_sha256: str
    compiler_artifact_sha256: str
    measurement_protocol_id: str | None = None
    measurement_protocol_sha256: str | None = None
    tracked_diff_sha256: str | None = None

    def as_record(self) -> dict[str, Any]:
        return {
            "repo_commit": self.repo_commit,
            "repo_dirty": self.repo_dirty,
            "tracked_diff_sha256": self.tracked_diff_sha256,
            "pipeline_profile_id": self.pipeline_profile_id,
            "pipeline_profile_sha256": self.pipeline_profile_sha256,
            "compiler_artifact_sha256": self.compiler_artifact_sha256,
            "measurement_protocol_id": self.measurement_protocol_id,
            "measurement_protocol_sha256": self.measurement_protocol_sha256,
        }


@dataclass(frozen=True)
class RunOptions:
    manifest_path: Path
    suite_root: Path
    workspace_root: Path
    output_path: Path
    state_root: Path
    compiler: StageSpec
    linker: StageSpec | None
    runner: StageSpec
    provenance: RunProvenance
    pipeline_profile_path: Path | None = None
    candidate_registry_path: Path | None = None
    candidate_pass_registry_path: Path | None = None
    measurement_protocol_path: Path | None = None
    measurement_protocol_assets: tuple[tuple[str, Path], ...] = ()
    analyzer: StageSpec | None = None
    compile_timeout_seconds: float = 120.0
    compile_repetitions: int = 5
    reuse_compile_cache: bool = False
    link_timeout_seconds: float = 120.0
    analyze_timeout_seconds: float = 120.0
    run_timeout_seconds: float = 1800.0
    timeout_policy: str = "fixed"
    baseline_timeout_path: Path | None = None
    timeout_minimum_seconds: float = 120.0
    timeout_multiplier: float = 3.0
    timeout_cap_seconds: float = 1800.0
    repetitions: int = 1
    max_workers: int = 1
    keep_going: bool = False
    retry_failures: bool = False
    seed: int = 20260809
    artifact_suffix: str = ".s"
    binary_suffix: str = ".elf"
    primary_metric_id: str = "wall_time_ns"
    metric_source: str = "wall_time"
    metric_pattern: str | None = None
    metric_unit: str = "ns"
    metric_profile_id: str | None = None
    metric_file: str | None = None
    analysis_file: str | None = None
    output_contract: str = "lf_return_trailer"
    result_file: str | None = None
    remarks_file: str | None = None
    additional_metrics: tuple[MeasurementSpec, ...] = ()
    wsl_executable: str = "wsl.exe"
    wsl_distribution: str | None = None
    run_id: str | None = None
    environment_label: str = "local_reference"
    evidence_level: str = "qemu_proxy"
    tool_versions: tuple[ToolVersion, ...] = ()


def _stage_record(stage: StageSpec | None) -> dict[str, Any] | None:
    if stage is None:
        return None
    command_hash = None
    if stage.command is not None:
        command_hash = sha256_json(
            {
                "command": list(stage.command),
                "environment": dict(sorted(stage.environment.items())),
            }
        )
    return {
        "kind": stage.kind,
        "adapter": stage.adapter,
        "command_sha256": command_hash,
        "executable": executable_label(stage.command),
        "environment_keys": sorted(stage.environment),
    }


def _placeholders(stage: StageSpec) -> set[str]:
    formatter = string.Formatter()
    result: set[str] = set()
    values: Iterable[str] = (*stage.command,) if stage.command is not None else ()
    for value in (*values, *stage.environment.values()):
        try:
            fields = formatter.parse(value)
            for _, name, _, _ in fields:
                if name:
                    result.add(name)
        except ValueError as exc:
            raise ConfigurationError(f"invalid command template: {exc}") from exc
    return result


def _require_path_placeholder(stage: StageSpec, logical_name: str, label: str) -> None:
    accepted = {logical_name, f"{logical_name}_host", f"{logical_name}_wsl"}
    if not (_placeholders(stage) & accepted):
        raise ConfigurationError(f"{label} must reference {{{logical_name}}} (or an explicit host/WSL variant)")


def _workspace_bound_file(
    workspace_root: Path,
    path: Path,
    *,
    label: str,
) -> Path:
    root = workspace_root.resolve(strict=True)
    resolved = (path if path.is_absolute() else root / path).resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ConfigurationError(f"{label} must stay within workspace_root") from exc
    if not resolved.is_file():
        raise ConfigurationError(f"{label} must be a regular file")
    return resolved


def _pipeline_profile_candidate_ids(
    path: Path | None,
    *,
    workspace_root: Path,
    require_v2: bool,
    candidate_order: list[str] | None = None,
) -> list[str]:
    if path is None:
        if require_v2:
            raise ConfigurationError(
                "candidate registry requires a physical PipelineProfile v2"
            )
        return []
    if require_v2:
        profile = load_pipeline_profile_v2(
            _workspace_bound_file(
                workspace_root, path, label="pipeline profile"
            ),
            candidate_order=candidate_order,
        )
        return list(profile["enable_candidates"])
    profile = read_json(
        _workspace_bound_file(workspace_root, path, label="pipeline profile")
    )
    if (
        isinstance(profile, dict)
        and profile.get("schema_version") == 2
        and profile.get("enable_candidates")
    ):
        raise ConfigurationError(
            "PipelineProfile v2 candidate enablement requires --candidate-registry"
        )
    return []


def _validate_options(options: RunOptions) -> None:
    if not options.workspace_root.is_absolute():
        raise ConfigurationError("workspace_root must be an absolute path")
    if options.compiler.command is None or options.runner.command is None:
        raise ConfigurationError("compiler and runner commands are required")
    if options.candidate_registry_path is not None:
        if options.candidate_pass_registry_path is None:
            raise ConfigurationError(
                "candidate registry requires a physical PassRegistry v2 snapshot"
            )
        catalog = load_and_validate(
            _workspace_bound_file(
                options.workspace_root,
                options.candidate_registry_path,
                label="candidate registry",
            )
        )
        pass_registry = load_and_validate(
            _workspace_bound_file(
                options.workspace_root,
                options.candidate_pass_registry_path,
                label="candidate pass registry",
            )
        )
        if catalog["schema_version"] != "candidate-catalog.v1":
            raise ConfigurationError("candidate registry must be candidate-catalog.v1")
        if pass_registry["schema_version"] != "pass-registry.v2":
            raise ConfigurationError("candidate pass registry must be pass-registry.v2")
        if catalog["pass_registry_sha256"] != sha256_json(pass_registry):
            raise ConfigurationError("candidate catalog/pass registry binding differs")
        descriptors = {item["candidate_id"]: item for item in catalog["candidates"]}
        enabled_candidate_ids = _pipeline_profile_candidate_ids(
            options.pipeline_profile_path,
            workspace_root=options.workspace_root,
            require_v2=True,
            candidate_order=list(descriptors),
        )
        unknown = sorted(set(enabled_candidate_ids) - set(descriptors))
        if unknown:
            raise ConfigurationError(
                "unknown enabled candidate: " + ", ".join(unknown)
            )
        unavailable = sorted(
            candidate_id
            for candidate_id in enabled_candidate_ids
            if descriptors[candidate_id]["implementation_status"] != "implemented"
        )
        if unavailable:
            raise ConfigurationError(
                "enabled candidate is not implemented: " + ", ".join(unavailable)
            )
        if options.remarks_file is None:
            raise ConfigurationError(
                "candidate campaign runs require optimization-remark.v2 output"
            )
    else:
        if options.candidate_pass_registry_path is not None:
            raise ConfigurationError(
                "candidate pass registry cannot be supplied without candidate registry"
            )
        _pipeline_profile_candidate_ids(
            options.pipeline_profile_path,
            workspace_root=options.workspace_root,
            require_v2=False,
        )
    if not isinstance(options.reuse_compile_cache, bool):
        raise ConfigurationError("reuse_compile_cache must be a boolean")
    if (
        options.evidence_level == "qemu_proxy"
        and options.metric_profile_id == "rv64gc-qemu-v1"
        and not options.tool_versions
    ):
        raise ConfigurationError("rv64gc-qemu-v1 qemu_proxy evidence requires explicit tool versions")
    if options.compiler.adapter not in {"host", "wsl"} or options.runner.adapter not in {"host", "wsl"}:
        raise ConfigurationError("stage adapter must be host or wsl")
    if options.linker is not None and options.linker.adapter not in {"host", "wsl"}:
        raise ConfigurationError("stage adapter must be host or wsl")
    if options.analyzer is not None and options.analyzer.adapter not in {"host", "wsl"}:
        raise ConfigurationError("stage adapter must be host or wsl")
    _require_path_placeholder(options.compiler, "source", "compiler command")
    _require_path_placeholder(options.compiler, "artifact", "compiler command")
    compiler_fields = _placeholders(options.compiler)
    uses_profile_path = bool(compiler_fields & {"profile", "profile_host", "profile_wsl"})
    if uses_profile_path != (options.pipeline_profile_path is not None):
        raise ConfigurationError("compiler {profile} placeholder and pipeline_profile_path must be configured together")
    if options.pipeline_profile_path is not None:
        profile_path = _workspace_bound_file(
            options.workspace_root,
            options.pipeline_profile_path,
            label="pipeline profile",
        )
        if not profile_path.is_file() or sha256_file(profile_path) != options.provenance.pipeline_profile_sha256:
            raise ConfigurationError("pipeline profile file does not match provenance SHA-256")
    if options.measurement_protocol_path is None:
        if (
            options.provenance.measurement_protocol_id is not None
            or options.provenance.measurement_protocol_sha256 is not None
        ):
            raise ConfigurationError("measurement protocol provenance requires its snapshot file")
        if options.evidence_level == "qemu_proxy":
            raise ConfigurationError("qemu_proxy evidence requires --measurement-protocol")
        if options.measurement_protocol_assets:
            raise ConfigurationError("measurement protocol assets require a snapshot file")
    else:
        protocol = load_and_validate(options.measurement_protocol_path.resolve(strict=True))
        if protocol["schema_version"] != "measurement-protocol.v1":
            raise ConfigurationError("measurement protocol must be measurement-protocol.v1")
        if (
            options.provenance.measurement_protocol_id != protocol["protocol_id"]
            or options.provenance.measurement_protocol_sha256 != sha256_json(protocol)
        ):
            raise ConfigurationError("measurement protocol file does not match provenance id/SHA-256")
        asset_map = dict(options.measurement_protocol_assets)
        if len(asset_map) != len(options.measurement_protocol_assets):
            raise ConfigurationError("measurement protocol assets contain duplicate keys")
        verify_measurement_protocol(
            protocol,
            workspace_root=options.workspace_root,
            assets=asset_map,
            runner=options.runner,
            wsl_executable=options.wsl_executable,
            wsl_distribution=options.wsl_distribution,
        )
    uses_remarks_path = bool(compiler_fields & {"remarks_file", "remarks_file_host", "remarks_file_wsl"})
    if uses_remarks_path != (options.remarks_file is not None):
        raise ConfigurationError("compiler {remarks_file} placeholder and remarks_file must be configured together")
    if options.remarks_file is not None:
        validate_relative_path(options.remarks_file, label="remarks file")
    if options.linker is not None:
        if options.linker.command is None:
            raise ConfigurationError("linker stage has no command")
        _require_path_placeholder(options.linker, "artifact", "linker command")
        _require_path_placeholder(options.linker, "binary", "linker command")
    if options.analyzer is not None:
        if options.analyzer.command is None:
            raise ConfigurationError("analyzer stage has no command")
        _require_path_placeholder(options.analyzer, "binary", "analyzer command")
        _require_path_placeholder(options.analyzer, "analysis_file", "analyzer command")
    run_fields = _placeholders(options.runner)
    if not run_fields.intersection({"binary", "binary_host", "binary_wsl", "artifact", "artifact_host", "artifact_wsl"}):
        raise ConfigurationError("runner command must reference {binary} or {artifact}")
    for value, label in (
        (options.compile_timeout_seconds, "compile timeout"),
        (options.link_timeout_seconds, "link timeout"),
        (options.analyze_timeout_seconds, "analyze timeout"),
        (options.run_timeout_seconds, "run timeout"),
    ):
        if value <= 0:
            raise ConfigurationError(f"{label} must be greater than zero")
    if options.timeout_policy not in {"fixed", "initial", "baseline_derived"}:
        raise ConfigurationError("timeout_policy must be fixed, initial, or baseline_derived")
    if options.timeout_policy == "baseline_derived" and options.baseline_timeout_path is None:
        raise ConfigurationError("baseline_derived timeout policy requires a baseline timeout run")
    if options.timeout_policy != "baseline_derived" and options.baseline_timeout_path is not None:
        raise ConfigurationError("baseline timeout run is only valid for baseline_derived policy")
    for value, label in (
        (options.timeout_minimum_seconds, "timeout minimum"),
        (options.timeout_multiplier, "timeout multiplier"),
        (options.timeout_cap_seconds, "timeout cap"),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ConfigurationError(f"{label} must be finite and greater than zero")
    if options.timeout_minimum_seconds > options.timeout_cap_seconds:
        raise ConfigurationError("timeout minimum cannot exceed timeout cap")
    if options.timeout_policy in {"initial", "baseline_derived"} and (
        not math.isclose(options.run_timeout_seconds, 1800.0, rel_tol=0, abs_tol=1e-12)
        or not math.isclose(options.timeout_minimum_seconds, 120.0, rel_tol=0, abs_tol=1e-12)
        or not math.isclose(options.timeout_multiplier, 3.0, rel_tol=0, abs_tol=1e-12)
        or not math.isclose(options.timeout_cap_seconds, 1800.0, rel_tol=0, abs_tol=1e-12)
    ):
        raise ConfigurationError(
            "initial/baseline-derived protocol requires run=1800, minimum=120, multiplier=3, cap=1800"
        )
    if not 1 <= options.repetitions <= 1000:
        raise ConfigurationError("repetitions must be between 1 and 1000")
    if not 1 <= options.max_workers <= 4:
        raise ConfigurationError("max_workers must be between 1 and 4")
    if not 1 <= options.compile_repetitions <= 100:
        raise ConfigurationError("compile_repetitions must be between 1 and 100")
    for suffix, label in ((options.artifact_suffix, "artifact suffix"), (options.binary_suffix, "binary suffix")):
        if not re.fullmatch(r"\.[A-Za-z0-9._-]+", suffix):
            raise ConfigurationError(f"{label} must be a simple extension beginning with a dot")
    if options.metric_source not in {"wall_time", "stdout", "stderr", "file"}:
        raise ConfigurationError("metric source must be wall_time, stdout, stderr, or file")
    if options.metric_source == "wall_time":
        if options.metric_pattern is not None:
            raise ConfigurationError("wall_time metric must not define a pattern")
        if options.metric_unit != "ns":
            raise ConfigurationError("wall_time metric unit is fixed to ns")
    elif options.metric_pattern is None:
        raise ConfigurationError("stdout/stderr/file metric requires a regular expression")
    if not options.metric_unit or len(options.metric_unit) > 64:
        raise ConfigurationError("metric unit must contain 1 to 64 characters")
    metric_ids = {options.primary_metric_id}
    valid_sources = {
        "wall_time", "stdout", "stderr", "file", "compile_time", "link_time", "artifact_size",
        "binary_size", "compile_stdout", "compile_stderr", "link_stdout", "link_stderr", "analyzer",
    }
    regex_sources = {"stdout", "stderr", "file", "compile_stdout", "compile_stderr", "link_stdout", "link_stderr"}
    for metric in options.additional_metrics:
        if metric.metric_id in metric_ids:
            raise ConfigurationError(f"duplicate metric id: {metric.metric_id}")
        metric_ids.add(metric.metric_id)
        if metric.source not in valid_sources:
            raise ConfigurationError(f"unknown measurement source: {metric.source}")
        if (metric.source in regex_sources) != (metric.pattern is not None):
            raise ConfigurationError(f"metric {metric.metric_id} has inconsistent pattern/source")
        if metric.source.startswith("link_") and options.linker is None:
            raise ConfigurationError(f"metric {metric.metric_id} requires a linker stage")
        if not metric.unit or len(metric.unit) > 64:
            raise ConfigurationError(f"metric {metric.metric_id} has an invalid unit")
        if metric.source == "analyzer" and options.analyzer is None:
            raise ConfigurationError(f"metric {metric.metric_id} requires an analyzer stage")
        if metric.source == "analyzer":
            expected_unit = ANALYZER_METRICS.get(metric.metric_id)
            if expected_unit is None:
                raise ConfigurationError(f"unknown standardized analyzer metric: {metric.metric_id}")
            if metric.unit != expected_unit:
                raise ConfigurationError(
                    f"analyzer metric {metric.metric_id} must use unit {expected_unit}"
                )
        if metric.source == "binary_size" and metric.metric_id != "binary_size_bytes":
            raise ConfigurationError("binary_size source must use metric_id=binary_size_bytes")
        if metric.source == "artifact_size" and metric.metric_id != "artifact_size_bytes":
            raise ConfigurationError("artifact_size source must use metric_id=artifact_size_bytes")
        if metric.source == "compile_time" and metric.metric_id != "compile_time_ns":
            raise ConfigurationError("compile_time source must use metric_id=compile_time_ns")
        if metric.source == "link_time" and metric.metric_id != "link_time_ns":
            raise ConfigurationError("link_time source must use metric_id=link_time_ns")
    if options.metric_profile_id is not None:
        if options.metric_profile_id != "rv64gc-qemu-v1":
            raise ConfigurationError("unknown metric profile")
        preset = rv64gc_qemu_v1()
        observed_primary = {
            "primary_metric_id": options.primary_metric_id,
            "metric_source": options.metric_source,
            "metric_unit": options.metric_unit,
            "metric_pattern": options.metric_pattern,
            "metric_file": options.metric_file,
            "analysis_file": options.analysis_file,
        }
        if observed_primary != {key: preset[key] for key in observed_primary}:
            raise ConfigurationError("rv64gc-qemu-v1 metric profile was modified or is incomplete")
        observed_additional = {
            item.metric_id: {
                "metric_id": item.metric_id,
                "source": item.source,
                "unit": item.unit,
                "pattern": item.pattern,
            }
            for item in options.additional_metrics
        }
        expected_additional = {item["metric_id"]: item for item in preset["additional"]}
        if any(
            observed_additional.get(metric_id) != expected
            for metric_id, expected in expected_additional.items()
        ):
            raise ConfigurationError("rv64gc-qemu-v1 metric profile was modified or is incomplete")
        if options.runner.kind != "qemu" or options.linker is None or options.analyzer is None:
            raise ConfigurationError("rv64gc-qemu-v1 requires QEMU runner, linker, and binary analyzer stages")
    uses_metric_file = options.metric_source == "file" or any(
        metric.source == "file" for metric in options.additional_metrics
    )
    if uses_metric_file:
        if options.metric_file is None:
            raise ConfigurationError("file metrics require a suite-relative --metric-file name")
        validate_relative_path(options.metric_file, label="metric file")
        _require_path_placeholder(options.runner, "metric_file", "runner command")
    elif options.metric_file is not None:
        raise ConfigurationError("metric_file was configured without a file-based metric")
    if options.output_contract not in {"lf_return_trailer", "process_exit", "result_file", "raw_stdout"}:
        raise ConfigurationError("output_contract must be lf_return_trailer, process_exit, result_file, or raw_stdout")
    if options.output_contract == "result_file":
        if options.result_file is None:
            raise ConfigurationError("result_file output contract requires a result file")
        validate_relative_path(options.result_file, label="result file")
        _require_path_placeholder(options.runner, "result_file", "runner command")
    elif options.result_file is not None:
        raise ConfigurationError("result_file was configured for an output contract that does not use it")
    uses_analyzer = any(metric.source == "analyzer" for metric in options.additional_metrics)
    if uses_analyzer:
        if options.analysis_file is None:
            raise ConfigurationError("analyzer metrics require an analysis_file")
        validate_relative_path(options.analysis_file, label="analysis file")
    elif options.analyzer is not None or options.analysis_file is not None:
        raise ConfigurationError("analyzer stage/file was configured without analyzer metrics")
    if options.environment_label not in {"official", "local_reference", "proxy"}:
        raise ConfigurationError("environment label must be official, local_reference, or proxy")
    if options.evidence_level not in {"compile_only", "qemu_correctness", "qemu_proxy", "boom_hardware"}:
        raise ConfigurationError("invalid evidence level")
    if options.environment_label == "official" and options.evidence_level != "boom_hardware":
        raise ConfigurationError("official performance evidence must be labeled boom_hardware")
    if options.evidence_level == "boom_hardware" and options.environment_label != "official":
        raise ConfigurationError("boom_hardware evidence requires the official environment label")
    if options.evidence_level == "boom_hardware" and options.runner.kind != "boom":
        raise ConfigurationError("boom_hardware evidence requires runner-kind=boom")
    if options.evidence_level in {"qemu_correctness", "qemu_proxy"} and options.runner.kind != "qemu":
        raise ConfigurationError("QEMU evidence requires runner-kind=qemu")
    if options.evidence_level != "compile_only" and options.output_contract == "raw_stdout":
        raise ConfigurationError("correctness/performance evidence must independently validate main return uint8")
    tools: set[str] = set()
    for version in options.tool_versions:
        if version.tool in tools:
            raise ConfigurationError(f"duplicate tool version: {version.tool}")
        tools.add(version.tool)
        record = version.as_record()
        if options.environment_label == "official" and record["comparison"] != "exact":
            raise ConfigurationError(
                f"official environment requires an exact expected version for {version.tool}"
            )
    if options.environment_label == "official" and not options.tool_versions:
        raise ConfigurationError("official environment requires explicit tool version evidence")
    sha256_pattern = re.compile(r"^[0-9a-f]{64}$")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", options.provenance.repo_commit):
        raise ConfigurationError("repo_commit must be a full 40- or 64-character Git object id")
    for value, label in (
        (options.provenance.pipeline_profile_sha256, "pipeline profile SHA-256"),
        (options.provenance.compiler_artifact_sha256, "compiler artifact SHA-256"),
    ):
        if not sha256_pattern.fullmatch(value):
            raise ConfigurationError(f"{label} must contain 64 lowercase hexadecimal characters")
    if options.provenance.tracked_diff_sha256 is not None and not sha256_pattern.fullmatch(
        options.provenance.tracked_diff_sha256
    ):
        raise ConfigurationError("tracked diff SHA-256 must contain 64 lowercase hexadecimal characters")
    if not options.provenance.repo_dirty and options.provenance.tracked_diff_sha256 is not None:
        raise ConfigurationError("a clean repository cannot declare a tracked diff digest")
    if (options.provenance.measurement_protocol_id is None) != (
        options.provenance.measurement_protocol_sha256 is None
    ):
        raise ConfigurationError("measurement protocol id/SHA-256 must be present together")
    if (
        options.provenance.measurement_protocol_sha256 is not None
        and not sha256_pattern.fullmatch(options.provenance.measurement_protocol_sha256)
    ):
        raise ConfigurationError("measurement protocol SHA-256 must contain 64 lowercase hexadecimal characters")


def _configuration(options: RunOptions) -> dict[str, Any]:
    baseline_timeout_run = (
        load_and_validate(options.baseline_timeout_path.resolve(strict=True))
        if options.baseline_timeout_path is not None
        else None
    )
    metrics = [
        {
            "metric_id": options.primary_metric_id,
            "source": options.metric_source,
            "pattern_sha256": sha256_json(options.metric_pattern) if options.metric_pattern is not None else None,
            "unit": options.metric_unit,
        },
        *[
            {
                "metric_id": metric.metric_id,
                "source": metric.source,
                "pattern_sha256": sha256_json(metric.pattern) if metric.pattern is not None else None,
                "unit": metric.unit,
            }
            for metric in options.additional_metrics
        ],
    ]
    candidate_registry = (
        load_and_validate(
            _workspace_bound_file(
                options.workspace_root,
                options.candidate_registry_path,
                label="candidate registry",
            )
        )
        if options.candidate_registry_path is not None
        else None
    )
    candidate_pass_registry = (
        load_and_validate(
            _workspace_bound_file(
                options.workspace_root,
                options.candidate_pass_registry_path,
                label="candidate pass registry",
            )
        )
        if options.candidate_pass_registry_path is not None
        else None
    )
    enabled_candidate_ids = _pipeline_profile_candidate_ids(
        options.pipeline_profile_path,
        workspace_root=options.workspace_root,
        require_v2=options.candidate_registry_path is not None,
        candidate_order=(
            None
            if candidate_registry is None
            else [item["candidate_id"] for item in candidate_registry["candidates"]]
        ),
    )
    return {
        "compiler": _stage_record(options.compiler),
        "pipeline_profile_file_sha256": (
            sha256_file(
                _workspace_bound_file(
                    options.workspace_root,
                    options.pipeline_profile_path,
                    label="pipeline profile",
                )
            )
            if options.pipeline_profile_path is not None
            else None
        ),
        "candidate_registry_sha256": (
            sha256_json(candidate_registry) if candidate_registry is not None else None
        ),
        "candidate_pass_registry_sha256": (
            sha256_json(candidate_pass_registry)
            if candidate_pass_registry is not None
            else None
        ),
        "enabled_candidate_ids": enabled_candidate_ids,
        "linker": _stage_record(options.linker),
        "analyzer": _stage_record(options.analyzer),
        "runner": _stage_record(options.runner),
        "primary_metric_id": options.primary_metric_id,
        "metric_profile_id": options.metric_profile_id,
        "metrics": metrics,
        "compile_timeout_seconds": options.compile_timeout_seconds,
        "compile_repetitions": options.compile_repetitions,
        "reuse_compile_cache": options.reuse_compile_cache,
        "compile_storage_contract": compile_storage_contract(options.reuse_compile_cache),
        "link_timeout_seconds": options.link_timeout_seconds,
        "analyze_timeout_seconds": options.analyze_timeout_seconds,
        "run_timeout_seconds": options.run_timeout_seconds,
        "timeout_policy": options.timeout_policy,
        "baseline_timeout_run_sha256": (
            sha256_json(baseline_timeout_run)
            if baseline_timeout_run is not None
            else None
        ),
        "baseline_timeout_run_id": (
            baseline_timeout_run["run_id"]
            if baseline_timeout_run is not None
            else None
        ),
        "timeout_minimum_seconds": options.timeout_minimum_seconds,
        "timeout_multiplier": options.timeout_multiplier,
        "timeout_cap_seconds": options.timeout_cap_seconds,
        "repetitions": options.repetitions,
        "max_workers": options.max_workers,
        "keep_going": options.keep_going,
        "retry_failures": options.retry_failures,
        "seed": options.seed,
        "artifact_suffix": options.artifact_suffix,
        "binary_suffix": options.binary_suffix,
        "wsl_distribution_sha256": (
            sha256_json(options.wsl_distribution) if options.wsl_distribution is not None else None
        ),
        "environment_label": options.environment_label,
        "tool_versions": [version.as_record() for version in options.tool_versions],
        "metric_file_sha256": sha256_json(options.metric_file) if options.metric_file is not None else None,
        "analysis_file_sha256": sha256_json(options.analysis_file) if options.analysis_file is not None else None,
        "remarks_file_sha256": sha256_json(options.remarks_file) if options.remarks_file is not None else None,
        "output_contract": options.output_contract,
        "result_file_sha256": sha256_json(options.result_file) if options.result_file is not None else None,
        "evidence_level": options.evidence_level,
        "consistency_fraction": 0.1,
        "consistency_repetitions": 3,
    }


def _summary(cases: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    statuses = [case["status"] for case in cases]
    pending = statuses.count("pending")
    passed = statuses.count("passed")
    return {
        "total_cases": len(cases),
        "passed_cases": passed,
        "failed_cases": len(cases) - pending - passed,
        "pending_cases": pending,
        "censored_cases": statuses.count("timeout"),
        "consistency_selected_cases": sum(bool(case["consistency_selected"]) for case in cases),
        "consistency_passed_cases": sum(case["consistency_passed"] is True for case in cases),
    }


def _compile_phase(sample: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(sample[key])
        for key in ("status", "duration_ns", "exit_code", "stdout", "stderr", "diagnostic")
    }


def _new_case(
    case: Mapping[str, Any],
    consistency_selected: bool,
    effective_timeout_seconds: float,
    timeout_derivation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "case_id": case["id"],
        "family": case["family"],
        "source_group": case["source_group"],
        "target": case["target"],
        "weight": case["weight"],
        "tags": list(case["tags"]),
        "data_role": case["provenance"]["data_role"],
        "oracle_pair": deepcopy(case.get("oracle_pair")),
        "effective_timeout_seconds": effective_timeout_seconds,
        "timeout_derivation": deepcopy(timeout_derivation),
        "attempt_index": 0,
        "attempt_started_at": None,
        "attempt_configuration_sha256": None,
        "source_sha256": case["source"]["sha256"],
        "input_sha256": None if case["input"] is None else case["input"]["sha256"],
        "expected_output_sha256": case["expected_output"]["sha256"],
        "artifact_sha256": None,
        "binary_sha256": None,
        "remarks_sha256": None,
        "remarks_event_count": None,
        "candidate_remark_summary": None,
        "analysis_sha256": None,
        "attempt_journal_sha256": None,
        "attempt_journal_event_count": None,
        "status": "pending",
        "cancellation_reason": None,
        "cache_hit": False,
        "compile": None,
        "compile_samples": [],
        "compile_statistics": None,
        "link": None,
        "analyze": None,
        "measurements": [],
        "samples": [],
        "consistency_selected": consistency_selected,
        "consistency_passed": None if not consistency_selected else False,
        "consistency_mismatched_metrics": [],
        "diagnostic": None,
        "attempts": [],
    }


_ATTEMPT_FIELDS = (
    "status",
    "cancellation_reason",
    "cache_hit",
    "artifact_sha256",
    "binary_sha256",
    "remarks_sha256",
    "remarks_event_count",
    "candidate_remark_summary",
    "analysis_sha256",
    "attempt_journal_sha256",
    "attempt_journal_event_count",
    "compile",
    "compile_samples",
    "compile_statistics",
    "link",
    "analyze",
    "measurements",
    "samples",
    "consistency_passed",
    "consistency_mismatched_metrics",
    "diagnostic",
)


def _normalized_candidate_remark_summary(
    summary: Mapping[str, Any],
    *,
    enabled_candidate_ids: list[str],
) -> dict[str, Any]:
    return {
        "event_count": summary["event_count"],
        "summary_count": summary["summary_count"],
        "paired_candidate_count": summary["paired_candidate_count"],
        "applied_count": summary["applied_count"],
        "rejected_count": summary["rejected_count"],
        "candidates": [
            {
                "candidate_id": candidate_id,
                **summary["by_candidate"][candidate_id],
            }
            for candidate_id in enabled_candidate_ids
        ],
    }


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

_JOURNAL_COMMITMENT_FIELDS = {
    "attempt_journal_sha256",
    "attempt_journal_event_count",
}


def _terminal_case_payload(case: Mapping[str, Any]) -> dict[str, Any]:
    """Return the normalized worker result committed by the terminal journal event."""
    return {
        key: deepcopy(value)
        for key, value in case.items()
        if key not in {"attempts", *_JOURNAL_COMMITMENT_FIELDS}
    }


def _bind_journal_commitment(
    case: dict[str, Any],
    snapshot: JournalSnapshot,
) -> None:
    if snapshot.terminal_case_result is None:
        raise ExecutionError("attempt journal lacks a terminal outcome")
    case["attempt_journal_sha256"] = snapshot.commitment_sha256
    case["attempt_journal_event_count"] = len(snapshot.events)


RemarkEvidenceValidator = Callable[[Path, Mapping[str, Any]], None]


@dataclass(frozen=True)
class VerifiedRunRawEvidence:
    """Canonical commitments plus process-local access to verified current remarks.

    ``document`` is deliberately path-free and safe to embed in normalized campaign
    evidence.  ``current_remark_paths`` is an in-process capability for callers that
    must re-validate the semantic contents of the exact physical remark file; it must
    never be serialized.
    """

    document: dict[str, Any]
    current_remark_paths: Mapping[str, Path | None]


def _run_terminal_identity(
    record: Mapping[str, Any],
    output_path: Path,
) -> dict[str, Any]:
    return {
        "schema_version": "benchmark-run-terminal-identity.v1",
        "run_id": record["run_id"],
        "manifest_sha256": record["manifest_sha256"],
        "configuration_sha256": record["configuration_sha256"],
        "started_at": record["started_at"],
        "output_path_sha256": path_identity(output_path),
    }


def _case_terminal_commitments_sha256(record: Mapping[str, Any]) -> str:
    return sha256_json(
        {
            "schema_version": "benchmark-run-case-terminal-commitments.v1",
            "cases": [
                {
                    "case_id": case["case_id"],
                    "case_sha256": sha256_json(case),
                }
                for case in record["cases"]
            ],
        }
    )


def _run_terminal_content(record: Mapping[str, Any]) -> dict[str, str]:
    expected_summary = _summary(record["cases"])
    if record["summary"] != expected_summary:
        raise ExecutionError("benchmark run summary differs from its case outcomes")
    return {
        "summary_sha256": sha256_json(expected_summary),
        "case_terminal_commitments_sha256": (
            _case_terminal_commitments_sha256(record)
        ),
    }


def _run_terminal_payload(record: Mapping[str, Any]) -> dict[str, str]:
    state = record["state"]
    if state not in {"completed", "failed", "interrupted"}:
        raise ExecutionError("run terminal payload requires a terminal state")
    completed_at = record["completed_at"]
    if state in {"completed", "failed"}:
        if not isinstance(completed_at, str):
            raise ExecutionError("final benchmark run lacks completed_at")
        observed_at = completed_at
    else:
        if completed_at is not None:
            raise ExecutionError("interrupted benchmark run cannot have completed_at")
        observed_at = record["updated_at"]
    if not isinstance(observed_at, str):
        raise ExecutionError("terminal benchmark run lacks an observation timestamp")
    return {
        "state": state,
        "observed_at": observed_at,
        **_run_terminal_content(record),
    }


def _run_terminal_journal(
    record: Mapping[str, Any],
    output_path: Path,
    run_directory: Path,
) -> RunTerminalJournal:
    return RunTerminalJournal(
        run_directory,
        identity=_run_terminal_identity(record, output_path),
    )


class _RawAttemptStore:
    """Single implementation of the run-state and raw-attempt identity contract."""

    def __init__(self, *, output_path: Path, state_root: Path) -> None:
        self.output_path = output_path
        self.state_root = state_root

    def run_directory(self, record: Mapping[str, Any]) -> Path:
        identity = f"{record['run_id']}:{record['manifest_sha256']}"
        return self.state_root / "runs" / safe_slug(identity)

    def state_identity(self, record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "benchmark-run-state.v1",
            "run_id": record["run_id"],
            "manifest_sha256": record["manifest_sha256"],
            "started_at": record["started_at"],
            "output_path_sha256": path_identity(self.output_path),
        }

    def bind_state_identity(
        self,
        record: Mapping[str, Any],
        run_directory: Path,
        *,
        create_if_missing: bool,
    ) -> None:
        if run_directory.is_symlink() or not run_directory.is_dir():
            raise ExecutionError("benchmark execution-state directory is missing or invalid")
        identity = self.state_identity(record)
        identity_path = run_directory / "identity.json"
        if identity_path.exists() or identity_path.is_symlink():
            if identity_path.is_symlink() or not identity_path.is_file():
                raise ExecutionError(
                    "benchmark execution-state identity is not a regular file"
                )
            try:
                observed = read_json(identity_path)
            except ValidationError as exc:
                raise ExecutionError("benchmark execution-state identity is unreadable") from exc
            if observed != identity:
                raise ExecutionError(
                    "benchmark execution state is already bound to another output or run record"
                )
            return
        if not create_if_missing:
            raise ExecutionError("benchmark execution-state identity is missing")
        atomic_write_json(identity_path, identity)

    @staticmethod
    def attempt_directory(
        run_directory: Path,
        *,
        case_id: str,
        attempt_index: int,
    ) -> Path:
        return (
            run_directory
            / "cases"
            / safe_slug(case_id)
            / "attempts"
            / f"attempt-{attempt_index:04d}"
        )

    @staticmethod
    def attempt_identity(
        record: Mapping[str, Any],
        *,
        case_id: str,
        attempt_index: int,
        started_at: str,
        configuration_sha256: str,
    ) -> dict[str, Any]:
        return raw_attempt_identity(
            run_id=record["run_id"],
            manifest_sha256=record["manifest_sha256"],
            case_id=case_id,
            attempt_index=attempt_index,
            started_at=started_at,
            configuration_sha256=configuration_sha256,
        )

    def bind_attempt_directory(
        self,
        record: Mapping[str, Any],
        run_directory: Path,
        *,
        case_id: str,
        attempt_index: int,
        started_at: str,
        configuration_sha256: str,
        allow_existing: bool,
        create_if_missing: bool,
    ) -> Path:
        directory = self.attempt_directory(
            run_directory,
            case_id=case_id,
            attempt_index=attempt_index,
        )
        identity = self.attempt_identity(
            record,
            case_id=case_id,
            attempt_index=attempt_index,
            started_at=started_at,
            configuration_sha256=configuration_sha256,
        )
        identity_path = directory / "identity.json"
        identity_sha256 = raw_attempt_identity_sha256(
            run_id=record["run_id"],
            manifest_sha256=record["manifest_sha256"],
            case_id=case_id,
            attempt_index=attempt_index,
            started_at=started_at,
            configuration_sha256=configuration_sha256,
        )
        if directory.exists() or directory.is_symlink():
            if (
                not allow_existing
                or directory.is_symlink()
                or not directory.is_dir()
                or identity_path.is_symlink()
                or not identity_path.is_file()
            ):
                raise ExecutionError(
                    f"raw attempt directory collision for case {case_id} attempt {attempt_index}"
                )
            try:
                observed = read_json(identity_path)
            except ValidationError as exc:
                raise ExecutionError(
                    f"raw attempt identity is unreadable for case {case_id} attempt {attempt_index}"
                ) from exc
            if observed != identity:
                raise ExecutionError(
                    f"raw attempt identity differs for case {case_id} attempt {attempt_index}"
                )
            AttemptJournal(directory, identity_sha256=identity_sha256).load()
            return directory
        if not create_if_missing:
            raise ExecutionError(
                f"raw attempt evidence is missing for case {case_id} attempt {attempt_index}"
            )
        directory.mkdir(parents=True, exist_ok=False)
        AttemptJournal(directory, identity_sha256=identity_sha256).initialize()
        durable_create_json(identity_path, identity)
        return directory

    def physical_attempts(
        self,
        record: Mapping[str, Any],
        run_directory: Path,
    ) -> dict[tuple[str, int], Path]:
        expected: dict[tuple[str, int], Path] = {}
        expected_case_directories: dict[str, str] = {}
        for case in record["cases"]:
            case_directory_name = safe_slug(case["case_id"])
            expected_case_directories[case_directory_name] = case["case_id"]
            for attempt in case["attempts"]:
                key = (case["case_id"], attempt["attempt_index"])
                expected[key] = self.attempt_directory(
                    run_directory,
                    case_id=case["case_id"],
                    attempt_index=attempt["attempt_index"],
                )
            if case["attempt_started_at"] is not None:
                key = (case["case_id"], case["attempt_index"])
                expected[key] = self.attempt_directory(
                    run_directory,
                    case_id=case["case_id"],
                    attempt_index=case["attempt_index"],
                )

        cases_root = run_directory / "cases"
        actual: dict[tuple[str, int], Path] = {}
        if cases_root.exists() or cases_root.is_symlink():
            if not cases_root.is_dir() or cases_root.is_symlink():
                raise ExecutionError("raw attempt cases root is not a regular directory")
            for case_directory in cases_root.iterdir():
                case_id = expected_case_directories.get(case_directory.name)
                if (
                    case_id is None
                    or case_directory.is_symlink()
                    or not case_directory.is_dir()
                ):
                    raise ExecutionError("raw attempt state contains an orphan case directory")
                children = list(case_directory.iterdir())
                if len(children) != 1 or children[0].name != "attempts":
                    raise ExecutionError("raw attempt case directory has an invalid layout")
                attempts_directory = children[0]
                if attempts_directory.is_symlink() or not attempts_directory.is_dir():
                    raise ExecutionError("raw attempt container is not a regular directory")
                for attempt_directory in attempts_directory.iterdir():
                    match = re.fullmatch(r"attempt-([0-9]{4})", attempt_directory.name)
                    if (
                        match is None
                        or attempt_directory.is_symlink()
                        or not attempt_directory.is_dir()
                    ):
                        raise ExecutionError("raw attempt state contains an invalid attempt entry")
                    key = (case_id, int(match.group(1)))
                    if key in actual:
                        raise ExecutionError("raw attempt state contains duplicate attempt identities")
                    actual[key] = attempt_directory
        if set(actual) != set(expected):
            missing = sorted(set(expected) - set(actual))
            orphan = sorted(set(actual) - set(expected))
            detail = f" missing={missing[0]}" if missing else f" orphan={orphan[0]}"
            raise ExecutionError("physical and normalized attempt sets differ:" + detail)
        if any(actual[key] != path for key, path in expected.items()):
            raise ExecutionError("raw attempt directory path differs from normalized identity")
        return actual

    @staticmethod
    def attempt_journal(
        record: Mapping[str, Any],
        directory: Path,
        *,
        case_id: str,
        attempt_index: int,
        started_at: str,
        configuration_sha256: str,
    ) -> AttemptJournal:
        identity_sha256 = raw_attempt_identity_sha256(
            run_id=record["run_id"],
            manifest_sha256=record["manifest_sha256"],
            case_id=case_id,
            attempt_index=attempt_index,
            started_at=started_at,
            configuration_sha256=configuration_sha256,
        )
        return AttemptJournal(directory, identity_sha256=identity_sha256)

    @staticmethod
    def verify_durable_case_payload_identity(
        case: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> None:
        if set(payload) != set(_terminal_case_payload(case)):
            raise ExecutionError("raw durable case payload has an invalid field set")
        for field, expected in (
            ("case_id", case["case_id"]),
            ("attempt_index", case["attempt_index"]),
            ("attempt_started_at", case["attempt_started_at"]),
            (
                "attempt_configuration_sha256",
                case["attempt_configuration_sha256"],
            ),
            ("source_sha256", case["source_sha256"]),
            ("input_sha256", case["input_sha256"]),
            ("expected_output_sha256", case["expected_output_sha256"]),
        ):
            if payload[field] != expected:
                raise ExecutionError(f"raw terminal case identity differs: {field}")

    @staticmethod
    def replace_current_case_payload(
        case: dict[str, Any], payload: Mapping[str, Any]
    ) -> None:
        attempts = case["attempts"]
        case.clear()
        case.update(deepcopy(dict(payload)))
        case["attempts"] = attempts

    def verify_archived_attempt(
        self,
        record: Mapping[str, Any],
        case: Mapping[str, Any],
        attempt: Mapping[str, Any],
        directory: Path,
    ) -> JournalSnapshot:
        journal = self.attempt_journal(
            record,
            directory,
            case_id=case["case_id"],
            attempt_index=attempt["attempt_index"],
            started_at=attempt["started_at"],
            configuration_sha256=attempt["configuration_sha256"],
        )
        snapshot = journal.load()
        terminal = snapshot.terminal_case_result
        if terminal is None:
            raise ExecutionError("archived attempt lacks a durable terminal journal outcome")
        journal.verify_terminal_raw_files(snapshot)
        expected_terminal = _terminal_case_payload(case)
        expected_terminal["attempt_index"] = attempt["attempt_index"]
        expected_terminal["attempt_started_at"] = attempt["started_at"]
        expected_terminal["attempt_configuration_sha256"] = attempt[
            "configuration_sha256"
        ]
        for field in _ATTEMPT_FIELDS:
            if field not in _JOURNAL_COMMITMENT_FIELDS:
                expected_terminal[field] = deepcopy(attempt[field])
        if (
            terminal != expected_terminal
            or attempt["attempt_journal_sha256"] != snapshot.commitment_sha256
            or attempt["attempt_journal_event_count"] != len(snapshot.events)
        ):
            raise ExecutionError("archived attempt differs from its durable terminal journal")
        return snapshot

    def verify_current_attempt(
        self,
        record: Mapping[str, Any],
        case: Mapping[str, Any],
        directory: Path,
    ) -> JournalSnapshot:
        started_at = case["attempt_started_at"]
        configuration_sha256 = case["attempt_configuration_sha256"]
        if started_at is None or configuration_sha256 is None:
            raise ExecutionError("cannot verify an unstarted current attempt")
        journal = self.attempt_journal(
            record,
            directory,
            case_id=case["case_id"],
            attempt_index=case["attempt_index"],
            started_at=started_at,
            configuration_sha256=configuration_sha256,
        )
        snapshot = journal.load()
        terminal = snapshot.terminal_case_result
        if terminal is None:
            raise ExecutionError("started attempt lacks a durable terminal journal outcome")
        journal.verify_terminal_raw_files(snapshot)
        self.verify_durable_case_payload_identity(case, terminal)
        if (
            terminal != _terminal_case_payload(case)
            or case["attempt_journal_sha256"] != snapshot.commitment_sha256
            or case["attempt_journal_event_count"] != len(snapshot.events)
        ):
            raise ExecutionError("normalized attempt result differs from its durable terminal journal")
        return snapshot


def _verified_remark_binding(
    *,
    record: Mapping[str, Any],
    terminal_payload: Mapping[str, Any],
    snapshot: JournalSnapshot,
    attempt_directory: Path,
) -> tuple[str | None, Path | None]:
    configured_path_sha256 = record["configuration"]["remarks_file_sha256"]
    normalized_sha256 = terminal_payload["remarks_sha256"]
    raw_files = snapshot.terminal_raw_files
    if raw_files is None:
        raise ExecutionError("terminal attempt lacks a raw file inventory")
    if configured_path_sha256 is None:
        if normalized_sha256 is not None:
            raise ExecutionError("normalized remarks exist without a configured remark path")
        return None, None

    matching = [
        item for item in raw_files if sha256_json(item["path"]) == configured_path_sha256
    ]
    if normalized_sha256 is None:
        if matching:
            raise ExecutionError(
                "physical configured remarks exist without normalized remark evidence"
            )
        return None, None
    if len(matching) != 1 or matching[0]["sha256"] != normalized_sha256:
        raise ExecutionError(
            "normalized remarks differ from the configured physical raw file"
        )
    item = matching[0]
    relative = validate_relative_path(item["path"], label="raw remark file")
    path = attempt_directory.joinpath(*relative.parts)
    if path.is_symlink() or not path.is_file():
        raise ExecutionError("verified raw remark path is not a regular file")
    if sha256_file(path) != item["sha256"] or path.stat().st_size != item["size_bytes"]:
        raise ExecutionError("verified raw remark file changed after journal validation")
    return sha256_json([item]), path


def _validate_read_only_run_layout(run_directory: Path) -> None:
    try:
        entries = {entry.name: entry for entry in run_directory.iterdir()}
    except OSError as exc:
        raise ExecutionError("cannot enumerate benchmark execution state") from exc
    if "identity.json" not in entries:
        raise ExecutionError("benchmark execution-state identity is missing")
    if ".run.lock" not in entries:
        raise ExecutionError("benchmark execution-state lease is missing")
    unknown = set(entries) - {
        "identity.json",
        "cases",
        ".run.lock",
        "run-terminal",
    }
    if unknown:
        raise ExecutionError("benchmark execution state contains an unexpected root entry")
    lock = entries.get(".run.lock")
    if lock is not None and (lock.is_symlink() or not lock.is_file()):
        raise ExecutionError("benchmark execution-state lease is not a regular file")
    cases = entries.get("cases")
    if cases is not None and (cases.is_symlink() or not cases.is_dir()):
        raise ExecutionError("raw attempt cases root is not a regular directory")
    terminal = entries.get("run-terminal")
    if terminal is None:
        raise ExecutionError("run terminal journal is missing")
    if terminal.is_symlink() or not terminal.is_dir():
        raise ExecutionError("run terminal journal is not a regular directory")


def _state_tree_sha256(run_directory: Path) -> str:
    files: list[dict[str, Any]] = []
    pending = [run_directory]
    while pending:
        directory = pending.pop()
        try:
            entries = list(directory.iterdir())
        except OSError as exc:
            raise ExecutionError("cannot enumerate benchmark raw evidence tree") from exc
        for entry in entries:
            relative = entry.relative_to(run_directory).as_posix()
            if relative == ".run.lock":
                if entry.is_symlink() or not entry.is_file():
                    raise ExecutionError("benchmark execution-state lease is not a regular file")
                continue
            if entry.is_symlink():
                raise ExecutionError("benchmark raw evidence tree contains a symbolic link")
            if entry.is_dir():
                pending.append(entry)
                continue
            if not entry.is_file():
                raise ExecutionError("benchmark raw evidence tree contains a non-regular entry")
            try:
                files.append(
                    {
                        "path": relative,
                        "sha256": sha256_file(entry),
                        "size_bytes": entry.stat().st_size,
                    }
                )
            except OSError as exc:
                raise ExecutionError("cannot hash benchmark raw evidence tree") from exc
    files.sort(key=lambda item: item["path"].encode("utf-8"))
    return sha256_json(
        {
            "schema_version": "benchmark-run-state-tree.v1",
            "files": files,
        }
    )


def _verify_run_raw_evidence_locked(
    run_record_path: Path,
    state_root: Path,
    *,
    remark_validator: RemarkEvidenceValidator | None = None,
) -> VerifiedRunRawEvidence:
    """Recompute a terminal run's normalized and physical raw-evidence closure."""

    resolved_record = resolve_without_symlinks(
        run_record_path, label="benchmark run record"
    )
    if not resolved_record.is_file():
        raise ValidationError("benchmark run record must be a regular file")
    physical_before = sha256_file(resolved_record)
    record = load_and_validate(resolved_record)
    if record["schema_version"] != "run-record.v1":
        raise ValidationError("raw evidence verifier requires run-record.v1")
    physical_after = sha256_file(resolved_record)
    if physical_before != physical_after:
        raise ExecutionError("benchmark run record changed during raw evidence verification")
    if record["state"] not in {"completed", "failed", "interrupted"}:
        raise ExecutionError("raw evidence verification requires a terminal run state")

    resolved_state_root = resolve_without_symlinks(
        state_root, label="benchmark state root"
    )
    if not resolved_state_root.is_dir():
        raise ValidationError("benchmark state root must be a regular directory")
    store = _RawAttemptStore(
        output_path=resolved_record,
        state_root=resolved_state_root,
    )
    lexical_run_directory = store.run_directory(record)
    run_directory = resolve_without_symlinks(
        lexical_run_directory, label="benchmark run state"
    )
    if run_directory != lexical_run_directory.absolute():
        raise ValidationError("benchmark run state path identity differs")
    _validate_read_only_run_layout(run_directory)
    store.bind_state_identity(
        record,
        run_directory,
        create_if_missing=False,
    )
    terminal_snapshot = _run_terminal_journal(
        record,
        resolved_record,
        run_directory,
    ).load()
    terminal_payload = terminal_snapshot.latest_terminal
    if terminal_payload is None:
        raise ExecutionError("run terminal journal lacks a terminal observation")
    if terminal_payload != _run_terminal_payload(record):
        raise ExecutionError(
            "normalized run terminal differs from its durable journal observation"
        )
    physical_attempts = store.physical_attempts(record, run_directory)
    state_tree_before = _state_tree_sha256(run_directory)

    cases_document: list[dict[str, Any]] = []
    current_remark_paths: dict[str, Path | None] = {}
    attempt_count = 0
    terminal_attempt_count = 0
    for case in record["cases"]:
        attempts_document: list[dict[str, Any]] = []
        current_remark_paths[case["case_id"]] = None
        for attempt in case["attempts"]:
            key = (case["case_id"], attempt["attempt_index"])
            directory = store.bind_attempt_directory(
                record,
                run_directory,
                case_id=case["case_id"],
                attempt_index=attempt["attempt_index"],
                started_at=attempt["started_at"],
                configuration_sha256=attempt["configuration_sha256"],
                allow_existing=True,
                create_if_missing=False,
            )
            if physical_attempts[key] != directory:
                raise ExecutionError("archived raw attempt path changed during verification")
            snapshot = store.verify_archived_attempt(record, case, attempt, directory)
            remark_files_sha256, _ = _verified_remark_binding(
                record=record,
                terminal_payload=snapshot.terminal_case_result or {},
                snapshot=snapshot,
                attempt_directory=directory,
            )
            attempts_document.append(
                {
                    "attempt_index": attempt["attempt_index"],
                    "identity_sha256": attempt["raw_attempt_identity_sha256"],
                    "journal_commitment_sha256": snapshot.commitment_sha256,
                    "journal_event_count": len(snapshot.events),
                    "raw_files_sha256": sha256_json(snapshot.terminal_raw_files),
                    "remark_files_sha256": remark_files_sha256,
                }
            )
            attempt_count += 1
            terminal_attempt_count += 1

        current_attempt_index: int | None = None
        if case["attempt_started_at"] is not None:
            current_attempt_index = case["attempt_index"]
            key = (case["case_id"], current_attempt_index)
            configuration_sha256 = case["attempt_configuration_sha256"]
            assert configuration_sha256 is not None
            directory = store.bind_attempt_directory(
                record,
                run_directory,
                case_id=case["case_id"],
                attempt_index=current_attempt_index,
                started_at=case["attempt_started_at"],
                configuration_sha256=configuration_sha256,
                allow_existing=True,
                create_if_missing=False,
            )
            if physical_attempts[key] != directory:
                raise ExecutionError("current raw attempt path changed during verification")
            snapshot = store.verify_current_attempt(record, case, directory)
            remark_files_sha256, remark_path = _verified_remark_binding(
                record=record,
                terminal_payload=snapshot.terminal_case_result or {},
                snapshot=snapshot,
                attempt_directory=directory,
            )
            identity_sha256 = raw_attempt_identity_sha256(
                run_id=record["run_id"],
                manifest_sha256=record["manifest_sha256"],
                case_id=case["case_id"],
                attempt_index=current_attempt_index,
                started_at=case["attempt_started_at"],
                configuration_sha256=configuration_sha256,
            )
            attempts_document.append(
                {
                    "attempt_index": current_attempt_index,
                    "identity_sha256": identity_sha256,
                    "journal_commitment_sha256": snapshot.commitment_sha256,
                    "journal_event_count": len(snapshot.events),
                    "raw_files_sha256": sha256_json(snapshot.terminal_raw_files),
                    "remark_files_sha256": remark_files_sha256,
                }
            )
            current_remark_paths[case["case_id"]] = remark_path
            if remark_path is not None and remark_validator is not None:
                remark_validator(remark_path, deepcopy(case))
            attempt_count += 1
            terminal_attempt_count += 1

        cases_document.append(
            {
                "case_id": case["case_id"],
                "attempts": attempts_document,
                "current_attempt_index": current_attempt_index,
            }
        )

    if attempt_count != len(physical_attempts):
        raise ExecutionError("verified and physical raw attempt counts differ")
    state_tree_after = _state_tree_sha256(run_directory)
    if state_tree_before != state_tree_after:
        raise ExecutionError("benchmark raw evidence tree changed during verification")
    if sha256_file(resolved_record) != physical_after:
        raise ExecutionError("benchmark run record changed during raw evidence verification")
    document = {
        "schema_version": "benchmark-run-raw-evidence.v1",
        "run_id": record["run_id"],
        "run_canonical_sha256": sha256_json(record),
        "run_physical_sha256": physical_after,
        "terminal_observed_at": terminal_payload["observed_at"],
        "terminal_journal_sha256": terminal_snapshot.commitment_sha256,
        "terminal_journal_event_count": len(terminal_snapshot.events),
        "state_tree_sha256": state_tree_after,
        "attempt_count": attempt_count,
        "terminal_attempt_count": terminal_attempt_count,
        "cases": cases_document,
    }
    document["raw_evidence_sha256"] = sha256_json(
        {
            "schema_version": "benchmark-run-raw-evidence-commitment.v1",
            "document": document,
        }
    )
    return VerifiedRunRawEvidence(
        document=document,
        current_remark_paths=current_remark_paths,
    )


def verify_run_raw_evidence(
    run_record_path: Path,
    state_root: Path,
    *,
    remark_validator: RemarkEvidenceValidator | None = None,
) -> VerifiedRunRawEvidence:
    """Verify raw evidence while holding the executor's output and run leases.

    Both lease files must already exist.  This verifier never synthesizes missing
    execution state and holds the locks in the same order as ``BenchmarkRun``.
    """

    resolved_record = resolve_without_symlinks(
        run_record_path, label="benchmark run record"
    )
    if not resolved_record.is_file():
        raise ValidationError("benchmark run record must be a regular file")
    preliminary_physical_sha256 = sha256_file(resolved_record)
    preliminary = load_and_validate(resolved_record)
    if preliminary["schema_version"] != "run-record.v1":
        raise ValidationError("raw evidence verifier requires run-record.v1")
    resolved_state_root = resolve_without_symlinks(
        state_root, label="benchmark state root"
    )
    if not resolved_state_root.is_dir():
        raise ValidationError("benchmark state root must be a regular directory")
    store = _RawAttemptStore(
        output_path=resolved_record,
        state_root=resolved_state_root,
    )
    run_directory = resolve_without_symlinks(
        store.run_directory(preliminary), label="benchmark run state"
    )
    output_lock = resolve_without_symlinks(
        output_lease_path(resolved_record), label="benchmark output lease"
    )
    run_lock = resolve_without_symlinks(
        run_directory / ".run.lock", label="benchmark execution-state lease"
    )
    if not output_lock.is_file() or not run_lock.is_file():
        raise ValidationError("benchmark execution leases must be regular files")
    lease_metadata = {
        "run_id": preliminary["run_id"],
        "manifest_sha256": preliminary["manifest_sha256"],
        "configuration_sha256": preliminary["configuration_sha256"],
        "output_path_sha256": path_identity(resolved_record),
        "state_root_sha256": path_identity(resolved_state_root),
    }
    with ExclusiveFileLease(
        output_lock,
        "output target",
        lease_metadata,
    ):
        if sha256_file(resolved_record) != preliminary_physical_sha256:
            raise ExecutionError("benchmark run record changed before lease acquisition")
        locked_record = load_and_validate(resolved_record)
        if locked_record != preliminary:
            raise ExecutionError("benchmark run identity changed before lease acquisition")
        locked_run_directory = store.run_directory(locked_record)
        if locked_run_directory != run_directory:
            raise ExecutionError("benchmark run state changed before lease acquisition")
        with ExclusiveFileLease(
            run_lock,
            "execution state",
            lease_metadata,
        ):
            return _verify_run_raw_evidence_locked(
                resolved_record,
                resolved_state_root,
                remark_validator=remark_validator,
            )


def _archive_attempt(record: Mapping[str, Any], case: dict[str, Any]) -> None:
    if case["status"] == "pending":
        raise ExecutionError("cannot archive a pending benchmark attempt")
    if case["attempt_index"] != len(case["attempts"]):
        raise ExecutionError("benchmark attempt index is not contiguous")
    started_at = case["attempt_started_at"]
    configuration_sha256 = case["attempt_configuration_sha256"]
    if started_at is None or configuration_sha256 is None:
        raise ExecutionError("cannot archive an attempt that was never started")
    failure_summary = _ATTEMPT_FAILURE_SUMMARIES.get(case["status"])
    if failure_summary is None:
        raise ExecutionError(f"cannot archive non-failure attempt status: {case['status']}")
    attempts = case["attempts"]
    identity_sha256 = raw_attempt_identity_sha256(
        run_id=record["run_id"],
        manifest_sha256=record["manifest_sha256"],
        case_id=case["case_id"],
        attempt_index=case["attempt_index"],
        started_at=started_at,
        configuration_sha256=configuration_sha256,
    )
    attempts.append(
        {
            "attempt_index": case["attempt_index"],
            "started_at": started_at,
            "archived_at": utc_now(),
            "configuration_sha256": configuration_sha256,
            "raw_attempt_identity_sha256": identity_sha256,
            "failure_summary": failure_summary,
            **{field: deepcopy(case[field]) for field in _ATTEMPT_FIELDS},
        }
    )


def _reset_case_for_pending(case: dict[str, Any], *, advance_attempt: bool) -> None:
    if advance_attempt:
        case["attempt_index"] += 1
    case.update(
        attempt_started_at=None,
        attempt_configuration_sha256=None,
        status="pending",
        cancellation_reason=None,
        cache_hit=False,
        compile=None,
        compile_samples=[],
        compile_statistics=None,
        artifact_sha256=None,
        binary_sha256=None,
        remarks_sha256=None,
        remarks_event_count=None,
        analysis_sha256=None,
        attempt_journal_sha256=None,
        attempt_journal_event_count=None,
        link=None,
        analyze=None,
        measurements=[],
        samples=[],
        consistency_passed=(False if case["consistency_selected"] else None),
        consistency_mismatched_metrics=[],
        diagnostic=None,
    )


def _run_id(configuration_sha256: str, manifest_sha256: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{stamp}-{manifest_sha256[:8]}-{configuration_sha256[:8]}"


class BenchmarkCompiler:
    """Source-to-artifact stage with explicit reusable or attempt-local storage."""

    def __init__(
        self,
        *,
        cache: CompileCache | None,
        renderer: CommandRenderer,
        stage: StageSpec,
        timeout_seconds: float,
        workspace_root: Path,
        privacy_roots: Sequence[Path],
        artifact_suffix: str,
        stage_fingerprint: str,
        repetitions: int,
        reuse_cache: bool,
        cancellation_event: threading.Event,
        candidate_remark_contract: Mapping[str, Any] | None,
    ) -> None:
        self.cache = cache
        self.renderer = renderer
        self.stage = stage
        self.timeout_seconds = timeout_seconds
        self.workspace_root = workspace_root
        self.privacy_roots = privacy_roots
        self.artifact_suffix = artifact_suffix
        self.stage_fingerprint = stage_fingerprint
        self.repetitions = repetitions
        self.reuse_cache = reuse_cache
        self.cancellation_event = cancellation_event
        self.candidate_remark_contract = candidate_remark_contract
        if self.reuse_cache != (self.cache is not None):
            raise ConfigurationError(
                "compile storage contract requires a cache only in reusable_cache_v2 mode"
            )

    def compile(
        self,
        *,
        case: Mapping[str, Any],
        source: Path,
        context_paths: Mapping[str, Path],
        context_scalars: Mapping[str, str],
    ) -> tuple[
        Path,
        dict[str, Any],
        tuple[dict[str, Any], ...],
        dict[str, Any] | None,
        bool,
        Path,
        Path,
    ]:
        cold_logs: dict[str, Path] = {}

        def build(artifact: Path, temporary: Path) -> CompileBuild:
            samples: list[dict[str, Any]] = []
            last_result: ProcessResult | None = None
            last_artifact: Path | None = None
            last_remarks: Path | None = None
            first_artifact_sha256: str | None = None
            first_artifact_index: int | None = None
            case_directory = context_paths["case_dir"]
            for index in range(self.repetitions):
                repetition_directory = temporary / f"repetition-{index:04d}"
                repetition_directory.mkdir(parents=True, exist_ok=False)
                repetition_artifact = repetition_directory / f"artifact{self.artifact_suffix}"
                paths = dict(context_paths)
                paths.update(
                    {
                        "source": source,
                        "artifact": repetition_artifact,
                        "binary": repetition_artifact,
                    }
                )
                if "remarks_file" in context_paths:
                    repetition_remarks = repetition_directory / "remarks.jsonl"
                    repetition_remarks.unlink(missing_ok=True)
                    paths["remarks_file"] = repetition_remarks
                command, environment = self.renderer.render(
                    self.stage,
                    paths=paths,
                    scalars={**context_scalars, "compile_sample_index": str(index)},
                    cwd=self.workspace_root,
                )
                result = run_process(
                    command,
                    cwd=self.workspace_root,
                    environment=environment,
                    stdin_path=None,
                    stdout_path=repetition_directory / "compile.stdout",
                    stderr_path=repetition_directory / "compile.stderr",
                    timeout_seconds=self.timeout_seconds,
                    privacy_roots=self.privacy_roots,
                    cancellation_event=self.cancellation_event,
                )
                phase = result.as_phase_record()
                last_result = result
                preserved_stdout = case_directory / f"compile-repetition-{index:04d}.stdout"
                preserved_stderr = case_directory / f"compile-repetition-{index:04d}.stderr"
                shutil.copy2(repetition_directory / "compile.stdout", preserved_stdout)
                shutil.copy2(repetition_directory / "compile.stderr", preserved_stderr)
                preserved_artifact = (
                    case_directory
                    / f"compile-repetition-{index:04d}.artifact{self.artifact_suffix}"
                )
                preserved_remarks = case_directory / f"compile-repetition-{index:04d}.remarks.jsonl"
                if repetition_artifact.is_file():
                    shutil.copy2(repetition_artifact, preserved_artifact)
                    phase["artifact_sha256"] = sha256_file(preserved_artifact)
                    phase["artifact_size_bytes"] = preserved_artifact.stat().st_size
                else:
                    phase["artifact_sha256"] = None
                    phase["artifact_size_bytes"] = None
                repetition_remarks = paths.get("remarks_file")
                if repetition_remarks is not None and repetition_remarks.is_file():
                    shutil.copy2(repetition_remarks, preserved_remarks)
                    phase["remarks_sha256"] = sha256_file(preserved_remarks)
                    with preserved_remarks.open("rb") as stream:
                        phase["remarks_event_count"] = sum(
                            bool(line.strip()) for line in stream
                        )
                    if result.status == "ok":
                        try:
                            if self.candidate_remark_contract is None:
                                load_and_validate_jsonl(preserved_remarks)
                            else:
                                validate_candidate_remark_jsonl(
                                    preserved_remarks,
                                    **self.candidate_remark_contract,
                                )
                        except ValidationError as exc:
                            phase["status"] = "error"
                            phase["diagnostic"] = sanitize_text(
                                f"compiler emitted invalid optimization remarks: {exc}",
                                self.privacy_roots,
                            )
                else:
                    phase["remarks_sha256"] = None
                    phase["remarks_event_count"] = None
                samples.append(phase)
                cold_logs.update(stdout=preserved_stdout, stderr=preserved_stderr)
                if phase["status"] == "error" and result.status == "ok":
                    return CompileBuild(_compile_phase(phase), tuple(samples), None)
                if result.status != "ok":
                    return CompileBuild(_compile_phase(phase), tuple(samples), None)
                if not repetition_artifact.is_file():
                    phase["status"] = "error"
                    phase["diagnostic"] = "compiler exited successfully without creating {artifact}"
                    return CompileBuild(_compile_phase(phase), tuple(samples), None)
                if "remarks_file" in context_paths:
                    repetition_remarks = paths["remarks_file"]
                    if not repetition_remarks.is_file():
                        phase["status"] = "error"
                        phase["diagnostic"] = "compiler exited successfully without creating {remarks_file}"
                        return CompileBuild(_compile_phase(phase), tuple(samples), None)
                    last_remarks = repetition_remarks
                artifact_sha256 = phase["artifact_sha256"]
                assert isinstance(artifact_sha256, str)
                if first_artifact_sha256 is None:
                    first_artifact_sha256 = artifact_sha256
                    first_artifact_index = index
                elif artifact_sha256 != first_artifact_sha256:
                    phase["status"] = "error"
                    phase["diagnostic"] = (
                        "compiler artifact differs across cold repetitions: "
                        f"sample_index={index}, first_sample_index={first_artifact_index}, "
                        f"first_sha256={first_artifact_sha256}, current_sha256={artifact_sha256}"
                    )
                    return CompileBuild(_compile_phase(phase), tuple(samples), None)
                last_artifact = repetition_artifact
            assert last_result is not None and last_artifact is not None
            shutil.copy2(last_artifact, artifact)
            shutil.copy2(last_artifact.parent / "compile.stdout", temporary / "compile.stdout")
            shutil.copy2(last_artifact.parent / "compile.stderr", temporary / "compile.stderr")
            if last_remarks is not None:
                shutil.copy2(last_remarks, temporary / "remarks.jsonl")
                remarks_destination = context_paths["remarks_file"]
                remarks_destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(last_remarks, remarks_destination)
            cold_stdout = case_directory / "cold-compile.stdout"
            cold_stderr = case_directory / "cold-compile.stderr"
            shutil.copy2(last_artifact.parent / "compile.stdout", cold_stdout)
            shutil.copy2(last_artifact.parent / "compile.stderr", cold_stderr)
            cold_logs.update(stdout=cold_stdout, stderr=cold_stderr)
            durations = [float(sample["duration_ns"]) for sample in samples]
            median = float(statistics.median(durations))
            mad = float(statistics.median(abs(value - median) for value in durations))
            compile_statistics = {
                "sample_count": len(samples),
                "median_duration_ns": median,
                "mad_duration_ns": mad,
            }
            return CompileBuild(last_result.as_phase_record(), tuple(samples), compile_statistics)

        if not self.reuse_cache:
            compile_directory = context_paths["case_dir"] / "attempt-local-compile"
            compile_directory.mkdir(parents=False, exist_ok=False)
            artifact = compile_directory / f"artifact{self.artifact_suffix}"
            direct = build(artifact, compile_directory)
            compile_stdout = cold_logs.get(
                "stdout", context_paths["case_dir"] / "cold-compile.stdout"
            )
            compile_stderr = cold_logs.get(
                "stderr", context_paths["case_dir"] / "cold-compile.stderr"
            )
            return (
                artifact,
                direct.phase,
                direct.samples,
                direct.statistics,
                False,
                compile_stdout,
                compile_stderr,
            )

        assert self.cache is not None
        key = sha256_json(
            {
                "schema": "compile-cache-key.v3",
                "storage_contract": compile_storage_contract(True),
                "source_sha256": case["source"]["sha256"],
                "input_sha256": None if case["input"] is None else case["input"]["sha256"],
                "target": case["target"],
                "stage_fingerprint": self.stage_fingerprint,
                "artifact_suffix": self.artifact_suffix,
                "compile_repetitions": self.repetitions,
            }
        )
        entry = self.cache.get_or_build(key, self.artifact_suffix, build)
        if entry.hit:
            compile_stdout = entry.artifact.parent / "compile.stdout"
            compile_stderr = entry.artifact.parent / "compile.stderr"
            case_directory = context_paths["case_dir"]
            for index in range(self.repetitions):
                cached_repetition = entry.artifact.parent / f"repetition-{index:04d}"
                for stream_name in ("stdout", "stderr"):
                    cached_stream = cached_repetition / f"compile.{stream_name}"
                    if not cached_stream.is_file():
                        raise ExecutionError("compile cache entry lacks cold repetition logs")
                    shutil.copy2(
                        cached_stream,
                        case_directory / f"compile-repetition-{index:04d}.{stream_name}",
                    )
                cached_artifact = cached_repetition / f"artifact{self.artifact_suffix}"
                shutil.copy2(
                    cached_artifact,
                    case_directory
                    / f"compile-repetition-{index:04d}.artifact{self.artifact_suffix}",
                )
                cached_remarks = cached_repetition / "remarks.jsonl"
                sample = entry.samples[index]
                if sample["remarks_sha256"] is not None:
                    shutil.copy2(
                        cached_remarks,
                        case_directory / f"compile-repetition-{index:04d}.remarks.jsonl",
                    )
            if "remarks_file" in context_paths:
                cached_remarks = entry.artifact.parent / "remarks.jsonl"
                if not cached_remarks.is_file():
                    raise ExecutionError("compile cache entry lacks configured optimization remarks")
                remarks_destination = context_paths["remarks_file"]
                remarks_destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cached_remarks, remarks_destination)
        else:
            compile_stdout = cold_logs.get("stdout", context_paths["case_dir"] / "cold-compile.stdout")
            compile_stderr = cold_logs.get("stderr", context_paths["case_dir"] / "cold-compile.stderr")
        return (
            entry.artifact,
            entry.phase,
            entry.samples,
            entry.statistics,
            entry.hit,
            compile_stdout,
            compile_stderr,
        )


class BenchmarkRun:
    def __init__(self, options: RunOptions) -> None:
        _validate_options(options)
        self.options = options
        self.manifest_path = options.manifest_path.resolve(strict=True)
        self.suite_root = options.suite_root.resolve(strict=True)
        self.workspace_root = options.workspace_root.resolve(strict=True)
        if not self.workspace_root.is_dir() or not self.suite_root.is_dir():
            raise ConfigurationError("workspace_root and suite_root must be directories")
        self.output_path = options.output_path.resolve()
        self.state_root = options.state_root.resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.manifest = load_and_validate(
            self.manifest_path,
            suite_root=self.suite_root,
            verify_files=True,
        )
        self.configuration = _configuration(options)
        self.provenance = options.provenance.as_record()
        if options.candidate_registry_path is None:
            self.candidate_remark_contract: dict[str, Any] | None = None
        else:
            assert options.candidate_pass_registry_path is not None
            candidate_catalog = load_and_validate(
                _workspace_bound_file(
                    self.workspace_root,
                    options.candidate_registry_path,
                    label="candidate registry",
                )
            )
            candidate_pass_registry = load_and_validate(
                _workspace_bound_file(
                    self.workspace_root,
                    options.candidate_pass_registry_path,
                    label="candidate pass registry",
                )
            )
            self.candidate_remark_contract = {
                "catalog": candidate_catalog,
                "pass_registry": candidate_pass_registry,
                "enabled_candidate_ids": self.configuration[
                    "enabled_candidate_ids"
                ],
                "candidate_registry_sha256": self.configuration[
                    "candidate_registry_sha256"
                ],
                "pipeline_profile_id": self.provenance["pipeline_profile_id"],
                "pipeline_profile_sha256": self.provenance[
                    "pipeline_profile_sha256"
                ],
            }
        self.measurement_protocol_assets = (
            resolve_measurement_protocol_assets(
                dict(options.measurement_protocol_assets),
                workspace_root=self.workspace_root,
            )
            if options.measurement_protocol_assets
            else {}
        )
        self.configuration_sha256 = sha256_json(
            {"configuration": self.configuration, "provenance": self.provenance}
        )
        self.manifest_sha256 = sha256_json(self.manifest)
        self.case_timeouts, self.case_timeout_derivations = self._case_timeout_plan()
        needs_wsl = any(
            stage is not None and stage.adapter == "wsl"
            for stage in (options.compiler, options.linker, options.analyzer, options.runner)
        )
        mapper = (
            WslPathMapper(options.wsl_executable, options.wsl_distribution) if needs_wsl else None
        )
        self.renderer = CommandRenderer(mapper)
        self.privacy_roots = (self.workspace_root, self.suite_root, self.state_root)
        self.metric_specs = (
            MeasurementSpec(
                options.primary_metric_id,
                options.metric_source,
                options.metric_unit,
                options.metric_pattern,
            ),
            *options.additional_metrics,
        )
        try:
            self.metric_patterns = {
                metric.metric_id: re.compile(metric.pattern)
                for metric in self.metric_specs
                if metric.pattern is not None
            }
        except re.error as exc:
            raise ConfigurationError(f"invalid metric regular expression: {exc}") from exc
        selected_count = math.ceil(len(self.manifest["cases"]) * 0.1)
        selected_order = sorted(
            self.manifest["cases"],
            key=lambda case: sha256_json({"seed": options.seed, "case_id": case["id"]}),
        )
        self.consistency_selected = {case["id"] for case in selected_order[:selected_count]}
        self._write_lock = threading.Lock()
        self.cancellation_event = threading.Event()
        compiler_record = self.configuration["compiler"]
        assert isinstance(compiler_record, dict)
        self.compiler = BenchmarkCompiler(
            cache=(CompileCache(self.state_root) if options.reuse_compile_cache else None),
            renderer=self.renderer,
            stage=options.compiler,
            timeout_seconds=options.compile_timeout_seconds,
            workspace_root=self.workspace_root,
            privacy_roots=self.privacy_roots,
            artifact_suffix=options.artifact_suffix,
            stage_fingerprint=sha256_json(
                {
                    "stage": compiler_record,
                    "compiler_artifact_sha256": self.provenance["compiler_artifact_sha256"],
                    "pipeline_profile_sha256": self.provenance["pipeline_profile_sha256"],
                    "candidate_registry_sha256": self.configuration[
                        "candidate_registry_sha256"
                    ],
                    "candidate_pass_registry_sha256": self.configuration[
                        "candidate_pass_registry_sha256"
                    ],
                    "measurement_protocol_sha256": self.provenance["measurement_protocol_sha256"],
                    "tool_versions": self.configuration["tool_versions"],
                }
            ),
            repetitions=options.compile_repetitions,
            reuse_cache=options.reuse_compile_cache,
            cancellation_event=self.cancellation_event,
            candidate_remark_contract=self.candidate_remark_contract,
        )

    def _case_timeout_plan(
        self,
    ) -> tuple[dict[str, float], dict[str, dict[str, Any] | None]]:
        if self.options.timeout_policy == "fixed":
            timeouts = {
                case["id"]: float(self.options.run_timeout_seconds)
                for case in self.manifest["cases"]
            }
            return timeouts, {case_id: None for case_id in timeouts}
        if self.options.timeout_policy == "initial":
            timeouts = {
                case["id"]: float(self.options.timeout_cap_seconds)
                for case in self.manifest["cases"]
            }
            return timeouts, {case_id: None for case_id in timeouts}
        assert self.options.baseline_timeout_path is not None
        baseline = load_and_validate(self.options.baseline_timeout_path.resolve(strict=True))
        if baseline["schema_version"] != "run-record.v1":
            raise ConfigurationError("baseline timeout evidence must be run-record.v1")
        if baseline["suite_id"] != self.manifest["suite_id"] or baseline["manifest_sha256"] != self.manifest_sha256:
            raise ConfigurationError("baseline timeout evidence describes a different suite/manifest")
        baseline_configuration = baseline["configuration"]
        if (
            baseline_configuration["timeout_policy"] != "initial"
            or not math.isclose(baseline_configuration["run_timeout_seconds"], 1800.0, rel_tol=0, abs_tol=1e-12)
            or not math.isclose(baseline_configuration["timeout_minimum_seconds"], 120.0, rel_tol=0, abs_tol=1e-12)
            or not math.isclose(baseline_configuration["timeout_multiplier"], 3.0, rel_tol=0, abs_tol=1e-12)
            or not math.isclose(baseline_configuration["timeout_cap_seconds"], 1800.0, rel_tol=0, abs_tol=1e-12)
        ):
            raise ConfigurationError(
                "baseline timeout evidence must use the canonical initial 1800-second protocol"
            )
        if baseline["provenance"]["measurement_protocol_sha256"] != self.provenance["measurement_protocol_sha256"]:
            raise ConfigurationError("baseline timeout measurement protocol differs")
        for key in ("runner", "linker", "analyzer", "primary_metric_id", "metrics", "environment_label", "evidence_level", "tool_versions", "output_contract"):
            if baseline["configuration"][key] != self.configuration[key]:
                raise ConfigurationError(f"baseline timeout environment differs: {key}")
        by_id = {case["case_id"]: case for case in baseline["cases"]}
        expected_ids = {case["id"] for case in self.manifest["cases"]}
        if set(by_id) != expected_ids:
            raise ConfigurationError("baseline timeout run case set differs from the scheduled manifest")
        result: dict[str, float] = {}
        derivations: dict[str, dict[str, Any] | None] = {}
        baseline_sha256 = sha256_json(baseline)
        for case_id in sorted(expected_ids):
            observation = by_id[case_id]
            manifest_case = next(case for case in self.manifest["cases"] if case["id"] == case_id)
            immutable = {
                "family": manifest_case["family"],
                "source_group": manifest_case["source_group"],
                "target": manifest_case["target"],
                "weight": manifest_case["weight"],
                "source_sha256": manifest_case["source"]["sha256"],
                "input_sha256": None if manifest_case["input"] is None else manifest_case["input"]["sha256"],
                "expected_output_sha256": manifest_case["expected_output"]["sha256"],
            }
            if any(observation[key] != value for key, value in immutable.items()):
                raise ConfigurationError(f"baseline timeout case provenance differs: {case_id}")
            if observation["status"] == "timeout":
                result[case_id] = float(self.options.timeout_cap_seconds)
                derivations[case_id] = {
                    "baseline_run_id": baseline["run_id"],
                    "baseline_run_sha256": baseline_sha256,
                    "baseline_case_status": "timeout",
                    "baseline_median_duration_ns": None,
                }
                continue
            if observation["status"] != "passed" or not observation["samples"]:
                raise ConfigurationError(f"baseline timeout observation is unusable: {case_id}")
            durations_ns = [float(sample["duration_ns"]) for sample in observation["samples"]]
            baseline_median_ns = float(statistics.median(durations_ns))
            baseline_seconds = baseline_median_ns / 1_000_000_000
            result[case_id] = min(
                float(self.options.timeout_cap_seconds),
                max(
                    float(self.options.timeout_minimum_seconds),
                    float(self.options.timeout_multiplier) * baseline_seconds,
                ),
            )
            derivations[case_id] = {
                "baseline_run_id": baseline["run_id"],
                "baseline_run_sha256": baseline_sha256,
                "baseline_case_status": "passed",
                "baseline_median_duration_ns": baseline_median_ns,
            }
        return result, derivations

    def _initial_record(self) -> dict[str, Any]:
        identifier = self.options.run_id or _run_id(self.configuration_sha256, self.manifest_sha256)
        now = utc_now()
        cases = [
            _new_case(
                case,
                case["id"] in self.consistency_selected,
                self.case_timeouts[case["id"]],
                self.case_timeout_derivations[case["id"]],
            )
            for case in self.manifest["cases"]
        ]
        record = {
            "schema_version": "run-record.v1",
            "run_id": identifier,
            "suite_id": self.manifest["suite_id"],
            "manifest_sha256": self.manifest_sha256,
            "manifest_case_count": len(self.manifest["cases"]),
            "manifest_case_ids_sha256": sha256_json([case["id"] for case in self.manifest["cases"]]),
            "configuration_sha256": self.configuration_sha256,
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
            "state": "running",
            "provenance": self.provenance,
            "configuration": self.configuration,
            "cases": cases,
            "summary": _summary(cases),
        }
        validate_document(record)
        return record

    def _load_record(self) -> dict[str, Any]:
        if not self.output_path.exists():
            return self._initial_record()
        record = load_and_validate(self.output_path)
        if record["manifest_sha256"] != self.manifest_sha256:
            raise ConfigurationError("cannot resume: benchmark manifest digest changed")
        if record["configuration_sha256"] != self.configuration_sha256:
            raise ConfigurationError("cannot resume: benchmark configuration digest changed")
        if self.options.run_id is not None and record["run_id"] != self.options.run_id:
            raise ConfigurationError("cannot resume: requested run_id differs from existing record")
        expected_ids = [case["id"] for case in self.manifest["cases"]]
        actual_ids = [case["case_id"] for case in record["cases"]]
        if actual_ids != expected_ids:
            raise ConfigurationError("cannot resume: case sequence differs from manifest")
        return record

    def _run_directory(self, record: Mapping[str, Any]) -> Path:
        return _RawAttemptStore(
            output_path=self.output_path,
            state_root=self.state_root,
        ).run_directory(record)

    def _bind_state_identity(
        self,
        record: Mapping[str, Any],
        run_directory: Path,
    ) -> None:
        _RawAttemptStore(
            output_path=self.output_path,
            state_root=self.state_root,
        ).bind_state_identity(record, run_directory, create_if_missing=True)

    def _attempt_directory(
        self,
        run_directory: Path,
        *,
        case_id: str,
        attempt_index: int,
    ) -> Path:
        return _RawAttemptStore.attempt_directory(
            run_directory,
            case_id=case_id,
            attempt_index=attempt_index,
        )

    def _attempt_identity(
        self,
        record: Mapping[str, Any],
        *,
        case_id: str,
        attempt_index: int,
        started_at: str,
        configuration_sha256: str,
    ) -> dict[str, Any]:
        return _RawAttemptStore.attempt_identity(
            record,
            case_id=case_id,
            attempt_index=attempt_index,
            started_at=started_at,
            configuration_sha256=configuration_sha256,
        )

    def _bind_attempt_directory(
        self,
        record: Mapping[str, Any],
        run_directory: Path,
        *,
        case_id: str,
        attempt_index: int,
        started_at: str,
        configuration_sha256: str,
        allow_existing: bool,
        create_if_missing: bool,
    ) -> Path:
        return _RawAttemptStore(
            output_path=self.output_path,
            state_root=self.state_root,
        ).bind_attempt_directory(
            record,
            run_directory,
            case_id=case_id,
            attempt_index=attempt_index,
            started_at=started_at,
            configuration_sha256=configuration_sha256,
            allow_existing=allow_existing,
            create_if_missing=create_if_missing,
        )

    def _physical_attempts(
        self,
        record: Mapping[str, Any],
        run_directory: Path,
    ) -> dict[tuple[str, int], Path]:
        return _RawAttemptStore(
            output_path=self.output_path,
            state_root=self.state_root,
        ).physical_attempts(record, run_directory)

    def _attempt_journal(
        self,
        record: Mapping[str, Any],
        directory: Path,
        *,
        case_id: str,
        attempt_index: int,
        started_at: str,
        configuration_sha256: str,
    ) -> AttemptJournal:
        return _RawAttemptStore.attempt_journal(
            record,
            directory,
            case_id=case_id,
            attempt_index=attempt_index,
            started_at=started_at,
            configuration_sha256=configuration_sha256,
        )

    def _recover_current_attempt(
        self,
        record: Mapping[str, Any],
        case: dict[str, Any],
        directory: Path,
    ) -> None:
        started_at = case["attempt_started_at"]
        configuration_sha256 = case["attempt_configuration_sha256"]
        assert started_at is not None and configuration_sha256 is not None
        journal = self._attempt_journal(
            record,
            directory,
            case_id=case["case_id"],
            attempt_index=case["attempt_index"],
            started_at=started_at,
            configuration_sha256=configuration_sha256,
        )
        snapshot = journal.load()
        terminal = snapshot.terminal_case_result
        if terminal is None:
            if snapshot.has_phase_evidence:
                raise ExecutionError(
                    f"raw phase evidence lacks a terminal outcome for case {case['case_id']} "
                    f"attempt {case['attempt_index']}"
                )
            if snapshot.events:
                raise ExecutionError("raw attempt journal lacks a recognized terminal outcome")
            journal.assert_no_uncommitted_raw_evidence()
            if case["status"] != "pending":
                raise ExecutionError("normalized terminal attempt lacks durable journal evidence")
            case["status"] = "cancelled"
            case["cancellation_reason"] = "execution_interrupted"
            case["diagnostic"] = "interrupted before the worker entered a benchmark phase"
            snapshot = journal.append_terminal(_terminal_case_payload(case))
            _bind_journal_commitment(case, snapshot)
            return

        journal.verify_terminal_raw_files(snapshot)

        self._verify_durable_case_payload_identity(case, terminal)

        if case["status"] == "pending":
            self._replace_current_case_payload(case, terminal)
            _bind_journal_commitment(case, snapshot)
            return
        if (
            _terminal_case_payload(case) != terminal
            or case["attempt_journal_sha256"] != snapshot.commitment_sha256
            or case["attempt_journal_event_count"] != len(snapshot.events)
        ):
            raise ExecutionError("normalized attempt result differs from its durable terminal journal")

    @staticmethod
    def _verify_durable_case_payload_identity(
        case: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> None:
        _RawAttemptStore.verify_durable_case_payload_identity(case, payload)

    @staticmethod
    def _replace_current_case_payload(
        case: dict[str, Any], payload: Mapping[str, Any]
    ) -> None:
        _RawAttemptStore.replace_current_case_payload(case, payload)

    def _verify_archived_attempt(
        self,
        record: Mapping[str, Any],
        case: Mapping[str, Any],
        attempt: Mapping[str, Any],
        directory: Path,
    ) -> None:
        _RawAttemptStore(
            output_path=self.output_path,
            state_root=self.state_root,
        ).verify_archived_attempt(record, case, attempt, directory)

    def _prepare_record_for_execution(
        self,
        record: dict[str, Any],
        run_directory: Path,
    ) -> None:
        physical = self._physical_attempts(record, run_directory)
        for case in record["cases"]:
            for attempt in case["attempts"]:
                directory = self._bind_attempt_directory(
                    record,
                    run_directory,
                    case_id=case["case_id"],
                    attempt_index=attempt["attempt_index"],
                    started_at=attempt["started_at"],
                    configuration_sha256=attempt["configuration_sha256"],
                    allow_existing=True,
                    create_if_missing=False,
                )
                if physical[(case["case_id"], attempt["attempt_index"])] != directory:
                    raise ExecutionError("archived raw attempt path changed during verification")
                self._verify_archived_attempt(record, case, attempt, directory)
            started_at = case["attempt_started_at"]
            configuration_sha256 = case["attempt_configuration_sha256"]
            if started_at is not None:
                assert configuration_sha256 is not None
                directory = self._bind_attempt_directory(
                    record,
                    run_directory,
                    case_id=case["case_id"],
                    attempt_index=case["attempt_index"],
                    started_at=started_at,
                    configuration_sha256=configuration_sha256,
                    allow_existing=True,
                    create_if_missing=False,
                )
                if physical[(case["case_id"], case["attempt_index"])] != directory:
                    raise ExecutionError("current raw attempt path changed during verification")
                self._recover_current_attempt(record, case, directory)
            if case["status"] == "cancelled":
                if started_at is None:
                    if case["cancellation_reason"] != "scheduler_cancelled":
                        raise ExecutionError(
                            f"unstarted cancellation has an invalid reason for case {case['case_id']}"
                    )
                    _reset_case_for_pending(case, advance_attempt=False)
                elif case["cancellation_reason"] == "infrastructure_failure":
                    raise ExecutionError(
                        "cannot resume infrastructure-failed attempt: "
                        f"case={case['case_id']}, attempt={case['attempt_index']}, "
                        f"diagnostic={case['diagnostic']}"
                    )
                elif case["cancellation_reason"] in {
                    "scheduler_cancelled",
                    "execution_interrupted",
                } or self.options.retry_failures:
                    _archive_attempt(record, case)
                    _reset_case_for_pending(case, advance_attempt=True)
            elif self.options.retry_failures and case["status"] not in {"pending", "passed"}:
                _archive_attempt(record, case)
                _reset_case_for_pending(case, advance_attempt=True)
        record["state"] = "running"
        record["completed_at"] = None
        record["updated_at"] = utc_now()
        record["summary"] = _summary(record["cases"])

    def _reserve_attempt(
        self,
        record: dict[str, Any],
        case: dict[str, Any],
        run_directory: Path,
    ) -> Path:
        if (
            case["status"] != "pending"
            or case["attempt_started_at"] is not None
            or case["attempt_configuration_sha256"] is not None
            or case["attempt_index"] != len(case["attempts"])
        ):
            raise ExecutionError("cannot reserve a non-pending or non-contiguous attempt")
        case["attempt_started_at"] = utc_now()
        case["attempt_configuration_sha256"] = self.configuration_sha256
        self._write_record(record)
        return self._bind_attempt_directory(
            record,
            run_directory,
            case_id=case["case_id"],
            attempt_index=case["attempt_index"],
            started_at=case["attempt_started_at"],
            configuration_sha256=case["attempt_configuration_sha256"],
            allow_existing=False,
            create_if_missing=True,
        )

    def _write_record(
        self,
        record: dict[str, Any],
        *,
        updated_at: str | None = None,
    ) -> None:
        with self._write_lock:
            record["updated_at"] = updated_at or utc_now()
            record["summary"] = _summary(record["cases"])
            validate_document(record)
            atomic_write_json(self.output_path, record)

    def _reconcile_run_terminal(
        self,
        record: dict[str, Any],
        run_directory: Path,
    ) -> RunTerminalSnapshot | None:
        journal = _run_terminal_journal(record, self.output_path, run_directory)
        if not journal.exists:
            if record["state"] in {"completed", "failed", "interrupted"}:
                raise ExecutionError(
                    "terminal benchmark run lacks a physical run terminal journal"
                )
            return None

        snapshot = journal.load()
        terminal = snapshot.latest_terminal
        if terminal is None:
            if record["state"] in {"completed", "failed", "interrupted"}:
                raise ExecutionError(
                    "terminal benchmark run lacks a physical terminal observation"
                )
            return snapshot

        if record["state"] in {"completed", "failed", "interrupted"}:
            if terminal != _run_terminal_payload(record):
                raise ExecutionError(
                    "normalized run terminal differs from its durable journal observation"
                )
            return snapshot

        current_content = _run_terminal_content(record)
        terminal_content = {
            "summary_sha256": terminal["summary_sha256"],
            "case_terminal_commitments_sha256": terminal[
                "case_terminal_commitments_sha256"
            ],
        }
        if current_content == terminal_content:
            record["state"] = terminal["state"]
            record["completed_at"] = (
                terminal["observed_at"]
                if terminal["state"] in {"completed", "failed"}
                else None
            )
            self._write_record(record, updated_at=terminal["observed_at"])
            return snapshot
        if terminal["state"] != "interrupted":
            raise ExecutionError(
                "running benchmark record diverges from a final run terminal observation"
            )
        return snapshot

    def _seal_run_terminal(
        self,
        record: dict[str, Any],
        run_directory: Path,
        *,
        state: str,
    ) -> RunTerminalSnapshot:
        if state not in {"completed", "failed", "interrupted"}:
            raise ExecutionError("cannot seal a non-terminal benchmark run state")
        if record["state"] != "running":
            raise ExecutionError("cannot seal a benchmark run that is not running")

        # Persist the exact case/summary prefix first. If the process stops after
        # the append-only terminal event but before the normalized terminal
        # write, the next invocation can recover that write without inventing
        # evidence or changing the observation time.
        self._write_record(record)
        observed_at = utc_now()
        record["state"] = state
        record["completed_at"] = (
            observed_at if state in {"completed", "failed"} else None
        )
        record["updated_at"] = observed_at
        record["summary"] = _summary(record["cases"])
        payload = _run_terminal_payload(record)
        snapshot = _run_terminal_journal(
            record,
            self.output_path,
            run_directory,
        ).seal(payload)
        self._write_record(record, updated_at=observed_at)
        return snapshot

    def _commit_scheduler_terminal(
        self,
        record: Mapping[str, Any],
        case: dict[str, Any],
        run_directory: Path,
        *,
        reason: str,
        diagnostic: str,
    ) -> None:
        # Recovery and terminal construction are transactional with respect to
        # the normalized record.  A durable journal publish may fail (or may
        # publish and then report an I/O error); neither case may expose a
        # prefix-only case that violates the run-record schema and masks the
        # original worker/sealing exceptions.
        working_case = deepcopy(case)
        started_at = case["attempt_started_at"]
        configuration_sha256 = case["attempt_configuration_sha256"]
        if started_at is None or configuration_sha256 is None:
            raise ExecutionError("cannot commit a terminal outcome for an unreserved attempt")
        directory = self._attempt_directory(
            run_directory,
            case_id=case["case_id"],
            attempt_index=case["attempt_index"],
        )
        journal = self._attempt_journal(
            record,
            directory,
            case_id=case["case_id"],
            attempt_index=case["attempt_index"],
            started_at=started_at,
            configuration_sha256=configuration_sha256,
        )
        observed = journal.load()
        if observed.terminal_case_result is not None:
            journal.verify_terminal_raw_files(observed)
            self._verify_durable_case_payload_identity(
                working_case, observed.terminal_case_result
            )
            self._replace_current_case_payload(
                working_case, observed.terminal_case_result
            )
            _bind_journal_commitment(working_case, observed)
            case.clear()
            case.update(working_case)
            return
        committed_prefix = observed.latest_committed_case_prefix
        if committed_prefix is not None:
            self._verify_durable_case_payload_identity(working_case, committed_prefix)
            self._replace_current_case_payload(working_case, committed_prefix)
        preserve_committed_failure = (
            reason == "infrastructure_failure" and working_case["status"] != "pending"
        )
        # A terminal tool/runtime/correctness failure committed immediately
        # before an orchestrator exception remains authoritative; only a
        # still-pending prefix is relabelled as infrastructure failure.
        if not preserve_committed_failure:
            working_case["status"] = "cancelled"
            working_case["cancellation_reason"] = reason
            working_case["diagnostic"] = diagnostic
        snapshot = journal.append_terminal(_terminal_case_payload(working_case))
        _bind_journal_commitment(working_case, snapshot)
        case.clear()
        case.update(working_case)

    def _context(
        self,
        *,
        case: Mapping[str, Any],
        source: Path,
        input_path: Path,
        expected: Path,
        artifact: Path,
        binary: Path,
        run_directory: Path,
        case_directory: Path,
        run_id: str,
    ) -> tuple[dict[str, Path], dict[str, str]]:
        paths = {
            "source": source,
            "input": input_path,
            "expected": expected,
            "artifact": artifact,
            "binary": binary,
            "workspace": self.workspace_root,
            "suite_root": self.suite_root,
            "state_dir": self.state_root,
            "run_dir": run_directory,
            "case_dir": case_directory,
            **self.measurement_protocol_assets,
        }
        if self.options.pipeline_profile_path is not None:
            paths["profile"] = _workspace_bound_file(
                self.workspace_root,
                self.options.pipeline_profile_path,
                label="pipeline profile",
            )
        if self.options.metric_file is not None:
            relative_metric = validate_relative_path(self.options.metric_file, label="metric file")
            metric_file = case_directory.joinpath(*relative_metric.parts).resolve()
            try:
                metric_file.relative_to(case_directory.resolve())
            except ValueError as exc:
                raise ConfigurationError("metric file escapes the case directory") from exc
            paths["metric_file"] = metric_file
        if self.options.analysis_file is not None:
            relative_analysis = validate_relative_path(self.options.analysis_file, label="analysis file")
            analysis_file = case_directory.joinpath(*relative_analysis.parts).resolve()
            try:
                analysis_file.relative_to(case_directory.resolve())
            except ValueError as exc:
                raise ConfigurationError("analysis file escapes the case directory") from exc
            paths["analysis_file"] = analysis_file
        if self.options.result_file is not None:
            relative_result = validate_relative_path(self.options.result_file, label="result file")
            result_file = case_directory.joinpath(*relative_result.parts).resolve()
            try:
                result_file.relative_to(case_directory.resolve())
            except ValueError as exc:
                raise ConfigurationError("result file escapes the case directory") from exc
            paths["result_file"] = result_file
        if self.options.remarks_file is not None:
            relative_remarks = validate_relative_path(self.options.remarks_file, label="remarks file")
            remarks_file = case_directory.joinpath(*relative_remarks.parts).resolve()
            try:
                remarks_file.relative_to(case_directory.resolve())
            except ValueError as exc:
                raise ConfigurationError("remarks file escapes the case directory") from exc
            paths["remarks_file"] = remarks_file
        scalars = {
            "case_id": case["id"],
            "family": case["family"],
            "target": case["target"],
            "run_id": run_id,
        }
        return paths, scalars

    def _measurement(self, spec: MeasurementSpec, value: float, origin: str) -> dict[str, Any]:
        if not math.isfinite(value) or value < 0:
            raise ExecutionError(f"metric {spec.metric_id} produced a non-finite or negative value")
        if spec.metric_id == self.options.primary_metric_id and value <= 0:
            raise ExecutionError("primary metric must be greater than zero")
        return {
            "metric_id": spec.metric_id,
            "value": value,
            "unit": spec.unit,
            "origin": origin,
            "availability": "measured",
            "reason": None,
        }

    def _collect_case_measurements(
        self,
        *,
        sources: Mapping[str, Path],
        scalar_values: Mapping[str, float],
        cached_compile: bool,
    ) -> list[dict[str, Any]]:
        measurements: list[dict[str, Any]] = []
        for spec in self.metric_specs:
            if spec.source in scalar_values:
                origin = "derived" if spec.source in {"artifact_size", "binary_size"} else "observed"
                if cached_compile and spec.source == "compile_time":
                    origin = "cached"
                measurements.append(self._measurement(spec, float(scalar_values[spec.source]), origin))
            elif spec.source in sources:
                pattern = self.metric_patterns[spec.metric_id]
                value = extract_metric(
                    sources[spec.source],
                    pattern,
                    allow_zero=spec.metric_id != self.options.primary_metric_id,
                )
                origin = "cached" if cached_compile and spec.source.startswith("compile_") else "observed"
                measurements.append(self._measurement(spec, value, origin))
        return measurements

    def _collect_run_measurements(
        self,
        *,
        result: ProcessResult,
        stdout_path: Path,
        stderr_path: Path,
        metric_file_path: Path | None,
        include_patterns: bool,
    ) -> list[dict[str, Any]]:
        measurements: list[dict[str, Any]] = []
        for spec in self.metric_specs:
            if spec.source == "wall_time":
                measurements.append(self._measurement(spec, float(result.duration_ns), "observed"))
            elif include_patterns and spec.source in {"stdout", "stderr", "file"}:
                if spec.source == "stdout":
                    path = stdout_path
                elif spec.source == "stderr":
                    path = stderr_path
                else:
                    if metric_file_path is None:
                        raise ExecutionError("file metric configured without a metric file")
                    path = metric_file_path
                value = extract_metric(
                    path,
                    self.metric_patterns[spec.metric_id],
                    allow_zero=spec.metric_id != self.options.primary_metric_id,
                )
                measurements.append(self._measurement(spec, value, "observed"))
        return measurements

    def _execute_case(
        self,
        case: Mapping[str, Any],
        run_id: str,
        run_directory: Path,
        case_directory: Path,
        journal: AttemptJournal,
        attempt_index: int,
        attempt_started_at: str,
        attempt_configuration_sha256: str,
    ) -> dict[str, Any]:
        effective_timeout_seconds = self.case_timeouts[case["id"]]
        record = _new_case(
            case,
            case["id"] in self.consistency_selected,
            effective_timeout_seconds,
            self.case_timeout_derivations[case["id"]],
        )
        record["attempt_index"] = attempt_index
        record["attempt_started_at"] = attempt_started_at
        record["attempt_configuration_sha256"] = attempt_configuration_sha256

        def finish() -> dict[str, Any]:
            snapshot = journal.append_terminal(_terminal_case_payload(record))
            _bind_journal_commitment(record, snapshot)
            return record

        def commit_phase(stage: str, details: Mapping[str, Any]) -> JournalSnapshot:
            return journal.append_phase_result(
                stage,
                {**deepcopy(dict(details)), "case_prefix": _terminal_case_payload(record)},
            )

        journal.append_phase_started("compile")
        source = resolve_manifest_path(self.suite_root, case["source"]["path"])
        expected = resolve_manifest_path(self.suite_root, case["expected_output"]["path"])
        expected_bytes = expected.read_bytes()
        if self.options.output_contract == "raw_stdout":
            expected_program_stdout = expected_bytes
            expected_return_uint8: int | None = None
        else:
            expected_program_stdout, expected_return_uint8 = _split_return_trailer(
                expected_bytes,
                label="expected output",
            )
        if case["input"] is None:
            input_path = case_directory / "empty.in"
            if not input_path.exists():
                input_path.write_bytes(b"")
        else:
            input_path = resolve_manifest_path(self.suite_root, case["input"]["path"])
        provisional_artifact = case_directory / f"artifact{self.options.artifact_suffix}"
        provisional_binary = case_directory / f"binary{self.options.binary_suffix}"
        paths, scalars = self._context(
            case=case,
            source=source,
            input_path=input_path,
            expected=expected,
            artifact=provisional_artifact,
            binary=provisional_binary,
            run_directory=run_directory,
            case_directory=case_directory,
            run_id=run_id,
        )
        (
            artifact,
            compile_phase,
            compile_samples,
            compile_statistics,
            cache_hit,
            compile_stdout,
            compile_stderr,
        ) = self.compiler.compile(
            case=case,
            source=source,
            context_paths=paths,
            context_scalars=scalars,
        )
        record["compile"] = compile_phase
        record["compile_samples"] = list(compile_samples)
        record["compile_statistics"] = compile_statistics
        record["cache_hit"] = cache_hit
        if compile_phase["status"] != "ok":
            record["status"] = (
                "cancelled" if compile_phase["status"] == "cancelled" else "compile_error"
            )
            if record["status"] == "cancelled":
                record["cancellation_reason"] = "scheduler_cancelled"
            record["diagnostic"] = compile_phase["diagnostic"] or "compiler stage failed"
            commit_phase(
                "compile",
                {
                    "compile": record["compile"],
                    "compile_samples": record["compile_samples"],
                    "compile_statistics": record["compile_statistics"],
                    "cache_hit": record["cache_hit"],
                },
            )
            return finish()
        if compile_statistics is None:
            raise ExecutionError("successful compiler stage lacks cold-compile statistics")
        record["artifact_sha256"] = sha256_file(artifact)
        remarks_path = paths.get("remarks_file")
        if remarks_path is not None:
            if not remarks_path.is_file():
                raise ExecutionError("successful compiler stage lacks configured optimization remarks")
            if self.candidate_remark_contract is None:
                remark_events = load_and_validate_jsonl(remarks_path)
                remark_event_count = len(remark_events)
                normalized_candidate_summary = None
            else:
                remark_summary = validate_candidate_remark_jsonl(
                    remarks_path,
                    **self.candidate_remark_contract,
                )
                remark_event_count = remark_summary["event_count"]
                normalized_candidate_summary = _normalized_candidate_remark_summary(
                    remark_summary,
                    enabled_candidate_ids=self.candidate_remark_contract[
                        "enabled_candidate_ids"
                    ],
                )
            record["remarks_sha256"] = sha256_file(remarks_path)
            record["remarks_event_count"] = remark_event_count
            record["candidate_remark_summary"] = normalized_candidate_summary

        record["measurements"].extend(
            self._collect_case_measurements(
                sources={
                    "compile_stdout": compile_stdout,
                    "compile_stderr": compile_stderr,
                },
                scalar_values={
                    "compile_time": float(compile_statistics["median_duration_ns"]),
                    "artifact_size": float(artifact.stat().st_size),
                },
                cached_compile=cache_hit,
            )
        )
        commit_phase(
            "compile",
            {
                "compile": record["compile"],
                "compile_samples": record["compile_samples"],
                "compile_statistics": record["compile_statistics"],
                "cache_hit": record["cache_hit"],
                "artifact_sha256": record["artifact_sha256"],
                "remarks_sha256": record["remarks_sha256"],
                "remarks_event_count": record["remarks_event_count"],
                "candidate_remark_summary": record[
                    "candidate_remark_summary"
                ],
            },
        )
        binary = provisional_binary
        if self.options.linker is None:
            shutil.copy2(artifact, binary)
        else:
            journal.append_phase_started("link")
            paths["artifact"] = artifact
            paths["binary"] = binary
            command, environment = self.renderer.render(
                self.options.linker,
                paths=paths,
                scalars=scalars,
                cwd=self.workspace_root,
            )
            link_result = run_process(
                command,
                cwd=self.workspace_root,
                environment=environment,
                stdin_path=None,
                stdout_path=case_directory / "link.stdout",
                stderr_path=case_directory / "link.stderr",
                timeout_seconds=self.options.link_timeout_seconds,
                privacy_roots=self.privacy_roots,
                cancellation_event=self.cancellation_event,
            )
            record["link"] = link_result.as_phase_record()
            if link_result.status != "ok":
                record["status"] = (
                    "cancelled" if link_result.status == "cancelled" else "link_error"
                )
                if record["status"] == "cancelled":
                    record["cancellation_reason"] = "scheduler_cancelled"
                record["diagnostic"] = link_result.diagnostic or "linker stage failed"
                commit_phase("link", {"link": record["link"]})
                return finish()
            if not binary.is_file():
                record["link"]["status"] = "error"
                record["link"]["diagnostic"] = "linker exited successfully without creating {binary}"
                record["status"] = "link_error"
                record["diagnostic"] = record["link"]["diagnostic"]
                commit_phase("link", {"link": record["link"]})
                return finish()

            record["measurements"].extend(
                self._collect_case_measurements(
                    sources={
                        "link_stdout": case_directory / "link.stdout",
                        "link_stderr": case_directory / "link.stderr",
                    },
                    scalar_values={"link_time": float(link_result.duration_ns)},
                    cached_compile=False,
                )
            )

        record["binary_sha256"] = sha256_file(binary)

        record["measurements"].extend(
            self._collect_case_measurements(
                sources={},
                scalar_values={"binary_size": float(binary.stat().st_size)},
                cached_compile=False,
            )
        )
        if self.options.linker is not None:
            commit_phase("link", {"link": record["link"]})

        analyzer_specs = {
            spec.metric_id: spec for spec in self.metric_specs if spec.source == "analyzer"
        }
        if self.options.analyzer is not None:
            journal.append_phase_started("analyze")
            analysis_file = paths["analysis_file"]
            analysis_file.parent.mkdir(parents=True, exist_ok=True)
            analysis_file.unlink(missing_ok=True)
            command, environment = self.renderer.render(
                self.options.analyzer,
                paths=paths,
                scalars=scalars,
                cwd=self.workspace_root,
            )
            analyze_result = run_process(
                command,
                cwd=self.workspace_root,
                environment=environment,
                stdin_path=None,
                stdout_path=case_directory / "analyze.stdout",
                stderr_path=case_directory / "analyze.stderr",
                timeout_seconds=self.options.analyze_timeout_seconds,
                privacy_roots=self.privacy_roots,
                cancellation_event=self.cancellation_event,
            )
            record["analyze"] = analyze_result.as_phase_record()
            if analyze_result.status != "ok":
                record["status"] = (
                    "cancelled" if analyze_result.status == "cancelled" else "analyze_error"
                )
                if record["status"] == "cancelled":
                    record["cancellation_reason"] = "scheduler_cancelled"
                record["diagnostic"] = analyze_result.diagnostic or "post-link analyzer failed"
                commit_phase("analyze", {"analyze": record["analyze"]})
                return finish()
            if not analysis_file.is_file():
                record["analyze"]["status"] = "error"
                record["analyze"]["diagnostic"] = "analyzer exited successfully without creating {analysis_file}"
                record["status"] = "analyze_error"
                record["diagnostic"] = record["analyze"]["diagnostic"]
                commit_phase("analyze", {"analyze": record["analyze"]})
                return finish()
            analysis = load_and_validate(analysis_file)
            if analysis["schema_version"] != "binary-analysis.v1":
                raise ExecutionError("analyzer output must be binary-analysis.v1")
            record["analysis_sha256"] = sha256_file(analysis_file)
            observed = {item["metric_id"]: item for item in analysis["measurements"]}
            if observed.keys() != analyzer_specs.keys():
                missing = sorted(analyzer_specs.keys() - observed.keys())
                unexpected = sorted(observed.keys() - analyzer_specs.keys())
                raise ExecutionError(
                    "analyzer metric set differs from configuration"
                    + (f"; missing={','.join(missing)}" if missing else "")
                    + (f"; unexpected={','.join(unexpected)}" if unexpected else "")
                )
            for metric_id, spec in sorted(analyzer_specs.items()):
                item = observed[metric_id]
                if item["unit"] != spec.unit:
                    raise ExecutionError(f"analyzer unit mismatch for metric {metric_id}")
                record["measurements"].append(
                    {
                        "metric_id": metric_id,
                        "value": item["value"],
                        "unit": item["unit"],
                        "origin": "observed",
                        "availability": item["availability"],
                        "reason": item["reason"],
                    }
                )
            commit_phase(
                "analyze",
                {
                    "analyze": record["analyze"],
                    "analysis_sha256": record["analysis_sha256"],
                },
            )

        paths["artifact"] = artifact
        paths["binary"] = binary
        metric_file_path = paths.get("metric_file")
        result_file_path = paths.get("result_file")
        repetitions = self.options.repetitions
        if record["consistency_selected"]:
            repetitions = max(repetitions, 3)
        for index in range(repetitions):
            run_stage = f"run-{index:04d}"
            journal.append_phase_started(run_stage)
            stdout_path = case_directory / f"run-{index:04d}.stdout"
            stderr_path = case_directory / f"run-{index:04d}.stderr"
            if metric_file_path is not None:
                metric_file_path.parent.mkdir(parents=True, exist_ok=True)
                metric_file_path.unlink(missing_ok=True)
            if result_file_path is not None:
                result_file_path.parent.mkdir(parents=True, exist_ok=True)
                result_file_path.unlink(missing_ok=True)
            command, environment = self.renderer.render(
                self.options.runner,
                paths=paths,
                scalars={**scalars, "sample_index": str(index)},
                cwd=self.workspace_root,
            )
            result = run_process(
                command,
                cwd=self.workspace_root,
                environment=environment,
                stdin_path=input_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout_seconds=effective_timeout_seconds,
                privacy_roots=self.privacy_roots,
                allow_nonzero_exit=self.options.output_contract == "process_exit",
                cancellation_event=self.cancellation_event,
            )
            mismatch: int | None = None
            measurements: list[dict[str, Any]] = []
            censoring = "none"
            censor_bound: float | None = None
            censor_unit: str | None = None
            censor_metric_id: str | None = None
            diagnostic = result.diagnostic
            observed_return_uint8: int | None = None
            program_stdout_bytes = stdout_path.read_bytes()
            if result.status == "cancelled":
                sample_status = "cancelled"
            elif result.status == "timeout":
                sample_status = "timeout"
                censoring = "right"
                censor_bound = effective_timeout_seconds * 1_000_000_000
                censor_unit = "ns"
                censor_metric_id = "wall_time_ns"
                measurements = self._collect_run_measurements(
                    result=result,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    metric_file_path=metric_file_path,
                    include_patterns=False,
                )
            elif result.status != "ok":
                sample_status = "runtime_error"
            else:
                try:
                    measurements = self._collect_run_measurements(
                        result=result,
                        stdout_path=stdout_path,
                        stderr_path=stderr_path,
                        metric_file_path=metric_file_path,
                        include_patterns=True,
                    )
                except ExecutionError as exc:
                    sample_status = "runtime_error"
                    diagnostic = sanitize_text(str(exc), self.privacy_roots)
                else:
                    observed_bytes = stdout_path.read_bytes()
                    try:
                        if self.options.output_contract == "lf_return_trailer":
                            program_stdout_bytes, observed_return_uint8 = _split_return_trailer(
                                observed_bytes,
                                label="program output",
                            )
                        elif self.options.output_contract == "process_exit":
                            program_stdout_bytes = observed_bytes
                            if result.exit_code is None or not 0 <= result.exit_code <= 255:
                                raise ExecutionError("runner process exit is outside uint8 range")
                            observed_return_uint8 = result.exit_code
                        elif self.options.output_contract == "result_file":
                            program_stdout_bytes = observed_bytes
                            assert result_file_path is not None
                            observed_return_uint8 = _read_result_uint8(result_file_path)
                        else:
                            program_stdout_bytes = observed_bytes
                    except ExecutionError as exc:
                        sample_status = "wrong_output"
                        diagnostic = sanitize_text(str(exc), self.privacy_roots)
                    else:
                        mismatch = _first_mismatch_bytes(expected_program_stdout, program_stdout_bytes)
                        if mismatch is not None:
                            sample_status = "wrong_output"
                            diagnostic = f"program stdout differs from expected bytes at offset {mismatch}"
                        elif observed_return_uint8 != expected_return_uint8:
                            sample_status = "wrong_output"
                            diagnostic = (
                                f"main return differs: expected {expected_return_uint8}, "
                                f"observed {observed_return_uint8}"
                            )
                        else:
                            sample_status = "passed"
            sample = {
                "index": index,
                "status": sample_status,
                "duration_ns": result.duration_ns,
                "exit_code": result.exit_code,
                "measurements": measurements,
                "censoring": censoring,
                "censor_bound": censor_bound,
                "censor_unit": censor_unit,
                "censor_metric_id": censor_metric_id,
                "stdout": result.stdout.as_record(),
                "program_stdout": _stream_record(program_stdout_bytes),
                "stderr": result.stderr.as_record(),
                "expected_return_uint8": expected_return_uint8,
                "observed_return_uint8": observed_return_uint8,
                "first_mismatch_offset": mismatch,
                "diagnostic": diagnostic,
            }
            record["samples"].append(sample)
            if sample_status != "passed":
                record["status"] = sample_status
                if sample_status == "cancelled":
                    record["cancellation_reason"] = "scheduler_cancelled"
                record["diagnostic"] = diagnostic
            # The phase result is the durable normalized prefix.  Bind a real
            # runtime/correctness failure before publishing it so a crash after
            # journal commit cannot relabel that failure as infrastructure.
            commit_phase(run_stage, {"sample": sample})
            if sample_status != "passed":
                return finish()
        record["status"] = "passed"
        if record["consistency_selected"]:
            deterministic_ids = {
                spec.metric_id for spec in self.metric_specs if spec.source in {"stdout", "stderr", "file"}
            }
            mismatched: list[str] = []
            if deterministic_ids:
                for metric_id in sorted(deterministic_ids):
                    observations: list[float | None] = []
                    for sample in record["samples"]:
                        by_id = {
                            item["metric_id"]: item["value"] for item in sample["measurements"]
                        }
                        observations.append(by_id.get(metric_id))
                    if observations[0] is None or any(
                        value != observations[0] for value in observations[1:]
                    ):
                        mismatched.append(metric_id)
            else:
                hashes = [
                    (sample["program_stdout"]["sha256"], sample["observed_return_uint8"])
                    for sample in record["samples"]
                ]
                if any(value != hashes[0] for value in hashes[1:]):
                    mismatched.append("program_output")
            record["consistency_mismatched_metrics"] = mismatched
            if mismatched:
                record["status"] = "measurement_inconsistent"
                record["diagnostic"] = "deterministic measurements disagree: " + ", ".join(mismatched)
                record["consistency_passed"] = False
            else:
                record["consistency_passed"] = True
        return finish()

    def _execute_locked(
        self,
        record: dict[str, Any],
        run_directory: Path,
    ) -> dict[str, Any]:
        self._reconcile_run_terminal(record, run_directory)
        if record["state"] in {"completed", "failed"}:
            return record
        self._prepare_record_for_execution(record, run_directory)
        self._write_record(record)
        manifest_by_id = {case["id"]: case for case in self.manifest["cases"]}
        records_by_id = {case["case_id"]: case for case in record["cases"]}
        pending_ids = [
            case["case_id"] for case in record["cases"] if case["status"] == "pending"
        ]
        if not pending_ids:
            state = (
                "completed" if record["summary"]["failed_cases"] == 0 else "failed"
            )
            self._seal_run_terminal(record, run_directory, state=state)
            return record

        iterator = iter(pending_ids)
        active: dict[Future[dict[str, Any]], str] = {}
        stop_submitting = False
        internal_errors: list[BaseException] = []
        executor = ThreadPoolExecutor(max_workers=self.options.max_workers, thread_name_prefix="benchmark")

        def submit(case_id: str) -> None:
            case_record = records_by_id[case_id]
            attempt_directory = self._reserve_attempt(
                record,
                case_record,
                run_directory,
            )
            assert case_record["attempt_started_at"] is not None
            assert case_record["attempt_configuration_sha256"] is not None
            journal = self._attempt_journal(
                record,
                attempt_directory,
                case_id=case_id,
                attempt_index=case_record["attempt_index"],
                started_at=case_record["attempt_started_at"],
                configuration_sha256=case_record["attempt_configuration_sha256"],
            )
            future = executor.submit(
                self._execute_case,
                manifest_by_id[case_id],
                record["run_id"],
                run_directory,
                attempt_directory,
                journal,
                case_record["attempt_index"],
                case_record["attempt_started_at"],
                case_record["attempt_configuration_sha256"],
            )
            active[future] = case_id

        try:
            while len(active) < self.options.max_workers:
                try:
                    case_id = next(iterator)
                except StopIteration:
                    break
                submit(case_id)

            while active:
                completed, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in completed:
                    case_id = active.pop(future)
                    try:
                        result = future.result()
                    except CancelledError:
                        self._commit_scheduler_terminal(
                            record,
                            records_by_id[case_id],
                            run_directory,
                            reason="scheduler_cancelled",
                            diagnostic="cancelled after an earlier failure",
                        )
                    except BaseException as exc:  # preserve the original infrastructure failure
                        original_diagnostic = sanitize_text(
                            f"{type(exc).__name__}: {exc}", self.privacy_roots
                        )
                        try:
                            self._commit_scheduler_terminal(
                                record,
                                records_by_id[case_id],
                                run_directory,
                                reason="infrastructure_failure",
                                diagnostic=original_diagnostic,
                            )
                        except BaseException as sealing_error:
                            internal_errors.extend((exc, sealing_error))
                        else:
                            internal_errors.append(exc)
                        stop_submitting = True
                        self.cancellation_event.set()
                    else:
                        result["attempts"] = records_by_id[case_id]["attempts"]
                        records_by_id[case_id].clear()
                        records_by_id[case_id].update(result)
                        if result["status"] != "passed" and not self.options.keep_going:
                            stop_submitting = True
                            self.cancellation_event.set()
                    self._write_record(record)

                if stop_submitting:
                    for future in active:
                        future.cancel()
                else:
                    while len(active) < self.options.max_workers:
                        try:
                            case_id = next(iterator)
                        except StopIteration:
                            break
                        submit(case_id)

            if stop_submitting:
                for case_id in iterator:
                    records_by_id[case_id]["status"] = "cancelled"
                    records_by_id[case_id]["cancellation_reason"] = "scheduler_cancelled"
                    records_by_id[case_id]["diagnostic"] = "cancelled after an earlier failure"
        except KeyboardInterrupt:
            self.cancellation_event.set()
            for future in active:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
            for case_id in sorted(set(active.values())):
                case = records_by_id[case_id]
                if case["attempt_started_at"] is None:
                    continue
                directory = self._attempt_directory(
                    run_directory,
                    case_id=case_id,
                    attempt_index=case["attempt_index"],
                )
                self._recover_current_attempt(record, case, directory)
            self._seal_run_terminal(record, run_directory, state="interrupted")
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        if internal_errors:
            self._seal_run_terminal(record, run_directory, state="interrupted")
            rendered = "; ".join(
                sanitize_text(
                    f"{type(error).__name__}: {error}", self.privacy_roots
                )
                for error in internal_errors
            )
            cause: BaseException = (
                internal_errors[0]
                if len(internal_errors) == 1
                else BaseExceptionGroup(
                    "benchmark worker and evidence-sealing failures",
                    internal_errors,
                )
            )
            raise ExecutionError(
                f"benchmark infrastructure failed: {sanitize_text(rendered, self.privacy_roots)}"
            ) from cause
        state = (
            "completed"
            if all(case["status"] == "passed" for case in record["cases"])
            else "failed"
        )
        self._seal_run_terminal(record, run_directory, state=state)
        return record

    def execute(self) -> dict[str, Any]:
        lease_metadata = {
            "run_id": self.options.run_id,
            "manifest_sha256": self.manifest_sha256,
            "configuration_sha256": self.configuration_sha256,
            "output_path_sha256": path_identity(self.output_path),
            "state_root_sha256": path_identity(self.state_root),
        }
        with ExclusiveFileLease(
            output_lease_path(self.output_path),
            "output target",
            lease_metadata,
        ) as output_lease:
            record = self._load_record()
            run_directory = self._run_directory(record)
            run_directory.mkdir(parents=True, exist_ok=True)
            bound_metadata = {
                **lease_metadata,
                "run_id": record["run_id"],
                "configuration_sha256": record["configuration_sha256"],
            }
            output_lease.bind(bound_metadata)
            with ExclusiveFileLease(
                run_directory / ".run.lock",
                "execution state",
                bound_metadata,
            ):
                self._bind_state_identity(record, run_directory)
                return self._execute_locked(record, run_directory)


def run_benchmark(options: RunOptions) -> dict[str, Any]:
    return BenchmarkRun(options).execute()
