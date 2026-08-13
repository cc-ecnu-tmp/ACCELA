from __future__ import annotations

import json
import math
import subprocess
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from .ablation import (
    _require_eligible_attempt_history,
    _require_formal_measurement,
    require_formal_measurement_configuration,
)
from .analyzer_contract import candidate_analyzer_stage
from .campaign import (
    build_campaign_environment_contract,
    candidate_execution_environment_sha256,
    campaign_run_status,
    campaign_status_chain,
    enforce_terminal_task_immutability,
    ready_campaign_task_ids,
    require_formal_suite_contract,
)
from .errors import ConfigurationError, ValidationError
from .journal import durable_create_json
from .metrics import cache_hotblock_metrics_v1, rv64gc_qemu_v1
from .schema import (
    load_and_validate,
    load_pipeline_profile_v2,
    schema_sha256,
    validate_candidate_remark_jsonl,
    validate_document,
    validate_pipeline_profile_v2,
)

if TYPE_CHECKING:
    from .execution import ReadOnlyRunRawEvidenceSnapshot, VerifiedRunRawEvidence
from .stats import (
    bootstrap_geometric_mean_ci,
    case_metric,
    compare_runs,
    family_geometric_means,
    metric_spec,
    weighted_geometric_mean,
)
from .util import (
    canonical_json_bytes,
    read_json,
    resolve_without_symlinks,
    safe_slug,
    sha256_artifact,
    sha256_bytes,
    sha256_file,
    sha256_json,
    validate_relative_path,
)


_CANDIDATE_SUITE_CASE_COUNTS = {
    "B1": 140,
    "B2": 20,
    "B3": 60,
    "B4": 59,
    "B5": 60,
    "B6": 88,
}

_FORMAL_CANDIDATE_COMPILER_COMMAND = (
    "sh",
    "scripts/benchmark-compile.sh",
    "{profile}",
    "{source}",
    "{artifact}",
    "{remarks_file}",
)
_FORMAL_CANDIDATE_LINKER_COMMAND = (
    "sh",
    "scripts/benchmark-link.sh",
    "{artifact}",
    "{binary}",
)
_FORMAL_CANDIDATE_CORRECTNESS_RUNNER_COMMAND = (
    "sh",
    "scripts/benchmark-qemu-correctness.sh",
    "{binary}",
    "{input}",
)


def verify_run_raw_evidence(*args: Any, **kwargs: Any) -> VerifiedRunRawEvidence:
    """Lazy bridge keeps candidates importable by the execution preflight."""

    from .execution import verify_run_raw_evidence as verify

    return verify(*args, **kwargs)


def verify_run_raw_evidence_read_only_snapshot(
    *args: Any, **kwargs: Any
) -> ReadOnlyRunRawEvidenceSnapshot:
    from .execution import verify_run_raw_evidence_read_only_snapshot as verify

    return verify(*args, **kwargs)


class _ReadOnlyRawEvidenceCache:
    """Per-authorization read-only raw snapshots with a final drift barrier."""

    def __init__(self) -> None:
        self._snapshots: dict[
            tuple[Path, Path], ReadOnlyRunRawEvidenceSnapshot
        ] = {}
        self._files: dict[Path, str] = {}

    def track_file(
        self,
        path: Path,
        *,
        label: str,
        expected_sha256: str | None = None,
    ) -> Path:
        physical = resolve_without_symlinks(path, label=label)
        if not physical.is_file():
            raise ValidationError(f"{label} must be a regular file")
        digest = sha256_file(physical)
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValidationError(f"{label} physical SHA-256 differs")
        previous = self._files.setdefault(physical, digest)
        if previous != digest:
            raise ValidationError(f"{label} changed during authorization")
        return physical

    def verify(
        self,
        run_record_path: Path,
        state_root: Path,
        *,
        remark_validator: Any = None,
    ) -> VerifiedRunRawEvidence:
        resolved_record = resolve_without_symlinks(
            run_record_path, label="candidate read-only run record"
        )
        resolved_state = resolve_without_symlinks(
            state_root, label="candidate read-only state root"
        )
        key = (resolved_record, resolved_state)
        snapshot = self._snapshots.get(key)
        if snapshot is None:
            snapshot = verify_run_raw_evidence_read_only_snapshot(
                resolved_record,
                resolved_state,
            )
            self._snapshots[key] = snapshot
        if remark_validator is not None:
            record = _load_version(
                resolved_record,
                "run-record.v1",
                label="candidate read-only raw remark run",
            )
            for case in record["cases"]:
                remark_path = snapshot.verified.current_remark_paths[
                    case["case_id"]
                ]
                if remark_path is not None:
                    remark_validator(remark_path, deepcopy(case))
        return snapshot.verified

    def assert_unchanged(self) -> None:
        for path, digest in self._files.items():
            if (
                resolve_without_symlinks(
                    path, label="candidate authorization immutable input"
                )
                != path
                or not path.is_file()
                or sha256_file(path) != digest
            ):
                raise ValidationError(
                    "candidate authorization input changed during verification"
                )
        for snapshot in self._snapshots.values():
            snapshot.assert_unchanged()


def _candidate_read_only_raw_verifier(
    *,
    raw_snapshot_cache: _ReadOnlyRawEvidenceCache | None,
    raw_evidence_verifier: Any | None,
) -> tuple[_ReadOnlyRawEvidenceCache | None, Any]:
    """Select one read-only cache or an explicitly injected verifier."""

    if raw_snapshot_cache is not None and raw_evidence_verifier is not None:
        raise ConfigurationError(
            "candidate raw replay accepts either a snapshot cache or a verifier"
        )
    if raw_snapshot_cache is not None:
        return raw_snapshot_cache, raw_snapshot_cache.verify
    if raw_evidence_verifier is not None:
        return None, raw_evidence_verifier
    cache = _ReadOnlyRawEvidenceCache()
    return cache, cache.verify


@dataclass(frozen=True)
class CandidateRunAuthorizationIntent:
    plan_path: Path
    status_path: Path
    status_ledger_paths: tuple[Path, ...]
    task_id: str
    workspace_root: Path
    manifest_path: Path
    suite_root: Path
    output_path: Path
    state_root: Path
    compiler_artifact_path: Path
    pipeline_profile_path: Path | None
    candidate_registry_path: Path | None
    candidate_pass_registry_path: Path | None
    measurement_protocol_path: Path | None
    baseline_timeout_path: Path | None
    run_id: str | None
    configuration: Mapping[str, Any]
    provenance: Mapping[str, Any]


def _latest_evidence_timestamp(values: Sequence[str]) -> str:
    if not values:
        raise ValidationError("derived candidate artifact lacks terminal evidence time")
    return max(
        values,
        key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")),
    )


def _require_candidate_run_protocol_configuration(
    run: Mapping[str, Any], *, data_role: str
) -> None:
    configuration = run["configuration"]
    if (
        configuration["compile_repetitions"] != 5
        or configuration["reuse_compile_cache"]
        or configuration["compile_storage_contract"] != "attempt_local_v1"
        or configuration["max_workers"] != 4
        or configuration["seed"] != 20260809
        or configuration["retry_failures"]
        or configuration["repetitions"] != 1
        or not math.isclose(
            configuration["consistency_fraction"], 0.1, rel_tol=0, abs_tol=1e-12
        )
        or configuration["consistency_repetitions"] != 3
    ):
        raise ValidationError(
            "candidate formal runs require jobs=4, five cold compiles, seed 20260809, "
            "10 percent x3 consistency, no cache, and no retry"
        )


def _require_candidate_run_protocol(
    run: Mapping[str, Any],
    *,
    data_role: str,
) -> None:
    _require_candidate_run_protocol_configuration(run, data_role=data_role)
    expected_count = _CANDIDATE_SUITE_CASE_COUNTS[data_role]
    if (
        run["manifest_case_count"] != expected_count
        or len(run["cases"]) != expected_count
        or {case["data_role"] for case in run["cases"]} != {data_role}
    ):
        raise ValidationError(
            f"candidate {data_role} run must bind the exact {expected_count}-case manifest"
        )


def _require_candidate_correctness_configuration(
    run: Mapping[str, Any], *, label: str
) -> None:
    configuration = run["configuration"]
    tool_ids = {item["tool"] for item in configuration["tool_versions"]}
    if (
        configuration["evidence_level"] != "qemu_correctness"
        or configuration["compiler"]["kind"] != "benchmark-compiler"
        or configuration["runner"]["kind"] != "qemu"
        or configuration["output_contract"] != "lf_return_trailer"
        or configuration["pipeline_profile_file_sha256"] is None
        or "qemu-system-riscv64" not in tool_ids
    ):
        raise ValidationError(
            f"{label} must use BenchmarkCompiler, the frozen pipeline, QEMU, "
            "and exact stdout/main-return correctness"
        )


def _require_candidate_correctness_run(
    run: Mapping[str, Any], *, label: str
) -> None:
    """Require the ACCELA BenchmarkCompiler-to-QEMU correctness path for B1."""

    _require_candidate_correctness_configuration(run, label=label)
    configuration = run["configuration"]
    enabled_candidate_ids = configuration.get("enabled_candidate_ids", [])
    if enabled_candidate_ids and (
        configuration["remarks_file_sha256"] is None
        or any(
            case["artifact_sha256"] is not None
            and (
                case["remarks_sha256"] is None
                or case["remarks_event_count"] is None
                or case["remarks_event_count"] <= 0
                or case.get("candidate_remark_summary") is None
            )
            for case in run["cases"]
        )
    ):
        raise ValidationError(
            f"{label} candidate-enabled correctness requires decision-observable remarks"
        )
    _require_eligible_attempt_history(run, context=f"{label} correctness evidence")


def _load_version(path: Path, version: str, *, label: str) -> dict[str, Any]:
    document = load_and_validate(path)
    if document["schema_version"] != version:
        raise ValidationError(f"{label} must be {version}")
    return document


def _candidate_workspace_root(workspace_root: Path) -> Path:
    root = resolve_without_symlinks(
        workspace_root,
        label="candidate workspace root",
    )
    if not root.is_dir():
        raise ValidationError("candidate workspace root must be a directory")
    return root


def _frozen_artifact_digest(
    workspace_root: Path,
    path: Path,
    document: Mapping[str, Any],
    *,
    label: str,
) -> dict[str, str]:
    root, physical, relative = _workspace_regular_path(
        workspace_root, path, label=label
    )
    return {
        "path": relative.as_posix(),
        "canonical_sha256": sha256_json(document),
        "physical_sha256": sha256_file(physical),
    }


def _workspace_regular_path(
    workspace_root: Path,
    path: Path,
    *,
    label: str,
) -> tuple[Path, Path, Path]:
    """Resolve a workspace file only after rejecting lexical symlink components."""

    root = _candidate_workspace_root(workspace_root)
    lexical = path if path.is_absolute() else root / path
    lexical = lexical.absolute()
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"{label} must stay within the campaign workspace") from exc
    cursor = root
    for component in relative.parts:
        cursor = cursor / component
        if cursor.is_symlink():
            raise ValidationError(f"{label} cannot traverse a symbolic link")
    try:
        physical = lexical.resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"{label} must be a physical regular file") from exc
    try:
        physical.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"{label} resolves outside the campaign workspace") from exc
    if not physical.is_file():
        raise ValidationError(f"{label} must be a physical regular file")
    return root, physical, relative


def _workspace_directory_path(
    workspace_root: Path,
    path: Path,
    *,
    label: str,
) -> tuple[Path, Path, Path]:
    root = _candidate_workspace_root(workspace_root)
    lexical = (path if path.is_absolute() else root / path).absolute()
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"{label} must stay within the campaign workspace") from exc
    physical = resolve_without_symlinks(lexical, label=label)
    try:
        physical.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"{label} resolves outside the campaign workspace") from exc
    if not physical.is_dir():
        raise ValidationError(f"{label} must be a physical directory")
    return root, physical, relative


def _frozen_compiler_artifact(
    workspace_root: Path,
    path: Path,
) -> dict[str, str]:
    root = _candidate_workspace_root(workspace_root)
    lexical = (path if path.is_absolute() else root / path).absolute()
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise ValidationError(
            "compiler artifact must stay within the campaign workspace"
        ) from exc
    resolved = resolve_without_symlinks(lexical, label="compiler artifact")
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValidationError("compiler artifact resolves outside the workspace") from exc
    return {
        "path": relative.as_posix(),
        "physical_sha256": sha256_artifact(lexical),
    }


def _load_frozen_artifact(
    workspace_root: Path,
    artifact: Mapping[str, str],
    *,
    label: str,
    version: str | None = None,
) -> dict[str, Any]:
    root, physical, _ = _workspace_regular_path(
        workspace_root, Path(artifact["path"]), label=label
    )
    if sha256_file(physical) != artifact["physical_sha256"]:
        raise ValidationError(f"{label} physical SHA-256 differs from freeze")
    raw = read_json(physical)
    if not isinstance(raw, dict):
        raise ValidationError(f"{label} must contain a JSON object")
    document = validate_document(raw) if version is not None else raw
    if version is not None and document["schema_version"] != version:
        raise ValidationError(f"{label} schema version differs from freeze")
    if sha256_json(document) != artifact["canonical_sha256"]:
        raise ValidationError(f"{label} canonical SHA-256 differs from freeze")
    return document


def _normalized_remark_summary(
    summary: Mapping[str, Any], *, enabled_candidate_ids: list[str]
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


def _candidate_remark_validator(
    *,
    run: Mapping[str, Any],
    catalog: Mapping[str, Any],
    pass_registry: Mapping[str, Any],
):
    enabled_candidate_ids = list(
        run["configuration"].get("enabled_candidate_ids", [])
    )
    catalog_ids = [item["candidate_id"] for item in catalog["candidates"]]
    if any(candidate_id not in catalog_ids for candidate_id in enabled_candidate_ids):
        raise ValidationError("candidate raw run enables an unknown candidate")

    def validate(path: Path, case: Mapping[str, Any]) -> None:
        summary = validate_candidate_remark_jsonl(
            path,
            catalog=catalog,
            pass_registry=pass_registry,
            enabled_candidate_ids=enabled_candidate_ids,
            candidate_registry_sha256=sha256_json(catalog),
            pipeline_profile_id=run["provenance"]["pipeline_profile_id"],
            pipeline_profile_sha256=run["provenance"][
                "pipeline_profile_sha256"
            ],
            require_candidate_observation=False,
        )
        expected = _normalized_remark_summary(
            summary, enabled_candidate_ids=enabled_candidate_ids
        )
        if (
            case["remarks_event_count"] != summary["event_count"]
            or case.get("candidate_remark_summary") != expected
        ):
            raise ValidationError(
                f"candidate raw remark summary differs from run case: {case['case_id']}"
            )

    return validate


def _verify_candidate_run_raw_evidence(
    *,
    run_path: Path,
    state_root: Path,
    catalog: Mapping[str, Any],
    pass_registry: Mapping[str, Any],
    raw_evidence_verifier: Any | None = None,
) -> tuple[dict[str, Any], VerifiedRunRawEvidence]:
    run = _load_version(run_path, "run-record.v1", label="candidate raw run")
    verifier = raw_evidence_verifier or verify_run_raw_evidence
    verified = verifier(
        run_path,
        state_root,
        remark_validator=_candidate_remark_validator(
            run=run, catalog=catalog, pass_registry=pass_registry
        ),
    )
    if verified.document["run_canonical_sha256"] != sha256_json(run):
        raise ValidationError("candidate raw verifier returned another run identity")
    return run, verified


def build_candidate_raw_evidence_registry(
    *,
    plan_path: Path,
    run_paths: Mapping[str, Path],
    workspace_root: Path,
    _raw_snapshot_cache: _ReadOnlyRawEvidenceCache | None = None,
    _raw_evidence_verifier: Any | None = None,
) -> dict[str, Any]:
    """Replay journals/raw files and bind every supplied campaign run immutably."""

    raw_snapshot_cache, raw_evidence_verifier = (
        _candidate_read_only_raw_verifier(
            raw_snapshot_cache=_raw_snapshot_cache,
            raw_evidence_verifier=_raw_evidence_verifier,
        )
    )
    plan = _load_version(
        plan_path, "candidate-campaign-plan.v1", label="candidate campaign plan"
    )
    registry = _build_candidate_raw_evidence_registry_from_plan(
        plan=plan,
        run_paths=run_paths,
        workspace_root=workspace_root,
        raw_evidence_verifier=raw_evidence_verifier,
    )
    if raw_snapshot_cache is not None:
        raw_snapshot_cache.assert_unchanged()
    return registry


def _build_candidate_raw_evidence_registry_from_plan(
    *,
    plan: Mapping[str, Any],
    run_paths: Mapping[str, Path],
    workspace_root: Path,
    raw_evidence_verifier: Any | None = None,
) -> dict[str, Any]:
    if plan["candidate_raw_evidence_schema_sha256"] != schema_sha256(
        "candidate-raw-evidence.v1"
    ):
        raise ValidationError("candidate campaign plan binds a stale raw-evidence schema")
    root = _candidate_workspace_root(workspace_root)
    _, state_root, _ = _workspace_directory_path(
        root,
        root / plan["raw_state_root"],
        label="candidate raw evidence state root",
    )
    catalog = _load_frozen_artifact(
        root,
        plan["artifacts"]["candidate_registry"],
        label="candidate raw evidence registry",
        version="candidate-catalog.v1",
    )
    pass_registry = _load_frozen_artifact(
        root,
        plan["artifacts"]["executable_pass_registry"],
        label="candidate raw evidence PassRegistry",
        version="pass-registry.v2",
    )
    screening = _load_frozen_artifact(
        root,
        plan["artifacts"]["screening"],
        label="candidate raw evidence screening",
        version="candidate-screening.v1",
    )
    base_pass_registry = _load_frozen_artifact(
        root,
        plan["artifacts"]["screening_base_pass_registry"],
        label="candidate raw evidence base PassRegistry",
        version="pass-registry.v2",
    )
    if (
        _load_and_reverify_candidate_screening(
            screening_path=root / plan["artifacts"]["screening"]["path"],
            workspace_root=root,
            raw_evidence_verifier=raw_evidence_verifier,
        )
        != screening
        or
        screening["base_pass_registry"]
        != plan["artifacts"]["screening_base_pass_registry"]
        or _require_executable_registry_bridge(
            screening=screening,
            catalog=catalog,
            executable_registry=pass_registry,
            workspace_root=root,
        )
        != base_pass_registry
    ):
        raise ValidationError("candidate raw evidence PassRegistry bridge differs")
    allowed_main = {
        task["task_id"]
        for task in plan["tasks"]
        if task["task_type"] == "run"
    }
    unknown = sorted(
        task_id
        for task_id in run_paths
        if task_id not in allowed_main and not task_id.startswith("diagnostic.")
    )
    if unknown:
        raise ConfigurationError(
            "unknown candidate raw run task: " + ", ".join(unknown)
        )
    rows: list[dict[str, Any]] = []
    observed_run_ids: set[str] = set()
    for task_id in sorted(run_paths):
        run_path = run_paths[task_id]
        _, physical, relative = _workspace_regular_path(
            root, run_path, label=f"candidate raw run {task_id}"
        )
        run, verified = _verify_candidate_run_raw_evidence(
            run_path=physical,
            state_root=state_root,
            catalog=catalog,
            pass_registry=pass_registry,
            raw_evidence_verifier=raw_evidence_verifier,
        )
        if run["run_id"] in observed_run_ids:
            raise ValidationError("candidate raw registry contains a repeated run id")
        observed_run_ids.add(run["run_id"])
        rows.append(
            {
                "task_id": task_id,
                "run_record": {
                    "path": relative.as_posix(),
                    "canonical_sha256": sha256_json(run),
                    "physical_sha256": sha256_file(physical),
                },
                "verification": verified.document,
            }
        )
    return validate_document(
        {
            "schema_version": "candidate-raw-evidence.v1",
            "registry_id": (
                f"{plan['campaign_id']}:raw:{sha256_json(rows)[:32]}"
            ),
            "campaign_id": plan["campaign_id"],
            "plan_sha256": sha256_json(plan),
            "raw_state_root": plan["raw_state_root"],
            "runs": rows,
        }
    )


def _load_and_reverify_candidate_raw_evidence_registry(
    *,
    plan: Mapping[str, Any],
    registry_path: Path,
    workspace_root: Path,
    expected_run_paths: Mapping[str, Path] | None = None,
    raw_evidence_verifier: Any | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    root = _candidate_workspace_root(workspace_root)
    registry = _load_version(
        registry_path,
        "candidate-raw-evidence.v1",
        label="candidate raw evidence registry",
    )
    registry = _reverify_candidate_raw_evidence_registry_document(
        plan=plan,
        registry=registry,
        workspace_root=root,
        expected_run_paths=expected_run_paths,
        raw_evidence_verifier=raw_evidence_verifier,
    )
    _, physical_registry, relative_registry = _workspace_regular_path(
        root, registry_path, label="candidate raw evidence registry"
    )
    return registry, {
        "path": relative_registry.as_posix(),
        "canonical_sha256": sha256_json(registry),
        "physical_sha256": sha256_file(physical_registry),
    }


def _reverify_candidate_raw_evidence_registry_document(
    *,
    plan: Mapping[str, Any],
    registry: Mapping[str, Any],
    workspace_root: Path,
    expected_run_paths: Mapping[str, Path] | None = None,
    raw_evidence_verifier: Any | None = None,
) -> dict[str, Any]:
    root = _candidate_workspace_root(workspace_root)
    document = validate_document(dict(registry))
    if document["schema_version"] != "candidate-raw-evidence.v1":
        raise ValidationError("candidate raw evidence registry version differs")
    if (
        document["campaign_id"] != plan["campaign_id"]
        or document["plan_sha256"] != sha256_json(plan)
        or document["raw_state_root"] != plan["raw_state_root"]
    ):
        raise ValidationError("candidate raw evidence registry binds another campaign")
    recorded_paths = {
        item["task_id"]: root / item["run_record"]["path"]
        for item in document["runs"]
    }
    if expected_run_paths is not None:
        normalized_expected = {
            task_id: _workspace_regular_path(
                root, path, label=f"candidate raw run {task_id}"
            )[2].as_posix()
            for task_id, path in expected_run_paths.items()
        }
        normalized_recorded = {
            item["task_id"]: item["run_record"]["path"]
            for item in document["runs"]
        }
        if normalized_recorded != normalized_expected:
            raise ValidationError(
                "candidate raw evidence registry run-path set differs from status inputs"
            )
    expected = _build_candidate_raw_evidence_registry_from_plan(
        plan=plan,
        run_paths=recorded_paths,
        workspace_root=root,
        raw_evidence_verifier=raw_evidence_verifier,
    )
    if expected != document:
        raise ValidationError(
            "candidate raw evidence registry differs from replayed journals/raw files"
        )
    return document


def candidate_raw_evidence_registry_artifact(
    *,
    registry: Mapping[str, Any],
    output_path: Path,
    workspace_root: Path,
) -> dict[str, str]:
    root = _candidate_workspace_root(workspace_root)
    document = validate_document(dict(registry))
    if document["schema_version"] != "candidate-raw-evidence.v1":
        raise ValidationError("candidate raw evidence registry version differs")
    lexical = (output_path if output_path.is_absolute() else root / output_path).absolute()
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise ValidationError(
            "candidate raw evidence registry output escapes the workspace"
        ) from exc
    cursor = root
    for component in relative.parent.parts:
        cursor = cursor / component
        if cursor.is_symlink():
            raise ValidationError(
                "candidate raw evidence registry output traverses a symbolic link"
            )
    if lexical.parent.resolve(strict=True) != lexical.parent or lexical.is_symlink():
        raise ValidationError(
            "candidate raw evidence registry output path identity differs"
        )
    return {
        "path": relative.as_posix(),
        "canonical_sha256": sha256_json(document),
        "physical_sha256": sha256_bytes(canonical_json_bytes(document) + b"\n"),
    }


def _verify_candidate_freeze_inputs(
    freeze: Mapping[str, Any],
    *,
    workspace_root: Path,
    candidate_order: list[str],
    raw_evidence_verifier: Any | None = None,
) -> dict[str, dict[str, Any]]:
    _, observed_tree = _clean_repository_identity(
        workspace_root, freeze["repository"]["repo_commit"]
    )
    if observed_tree != freeze["repository"]["repo_tree"]:
        raise ValidationError("frozen repository tree differs from the current clean HEAD")
    if _frozen_compiler_artifact(
        workspace_root,
        workspace_root / freeze["repository"]["compiler_artifact"]["path"],
    ) != freeze["repository"]["compiler_artifact"]:
        raise ValidationError("frozen compiler artifact physical identity has drifted")
    snapshots = freeze["snapshots"]
    versions = {
        "candidate_registry": "candidate-catalog.v1",
        "executable_pass_registry": "pass-registry.v2",
        "screening_base_pass_registry": "pass-registry.v2",
        "matrix": "candidate-profile-matrix.v1",
        "screening": "candidate-screening.v1",
        "oracle_capture": "candidate-oracle-capture.v1",
    }
    documents = {
        key: _load_frozen_artifact(
            workspace_root,
            snapshots[key],
            label=f"frozen {key}",
            version=version,
        )
        for key, version in versions.items()
    }
    if (
        _load_and_reverify_candidate_screening(
            screening_path=workspace_root / snapshots["screening"]["path"],
            workspace_root=workspace_root,
            raw_evidence_verifier=raw_evidence_verifier,
        )
        != documents["screening"]
        or _require_executable_registry_bridge(
            screening=documents["screening"],
            catalog=documents["candidate_registry"],
            executable_registry=documents["executable_pass_registry"],
            workspace_root=workspace_root,
        )
        != documents["screening_base_pass_registry"]
    ):
        raise ValidationError("frozen PassRegistry bridge differs")
    for key in ("run_record_schema", "candidate_study_schema"):
        _load_frozen_artifact(
            workspace_root,
            snapshots[key],
            label=f"frozen {key}",
        )
    for suite in freeze["suites"]:
        manifest = _load_frozen_artifact(
            workspace_root,
            suite["manifest"],
            label=f"frozen {suite['data_role']} manifest",
            version="benchmark-manifest.v1",
        )
        require_formal_suite_contract(
            role=suite["data_role"],
            manifest=manifest,
            manifest_path=workspace_root / suite["manifest"]["path"],
        )
        if (
            manifest["suite_id"] != suite["suite_id"]
            or len(manifest["cases"]) != suite["case_count"]
        ):
            raise ValidationError(
                f"frozen {suite['data_role']} suite metadata differs from its manifest"
            )
    profile = _load_frozen_artifact(
        workspace_root,
        freeze["base_pipeline_profile"]["artifact"],
        label="frozen candidate-empty profile",
    )
    validate_pipeline_profile_v2(profile, candidate_order=candidate_order)
    protocol_paths = {
        mode: workspace_root / freeze["measurement_protocols"][mode]["path"]
        for mode in ("standard_proxy", "cache_hotblock")
    }
    for mode, path in protocol_paths.items():
        protocol = freeze["measurement_protocols"][mode]
        _load_frozen_artifact(
            workspace_root,
            {
                "path": protocol["path"],
                "canonical_sha256": protocol["protocol_sha256"],
                "physical_sha256": protocol["physical_sha256"],
            },
            label=f"frozen {mode} protocol",
            version="measurement-protocol.v1",
        )
    toolchain_snapshot = freeze["reference_toolchain"]["snapshot"]
    _load_frozen_artifact(
        workspace_root,
        toolchain_snapshot,
        label="frozen reference toolchain",
    )
    environment = build_campaign_environment_contract(
        measurement_protocol_path=protocol_paths["standard_proxy"],
        hotblock_measurement_protocol_path=protocol_paths["cache_hotblock"],
        reference_toolchain_path=workspace_root / toolchain_snapshot["path"],
        workspace_root=workspace_root,
        include_candidate_workspace_bootstrap=True,
    )
    for mode in ("standard_proxy", "cache_hotblock"):
        frozen_contract = dict(freeze["measurement_protocols"][mode])
        frozen_contract.pop("path")
        frozen_contract.pop("physical_sha256")
        if frozen_contract != environment["measurement_protocols"][mode]:
            raise ValidationError(
                f"frozen {mode} normalized protocol contract has drifted"
            )
    frozen_toolchain = dict(freeze["reference_toolchain"])
    frozen_toolchain.pop("snapshot")
    environment_toolchain = dict(environment["reference_toolchain"])
    environment_toolchain.pop("snapshot_sha256")
    if (
        frozen_toolchain != environment_toolchain
        or freeze["analyzer"] != environment["analyzer"]
    ):
        raise ValidationError("frozen reference toolchain contract has drifted")
    if freeze["execution_environment_sha256"] != (
        candidate_execution_environment_sha256(
            environment={
                **environment,
                "measurement_protocols": freeze["measurement_protocols"],
                "reference_toolchain": freeze["reference_toolchain"],
            },
            compiler_artifact_sha256=freeze["repository"]["compiler_artifact"][
                "physical_sha256"
            ],
        )
    ):
        raise ValidationError("frozen candidate execution environment has drifted")
    screening = documents["screening"]
    capture = documents["oracle_capture"]
    replayed_capture = _load_and_reverify_candidate_oracle_capture(
        capture_path=workspace_root / snapshots["oracle_capture"]["path"],
        workspace_root=workspace_root,
        raw_evidence_verifier=raw_evidence_verifier,
    )
    if (
        replayed_capture != capture
        or
        screening["oracle_capture_sha256"] != sha256_json(capture)
        or screening["candidate_evidence_sha256"]
        != capture["candidate_evidence_sha256"]
        or snapshots["candidate_evidence_sha256"]
        != capture["candidate_evidence_sha256"]
        or snapshots["screening_spec_sha256"]
        != screening["screening_spec_sha256"]
        or snapshots["oracle_plan_sha256"] != capture["oracle_plan_sha256"]
        or screening["pass_registry_sha256"]
        != snapshots["screening_base_pass_registry"]["canonical_sha256"]
        or screening["base_pass_registry"]
        != snapshots["screening_base_pass_registry"]
    ):
        raise ValidationError(
            "frozen screening/Oracle/evidence/plan identities are inconsistent"
        )
    return documents


def _candidate_map(catalog: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {item["candidate_id"]: item for item in catalog["candidates"]}


def _ordered_candidate_paths(
    catalog: Mapping[str, Any], candidate_paths: Mapping[str, Path]
) -> list[tuple[str, Path]]:
    """Preserve the frozen PassRegistry/catalog implementation order."""

    return [
        (item["candidate_id"], candidate_paths[item["candidate_id"]])
        for item in catalog["candidates"]
        if item["candidate_id"] in candidate_paths
    ]


def _require_screening_capture_binding(
    screening: Mapping[str, Any],
    oracle_capture: Mapping[str, Any],
) -> None:
    if (
        screening["oracle_capture_sha256"] != sha256_json(oracle_capture)
        or screening["candidate_evidence_sha256"]
        != oracle_capture["candidate_evidence_sha256"]
        or screening["oracle_threshold"] != 1.10
        or screening["pass_registry_sha256"]
        != screening["base_pass_registry"]["canonical_sha256"]
    ):
        raise ValidationError(
            "candidate freeze screening/Oracle capture binding differs"
        )


def _require_catalog_registry(
    catalog: Mapping[str, Any],
    registry: Mapping[str, Any],
) -> None:
    if registry["schema_version"] != "pass-registry.v2":
        raise ValidationError("candidate execution requires pass-registry.v2")
    if catalog["pass_registry_sha256"] != sha256_json(registry):
        raise ValidationError("candidate registry does not bind the supplied pass registry")
    registry_candidates = [
        item for item in registry["passes"] if item["lifecycle"] == "candidate"
    ]
    registry_by_id = {item["id"]: item for item in registry_candidates}
    catalog_ids = [item["candidate_id"] for item in catalog["candidates"]]
    unknown = sorted(set(catalog_ids) - set(registry_by_id))
    if unknown:
        raise ValidationError(
            "candidate catalog references unknown PassRegistry v2 candidates: "
            + ", ".join(unknown)
        )
    expected_order = [
        item["id"] for item in registry_candidates if item["id"] in set(catalog_ids)
    ]
    if catalog_ids != expected_order:
        raise ValidationError("candidate catalog order differs from PassRegistry v2 candidates()")
    for descriptor in catalog["candidates"]:
        registered = registry_by_id[descriptor["candidate_id"]]
        obligations = [
            item["obligation_id"] for item in descriptor["legality_obligations"]
        ]
        if (
            descriptor["stage"] != registered["stage"]
            or descriptor["display_name"] != registered["display_name"]
            or descriptor["remark_pass_id"] != registered["id"]
            or obligations != registered["legality_obligation_ids"]
        ):
            raise ValidationError(
                f"candidate catalog descriptor differs from PassRegistry v2: {descriptor['candidate_id']}"
            )


def _load_screening_base_pass_registry(
    screening: Mapping[str, Any],
    *,
    workspace_root: Path,
) -> dict[str, Any]:
    base = _load_frozen_artifact(
        workspace_root,
        screening["base_pass_registry"],
        label="candidate screening base PassRegistry v2",
        version="pass-registry.v2",
    )
    if (
        sha256_json(base) != screening["pass_registry_sha256"]
        or any(item["lifecycle"] == "candidate" for item in base["passes"])
    ):
        raise ValidationError(
            "candidate screening base PassRegistry must be the exact pre-implementation export"
        )
    return base


def _require_executable_registry_bridge(
    *,
    screening: Mapping[str, Any],
    catalog: Mapping[str, Any],
    executable_registry: Mapping[str, Any],
    workspace_root: Path,
) -> dict[str, Any]:
    """Bind the immutable screening registry to the post-implementation export."""

    base = _load_screening_base_pass_registry(
        screening,
        workspace_root=workspace_root,
    )
    _require_catalog_registry(catalog, executable_registry)
    qualified_rows = [
        item
        for item in screening["candidates"]
        if item["qualification_status"] == "qualified"
    ]
    qualified_ids = [
        item["implementation_candidate_id"] for item in qualified_rows
    ]
    if not qualified_ids or any(item is None for item in qualified_ids):
        raise ValidationError(
            "candidate executable registry requires a non-empty qualified implementation set"
        )
    executable_candidates = [
        item for item in executable_registry["passes"]
        if item["lifecycle"] == "candidate"
    ]
    executable_non_candidates = [
        item for item in executable_registry["passes"]
        if item["lifecycle"] != "candidate"
    ]
    catalog_ids = [item["candidate_id"] for item in catalog["candidates"]]
    catalog_by_id = {
        item["candidate_id"]: item for item in catalog["candidates"]
    }
    executable_by_id = {item["id"]: item for item in executable_candidates}
    if (
        executable_non_candidates != base["passes"]
        or [item["id"] for item in executable_candidates] != qualified_ids
        or catalog_ids != qualified_ids
        or sha256_json(executable_registry) == sha256_json(base)
    ):
        raise ValidationError(
            "candidate executable PassRegistry must equal the base non-candidate projection "
            "plus exactly the qualified implementations"
        )
    for row in qualified_rows:
        candidate_id = row["implementation_candidate_id"]
        screening_obligations = row["legality_obligation_ids"]
        catalog_obligations = [
            item["obligation_id"]
            for item in catalog_by_id[candidate_id]["legality_obligations"]
        ]
        if (
            screening_obligations != catalog_obligations
            or screening_obligations
            != executable_by_id[candidate_id]["legality_obligation_ids"]
        ):
            raise ValidationError(
                "candidate executable PassRegistry legality obligations differ "
                f"from screening: {candidate_id}"
            )
    return base


def _run_ref(run: Mapping[str, Any]) -> dict[str, str]:
    return {
        "run_id": run["run_id"],
        "run_sha256": sha256_json(run),
        "configuration_sha256": run["configuration_sha256"],
    }


def _raw_run_ref_document(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: document[key]
        for key in (
            "run_id",
            "run_canonical_sha256",
            "run_physical_sha256",
            "terminal_observed_at",
            "terminal_journal_sha256",
            "terminal_journal_event_count",
            "state_tree_sha256",
            "raw_evidence_sha256",
            "attempt_count",
            "terminal_attempt_count",
        )
    }


def _raw_run_ref(verified: VerifiedRunRawEvidence) -> dict[str, Any]:
    return _raw_run_ref_document(verified.document)


def _raw_verifications_by_run_id(
    registry: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    result = {
        item["verification"]["run_id"]: item["verification"]
        for item in registry["runs"]
    }
    if len(result) != len(registry["runs"]):
        raise ValidationError("candidate raw evidence registry repeats a run id")
    return result


def _binding_record(
    run: Mapping[str, Any], *, include_execution_environment: bool = False
) -> dict[str, Any]:
    provenance = run["provenance"]
    binding = {
        "repo_commit": provenance["repo_commit"],
        "repo_dirty": provenance["repo_dirty"],
        "tracked_diff_sha256": provenance["tracked_diff_sha256"],
        "compiler_artifact_sha256": provenance["compiler_artifact_sha256"],
        "measurement_protocol_id": provenance["measurement_protocol_id"],
        "measurement_protocol_sha256": provenance["measurement_protocol_sha256"],
        "pipeline_profile_id": provenance["pipeline_profile_id"],
        "pipeline_profile_sha256": provenance["pipeline_profile_sha256"],
    }
    if include_execution_environment:
        binding["execution_environment_sha256"] = provenance.get(
            "execution_environment_sha256"
        )
    return binding


def _require_common_binding(
    baseline: Mapping[str, Any],
    other: Mapping[str, Any],
    *,
    label: str,
    require_execution_environment: bool = True,
) -> None:
    common_keys = (
        "repo_commit",
        "repo_dirty",
        "tracked_diff_sha256",
        "compiler_artifact_sha256",
        "execution_environment_sha256",
        "measurement_protocol_id",
        "measurement_protocol_sha256",
    )
    if not require_execution_environment:
        common_keys = tuple(
            key for key in common_keys if key != "execution_environment_sha256"
        )
    left = _binding_record(
        baseline, include_execution_environment=require_execution_environment
    )
    right = _binding_record(
        other, include_execution_environment=require_execution_environment
    )
    if any(left[key] != right[key] for key in common_keys):
        raise ValidationError(
            f"{label} differs from FULL in artifact/commit/protocol provenance"
        )


def _require_candidate_configuration(
    run: Mapping[str, Any],
    *,
    registry_sha256: str,
    pass_registry_sha256: str,
    enabled_candidate_ids: list[str],
    profile: Mapping[str, Any] | None,
    label: str,
) -> None:
    configuration = run["configuration"]
    if configuration.get("candidate_registry_sha256") != registry_sha256:
        raise ValidationError(f"{label} does not bind the exact candidate registry")
    if configuration.get("candidate_pass_registry_sha256") != pass_registry_sha256:
        raise ValidationError(f"{label} does not bind the exact PassRegistry v2")
    if configuration.get("enabled_candidate_ids") != enabled_candidate_ids:
        raise ValidationError(f"{label} has an unexpected enabled candidate set")
    if profile is not None and (
        run["provenance"]["pipeline_profile_id"] != profile["profile_id"]
        or run["provenance"]["pipeline_profile_sha256"] != profile["profile_sha256"]
        or configuration.get("pipeline_profile_file_sha256")
        != profile["profile_sha256"]
    ):
        raise ValidationError(f"{label} does not bind the exact matrix PipelineProfile v2")


def _candidate_profile_bytes(profile: Mapping[str, Any]) -> bytes:
    return canonical_json_bytes(profile) + b"\n"


def _workspace_output_directory(
    workspace_root: Path,
    output_directory: Path,
    *,
    label: str,
) -> Path:
    """Create an in-workspace directory without following lexical symlinks."""

    root = _candidate_workspace_root(workspace_root)
    lexical = (
        output_directory
        if output_directory.is_absolute()
        else root / output_directory
    ).absolute()
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise ConfigurationError(f"{label} must stay within workspace_root") from exc
    cursor = root
    for component in relative.parts:
        cursor = cursor / component
        if cursor.is_symlink():
            raise ValidationError(f"{label} must not traverse a symbolic link")
        if cursor.exists():
            if not cursor.is_dir():
                raise ValidationError(f"{label} must be a directory")
        else:
            cursor.mkdir()
        observed = resolve_without_symlinks(cursor, label=label)
        if observed != cursor:
            raise ValidationError(f"{label} resolves to another path")
    return cursor


def _publish_immutable_json(
    destination: Path,
    document: Mapping[str, Any],
    *,
    label: str,
) -> None:
    payload = canonical_json_bytes(document) + b"\n"
    if destination.is_symlink():
        raise ValidationError(f"{label} must not be a symbolic link")
    if destination.exists():
        if not destination.is_file() or destination.read_bytes() != payload:
            raise ValidationError(f"{label} already exists with different bytes")
        return
    durable_create_json(destination, document)


def _verify_matrix_profiles(
    workspace_root: Path,
    matrix: Mapping[str, Any],
    *,
    candidate_order: list[str],
) -> dict[str, Mapping[str, Any]]:
    root = _candidate_workspace_root(workspace_root)
    result: dict[str, Mapping[str, Any]] = {}
    for record in matrix["profiles"]:
        _, path, _ = _workspace_regular_path(
            root,
            Path(record["path"]),
            label=f"candidate profile {record['profile_id']}",
        )
        if sha256_file(path) != record["profile_sha256"]:
            raise ValidationError(
                f"candidate profile physical hash differs: {record['profile_id']}"
            )
        profile = load_pipeline_profile_v2(path, candidate_order=candidate_order)
        expected_arity = {"candidate_empty": 0, "single": 1, "pair": 2}[
            record["kind"]
        ]
        if (
            profile["base"] != "FULL"
            or profile["disable"] != []
            or profile["enable_candidates"] != record["enabled_candidate_ids"]
            or record["candidate_ids"] != record["enabled_candidate_ids"]
            or len(record["candidate_ids"]) != expected_arity
        ):
            raise ValidationError(
                f"candidate profile FULL/disable/enable contract differs: {record['profile_id']}"
            )
        if record["kind"] == "candidate_empty" and record["profile_id"] != "candidate-empty":
            raise ValidationError("candidate-empty profile id is not canonical")
        result[record["profile_id"]] = record
    return result


def generate_candidate_profile_matrix(
    *,
    catalog_path: Path,
    pass_registry_path: Path,
    matrix_id: str,
    workspace_root: Path,
    output_directory: Path,
    pairs: tuple[tuple[str, str], ...] = (),
    top_candidates: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build physical candidate-empty/single/explicit-Top3-pair profiles."""

    catalog = _load_version(
        catalog_path, "candidate-catalog.v1", label="candidate registry"
    )
    registry = _load_version(
        pass_registry_path, "pass-registry.v2", label="pass registry"
    )
    registry_sha256 = sha256_json(registry)
    _require_catalog_registry(catalog, registry)
    root = _candidate_workspace_root(workspace_root)
    output = _workspace_output_directory(
        root,
        output_directory,
        label="candidate profile output",
    )
    candidate_ids = [item["candidate_id"] for item in catalog["candidates"]]
    known = set(candidate_ids)
    if len(top_candidates) > 3 or len(top_candidates) != len(set(top_candidates)):
        raise ConfigurationError("Top candidate selection must contain at most three unique ids")
    unknown_top = sorted(set(top_candidates) - known)
    if unknown_top:
        raise ConfigurationError("unknown Top candidate: " + ", ".join(unknown_top))
    requested_pairs = [*pairs, *combinations(top_candidates, 2)]
    normalized_pairs: list[tuple[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    candidate_order = {candidate_id: index for index, candidate_id in enumerate(candidate_ids)}
    for left, right in requested_pairs:
        if left == right:
            raise ConfigurationError("candidate pair ids must be distinct")
        unknown = sorted({left, right} - known)
        if unknown:
            raise ConfigurationError("candidate pair references unknown id: " + ", ".join(unknown))
        pair = tuple(sorted((left, right), key=candidate_order.__getitem__))
        if pair in seen_pairs:
            raise ConfigurationError(f"duplicate candidate pair: {left}+{right}")
        seen_pairs.add(pair)
        normalized_pairs.append(pair)
    pair_candidates = {
        candidate_id for pair in normalized_pairs for candidate_id in pair
    }
    if len(normalized_pairs) > 3 or len(pair_candidates) > 3:
        raise ConfigurationError(
            "candidate diagnostic matrix may contain only the at-most-three Top3 pairs"
        )
    if len(pair_candidates) == 3 and set(normalized_pairs) != set(
        combinations(
            sorted(pair_candidates, key=candidate_order.__getitem__),
            2,
        )
    ):
        raise ConfigurationError(
            "three-candidate diagnostics require all and only the three Top3 pairs"
        )

    generated: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def add_profile(profile_id: str, kind: str, enabled: list[str]) -> None:
        profile = validate_pipeline_profile_v2(
            {
            "schema_version": 2,
            "base": "FULL",
            "disable": [],
            "enable_candidates": enabled,
            },
            candidate_order=candidate_ids,
        )
        payload = _candidate_profile_bytes(profile)
        filename = safe_slug(profile_id) + ".json"
        physical = output / "profiles" / filename
        generated.append(
            (
                {
                    "profile_id": profile_id,
                    "kind": kind,
                    "candidate_ids": enabled,
                    "enabled_candidate_ids": enabled,
                    "profile_sha256": sha256_bytes(payload),
                    "path": physical.relative_to(root).as_posix(),
                },
                profile,
            )
        )

    add_profile("candidate-empty", "candidate_empty", [])
    for candidate_id in candidate_ids:
        add_profile(f"full+{candidate_id}", "single", [candidate_id])
    for left, right in normalized_pairs:
        add_profile(f"full+{left}+{right}", "pair", [left, right])

    profiles = [record for record, _ in generated]
    schedule: list[dict[str, Any]] = []
    for profile in profiles:
        if profile["kind"] == "candidate_empty":
            continue
        schedule.append(
            {
                "kind": profile["kind"],
                "candidate_ids": profile["candidate_ids"],
                "baseline_profile_id": "candidate-empty",
                "candidate_profile_id": profile["profile_id"],
                "comparison_direction": "baseline_over_variant",
            }
        )
    base_profile = {
        "profile_id": "candidate-empty",
        "profile_sha256": profiles[0]["profile_sha256"],
    }
    matrix = validate_document(
        {
            "schema_version": "candidate-profile-matrix.v1",
            "matrix_id": matrix_id,
            "candidate_registry_sha256": sha256_json(catalog),
            "pass_registry_sha256": registry_sha256,
            "base_pipeline_profile": base_profile,
            "profiles": profiles,
            "schedule": schedule,
        }
    )
    expected_files = {
        (root / record["path"]).relative_to(output).as_posix()
        for record in profiles
    } | {"matrix.json"}
    if output.exists():
        existing_files: set[str] = set()
        existing_directories: set[str] = set()
        for path in output.rglob("*"):
            relative = path.relative_to(output).as_posix()
            if path.is_symlink():
                raise ValidationError(
                    "candidate profile directory contains a symbolic link: "
                    + relative
                )
            if path.is_file():
                existing_files.add(relative)
            elif path.is_dir():
                existing_directories.add(relative)
            else:
                raise ValidationError(
                    "candidate profile directory contains a non-regular entry: "
                    + relative
                )
        unexpected_directories = sorted(existing_directories - {"profiles"})
        if unexpected_directories:
            raise ConfigurationError(
                "candidate profile directory contains unmanaged directories: "
                + ", ".join(unexpected_directories[:10])
            )
        if (existing_files or existing_directories) and (
            existing_files != expected_files
            or existing_directories != {"profiles"}
        ):
            missing = sorted(expected_files - existing_files)
            unexpected = sorted(existing_files - expected_files)
            raise ConfigurationError(
                "candidate profile directory is not the exact managed file set: "
                f"missing={missing[:10]}, unexpected={unexpected[:10]}"
            )
    profiles_directory = _workspace_output_directory(
        root,
        output / "profiles",
        label="candidate profile directory",
    )
    assert profiles_directory == output / "profiles"
    for record, profile in generated:
        _publish_immutable_json(
            root / record["path"],
            profile,
            label=f"candidate profile {record['profile_id']}",
        )
    _publish_immutable_json(
        output / "matrix.json",
        matrix,
        label="candidate profile matrix",
    )
    return matrix


def build_candidate_screening(
    *,
    candidate_evidence_path: Path,
    screening_spec_path: Path,
    pass_registry_path: Path,
    oracle_capture_path: Path,
    workspace_root: Path,
    screening_id: str,
    raw_evidence_verifier: Any | None = None,
) -> dict[str, Any]:
    evidence = _load_version(
        candidate_evidence_path,
        "candidate-evidence.v1",
        label="candidate evidence",
    )
    spec = _load_version(
        screening_spec_path,
        "candidate-screening-spec.v1",
        label="candidate screening spec",
    )
    pass_registry = _load_version(
        pass_registry_path,
        "pass-registry.v2",
        label="candidate screening PassRegistry v2",
    )
    pass_registry_sha256 = sha256_json(pass_registry)
    if any(item["lifecycle"] == "candidate" for item in pass_registry["passes"]):
        raise ValidationError(
            "candidate screening requires the pre-implementation PassRegistry with zero candidates"
        )
    if spec["pass_registry_sha256"] != pass_registry_sha256:
        raise ValidationError("candidate screening spec binds a different PassRegistry v2")
    pass_by_id = {item["id"]: item for item in pass_registry["passes"]}
    evidence_ids = [item["candidate_id"] for item in evidence["candidates"]]
    if len(evidence_ids) != 11:
        raise ValidationError("candidate screening requires the complete eleven-family evidence pool")
    spec_by_id = {item["candidate_id"]: item for item in spec["candidates"]}
    if set(spec_by_id) != set(evidence_ids):
        raise ValidationError("candidate screening spec does not cover the exact evidence pool")
    capture = _load_and_reverify_candidate_oracle_capture(
        capture_path=oracle_capture_path,
        workspace_root=workspace_root,
        raw_evidence_verifier=raw_evidence_verifier,
    )
    if capture["candidate_evidence_sha256"] != sha256_json(evidence):
        raise ValidationError("Oracle capture binds different candidate evidence")
    capture_by_id = {item["candidate_id"]: item for item in capture["candidates"]}
    if set(capture_by_id) != set(evidence_ids):
        raise ValidationError("Oracle capture does not cover all eleven candidates")
    capture_by_family = {
        item["oracle_family_id"]: item for item in capture["candidates"]
    }
    if len(capture_by_family) != len(capture["candidates"]):
        raise ValidationError("Oracle capture repeats an Oracle family")

    results: list[dict[str, Any]] = []
    duplicate_groups: dict[str, list[str]] = {}
    for evidence_item in evidence["candidates"]:
        candidate_id = evidence_item["candidate_id"]
        structural = spec_by_id[candidate_id]
        if (
            structural["oracle_family_id"]
            != capture_by_id[candidate_id]["oracle_family_id"]
            or structural["oracle_family_id"]
            != evidence_item["cleanroom_oracle_family_id"]
        ):
            raise ValidationError(
                f"candidate screening Oracle family identity differs: {candidate_id}"
            )
        implementation_id = structural["implementation_candidate_id"]
        overlap_ids = structural["overlaps_existing_pass_ids"]
        unknown_overlaps = sorted(set(overlap_ids) - set(pass_by_id))
        candidate_overlaps = sorted(
            pass_id
            for pass_id in overlap_ids
            if pass_id in pass_by_id
            and pass_by_id[pass_id]["lifecycle"] == "candidate"
        )
        if unknown_overlaps or candidate_overlaps:
            raise ValidationError(
                f"candidate screening overlap ids must be existing non-candidate passes: {candidate_id}"
            )
        obligations = evidence_item["legality_obligation_ids"]
        if structural["structural_disposition"] == "eligible":
            if implementation_id is None:
                raise ValidationError(
                    f"eligible candidate lacks an implementation pass id: {candidate_id}"
                )
            if not obligations or any(
                not obligation.startswith(f"{implementation_id}.")
                for obligation in obligations
            ):
                raise ValidationError(
                    f"candidate legality obligations are not scoped to {implementation_id}"
                )
            if implementation_id in pass_by_id:
                raise ValidationError(
                    f"future candidate implementation id conflicts with an existing pass: {implementation_id}"
                )
        reasons: list[str] = []
        structural_reason = structural["structural_reason"]
        if structural_reason is not None:
            reasons.append(structural_reason)
        if evidence_item["legality_proof_path"] != "clear":
            reasons.append("unclear_legality")
        if evidence_item["specification_status"] != "clear":
            reasons.append("unclear_specification")
        primary_family = structural["oracle_family_id"]
        eligible_oracle_refs = structural["eligible_oracle_structure_refs"]
        eligible_ref_tuples = [
            (ref["oracle_family_id"], ref["structure_id"])
            for ref in eligible_oracle_refs
        ]
        captured_by_ref = {
            (family_id, captured["structure_id"]): captured
            for family_id, family_capture in capture_by_family.items()
            for captured in family_capture["structures"]
        }
        unknown_eligible = sorted(set(eligible_ref_tuples) - set(captured_by_ref))
        if unknown_eligible:
            raise ValidationError(
                f"candidate screening allows unknown Oracle structures for {candidate_id}: "
                + ", ".join(f"{family}/{structure}" for family, structure in unknown_eligible)
            )
        source_structures = [
            (primary_family, captured)
            for captured in capture_by_id[candidate_id]["structures"]
        ]
        source_structures.extend(
            (family_id, captured_by_ref[(family_id, structure_id)])
            for family_id, structure_id in eligible_ref_tuples
            if family_id != primary_family
        )
        source_refs = [
            (family_id, captured["structure_id"])
            for family_id, captured in source_structures
        ]
        if len(source_refs) != len(set(source_refs)):
            raise ValidationError(
                f"candidate screening repeats an Oracle source structure: {candidate_id}"
            )
        if eligible_ref_tuples != [
            source_ref
            for source_ref in source_refs
            if source_ref in set(eligible_ref_tuples)
        ]:
            raise ValidationError(
                f"candidate screening eligible structures are not an ordered capture subset: {candidate_id}"
            )
        eligible_ref_set = set(eligible_ref_tuples)
        oracle_structures: list[dict[str, Any]] = []
        for source_family, captured in source_structures:
            geometric_mean = captured["geometric_mean_upper_bound"]
            source_ref = (source_family, captured["structure_id"])
            screening_eligible = source_ref in eligible_ref_set
            meets = bool(
                screening_eligible
                and
                captured["eligible_for_ranking"]
                and geometric_mean is not None
                and geometric_mean >= 1.10
            )
            oracle_structures.append(
                {
                    "oracle_family_id": source_family,
                    "structure_id": captured["structure_id"],
                    "sizes": captured["sizes"],
                    "paired_datasets": captured["paired_datasets"],
                    "eligible_for_ranking": captured["eligible_for_ranking"],
                    "ineligibility_reason": captured["ineligibility_reason"],
                    "geometric_mean_speedup": geometric_mean,
                    "eligible_for_candidate_screening": screening_eligible,
                    "meets_threshold": meets,
                }
            )
        qualifying_structures = [
            {
                "oracle_family_id": item["oracle_family_id"],
                "structure_id": item["structure_id"],
            }
            for item in oracle_structures
            if item["meets_threshold"]
        ]
        if not qualifying_structures and eligible_oracle_refs:
            if any(
                item["eligible_for_candidate_screening"]
                and item["eligible_for_ranking"]
                for item in oracle_structures
            ):
                reasons.append("oracle_structure_below_1_10")
            else:
                reasons.append("no_complete_oracle_structure")
        reasons = list(dict.fromkeys(reasons))
        if structural["structural_disposition"] == "rejected":
            qualification = "rejected"
        elif reasons:
            qualification = "blocked"
        else:
            qualification = "qualified"
        duplicate = structural["duplicate_of"]
        if duplicate is not None:
            duplicate_groups.setdefault(duplicate, []).append(candidate_id)
        results.append(
            {
                "candidate_id": candidate_id,
                "oracle_family_id": structural["oracle_family_id"],
                "implementation_candidate_id": structural[
                    "implementation_candidate_id"
                ],
                "family_kind": structural["family_kind"],
                "overlaps_existing_pass_ids": structural[
                    "overlaps_existing_pass_ids"
                ],
                "duplicate_of": duplicate,
                "eligible_oracle_structure_refs": eligible_oracle_refs,
                "oracle_structures": oracle_structures,
                "qualifying_oracle_structure_refs": qualifying_structures,
                "legality_proof_path": evidence_item["legality_proof_path"],
                "legality_obligation_ids": evidence_item[
                    "legality_obligation_ids"
                ],
                "implementation_cost": evidence_item["implementation_cost"],
                "risk": evidence_item["risk"],
                "specification_status": evidence_item["specification_status"],
                "requires_boom_feature": evidence_item["requires_boom_feature"],
                "qualification_status": qualification,
                "rejection_reasons": reasons,
            }
        )
    return validate_document(
        {
            "schema_version": "candidate-screening.v1",
            "screening_id": screening_id,
            "generated_at": capture["generated_at"],
            "candidate_evidence_sha256": sha256_json(evidence),
            "screening_spec_sha256": sha256_json(spec),
            "pass_registry_sha256": pass_registry_sha256,
            "base_pass_registry": _frozen_artifact_digest(
                workspace_root,
                pass_registry_path,
                pass_registry,
                label="candidate screening base PassRegistry v2",
            ),
            "sources": {
                "candidate_evidence": _frozen_artifact_digest(
                    workspace_root,
                    candidate_evidence_path,
                    evidence,
                    label="candidate screening evidence",
                ),
                "screening_spec": _frozen_artifact_digest(
                    workspace_root,
                    screening_spec_path,
                    spec,
                    label="candidate screening spec",
                ),
                "oracle_capture": _frozen_artifact_digest(
                    workspace_root,
                    oracle_capture_path,
                    capture,
                    label="candidate screening Oracle capture",
                ),
            },
            "oracle_threshold": 1.10,
            "oracle_capture_sha256": sha256_json(capture),
            "candidates": results,
            "duplicate_groups": [
                {
                    "canonical_candidate_id": canonical,
                    "duplicate_candidate_ids": sorted(duplicates),
                }
                for canonical, duplicates in sorted(duplicate_groups.items())
            ],
        }
    )


def _load_and_reverify_candidate_screening(
    *,
    screening_path: Path,
    workspace_root: Path,
    raw_evidence_verifier: Any | None = None,
) -> dict[str, Any]:
    """Rebuild screening from every frozen source and require exact identity."""

    root = _candidate_workspace_root(workspace_root)
    _, physical, _ = _workspace_regular_path(
        root,
        screening_path,
        label="candidate screening",
    )
    screening = _load_version(
        physical,
        "candidate-screening.v1",
        label="candidate screening",
    )
    sources = screening["sources"]
    expected = build_candidate_screening(
        candidate_evidence_path=root / sources["candidate_evidence"]["path"],
        screening_spec_path=root / sources["screening_spec"]["path"],
        pass_registry_path=root / screening["base_pass_registry"]["path"],
        oracle_capture_path=root / sources["oracle_capture"]["path"],
        workspace_root=root,
        screening_id=screening["screening_id"],
        raw_evidence_verifier=raw_evidence_verifier,
    )
    if expected != screening:
        raise ValidationError(
            "candidate screening differs from exact source/capture replay"
        )
    return screening


def _terminal_candidate_reason(
    *,
    correctness_failures: int,
    excluded_cases: int,
    censored_cases: int,
    comparable_cases: int,
    paired_candidate_count: int,
) -> str | None:
    if correctness_failures:
        return "correctness_failure"
    if excluded_cases:
        return "incomplete_profile"
    if censored_cases:
        return "right_censored"
    if comparable_cases == 0:
        return "no_comparable_cases"
    if paired_candidate_count == 0:
        return "no_candidate_observation"
    return None


def _require_analysis_terminal_run(
    run: Mapping[str, Any], *, label: str
) -> None:
    """Accept every immutable campaign terminal, including interruption evidence."""

    if run["state"] not in {"completed", "failed", "interrupted"}:
        raise ValidationError(f"{label} is not terminal")


def _measured_case_value(
    case: Mapping[str, Any],
    metric_id: str,
) -> float | None:
    for measurement in case["measurements"]:
        if measurement["metric_id"] == metric_id:
            if measurement["availability"] != "measured" or measurement["value"] is None:
                return None
            return float(measurement["value"])
    return None


def build_candidate_study(
    *,
    catalog_path: Path,
    pass_registry_path: Path,
    matrix_path: Path,
    workspace_root: Path,
    raw_state_root: Path,
    baseline_path: Path,
    candidate_paths: Mapping[str, Path],
    study_id: str,
    title: str,
    bootstrap_samples: int = 10_000,
    seed: int = 20260809,
    interaction_paths: Mapping[tuple[str, str], Path] | None = None,
) -> dict[str, Any]:
    """Compare FULL to FULL+candidate directly; ratios are never inverted."""

    catalog = _load_version(
        catalog_path, "candidate-catalog.v1", label="candidate registry"
    )
    pass_registry = _load_version(
        pass_registry_path, "pass-registry.v2", label="pass registry"
    )
    _require_catalog_registry(catalog, pass_registry)
    matrix = _load_version(
        matrix_path, "candidate-profile-matrix.v1", label="candidate profile matrix"
    )
    registry_sha256 = sha256_json(catalog)
    if matrix["candidate_registry_sha256"] != registry_sha256:
        raise ValidationError("candidate matrix does not bind the supplied registry")
    if matrix["pass_registry_sha256"] != catalog["pass_registry_sha256"]:
        raise ValidationError("candidate matrix/pass-registry binding drifted")
    candidates = _candidate_map(catalog)
    profiles = _verify_matrix_profiles(
        workspace_root,
        matrix,
        candidate_order=[item["candidate_id"] for item in catalog["candidates"]],
    )
    scheduled = {
        item["candidate_ids"][0]: item
        for item in matrix["schedule"]
        if item["kind"] == "single"
    }
    unknown = sorted(set(candidate_paths) - set(scheduled))
    if unknown:
        raise ConfigurationError(
            "candidate run is not scheduled by the matrix: " + ", ".join(unknown)
        )

    _, physical_state_root, _ = _workspace_directory_path(
        workspace_root,
        raw_state_root,
        label="candidate study raw evidence state root",
    )
    baseline, baseline_verified = _verify_candidate_run_raw_evidence(
        run_path=baseline_path,
        state_root=physical_state_root,
        catalog=catalog,
        pass_registry=pass_registry,
    )
    if baseline["state"] != "completed":
        raise ValidationError("candidate study requires a completed all-correct FULL run")
    data_roles = {case["data_role"] for case in baseline["cases"]}
    if len(data_roles) != 1 or next(iter(data_roles)) not in {"B2", "B3", "B4", "B5", "B6"}:
        raise ValidationError("candidate study requires one B2-B6 data role")
    data_role = next(iter(data_roles))
    base_profile = profiles["candidate-empty"]
    if (
        baseline["provenance"]["pipeline_profile_id"] != base_profile["profile_id"]
        or baseline["provenance"]["pipeline_profile_sha256"]
        != base_profile["profile_sha256"]
    ):
        raise ValidationError("FULL run does not bind the matrix base pipeline profile")
    _require_candidate_configuration(
        baseline,
        registry_sha256=registry_sha256,
        pass_registry_sha256=catalog["pass_registry_sha256"],
        enabled_candidate_ids=[],
        profile=base_profile,
        label="FULL run",
    )
    _require_formal_measurement(baseline, require_accela_pipeline=True)
    _require_candidate_run_protocol(baseline, data_role=data_role)
    primary = metric_spec(baseline)
    if primary["source"] == "wall_time":
        raise ValidationError("candidate ranking requires a counter/size primary metric")

    terminal_times = [baseline_verified.document["terminal_observed_at"]]
    results: list[dict[str, Any]] = []
    candidate_raw_refs: list[dict[str, Any]] = []
    for candidate_id, path in _ordered_candidate_paths(catalog, candidate_paths):
        descriptor = candidates[candidate_id]
        candidate, candidate_verified = _verify_candidate_run_raw_evidence(
            run_path=path,
            state_root=physical_state_root,
            catalog=catalog,
            pass_registry=pass_registry,
        )
        _require_analysis_terminal_run(
            candidate, label=f"candidate run {candidate_id}"
        )
        terminal_times.append(candidate_verified.document["terminal_observed_at"])
        if (
            candidate["suite_id"] != baseline["suite_id"]
            or candidate["manifest_sha256"] != baseline["manifest_sha256"]
        ):
            raise ValidationError(f"candidate run suite/manifest differs from FULL: {candidate_id}")
        _require_common_binding(baseline, candidate, label=f"candidate {candidate_id}")
        _require_candidate_configuration(
            candidate,
            registry_sha256=registry_sha256,
            pass_registry_sha256=catalog["pass_registry_sha256"],
            enabled_candidate_ids=[candidate_id],
            profile=profiles[scheduled[candidate_id]["candidate_profile_id"]],
            label=f"candidate {candidate_id}",
        )
        _require_formal_measurement(candidate, require_accela_pipeline=True)
        _require_candidate_run_protocol(candidate, data_role=data_role)
        comparison = compare_runs(baseline, candidate)
        candidate_raw_refs.append(
            {"candidate_id": candidate_id, "run": _raw_run_ref(candidate_verified)}
        )

        obligation_ids = [
            item["obligation_id"] for item in descriptor["legality_obligations"]
        ]
        remark_case_count, remark_totals = _candidate_remark_totals(
            candidate, candidate_ids=[candidate_id]
        )
        paired_candidate_count = remark_totals[candidate_id][
            "paired_candidate_count"
        ]
        applied_count = remark_totals[candidate_id]["applied_count"]
        rejected_count = remark_totals[candidate_id]["rejected_count"]

        interval = bootstrap_geometric_mean_ci(
            comparison.pairs,
            samples=bootstrap_samples,
            seed=seed,
        )
        reason = _terminal_candidate_reason(
            correctness_failures=comparison.correctness_failures,
            excluded_cases=comparison.excluded_cases,
            censored_cases=comparison.censored_cases,
            comparable_cases=len(comparison.pairs),
            paired_candidate_count=paired_candidate_count,
        )
        baseline_cases = {case["case_id"]: case for case in baseline["cases"]}
        candidate_cases = {case["case_id"]: case for case in candidate["cases"]}
        static_pairs = [
            (
                _measured_case_value(baseline_cases[pair.case_id], "elf_text_bytes"),
                _measured_case_value(candidate_cases[pair.case_id], "elf_text_bytes"),
            )
            for pair in comparison.pairs
        ]
        if static_pairs and all(left is not None and right is not None for left, right in static_pairs):
            static_full = sum(float(left) for left, _ in static_pairs if left is not None)
            static_candidate = sum(float(right) for _, right in static_pairs if right is not None)
            if static_full <= 0 or static_candidate <= 0:
                static_full = None
                static_candidate = None
                static_ratio = None
            else:
                static_ratio = static_full / static_candidate
        else:
            static_full = None
            static_candidate = None
            static_ratio = None
        results.append(
            {
                "candidate_id": candidate_id,
                "logical_profile_id": scheduled[candidate_id]["candidate_profile_id"],
                "run_id": candidate["run_id"],
                "run_sha256": sha256_json(candidate),
                "configuration_sha256": candidate["configuration_sha256"],
                "enabled_candidate_ids": [candidate_id],
                "comparable_cases": len(comparison.pairs),
                "comparable_source_groups": len(
                    {pair.source_group_id for pair in comparison.pairs}
                ),
                "correctness_failures": comparison.correctness_failures,
                "censored_cases": comparison.censored_cases,
                "excluded_cases": comparison.excluded_cases,
                "eligible_for_ranking": reason is None,
                "ineligibility_reason": reason,
                "case_geometric_mean_speedup": comparison.geometric_mean_speedup,
                "source_group_geometric_mean_speedup": (
                    comparison.source_group_geometric_mean_speedup
                ),
                "confidence_interval_95": (
                    None
                    if interval is None
                    else {"low": interval[0], "high": interval[1]}
                ),
                "static_text_bytes_full": static_full,
                "static_text_bytes_full_plus_candidate": static_candidate,
                "static_text_ratio": static_ratio,
                "remarks": {
                    "case_count": remark_case_count,
                    "paired_candidate_count": paired_candidate_count,
                    "applied_count": applied_count,
                    "rejected_count": rejected_count,
                    "legality_obligation_ids": obligation_ids,
                },
                "per_cases": [
                    {
                        "case_id": pair.case_id,
                        "source_group": pair.source_group_id,
                        "family": pair.family,
                        "target": pair.target,
                        "weight": pair.weight,
                        "metric_full": pair.baseline_value,
                        "metric_full_plus_candidate": pair.candidate_value,
                        "speedup": pair.speedup,
                    }
                    for pair in comparison.pairs
                ],
                "families": family_geometric_means(comparison.pairs),
            }
        )
    requested_interactions = interaction_paths or {}
    interactions: list[dict[str, Any]] = []
    interaction_raw_refs: list[dict[str, Any]] = []
    if requested_interactions:
        if data_role != "B3":
            raise ConfigurationError(
                "candidate pair interactions are diagnostic B3 evidence only"
            )
        candidate_order = {
            candidate_id: index for index, candidate_id in enumerate(candidates)
        }
        normalized_paths: dict[tuple[str, str], Path] = {}
        for raw_pair, path in requested_interactions.items():
            if len(raw_pair) != 2 or raw_pair[0] == raw_pair[1]:
                raise ConfigurationError(
                    "candidate interaction requires two distinct candidate ids"
                )
            unknown_pair = sorted(set(raw_pair) - set(candidates))
            if unknown_pair:
                raise ConfigurationError(
                    "candidate interaction references unknown candidates: "
                    + ", ".join(unknown_pair)
                )
            pair = tuple(sorted(raw_pair, key=candidate_order.__getitem__))
            if pair in normalized_paths:
                raise ConfigurationError(
                    f"duplicate candidate interaction: {pair[0]}+{pair[1]}"
                )
            normalized_paths[pair] = path
        eligible_singles = sorted(
            (item for item in results if item["eligible_for_ranking"]),
            key=lambda item: (
                -float(item["case_geometric_mean_speedup"]),
                item["candidate_id"],
            ),
        )
        top_candidate_ids = [
            item["candidate_id"] for item in eligible_singles[:3]
        ]
        ordered_pairs = [
            tuple(sorted(pair, key=candidate_order.__getitem__))
            for pair in combinations(top_candidate_ids, 2)
        ]
        expected_pairs = set(ordered_pairs)
        if set(normalized_paths) != expected_pairs:
            raise ValidationError(
                "candidate interactions must cover exactly the B3 single-GM Top3 pairs"
            )
        scheduled_pairs = {
            tuple(item["candidate_ids"]): item
            for item in matrix["schedule"]
            if item["kind"] == "pair"
        }
        single_by_id = {item["candidate_id"]: item for item in results}
        for pair in ordered_pairs:
            schedule = scheduled_pairs.get(pair)
            if schedule is None:
                raise ValidationError(
                    f"candidate matrix lacks the selected Top3 pair: {pair[0]}+{pair[1]}"
                )
            pair_key = "+".join(pair)
            run, pair_verified = _verify_candidate_run_raw_evidence(
                run_path=normalized_paths[pair],
                state_root=physical_state_root,
                catalog=catalog,
                pass_registry=pass_registry,
            )
            _require_analysis_terminal_run(
                run, label=f"candidate interaction run {pair_key}"
            )
            terminal_times.append(pair_verified.document["terminal_observed_at"])
            if (
                run["suite_id"] != baseline["suite_id"]
                or run["manifest_sha256"] != baseline["manifest_sha256"]
            ):
                raise ValidationError(
                    f"candidate interaction suite/manifest differs from FULL: {pair_key}"
                )
            _require_common_binding(
                baseline, run, label=f"candidate interaction {pair_key}"
            )
            profile = profiles[schedule["candidate_profile_id"]]
            _require_candidate_configuration(
                run,
                registry_sha256=registry_sha256,
                pass_registry_sha256=catalog["pass_registry_sha256"],
                enabled_candidate_ids=list(pair),
                profile=profile,
                label=f"candidate interaction {pair_key}",
            )
            _require_formal_measurement(run, require_accela_pipeline=True)
            _require_candidate_run_protocol(run, data_role=data_role)
            comparison = compare_runs(baseline, run)
            _, pair_totals = _candidate_remark_totals(
                run, candidate_ids=list(pair)
            )
            observations = {
                candidate_id: pair_totals[candidate_id]["paired_candidate_count"]
                for candidate_id in pair
            }
            interaction_raw_refs.append(
                {
                    "candidate_ids": list(pair),
                    "run": _raw_run_ref(pair_verified),
                }
            )
            reason = _terminal_candidate_reason(
                correctness_failures=comparison.correctness_failures,
                excluded_cases=comparison.excluded_cases,
                censored_cases=comparison.censored_cases,
                comparable_cases=len(comparison.pairs),
                paired_candidate_count=min(observations.values()),
            )
            constituent_results = [single_by_id[candidate_id] for candidate_id in pair]
            if reason is None and any(
                not item["eligible_for_ranking"] for item in constituent_results
            ):
                reason = "constituent_ineligible"
            pair_gm = comparison.geometric_mean_speedup
            expected_speedup = (
                math.prod(
                    float(item["case_geometric_mean_speedup"])
                    for item in constituent_results
                )
                if reason is None
                else None
            )
            interactions.append(
                {
                    "candidate_ids": list(pair),
                    "logical_profile_id": schedule["candidate_profile_id"],
                    "run_id": run["run_id"],
                    "run_sha256": sha256_json(run),
                    "configuration_sha256": run["configuration_sha256"],
                    "enabled_candidate_ids": list(pair),
                    "comparable_cases": len(comparison.pairs),
                    "correctness_failures": comparison.correctness_failures,
                    "censored_cases": comparison.censored_cases,
                    "excluded_cases": comparison.excluded_cases,
                    "candidate_observations": [
                        {
                            "candidate_id": candidate_id,
                            "paired_candidate_count": observations[candidate_id],
                        }
                        for candidate_id in pair
                    ],
                    "eligible_for_ranking": reason is None,
                    "ineligibility_reason": reason,
                    "pair_case_geometric_mean_speedup": (
                        pair_gm if reason is None else None
                    ),
                    "expected_multiplicative_speedup": expected_speedup,
                    "delta_ln_geometric_mean": (
                        math.log(float(pair_gm))
                        - sum(
                            math.log(float(item["case_geometric_mean_speedup"]))
                            for item in constituent_results
                        )
                        if reason is None and pair_gm is not None
                        else None
                    ),
                    "per_cases": [
                        {
                            "case_id": item.case_id,
                            "source_group": item.source_group_id,
                            "family": item.family,
                            "target": item.target,
                            "weight": item.weight,
                            "metric_full": item.baseline_value,
                            "metric_full_plus_candidate": item.candidate_value,
                            "speedup": item.speedup,
                        }
                        for item in comparison.pairs
                    ],
                }
            )

    return validate_document(
        {
            "schema_version": "candidate-study.v1",
            "study_id": study_id,
            "title": title,
            "generated_at": _latest_evidence_timestamp(terminal_times),
            "matrix_sha256": sha256_json(matrix),
            "candidate_registry_sha256": registry_sha256,
            "pass_registry_sha256": catalog["pass_registry_sha256"],
            "suite_id": baseline["suite_id"],
            "data_role": data_role,
            "manifest_sha256": baseline["manifest_sha256"],
            "bindings": _binding_record(
                baseline, include_execution_environment=True
            ),
            "baseline": _run_ref(baseline),
            "raw_evidence": {
                "baseline": _raw_run_ref(baseline_verified),
                "candidates": candidate_raw_refs,
                "interactions": interaction_raw_refs,
            },
            "primary_metric_id": baseline["configuration"]["primary_metric_id"],
            "metric_unit": primary["unit"],
            "bootstrap_samples": bootstrap_samples,
            "seed": seed,
            "candidates": results,
            "interactions": interactions,
        }
    )


def _oracle_pair_rows(
    plan: Mapping[str, Any],
    baseline: Mapping[str, Any],
    optimized: Mapping[str, Any],
) -> list[dict[str, Any]]:
    left_cases = {case["case_id"]: case for case in baseline["cases"]}
    right_cases = {case["case_id"]: case for case in optimized["cases"]}
    primary = baseline["configuration"]["primary_metric_id"]
    rows: list[dict[str, Any]] = []
    for pair in plan["pairs"]:
        identity = pair["pair_id"].split(":")
        if (
            len(identity) != 3
            or identity[0] != pair["family"]
            or identity[2] not in {"small", "medium", "large"}
        ):
            raise ValidationError(
                "candidate Oracle pair_id must be FAMILY:STRUCTURE:small|medium|large"
            )
        _, structure_id, size = identity
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
                    raise ValidationError(
                        f"oracle {leg} case content differs from plan: {pair['pair_id']}"
                    )
                pairing = case.get("oracle_pair")
                if (
                    pairing is None
                    or pairing["pair_id"] != pair["pair_id"]
                    or pairing["leg"] != leg
                ):
                    raise ValidationError(
                        f"oracle {leg} pairing differs from plan: {pair['pair_id']}"
                    )
            statuses = {left["status"], right["status"]}
            if "timeout" in statuses:
                reason = "right_censored"
            elif statuses & {"pending", "cancelled"}:
                reason = "incomplete_run"
            elif statuses != {"passed"}:
                reason = "correctness_failure"
        left_value = (
            case_metric(left, primary)
            if left is not None and left["status"] == "passed"
            else None
        )
        right_value = (
            case_metric(right, primary)
            if right is not None and right["status"] == "passed"
            else None
        )
        rows.append(
            {
                "pair_id": pair["pair_id"],
                "family": pair["family"],
                "structure_id": structure_id,
                "size": size,
                "baseline_case_id": pair["baseline"]["case_id"],
                "optimized_case_id": pair["optimized"]["case_id"],
                "eligible_for_ranking": reason is None,
                "ineligibility_reason": reason,
                "baseline_metric_value": left_value,
                "optimized_metric_value": right_value,
                "speedup": (
                    left_value / right_value
                    if reason is None and left_value is not None and right_value is not None
                    else None
                ),
            }
        )
    return rows


def capture_candidate_oracle(
    *,
    candidate_evidence_path: Path,
    oracle_plan_path: Path,
    baseline_path: Path,
    optimized_path: Path,
    state_root: Path,
    workspace_root: Path,
    capture_id: str,
    raw_evidence_verifier: Any | None = None,
) -> dict[str, Any]:
    evidence = _load_version(
        candidate_evidence_path,
        "candidate-evidence.v1",
        label="candidate evidence",
    )
    plan = _load_version(oracle_plan_path, "oracle-plan.v1", label="oracle plan")
    if plan["evidence_class"] != "cleanroom":
        raise ValidationError(
            "candidate screening Oracle capture requires the single cleanroom 99-pair plan"
        )
    if len(plan["pairs"]) != 99 or len(evidence["candidates"]) != 11:
        raise ValidationError(
            "candidate screening Oracle capture requires 11 families x 3 structures x 3 sizes"
        )
    baseline = _load_version(baseline_path, "run-record.v1", label="oracle baseline run")
    optimized = _load_version(optimized_path, "run-record.v1", label="oracle optimized run")
    root, physical_state_root, relative_state_root = _workspace_directory_path(
        workspace_root,
        state_root,
        label="candidate Oracle raw evidence state root",
    )
    _, physical_baseline, relative_baseline = _workspace_regular_path(
        root, baseline_path, label="candidate Oracle baseline run"
    )
    _, physical_optimized, relative_optimized = _workspace_regular_path(
        root, optimized_path, label="candidate Oracle optimized run"
    )
    verifier = raw_evidence_verifier or verify_run_raw_evidence
    baseline_verified = verifier(
        physical_baseline, physical_state_root
    )
    optimized_verified = verifier(
        physical_optimized, physical_state_root
    )
    if (
        baseline_verified.document["run_canonical_sha256"] != sha256_json(baseline)
        or optimized_verified.document["run_canonical_sha256"]
        != sha256_json(optimized)
    ):
        raise ValidationError("candidate Oracle raw verifier run identity differs")
    if (
        baseline["run_id"] != plan["baseline_run_id"]
        or optimized["run_id"] != plan["optimized_run_id"]
    ):
        raise ValidationError("oracle run identities do not match the plan")
    profile = plan["pipeline_profile"]
    for label, run in (("baseline", baseline), ("optimized", optimized)):
        configuration = run["configuration"]
        if run["state"] not in {"completed", "failed"}:
            raise ValidationError(f"oracle {label} run is not terminal")
        if (
            run["provenance"]["pipeline_profile_id"] != profile["profile_id"]
            or run["provenance"]["pipeline_profile_sha256"]
            != profile["profile_sha256"]
        ):
            raise ValidationError(f"oracle {label} run profile differs from the plan")
        if (
            configuration["timeout_policy"] != "initial"
            or configuration["baseline_timeout_run_sha256"] is not None
            or configuration["baseline_timeout_run_id"] is not None
            or any(case.get("timeout_derivation") is not None for case in run["cases"])
        ):
            raise ValidationError(
                f"oracle {label} run requires exact initial timeout evidence "
                "without baseline derivations"
            )
        _require_formal_measurement(run, require_accela_pipeline=True)
    _require_common_binding(
        baseline,
        optimized,
        label="oracle optimized run",
        require_execution_environment=False,
    )
    if baseline["configuration"] != optimized["configuration"]:
        raise ValidationError("oracle source legs require identical run configuration")
    primary = metric_spec(baseline)
    if primary != metric_spec(optimized):
        raise ValidationError("oracle source legs differ in primary metric")
    rows = _oracle_pair_rows(plan, baseline, optimized)
    results: list[dict[str, Any]] = []
    family_ids = [
        descriptor["cleanroom_oracle_family_id"]
        for descriptor in evidence["candidates"]
    ]
    if any(family_id is None for family_id in family_ids) or len(family_ids) != len(
        set(family_ids)
    ):
        raise ValidationError(
            "all eleven candidate families require distinct cleanroom Oracle family ids"
        )
    if set(family_ids) != {row["family"] for row in rows}:
        raise ValidationError(
            "candidate cleanroom family mapping differs from the 99-pair Oracle plan"
        )
    for descriptor in evidence["candidates"]:
        family_id = descriptor["cleanroom_oracle_family_id"]
        assert family_id is not None
        selected = [
            row for row in rows if row["family"] == family_id
        ]
        structure_ids = sorted({row["structure_id"] for row in selected})
        if len(selected) != 9 or len(structure_ids) != 3:
            raise ValidationError(
                f"candidate Oracle family lacks exactly 3 structures x 3 sizes: {family_id}"
            )
        structures: list[dict[str, Any]] = []
        for structure_id in structure_ids:
            structure_rows = [
                row for row in selected if row["structure_id"] == structure_id
            ]
            by_size = {row["size"]: row for row in structure_rows}
            if len(structure_rows) != 3 or set(by_size) != {
                "small",
                "medium",
                "large",
            }:
                raise ValidationError(
                    f"candidate Oracle structure lacks one pair per size: {family_id}:{structure_id}"
                )
            reason = (
                None
                if all(row["eligible_for_ranking"] for row in structure_rows)
                else "incomplete_or_incorrect_oracle"
            )
            structures.append(
                {
                    "structure_id": structure_id,
                    "sizes": {
                        size: by_size[size]
                        for size in ("small", "medium", "large")
                    },
                    "paired_datasets": 3,
                    "eligible_for_ranking": reason is None,
                    "ineligibility_reason": reason,
                    "geometric_mean_upper_bound": (
                        weighted_geometric_mean(
                            (float(by_size[size]["speedup"]), 1.0)
                            for size in ("small", "medium", "large")
                        )
                        if reason is None
                        else None
                    ),
                }
            )
        results.append(
            {
                "candidate_id": descriptor["candidate_id"],
                "oracle_family_id": family_id,
                "structures": structures,
            }
        )
    return validate_document(
        {
            "schema_version": "candidate-oracle-capture.v1",
            "capture_id": capture_id,
            "generated_at": _latest_evidence_timestamp(
                [
                    baseline_verified.document["terminal_observed_at"],
                    optimized_verified.document["terminal_observed_at"],
                ]
            ),
            "candidate_evidence_sha256": sha256_json(evidence),
            "oracle_plan_sha256": sha256_json(plan),
            "sources": {
                "candidate_evidence": _frozen_artifact_digest(
                    root,
                    candidate_evidence_path,
                    evidence,
                    label="candidate Oracle evidence source",
                ),
                "oracle_plan": _frozen_artifact_digest(
                    root,
                    oracle_plan_path,
                    plan,
                    label="candidate Oracle plan source",
                ),
            },
            "evidence_class": "cleanroom",
            "pair_count": 99,
            "bindings": _binding_record(baseline),
            "baseline": _run_ref(baseline),
            "optimized": _run_ref(optimized),
            "raw_state_root": relative_state_root.as_posix(),
            "raw_evidence": {
                "baseline": {
                    "run_record_path": relative_baseline.as_posix(),
                    **_raw_run_ref(baseline_verified),
                },
                "optimized": {
                    "run_record_path": relative_optimized.as_posix(),
                    **_raw_run_ref(optimized_verified),
                },
            },
            "primary_metric_id": baseline["configuration"]["primary_metric_id"],
            "metric_unit": primary["unit"],
            "candidates": results,
        }
    )


def _load_and_reverify_candidate_oracle_capture(
    *,
    capture_path: Path,
    workspace_root: Path,
    raw_evidence_verifier: Any | None = None,
) -> dict[str, Any]:
    root = _candidate_workspace_root(workspace_root)
    capture = _load_version(
        capture_path,
        "candidate-oracle-capture.v1",
        label="candidate Oracle capture",
    )
    evidence_artifact = capture["sources"]["candidate_evidence"]
    plan_artifact = capture["sources"]["oracle_plan"]
    _load_frozen_artifact(
        root,
        evidence_artifact,
        label="candidate Oracle evidence source",
        version="candidate-evidence.v1",
    )
    _load_frozen_artifact(
        root,
        plan_artifact,
        label="candidate Oracle plan source",
        version="oracle-plan.v1",
    )
    expected = capture_candidate_oracle(
        candidate_evidence_path=root / evidence_artifact["path"],
        oracle_plan_path=root / plan_artifact["path"],
        baseline_path=root
        / capture["raw_evidence"]["baseline"]["run_record_path"],
        optimized_path=root
        / capture["raw_evidence"]["optimized"]["run_record_path"],
        state_root=root / capture["raw_state_root"],
        workspace_root=root,
        capture_id=capture["capture_id"],
        raw_evidence_verifier=raw_evidence_verifier,
    )
    if expected != capture:
        raise ValidationError(
            "candidate Oracle capture differs from replayed source-leg raw evidence"
        )
    return capture


def build_candidate_campaign_plan(
    *,
    catalog_path: Path,
    pass_registry_path: Path,
    matrix_path: Path,
    screening_path: Path,
    suite_paths: Mapping[str, Path],
    measurement_protocol_path: Path,
    hotblock_measurement_protocol_path: Path,
    reference_toolchain_path: Path,
    compiler_artifact_path: Path,
    raw_state_root: Path,
    workspace_root: Path,
    campaign_id: str,
    _raw_snapshot_cache: _ReadOnlyRawEvidenceCache | None = None,
) -> dict[str, Any]:
    """Build the single authoritative B1-through-final candidate task DAG."""

    raw_snapshot_cache = (
        _ReadOnlyRawEvidenceCache()
        if _raw_snapshot_cache is None
        else _raw_snapshot_cache
    )
    catalog = _load_version(
        catalog_path, "candidate-catalog.v1", label="candidate registry"
    )
    pass_registry = _load_version(
        pass_registry_path, "pass-registry.v2", label="pass registry"
    )
    matrix = _load_version(
        matrix_path, "candidate-profile-matrix.v1", label="candidate profile matrix"
    )
    screening = _load_and_reverify_candidate_screening(
        screening_path=screening_path,
        workspace_root=workspace_root,
        raw_evidence_verifier=raw_snapshot_cache.verify,
    )
    measurement_protocol = _load_version(
        measurement_protocol_path,
        "measurement-protocol.v1",
        label="candidate standard measurement protocol",
    )
    hotblock_measurement_protocol = _load_version(
        hotblock_measurement_protocol_path,
        "measurement-protocol.v1",
        label="candidate cache/hotblock measurement protocol",
    )
    if measurement_protocol["measurement_mode"] != "standard_proxy":
        raise ValidationError("candidate formal DAG requires standard_proxy protocol")
    if hotblock_measurement_protocol["measurement_mode"] != "cache_hotblock":
        raise ValidationError("candidate formal DAG requires cache_hotblock protocol")
    environment = build_campaign_environment_contract(
        measurement_protocol_path=measurement_protocol_path,
        hotblock_measurement_protocol_path=hotblock_measurement_protocol_path,
        reference_toolchain_path=reference_toolchain_path,
        workspace_root=workspace_root,
        include_candidate_workspace_bootstrap=True,
    )
    _require_executable_registry_bridge(
        screening=screening,
        catalog=catalog,
        executable_registry=pass_registry,
        workspace_root=workspace_root,
    )
    registry_sha256 = sha256_json(catalog)
    pass_registry_sha256 = sha256_json(pass_registry)
    if (
        matrix["candidate_registry_sha256"] != registry_sha256
        or matrix["pass_registry_sha256"] != pass_registry_sha256
    ):
        raise ValidationError("candidate campaign matrix/registry binding differs")
    qualified_ids = [
        item["implementation_candidate_id"]
        for item in screening["candidates"]
        if item["qualification_status"] == "qualified"
    ]
    assert all(item is not None for item in qualified_ids)
    candidate_ids = [item["candidate_id"] for item in catalog["candidates"]]
    if candidate_ids != qualified_ids:
        raise ValidationError(
            "candidate campaign registry must contain exactly the qualified screening candidates"
        )
    profiles = _verify_matrix_profiles(
        workspace_root,
        matrix,
        candidate_order=candidate_ids,
    )
    if any(
        item["kind"] == "pair"
        for item in [*matrix["profiles"], *matrix["schedule"]]
    ):
        raise ValidationError(
            "pre-B3 formal candidate matrix must be singles-only; pairs belong to the B3-bound diagnostic child plan"
        )
    single_profiles = {
        item["candidate_ids"][0]: profiles[item["candidate_profile_id"]]
        for item in matrix["schedule"]
        if item["kind"] == "single"
    }
    if set(single_profiles) != set(candidate_ids):
        raise ValidationError("candidate campaign matrix lacks an exact single profile set")
    if set(suite_paths) != set(_CANDIDATE_SUITE_CASE_COUNTS):
        raise ConfigurationError("candidate campaign plan requires exactly B1 through B6 manifests")
    suites: list[dict[str, Any]] = []
    for role, case_count in _CANDIDATE_SUITE_CASE_COUNTS.items():
        manifest = _load_version(
            suite_paths[role],
            "benchmark-manifest.v1",
            label=f"candidate {role} manifest",
        )
        require_formal_suite_contract(
            role=role, manifest=manifest, manifest_path=suite_paths[role]
        )
        suites.append(
            {
                "data_role": role,
                "suite_id": manifest["suite_id"],
                "manifest": _frozen_artifact_digest(
                    workspace_root,
                    suite_paths[role],
                    manifest,
                    label=f"candidate {role} manifest",
                ),
                "case_count": case_count,
            }
        )

    tasks: list[dict[str, Any]] = []

    def add_task(
        *,
        task_id: str,
        task_type: str,
        stage: str,
        kind: str,
        candidate_ids_for_task: list[str],
        data_role: str | None,
        dependencies: list[str],
        terminal_dependencies: list[str],
        gate_kind: str = "always",
        gate_candidate_id: str | None = None,
        profile: Mapping[str, Any] | None = None,
        reference_profile_id: str | None = None,
        ranking_evidence: bool = False,
    ) -> None:
        tasks.append(
            {
                "task_id": task_id,
                "task_type": task_type,
                "stage": stage,
                "kind": kind,
                "candidate_ids": candidate_ids_for_task,
                "data_role": data_role,
                "run_id": (
                    f"{campaign_id}:{task_id}" if task_type == "run" else None
                ),
                "logical_profile_id": (
                    profile["profile_id"] if profile is not None else None
                ),
                "candidate_profile_sha256": (
                    profile["profile_sha256"] if profile is not None else None
                ),
                "candidate_profile_path": (
                    profile["path"] if profile is not None else None
                ),
                "reference_profile_id": reference_profile_id,
                "dependencies": dependencies,
                "terminal_dependencies": terminal_dependencies,
                "gate": {
                    "kind": gate_kind,
                    "candidate_id": gate_candidate_id,
                },
                "ranking_evidence": ranking_evidence,
            }
        )

    baseline_profile = profiles["candidate-empty"]
    add_task(
        task_id="run.B1.full",
        task_type="run",
        stage="B1",
        kind="candidate_empty",
        candidate_ids_for_task=[],
        data_role="B1",
        dependencies=[],
        terminal_dependencies=[],
        profile=baseline_profile,
    )
    previous_b1: str | None = None
    for candidate_id in candidate_ids:
        task_id = f"run.B1.{candidate_id}"
        add_task(
            task_id=task_id,
            task_type="run",
            stage="B1",
            kind="single",
            candidate_ids_for_task=[candidate_id],
            data_role="B1",
            dependencies=["run.B1.full"],
            terminal_dependencies=([] if previous_b1 is None else [previous_b1]),
            profile=single_profiles[candidate_id],
        )
        previous_b1 = task_id
    b1_tasks = [f"run.B1.{item}" for item in candidate_ids]
    add_task(
        task_id="run.B2.full",
        task_type="run",
        stage="B2",
        kind="candidate_empty",
        candidate_ids_for_task=[],
        data_role="B2",
        dependencies=["run.B1.full"],
        terminal_dependencies=b1_tasks,
        profile=baseline_profile,
    )
    previous_b2: str | None = None
    for candidate_id in candidate_ids:
        task_id = f"run.B2.{candidate_id}"
        add_task(
            task_id=task_id,
            task_type="run",
            stage="B2",
            kind="single",
            candidate_ids_for_task=[candidate_id],
            data_role="B2",
            dependencies=["run.B2.full"],
            terminal_dependencies=[
                f"run.B1.{candidate_id}",
                *([] if previous_b2 is None else [previous_b2]),
            ],
            gate_kind="b1_completed",
            gate_candidate_id=candidate_id,
            profile=single_profiles[candidate_id],
        )
        previous_b2 = task_id
    add_task(
        task_id="study.B2",
        task_type="study",
        stage="B2",
        kind="study",
        candidate_ids_for_task=[],
        data_role="B2",
        dependencies=["run.B2.full"],
        terminal_dependencies=[f"run.B2.{item}" for item in candidate_ids],
    )
    add_task(
        task_id="freeze",
        task_type="freeze",
        stage="freeze",
        kind="freeze",
        candidate_ids_for_task=[],
        data_role=None,
        dependencies=["study.B2"],
        terminal_dependencies=[],
        gate_kind="b2_formal_complete",
    )
    add_task(
        task_id="run.B3.full",
        task_type="run",
        stage="B3",
        kind="candidate_empty",
        candidate_ids_for_task=[],
        data_role="B3",
        dependencies=["freeze"],
        terminal_dependencies=[],
        profile=baseline_profile,
        ranking_evidence=True,
    )
    previous_b3_profile = "run.B3.full"
    for key, reference_profile_id in (
        ("gcc", "gcc-13.3-o2"),
        ("clang", "clang-18-o3"),
    ):
        task_id = f"run.B3.{key}"
        add_task(
            task_id=task_id,
            task_type="run",
            stage="B3",
            kind="reference",
            candidate_ids_for_task=[],
            data_role="B3",
            dependencies=[
                "freeze", *(["run.B3.full"] if key == "gcc" else [])
            ],
            terminal_dependencies=(
                [] if key == "gcc" else [previous_b3_profile]
            ),
            reference_profile_id=reference_profile_id,
            ranking_evidence=False,
        )
        previous_b3_profile = task_id
    for candidate_id in candidate_ids:
        task_id = f"run.B3.{candidate_id}"
        add_task(
            task_id=task_id,
            task_type="run",
            stage="B3",
            kind="single",
            candidate_ids_for_task=[candidate_id],
            data_role="B3",
            dependencies=["freeze", "run.B3.full"],
            terminal_dependencies=[
                f"run.B1.{candidate_id}", previous_b3_profile
            ],
            gate_kind="b1_completed",
            gate_candidate_id=candidate_id,
            profile=single_profiles[candidate_id],
            ranking_evidence=True,
        )
        previous_b3_profile = task_id
    add_task(
        task_id="study.B3",
        task_type="study",
        stage="B3",
        kind="study",
        candidate_ids_for_task=[],
        data_role="B3",
        dependencies=["run.B3.full"],
        terminal_dependencies=[
            "run.B3.gcc",
            "run.B3.clang",
            *[f"run.B3.{item}" for item in candidate_ids],
        ],
    )
    previous_validation_study = "study.B3"
    for role in ("B4", "B5", "B6"):
        add_task(
            task_id=f"run.{role}.full",
            task_type="run",
            stage=role,
            kind="candidate_empty",
            candidate_ids_for_task=[],
            data_role=role,
            dependencies=[previous_validation_study],
            terminal_dependencies=[],
            gate_kind="b3_has_promoted",
            profile=baseline_profile,
            ranking_evidence=True,
        )
        previous_profile: str | None = None
        for candidate_id in candidate_ids:
            task_id = f"run.{role}.{candidate_id}"
            add_task(
                task_id=task_id,
                task_type="run",
                stage=role,
                kind="single",
                candidate_ids_for_task=[candidate_id],
                data_role=role,
                dependencies=["study.B3", f"run.{role}.full"],
                terminal_dependencies=(
                    [] if previous_profile is None else [previous_profile]
                ),
                gate_kind="b3_promoted",
                gate_candidate_id=candidate_id,
                profile=single_profiles[candidate_id],
                ranking_evidence=True,
            )
            previous_profile = task_id
        add_task(
            task_id=f"study.{role}",
            task_type="study",
            stage=role,
            kind="study",
            candidate_ids_for_task=[],
            data_role=role,
            dependencies=[f"run.{role}.full"],
            terminal_dependencies=[f"run.{role}.{item}" for item in candidate_ids],
            gate_kind="b3_has_promoted",
        )
        previous_validation_study = f"study.{role}"
    add_task(
        task_id="final",
        task_type="final",
        stage="final",
        kind="final",
        candidate_ids_for_task=[],
        data_role=None,
        dependencies=["study.B3"],
        terminal_dependencies=["study.B4", "study.B5", "study.B6"],
    )
    root = _candidate_workspace_root(workspace_root)
    _, raw_state_physical, raw_state_relative = _workspace_directory_path(
        root, raw_state_root, label="candidate raw evidence state root"
    )
    oracle_capture = _load_frozen_artifact(
        root,
        screening["sources"]["oracle_capture"],
        label="candidate campaign screening Oracle capture",
        version="candidate-oracle-capture.v1",
    )
    _, oracle_state_physical, _ = _workspace_directory_path(
        root,
        root / oracle_capture["raw_state_root"],
        label="candidate campaign screening Oracle raw state root",
    )
    _require_disjoint_candidate_raw_namespaces(
        candidate_state_root=raw_state_physical,
        oracle_state_root=oracle_state_physical,
    )
    _require_git_ignored_path(
        workspace_root=root,
        path=raw_state_physical,
        label="candidate raw evidence state root",
    )
    base_profile_path = root / baseline_profile["path"]
    repo_commit, repo_tree = _clean_repository_identity(root)
    compiler_artifact = _frozen_compiler_artifact(
        root, compiler_artifact_path
    )
    protocol_documents = {
        "standard_proxy": (measurement_protocol_path, measurement_protocol),
        "cache_hotblock": (
            hotblock_measurement_protocol_path,
            hotblock_measurement_protocol,
        ),
    }
    measurement_protocols: dict[str, dict[str, Any]] = {}
    for mode, (protocol_path, protocol_document) in protocol_documents.items():
        artifact = _frozen_artifact_digest(
            root,
            protocol_path,
            protocol_document,
            label=f"candidate {mode} measurement protocol",
        )
        contract = dict(environment["measurement_protocols"][mode])
        if artifact["canonical_sha256"] != contract["protocol_sha256"]:
            raise ValidationError(
                f"candidate campaign {mode} protocol changed while freezing"
            )
        contract["path"] = artifact["path"]
        contract["physical_sha256"] = artifact["physical_sha256"]
        measurement_protocols[mode] = contract
    toolchain_document = read_json(reference_toolchain_path)
    if not isinstance(toolchain_document, dict):
        raise ValidationError("candidate reference toolchain must be a JSON object")
    toolchain_contract = dict(environment["reference_toolchain"])
    expected_toolchain_physical = toolchain_contract.pop("snapshot_sha256")
    toolchain_artifact = _frozen_artifact_digest(
        root,
        reference_toolchain_path,
        toolchain_document,
        label="candidate reference toolchain",
    )
    if toolchain_artifact["physical_sha256"] != expected_toolchain_physical:
        raise ValidationError("candidate reference toolchain physical hash differs")
    toolchain_contract["snapshot"] = toolchain_artifact
    candidate_environment = {
        **environment,
        "measurement_protocols": measurement_protocols,
        "reference_toolchain": toolchain_contract,
    }
    executable_pass_registry_artifact = _frozen_artifact_digest(
        workspace_root,
        pass_registry_path,
        pass_registry,
        label="candidate executable PassRegistry v2",
    )
    if (
        executable_pass_registry_artifact["path"]
        == screening["base_pass_registry"]["path"]
    ):
        raise ValidationError(
            "candidate executable PassRegistry must use a new artifact path"
        )
    plan = validate_document(
        {
            "schema_version": "candidate-campaign-plan.v1",
            "campaign_id": campaign_id,
            "run_namespace": f"{campaign_id}:",
            "repository": {
                "repo_commit": repo_commit,
                "repo_tree": repo_tree,
                "compiler_artifact": compiler_artifact,
            },
            "measurement_protocols": measurement_protocols,
            "reference_toolchain": toolchain_contract,
            "analyzer": environment["analyzer"],
            "execution_environment_sha256": candidate_execution_environment_sha256(
                environment=candidate_environment,
                compiler_artifact_sha256=compiler_artifact["physical_sha256"],
            ),
            "artifacts": {
                "candidate_registry": _frozen_artifact_digest(
                    workspace_root, catalog_path, catalog, label="candidate registry"
                ),
                "executable_pass_registry": executable_pass_registry_artifact,
                "screening_base_pass_registry": screening[
                    "base_pass_registry"
                ],
                "matrix": _frozen_artifact_digest(
                    workspace_root, matrix_path, matrix, label="candidate matrix"
                ),
                "screening": _frozen_artifact_digest(
                    workspace_root,
                    screening_path,
                    screening,
                    label="candidate screening",
                ),
            },
            "run_record_schema_sha256": schema_sha256("run-record.v1"),
            "candidate_study_schema_sha256": schema_sha256("candidate-study.v1"),
            "candidate_freeze_schema_sha256": schema_sha256("candidate-freeze.v1"),
            "candidate_raw_evidence_schema_sha256": schema_sha256(
                "candidate-raw-evidence.v1"
            ),
            "raw_state_root": raw_state_relative.as_posix(),
            "base_pipeline_profile": {
                **matrix["base_pipeline_profile"],
                "path": baseline_profile["path"],
                "physical_sha256": sha256_file(base_profile_path),
            },
            "suites": suites,
            "study_ids": {
                role: f"{campaign_id}:study:{role}"
                for role in ("B2", "B3", "B4", "B5", "B6")
            },
            "qualified_candidate_ids": candidate_ids,
            "tasks": tasks,
        }
    )
    raw_snapshot_cache.assert_unchanged()
    return plan


def _verify_candidate_plan_execution_environment(
    plan: Mapping[str, Any], *, workspace_root: Path
) -> dict[str, Any]:
    """Rebuild and compare every physical input covered by the plan hash."""

    root = _candidate_workspace_root(workspace_root)
    snapshot_artifact = plan["reference_toolchain"]["snapshot"]
    _load_frozen_artifact(
        root,
        snapshot_artifact,
        label="candidate campaign reference toolchain",
    )
    protocol_paths: dict[str, Path] = {}
    for mode in ("standard_proxy", "cache_hotblock"):
        relative_path = plan["measurement_protocols"][mode]["path"]
        validate_relative_path(
            relative_path,
            label=f"candidate campaign {mode} protocol path",
        )
        protocol_paths[mode] = root / relative_path
    environment = build_campaign_environment_contract(
        measurement_protocol_path=protocol_paths["standard_proxy"],
        hotblock_measurement_protocol_path=protocol_paths["cache_hotblock"],
        reference_toolchain_path=root / snapshot_artifact["path"],
        workspace_root=root,
        include_candidate_workspace_bootstrap=True,
    )
    measurement_protocols: dict[str, dict[str, Any]] = {}
    for mode, protocol_path in protocol_paths.items():
        contract = dict(environment["measurement_protocols"][mode])
        contract["path"] = plan["measurement_protocols"][mode]["path"]
        contract["physical_sha256"] = sha256_file(
            resolve_without_symlinks(
                protocol_path,
                label=f"candidate campaign {mode} measurement protocol",
            )
        )
        measurement_protocols[mode] = contract
    normalized_toolchain = dict(environment["reference_toolchain"])
    snapshot_physical_sha256 = normalized_toolchain.pop("snapshot_sha256")
    normalized_toolchain["snapshot"] = snapshot_artifact
    if (
        snapshot_physical_sha256 != snapshot_artifact["physical_sha256"]
        or normalized_toolchain != plan["reference_toolchain"]
        or environment["run_record_schema_sha256"]
        != plan["run_record_schema_sha256"]
        or measurement_protocols != plan["measurement_protocols"]
        or environment["analyzer"] != plan["analyzer"]
    ):
        raise ValidationError("candidate campaign execution environment has drifted")
    candidate_environment = {
        **environment,
        "measurement_protocols": measurement_protocols,
        "reference_toolchain": normalized_toolchain,
    }
    expected_sha256 = candidate_execution_environment_sha256(
        environment=candidate_environment,
        compiler_artifact_sha256=plan["repository"]["compiler_artifact"][
            "physical_sha256"
        ],
    )
    if plan["execution_environment_sha256"] != expected_sha256:
        raise ValidationError("candidate campaign execution environment hash differs")
    return candidate_environment


def _require_candidate_analyzer_binding(
    *,
    contract: Mapping[str, Any],
    run: Mapping[str, Any],
    toolchain: str | None,
    label: str,
) -> None:
    expected = (
        None
        if toolchain is None
        else candidate_analyzer_stage(contract, toolchain=toolchain)
    )
    if run["configuration"].get("analyzer") != expected:
        raise ValidationError(f"{label} analyzer contract differs")


def _candidate_task_analyzer_toolchain(task: Mapping[str, Any]) -> str | None:
    if task["data_role"] == "B1":
        return None
    if task["kind"] != "reference":
        return "accela"
    reference_toolchains = {
        "gcc-13.3-o2": "gcc",
        "clang-18-o3": "clang",
    }
    toolchain = reference_toolchains.get(task["reference_profile_id"])
    if toolchain is None:
        raise ValidationError("candidate reference analyzer profile is unknown")
    return toolchain


def _formal_candidate_stage(
    *, kind: str, command: Sequence[str]
) -> dict[str, Any]:
    return {
        "kind": kind,
        "adapter": "host",
        "command_sha256": sha256_json(
            {"command": list(command), "environment": {}}
        ),
        "executable": "sh",
        "environment_keys": [],
    }


def _candidate_compiler_stage() -> dict[str, Any]:
    return _formal_candidate_stage(
        kind="benchmark-compiler",
        command=_FORMAL_CANDIDATE_COMPILER_COMMAND,
    )


def _require_candidate_runtime_stages(
    *,
    run: Mapping[str, Any],
    data_role: str,
    protocol: Mapping[str, Any] | None,
    label: str,
) -> None:
    configuration = run["configuration"]
    if configuration.get("linker") != _formal_candidate_stage(
        kind="external", command=_FORMAL_CANDIDATE_LINKER_COMMAND
    ):
        raise ValidationError(f"{label} linker contract differs")
    runner = configuration.get("runner")
    if data_role == "B1":
        expected_runner = _formal_candidate_stage(
            kind="qemu",
            command=_FORMAL_CANDIDATE_CORRECTNESS_RUNNER_COMMAND,
        )
        if protocol is not None or runner != expected_runner:
            raise ValidationError(f"{label} correctness runner contract differs")
        return
    expected_environment_keys = [
        "QEMU_CACHE_PLUGIN",
        *(
            ["QEMU_HOTBLOCKS_PLUGIN"]
            if protocol is not None
            and protocol["measurement_mode"] == "cache_hotblock"
            else []
        ),
        "QEMU_PROFILE_PLUGIN",
        "QEMU_SYSTEM_RISCV64",
    ]
    if protocol is None or not isinstance(runner, Mapping) or (
        runner.get("kind") != "qemu"
        or runner.get("adapter") != protocol["runner_adapter"]
        or runner.get("command_sha256") != protocol["runner_command_sha256"]
        or runner.get("executable") != "sh"
        or runner.get("environment_keys") != expected_environment_keys
    ):
        raise ValidationError(f"{label} proxy runner contract differs")


def _require_candidate_result_contract(
    run: Mapping[str, Any], *, label: str
) -> None:
    configuration = run["configuration"]
    if (
        configuration.get("output_contract") != "lf_return_trailer"
        or configuration.get("result_file_sha256") is not None
        or configuration.get("environment_label") != "proxy"
    ):
        raise ValidationError(
            f"{label} result contract differs from the frozen proxy contract"
        )


def _require_candidate_cache_metric_extension(
    run: Mapping[str, Any], *, label: str
) -> None:
    preset = rv64gc_qemu_v1()
    base_ids = {preset["primary_metric_id"]} | {
        item["metric_id"] for item in preset["additional"]
    }
    expected = {
        item["metric_id"]: {
            "metric_id": item["metric_id"],
            "source": item["source"],
            "pattern_sha256": (
                None if item["pattern"] is None else sha256_json(item["pattern"])
            ),
            "unit": item["unit"],
        }
        for item in cache_hotblock_metrics_v1()
    }
    observed = {
        item["metric_id"]: item
        for item in run["configuration"]["metrics"]
        if item["metric_id"] not in base_ids
    }
    if observed != expected:
        raise ValidationError(f"{label} cache/hotblock metric extension differs")


def _candidate_timeout_baseline_task_id(task: Mapping[str, Any]) -> str | None:
    task_id = task["task_id"]
    if task_id.startswith("diagnostic."):
        if task["kind"] == "pair":
            return "run.B3.full"
        if task["kind"] != "cache_hotblock":
            raise ValidationError("candidate diagnostic timeout task kind is unknown")
        return None if not task["candidate_ids"] else "diagnostic.cache.full"
    if (
        task["data_role"] == "B1"
        or task["kind"] in {"candidate_empty", "reference"}
    ):
        return None
    return f"run.{task['data_role']}.full"


def _require_candidate_timeout_configuration(
    *,
    task: Mapping[str, Any],
    run: Mapping[str, Any],
    baseline_run: Mapping[str, Any] | None,
    label: str,
) -> None:
    expected_baseline_task_id = _candidate_timeout_baseline_task_id(task)
    configuration = run["configuration"]
    if any(
        not math.isclose(configuration[field], expected, rel_tol=0, abs_tol=1e-12)
        for field, expected in (
            ("run_timeout_seconds", 1800.0),
            ("timeout_minimum_seconds", 120.0),
            ("timeout_multiplier", 3.0),
            ("timeout_cap_seconds", 1800.0),
        )
    ):
        raise ValidationError(f"{label} timeout constants differ")
    if expected_baseline_task_id is None:
        if (
            baseline_run is not None
            or configuration["timeout_policy"] != "initial"
            or configuration["baseline_timeout_run_id"] is not None
            or configuration["baseline_timeout_run_sha256"] is not None
        ):
            raise ValidationError(f"{label} requires canonical initial timeouts")
        return
    if baseline_run is None:
        raise ValidationError(
            f"{label} lacks the authorized timeout baseline {expected_baseline_task_id}"
        )
    if (
        baseline_run["run_id"] is None
        or configuration["timeout_policy"] != "baseline_derived"
        or configuration["baseline_timeout_run_id"] != baseline_run["run_id"]
        or configuration["baseline_timeout_run_sha256"]
        != sha256_json(baseline_run)
        or baseline_run["state"] != "completed"
        or any(case["status"] != "passed" for case in baseline_run["cases"])
    ):
        raise ValidationError(
            f"{label} timeout baseline identity/state differs from {expected_baseline_task_id}"
        )


def _require_candidate_compiler_binding(
    *,
    plan: Mapping[str, Any],
    task: Mapping[str, Any],
    run: Mapping[str, Any],
    label: str,
) -> None:
    if task.get("kind") == "reference":
        _require_frozen_reference_run(run, task, plan)
        return
    if (
        run["configuration"].get("compiler") != _candidate_compiler_stage()
        or run["provenance"]["compiler_artifact_sha256"]
        != plan["repository"]["compiler_artifact"]["physical_sha256"]
    ):
        raise ValidationError(f"{label} compiler contract differs")


def _require_candidate_tool_versions(
    *,
    plan: Mapping[str, Any],
    task: Mapping[str, Any],
    run: Mapping[str, Any],
    label: str,
) -> None:
    expected = dict(plan["reference_toolchain"]["common_tool_versions"])
    if task.get("kind") == "reference":
        reference = next(
            (
                item
                for item in plan["reference_toolchain"]["baselines"]
                if item["profile_id"] == task["reference_profile_id"]
            ),
            None,
        )
        if reference is None:
            raise ValidationError(f"{label} lacks a frozen reference baseline")
        expected[reference["tool"]] = reference["version"]
    else:
        expected["accela-jdk"] = plan["reference_toolchain"][
            "accela_jdk_version"
        ]
    observed = {
        item["tool"]: item for item in run["configuration"]["tool_versions"]
    }
    if set(observed) != set(expected) or any(
        observed[tool]
        != {
            "tool": tool,
            "actual": version,
            "official_expected": version,
            "comparison": "exact",
        }
        for tool, version in expected.items()
    ):
        raise ValidationError(f"{label} tool versions differ from the frozen snapshot")


def _validate_campaign_run_static(
    plan: Mapping[str, Any],
    task: Mapping[str, Any],
    run: Mapping[str, Any],
    *,
    profiles: Mapping[str, Mapping[str, Any]],
    baseline_run: Mapping[str, Any] | None = None,
) -> None:
    if run["run_id"] != task["run_id"]:
        raise ValidationError(f"campaign task run id differs: {task['task_id']}")
    if (
        run["provenance"].get("execution_environment_sha256")
        != plan["execution_environment_sha256"]
        or run["provenance"]["repo_commit"]
        != plan["repository"]["repo_commit"]
        or run["provenance"]["repo_dirty"]
        or run["provenance"]["tracked_diff_sha256"] is not None
    ):
        raise ValidationError(
            f"campaign task repository/execution environment differs: {task['task_id']}"
        )
    suite = next(
        item for item in plan["suites"] if item["data_role"] == task["data_role"]
    )
    if (
        run["suite_id"] != suite["suite_id"]
        or run["manifest_sha256"] != suite["manifest"]["canonical_sha256"]
    ):
        raise ValidationError(f"campaign task suite/manifest differs: {task['task_id']}")
    _require_candidate_campaign_protocol_binding(plan=plan, task=task, run=run)
    _require_candidate_run_protocol_configuration(
        run, data_role=task["data_role"]
    )
    _require_candidate_result_contract(
        run, label=f"campaign task {task['task_id']}"
    )
    _require_candidate_runtime_stages(
        run=run,
        data_role=task["data_role"],
        protocol=(
            None
            if task["data_role"] == "B1"
            else plan["measurement_protocols"]["standard_proxy"]
        ),
        label=f"campaign task {task['task_id']}",
    )
    _require_candidate_timeout_configuration(
        task=task,
        run=run,
        baseline_run=baseline_run,
        label=f"campaign task {task['task_id']}",
    )
    if task["data_role"] != "B1":
        require_formal_measurement_configuration(
            run,
            require_accela_pipeline=task["kind"] != "reference",
        )
    expected_evidence = "qemu_correctness" if task["data_role"] == "B1" else "qemu_proxy"
    if run["configuration"]["evidence_level"] != expected_evidence:
        raise ValidationError(
            f"campaign task evidence level differs: {task['task_id']}"
        )
    _require_candidate_analyzer_binding(
        contract=plan["analyzer"],
        run=run,
        toolchain=_candidate_task_analyzer_toolchain(task),
        label=f"campaign task {task['task_id']}",
    )
    _require_candidate_compiler_binding(
        plan=plan,
        task=task,
        run=run,
        label=f"campaign task {task['task_id']}",
    )
    _require_candidate_tool_versions(
        plan=plan,
        task=task,
        run=run,
        label=f"campaign task {task['task_id']}",
    )
    if task["data_role"] == "B1":
        _require_candidate_correctness_configuration(
            run, label=f"campaign B1 task {task['task_id']}"
        )
    if task["kind"] == "reference":
        if (
            run["provenance"]["pipeline_profile_id"]
            != task["reference_profile_id"]
            or run["configuration"].get("enabled_candidate_ids", [])
        ):
            raise ValidationError(
                f"campaign reference task profile differs: {task['task_id']}"
            )
        return
    _require_candidate_configuration(
        run,
        registry_sha256=plan["artifacts"]["candidate_registry"]["canonical_sha256"],
        pass_registry_sha256=plan["artifacts"]["executable_pass_registry"]["canonical_sha256"],
        enabled_candidate_ids=task["candidate_ids"],
        profile=profiles[task["logical_profile_id"]],
        label=f"campaign task {task['task_id']}",
    )


def _validate_campaign_run(
    plan: Mapping[str, Any],
    task: Mapping[str, Any],
    run: Mapping[str, Any],
    *,
    profiles: Mapping[str, Mapping[str, Any]],
    baseline_run: Mapping[str, Any] | None = None,
) -> None:
    _validate_campaign_run_static(
        plan,
        task,
        run,
        profiles=profiles,
        baseline_run=baseline_run,
    )
    _require_candidate_run_protocol(run, data_role=task["data_role"])
    if task["data_role"] == "B1":
        _require_candidate_correctness_run(
            run, label=f"campaign B1 task {task['task_id']}"
        )


def _require_frozen_reference_run(
    run: Mapping[str, Any],
    task: Mapping[str, Any],
    freeze: Mapping[str, Any],
) -> None:
    reference = next(
        (
            item
            for item in freeze["reference_toolchain"]["baselines"]
            if item["profile_id"] == task["reference_profile_id"]
        ),
        None,
    )
    if reference is None:
        raise ValidationError("candidate reference run lacks a frozen toolchain baseline")
    compiler = run["configuration"]["compiler"]
    versions = {
        item["tool"]: item for item in run["configuration"]["tool_versions"]
    }
    observed = versions.get(reference["tool"])
    if (
        compiler
        != {
            "kind": "external",
            "adapter": "host",
            "command_sha256": reference["compiler_command_sha256"],
            "executable": reference["compiler_executable"],
            "environment_keys": [],
        }
        or run["provenance"]["compiler_artifact_sha256"]
        != freeze["reference_toolchain"]["snapshot"]["physical_sha256"]
        or run["provenance"]["pipeline_profile_sha256"]
        != reference["profile_sha256"]
        or observed is None
        or observed["actual"] != reference["version"]
        or observed["official_expected"] != reference["version"]
        or observed["comparison"] != "exact"
    ):
        raise ValidationError(
            f"candidate reference run differs from frozen {reference['compiler_baseline']} contract"
        )


def _require_candidate_campaign_protocol_binding(
    *,
    plan: Mapping[str, Any],
    task: Mapping[str, Any],
    run: Mapping[str, Any],
) -> None:
    provenance = run["provenance"]
    observed = (
        provenance["measurement_protocol_id"],
        provenance["measurement_protocol_sha256"],
    )
    if task["data_role"] == "B1":
        expected = (None, None)
        label = "B1 correctness"
    else:
        standard_protocol = plan["measurement_protocols"]["standard_proxy"]
        expected = (
            standard_protocol["protocol_id"],
            standard_protocol["protocol_sha256"],
        )
        label = f"{task['data_role']} standard-proxy"
    if observed != expected:
        raise ValidationError(
            f"candidate campaign {label} protocol binding differs: {task['task_id']}"
        )


def _validate_candidate_diagnostic_run_static(
    *,
    task: Mapping[str, Any],
    run: Mapping[str, Any],
    profile: Mapping[str, Any],
    candidate_registry_sha256: str,
    pass_registry_sha256: str,
    b3_suite: Mapping[str, Any],
    freeze: Mapping[str, Any],
    baseline_run: Mapping[str, Any] | None = None,
) -> None:
    protocol_contract = freeze["measurement_protocols"][task["measurement_mode"]]
    if (
        run["run_id"] != task["run_id"]
        or run["suite_id"] != b3_suite["suite_id"]
        or run["manifest_sha256"]
        != b3_suite["manifest"]["canonical_sha256"]
        or run["configuration"]["evidence_level"] != "qemu_proxy"
        or run["provenance"]["repo_commit"]
        != freeze["repository"]["repo_commit"]
        or run["provenance"]["repo_dirty"]
        or run["provenance"]["tracked_diff_sha256"] is not None
        or run["provenance"]["compiler_artifact_sha256"]
        != freeze["repository"]["compiler_artifact"]["physical_sha256"]
        or run["provenance"].get("execution_environment_sha256")
        != freeze["execution_environment_sha256"]
        or run["provenance"]["measurement_protocol_id"]
        != protocol_contract["protocol_id"]
        or run["provenance"]["measurement_protocol_sha256"]
        != protocol_contract["protocol_sha256"]
    ):
        raise ValidationError(
            f"candidate diagnostic run binding differs: {task['task_id']}"
        )
    _require_candidate_analyzer_binding(
        contract=freeze["analyzer"],
        run=run,
        toolchain="accela",
        label=f"candidate diagnostic run {task['task_id']}",
    )
    _require_candidate_tool_versions(
        plan=freeze,
        task=task,
        run=run,
        label=f"candidate diagnostic run {task['task_id']}",
    )
    _require_candidate_compiler_binding(
        plan=freeze,
        task=task,
        run=run,
        label=f"candidate diagnostic run {task['task_id']}",
    )
    _require_candidate_run_protocol_configuration(run, data_role="B3")
    _require_candidate_result_contract(
        run, label=f"candidate diagnostic run {task['task_id']}"
    )
    _require_candidate_runtime_stages(
        run=run,
        data_role="B3",
        protocol=protocol_contract,
        label=f"candidate diagnostic run {task['task_id']}",
    )
    _require_candidate_timeout_configuration(
        task=task,
        run=run,
        baseline_run=baseline_run,
        label=f"candidate diagnostic run {task['task_id']}",
    )
    require_formal_measurement_configuration(
        run,
        require_accela_pipeline=True,
        allow_metric_superset=task["measurement_mode"] == "cache_hotblock",
    )
    if task["measurement_mode"] == "cache_hotblock":
        _require_candidate_cache_metric_extension(
            run,
            label=f"candidate diagnostic run {task['task_id']}",
        )
    _require_candidate_configuration(
        run,
        registry_sha256=candidate_registry_sha256,
        pass_registry_sha256=pass_registry_sha256,
        enabled_candidate_ids=task["candidate_ids"],
        profile=profile,
        label=f"candidate diagnostic run {task['task_id']}",
    )


def _validate_candidate_diagnostic_run(
    *,
    task: Mapping[str, Any],
    run: Mapping[str, Any],
    profile: Mapping[str, Any],
    candidate_registry_sha256: str,
    pass_registry_sha256: str,
    b3_suite: Mapping[str, Any],
    freeze: Mapping[str, Any],
    baseline_run: Mapping[str, Any] | None = None,
) -> None:
    _validate_candidate_diagnostic_run_static(
        task=task,
        run=run,
        profile=profile,
        candidate_registry_sha256=candidate_registry_sha256,
        pass_registry_sha256=pass_registry_sha256,
        b3_suite=b3_suite,
        freeze=freeze,
        baseline_run=baseline_run,
    )
    _require_candidate_run_protocol(run, data_role="B3")


def _require_authorized_regular_path(
    *,
    workspace_root: Path,
    observed_path: Path | None,
    expected_path: str,
    label: str,
) -> Path:
    if observed_path is None:
        raise ValidationError(f"{label} is required")
    _, physical, relative = _workspace_regular_path(
        workspace_root, observed_path, label=label
    )
    if relative.as_posix() != expected_path:
        raise ValidationError(f"{label} path differs from the authorized campaign")
    return physical


def _require_authorized_output_path(
    *, workspace_root: Path, output_path: Path, label: str
) -> Path:
    root = _candidate_workspace_root(workspace_root)
    lexical = (output_path if output_path.is_absolute() else root / output_path).absolute()
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"{label} must stay within the campaign workspace") from exc
    if ".accela-benchmark-locks" in relative.parts:
        raise ValidationError(f"{label} cannot use the benchmark lease namespace")
    cursor = root
    for component in relative.parent.parts:
        cursor = cursor / component
        if cursor.is_symlink():
            raise ValidationError(f"{label} cannot traverse a symbolic link")
    parent = lexical.parent.resolve(strict=True)
    if parent != lexical.parent or lexical.is_symlink():
        raise ValidationError(f"{label} path identity differs")
    if lexical.exists() and not lexical.is_file():
        raise ValidationError(f"{label} must be a regular-file target")
    return lexical


def _require_git_ignored_path(
    *, workspace_root: Path, path: Path, label: str
) -> None:
    root = _candidate_workspace_root(workspace_root)
    lexical = (path if path.is_absolute() else root / path).absolute()
    try:
        relative = lexical.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValidationError(f"{label} must stay within the campaign workspace") from exc

    def git(*arguments: str) -> int:
        try:
            result = subprocess.run(
                ("git", "--no-optional-locks", "-C", str(root), *arguments),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConfigurationError(
                f"cannot verify {label} Git ignore identity"
            ) from exc
        return result.returncode

    tracked = git("ls-files", "--error-unmatch", "--", relative)
    if tracked == 0:
        raise ValidationError(f"{label} cannot be a tracked repository path")
    if tracked != 1:
        raise ConfigurationError(f"cannot verify {label} tracked-file identity")
    ignored = git("check-ignore", "--quiet", "--no-index", "--", relative)
    if ignored == 1:
        raise ValidationError(f"{label} must be covered by the repository ignore policy")
    if ignored != 0:
        raise ConfigurationError(f"cannot verify {label} Git ignore identity")


def _require_candidate_output_separation(
    *,
    output_path: Path,
    protected_files: Sequence[Path],
    protected_directories: Sequence[Path],
) -> None:
    output = output_path.absolute()
    if any(output == path.absolute() for path in protected_files):
        raise ValidationError(
            "candidate run output collides with immutable campaign input/evidence"
        )
    for directory in protected_directories:
        protected = directory.absolute()
        if output == protected or output.is_relative_to(protected):
            raise ValidationError(
                "candidate run output collides with a protected campaign subtree"
            )


def _require_disjoint_candidate_raw_namespaces(
    *, candidate_state_root: Path, oracle_state_root: Path
) -> None:
    candidate = candidate_state_root.absolute()
    oracle = oracle_state_root.absolute()
    if candidate.is_relative_to(oracle) or oracle.is_relative_to(candidate):
        raise ValidationError(
            "candidate campaign and screening Oracle raw state namespaces overlap"
        )


def _require_candidate_campaign_publication_paths(
    *,
    plan: Mapping[str, Any],
    matrix: Mapping[str, Any],
    workspace_root: Path,
    raw_output_path: Path,
    status_output_path: Path,
    protected_input_paths: Sequence[Path],
) -> None:
    root = _candidate_workspace_root(workspace_root)
    raw_output = _require_authorized_output_path(
        workspace_root=root,
        output_path=raw_output_path,
        label="candidate raw evidence registry output",
    )
    status_output = _require_authorized_output_path(
        workspace_root=root,
        output_path=status_output_path,
        label="candidate campaign status output",
    )
    if raw_output == status_output:
        raise ValidationError(
            "candidate raw registry and campaign status outputs must differ"
        )
    for output, label in (
        (raw_output, "candidate raw evidence registry output"),
        (status_output, "candidate campaign status output"),
    ):
        _require_git_ignored_path(
            workspace_root=root,
            path=output,
            label=label,
        )

    protected_files: set[Path] = set()
    for input_path in protected_input_paths:
        protected_files.add(
            _workspace_regular_path(
                root,
                input_path,
                label="candidate campaign publication input",
            )[1]
        )
    artifact_paths = {
        plan["base_pipeline_profile"]["path"],
        plan["reference_toolchain"]["snapshot"]["path"],
        *(item["manifest"]["path"] for item in plan["suites"]),
        *(item["path"] for item in plan["measurement_protocols"].values()),
        *(item["path"] for item in plan["artifacts"].values()),
        *(item["path"] for item in matrix["profiles"]),
    }
    for relative in artifact_paths:
        protected_files.add(
            _workspace_regular_path(
                root,
                root / relative,
                label="candidate campaign frozen publication input",
            )[1]
        )
    _, state_root, _ = _workspace_directory_path(
        root,
        root / plan["raw_state_root"],
        label="candidate campaign raw state root",
    )
    if any(output.parent != state_root.parent for output in (raw_output, status_output)):
        raise ValidationError(
            "candidate campaign publications must use the exact state sibling directory"
        )
    compiler = resolve_without_symlinks(
        root / plan["repository"]["compiler_artifact"]["path"],
        label="candidate campaign compiler artifact",
    )
    protected_directories = {
        state_root,
        compiler if compiler.is_dir() else compiler.parent,
    }
    git_control = root / ".git"
    if git_control.is_dir():
        protected_directories.add(git_control.absolute())
    elif git_control.is_file():
        protected_files.add(git_control.absolute())
    for output in (raw_output, status_output):
        _require_candidate_output_separation(
            output_path=output,
            protected_files=tuple(protected_files),
            protected_directories=tuple(protected_directories),
        )


def _authorized_candidate_ready_tasks(
    *, plan: Mapping[str, Any], status: Mapping[str, Any]
) -> list[str]:
    planned_ids = [item["task_id"] for item in plan["tasks"]]
    if [item["task_id"] for item in status["tasks"]] != planned_ids:
        raise ValidationError("candidate authorization status task set differs from plan")
    ready = ready_campaign_task_ids(
        tasks=plan["tasks"], statuses=status["tasks"]
    )
    diagnostic = status["diagnostic_plan"]
    if diagnostic is None:
        return ready
    diagnostic_ready: list[str] = []
    diagnostic_active = False
    for row in [*diagnostic["tasks"], diagnostic["study"]]:
        if row["status"] in {
            "completed",
            "failed",
            "interrupted",
            "ineligible",
        }:
            continue
        diagnostic_active = True
        if row["status"] == "pending":
            diagnostic_ready = [row["task_id"]]
        break
    return diagnostic_ready if diagnostic_active else ready


def validate_candidate_status_projection_prelease(
    *,
    plan_path: Path,
    plan: Mapping[str, Any],
    status_path: Path,
    status: Mapping[str, Any],
    ledger_paths: Sequence[Path],
    raw_evidence_registry: Mapping[str, Any],
    workspace_root: Path,
    raw_evidence_verifier: Any,
) -> None:
    """Rebuild the ledger head from its exact physical evidence before a run."""

    root = _candidate_workspace_root(workspace_root)
    if not ledger_paths or ledger_paths[-1] != status_path:
        raise ValidationError(
            "candidate status projection requires the physical ledger head"
        )
    planned_task_ids = [item["task_id"] for item in plan["tasks"]]
    if [item["task_id"] for item in status["tasks"]] != planned_task_ids:
        raise ValidationError(
            "candidate status projection task order differs from plan"
        )
    raw_by_task = {
        item["task_id"]: item for item in raw_evidence_registry["runs"]
    }
    if len(raw_by_task) != len(raw_evidence_registry["runs"]):
        raise ValidationError("candidate status projection repeats a raw task")
    run_paths = {
        task_id: _workspace_regular_path(
            root,
            root / item["run_record"]["path"],
            label=f"candidate status projection run {task_id}",
        )[1]
        for task_id, item in raw_by_task.items()
    }
    study_paths: dict[str, Path] = {}
    freeze_path: Path | None = None
    final_path: Path | None = None
    task_specs = {item["task_id"]: item for item in plan["tasks"]}
    for row in status["tasks"]:
        evidence_path = row["evidence_path"]
        if evidence_path is None:
            continue
        physical = _workspace_regular_path(
            root,
            root / evidence_path,
            label=f"candidate status projection evidence {row['task_id']}",
        )[1]
        spec = task_specs.get(row["task_id"])
        if spec is None:
            raise ValidationError(
                "candidate status projection task is absent from plan"
            )
        if spec["task_type"] == "run":
            raw = raw_by_task.get(row["task_id"])
            if raw is None or raw["run_record"]["path"] != evidence_path:
                raise ValidationError(
                    "candidate status run evidence path differs from raw evidence"
                )
        elif row["task_id"].startswith("study."):
            study_paths[row["task_id"].removeprefix("study.")] = physical
        elif row["task_id"] == "freeze":
            freeze_path = physical
        elif row["task_id"] == "final":
            final_path = physical
        else:
            raise ValidationError("candidate status projection task kind is unknown")

    diagnostic_matrix_path: Path | None = None
    diagnostic = status["diagnostic_plan"]
    if diagnostic is not None:
        diagnostic_matrix_path = _workspace_regular_path(
            root,
            root / diagnostic["matrix"]["path"],
            label="candidate status projection diagnostic matrix",
        )[1]
        for row in diagnostic["tasks"]:
            evidence_path = row["evidence_path"]
            if evidence_path is None:
                continue
            raw = raw_by_task.get(row["task_id"])
            if raw is None or raw["run_record"]["path"] != evidence_path:
                raise ValidationError(
                    "candidate diagnostic status path differs from raw evidence"
                )
        diagnostic_study = diagnostic["study"]
        if diagnostic_study["evidence_path"] is not None:
            study_paths["diagnostic"] = _workspace_regular_path(
                root,
                root / diagnostic_study["evidence_path"],
                label="candidate status projection diagnostic study",
            )[1]
    authorized_run_tasks = {
        item["task_id"]
        for item in plan["tasks"]
        if item["task_type"] == "run"
    }
    if diagnostic is not None:
        authorized_run_tasks.update(
            item["task_id"] for item in diagnostic["tasks"]
        )
    unknown_raw_tasks = sorted(set(raw_by_task) - authorized_run_tasks)
    if unknown_raw_tasks:
        raise ValidationError(
            "candidate status projection raw task is not authorized: "
            + ", ".join(unknown_raw_tasks)
        )

    previous_path = ledger_paths[-2] if len(ledger_paths) > 1 else None
    rebuilt = update_candidate_campaign_status(
        plan_path=plan_path,
        run_paths=run_paths,
        study_paths=study_paths,
        diagnostic_matrix_path=diagnostic_matrix_path,
        freeze_path=freeze_path,
        final_path=final_path,
        raw_evidence_registry=raw_evidence_registry,
        raw_evidence_registry_output_path=_workspace_regular_path(
            root,
            root / status["raw_evidence_registry"]["path"],
            label="candidate status projection raw evidence registry",
        )[1],
        status_output_path=status_path,
        workspace_root=root,
        previous_status_path=previous_path,
        status_ledger_paths=ledger_paths[:-1],
        started_at=status["started_at"],
        as_of=status["as_of"],
        _raw_evidence_verifier=raw_evidence_verifier,
    )
    if rebuilt != status:
        raise ValidationError(
            "candidate status differs from the central evidence projection"
        )


def authorize_candidate_run_prelease(
    intent: CandidateRunAuthorizationIntent,
) -> None:
    """Authorize one exact ready candidate run before execution writes state."""

    root = _candidate_workspace_root(intent.workspace_root)
    raw_snapshot_cache = _ReadOnlyRawEvidenceCache()
    _, plan_physical, _ = _workspace_regular_path(
        root, intent.plan_path, label="candidate run authorization plan"
    )
    raw_snapshot_cache.track_file(
        plan_physical, label="candidate run authorization plan"
    )
    _, status_physical, _ = _workspace_regular_path(
        root, intent.status_path, label="candidate run authorization status"
    )
    raw_snapshot_cache.track_file(
        status_physical, label="candidate run authorization status"
    )
    ledger_physical_paths = tuple(
        _workspace_regular_path(
            root,
            path,
            label=f"candidate run authorization ledger entry {index}",
        )[1]
        for index, path in enumerate(intent.status_ledger_paths)
    )
    for index, ledger_path in enumerate(ledger_physical_paths):
        raw_snapshot_cache.track_file(
            ledger_path,
            label=f"candidate run authorization ledger entry {index}",
        )
    plan = _load_version(
        plan_physical,
        "candidate-campaign-plan.v1",
        label="candidate run authorization plan",
    )
    _, state_physical, state_relative = _workspace_directory_path(
        root, intent.state_root, label="candidate run authorization state root"
    )
    if state_relative.as_posix() != plan["raw_state_root"]:
        raise ValidationError(
            "candidate run authorization state root differs from plan"
        )
    output_physical = _require_authorized_output_path(
        workspace_root=root,
        output_path=intent.output_path,
        label="candidate run authorization output",
    )
    if not output_physical.is_relative_to(state_physical.parent):
        raise ValidationError(
            "candidate run output must stay in the campaign-owned state sibling namespace"
        )
    _require_git_ignored_path(
        workspace_root=root,
        path=output_physical,
        label="candidate run authorization output",
    )
    status = _load_version(
        status_physical,
        "candidate-campaign-status.v1",
        label="candidate run authorization status",
    )
    if not intent.status_ledger_paths:
        raise ConfigurationError(
            "formal candidate run authorization requires the complete status ledger"
        )
    ledger_head_physical = ledger_physical_paths[-1]
    if status_physical != ledger_head_physical:
        raise ValidationError(
            "candidate run authorization status must be the physical ledger head"
        )
    _validate_candidate_status_ledger(
        plan=plan,
        status=status,
        ledger_paths=ledger_physical_paths,
        workspace_root=root,
        raw_evidence_verifier=raw_snapshot_cache.verify,
    )
    for index, ledger_path in enumerate(ledger_physical_paths):
        ledger_entry = _load_version(
            ledger_path,
            "candidate-campaign-status.v1",
            label=f"candidate run authorization ledger entry {index}",
        )
        raw_snapshot_cache.track_file(
            root / ledger_entry["raw_evidence_registry"]["path"],
            label=f"candidate run authorization raw registry {index}",
            expected_sha256=ledger_entry["raw_evidence_registry"][
                "physical_sha256"
            ],
        )
        for row in ledger_entry["tasks"]:
            if row["evidence_path"] is not None:
                raw_snapshot_cache.track_file(
                    root / row["evidence_path"],
                    label=(
                        "candidate run authorization task evidence "
                        + row["task_id"]
                    ),
                    expected_sha256=row["evidence_physical_sha256"],
                )
        diagnostic_ledger = ledger_entry["diagnostic_plan"]
        if diagnostic_ledger is not None:
            raw_snapshot_cache.track_file(
                root / diagnostic_ledger["matrix"]["path"],
                label="candidate run authorization diagnostic matrix",
                expected_sha256=diagnostic_ledger["matrix"][
                    "physical_sha256"
                ],
            )
            for row in diagnostic_ledger["tasks"]:
                if row["evidence_path"] is not None:
                    raw_snapshot_cache.track_file(
                        root / row["evidence_path"],
                        label=(
                            "candidate run authorization diagnostic evidence "
                            + row["task_id"]
                        ),
                        expected_sha256=row["evidence_physical_sha256"],
                    )
            diagnostic_study = diagnostic_ledger["study"]
            if diagnostic_study["evidence_path"] is not None:
                raw_snapshot_cache.track_file(
                    root / diagnostic_study["evidence_path"],
                    label="candidate run authorization diagnostic study",
                    expected_sha256=diagnostic_study[
                        "evidence_physical_sha256"
                    ],
                )
    if (
        status["campaign_id"] != plan["campaign_id"]
        or status["plan_sha256"] != sha256_json(plan)
        or status["execution_environment_sha256"]
        != plan["execution_environment_sha256"]
        or status["analyzer"] != plan["analyzer"]
    ):
        raise ValidationError("candidate run authorization status binds another plan")
    current_raw_registry, current_raw_registry_artifact = (
        _load_and_reverify_candidate_raw_evidence_registry(
            plan=plan,
            registry_path=root / status["raw_evidence_registry"]["path"],
            workspace_root=root,
            raw_evidence_verifier=raw_snapshot_cache.verify,
        )
    )
    if current_raw_registry_artifact != status["raw_evidence_registry"]:
        raise ValidationError(
            "candidate run authorization raw-evidence registry differs"
        )
    validate_candidate_status_projection_prelease(
        plan_path=plan_physical,
        plan=plan,
        status_path=status_physical,
        status=status,
        ledger_paths=ledger_physical_paths,
        raw_evidence_registry=current_raw_registry,
        workspace_root=root,
        raw_evidence_verifier=raw_snapshot_cache.verify,
    )
    ready = _authorized_candidate_ready_tasks(plan=plan, status=status)
    if status["ready_tasks"] != ready or ready != [intent.task_id]:
        raise ValidationError(
            "formal candidate run must be the unique current ready task"
        )

    _, observed_tree = _clean_repository_identity(
        root, plan["repository"]["repo_commit"]
    )
    if (
        observed_tree != plan["repository"]["repo_tree"]
        or _frozen_compiler_artifact(
            root, root / plan["repository"]["compiler_artifact"]["path"]
        )
        != plan["repository"]["compiler_artifact"]
    ):
        raise ValidationError(
            "candidate run authorization repository/compiler artifact drifted"
        )
    _verify_candidate_plan_execution_environment(plan, workspace_root=root)
    screening = _load_and_reverify_candidate_screening(
        screening_path=root / plan["artifacts"]["screening"]["path"],
        workspace_root=root,
        raw_evidence_verifier=raw_snapshot_cache.verify,
    )
    oracle_capture_artifact = screening["sources"]["oracle_capture"]
    oracle_capture = _load_and_reverify_candidate_oracle_capture(
        capture_path=root / oracle_capture_artifact["path"],
        workspace_root=root,
        raw_evidence_verifier=raw_snapshot_cache.verify,
    )
    _, oracle_state_physical, _ = _workspace_directory_path(
        root,
        root / oracle_capture["raw_state_root"],
        label="candidate authorization Oracle raw state root",
    )
    _require_disjoint_candidate_raw_namespaces(
        candidate_state_root=state_physical,
        oracle_state_root=oracle_state_physical,
    )
    raw_snapshot_cache.track_file(
        root / plan["artifacts"]["screening"]["path"],
        label="candidate run authorization screening",
        expected_sha256=plan["artifacts"]["screening"]["physical_sha256"],
    )
    raw_snapshot_cache.track_file(
        root / oracle_capture_artifact["path"],
        label="candidate run authorization Oracle capture",
        expected_sha256=oracle_capture_artifact["physical_sha256"],
    )

    candidate_ids = plan["qualified_candidate_ids"]
    catalog = _load_frozen_artifact(
        root,
        plan["artifacts"]["candidate_registry"],
        label="candidate run authorization registry",
        version="candidate-catalog.v1",
    )
    pass_registry = _load_frozen_artifact(
        root,
        plan["artifacts"]["executable_pass_registry"],
        label="candidate run authorization PassRegistry",
        version="pass-registry.v2",
    )
    matrix = _load_frozen_artifact(
        root,
        plan["artifacts"]["matrix"],
        label="candidate run authorization matrix",
        version="candidate-profile-matrix.v1",
    )
    profiles = _verify_matrix_profiles(
        root, matrix, candidate_order=candidate_ids
    )

    main_tasks = {item["task_id"]: item for item in plan["tasks"]}
    task = main_tasks.get(intent.task_id)
    diagnostic_task: Mapping[str, Any] | None = None
    diagnostic_profiles: Mapping[str, Mapping[str, Any]] | None = None
    diagnostic_matrix: Mapping[str, Any] | None = None
    if task is None:
        diagnostic = status["diagnostic_plan"]
        if diagnostic is None:
            raise ValidationError("candidate authorization task is unknown")
        diagnostic_task = next(
            (
                item
                for item in diagnostic["tasks"]
                if item["task_id"] == intent.task_id
            ),
            None,
        )
        if diagnostic_task is None:
            raise ValidationError("candidate authorization task is not a run")
        for key in (
            "configuration_sha256",
            "evidence_sha256",
            "evidence_physical_sha256",
            "evidence_path",
            "started_at",
            "completed_at",
            "failure_reason",
        ):
            if diagnostic_task[key] is not None:
                raise ValidationError(
                    "pending candidate diagnostic authorization carries evidence state"
                )
        main_by_id = {item["task_id"]: item for item in status["tasks"]}
        if (
            main_by_id["freeze"]["status"] != "completed"
            or main_by_id["freeze"]["evidence_sha256"]
            != diagnostic["source_freeze_sha256"]
            or main_by_id["study.B3"]["status"] != "completed"
            or main_by_id["study.B3"]["evidence_sha256"]
            != diagnostic["source_study_sha256"]
        ):
            raise ValidationError(
                "candidate diagnostic authorization source freeze/study differs"
            )
        diagnostic_matrix = _load_frozen_artifact(
            root,
            diagnostic["matrix"],
            label="candidate diagnostic authorization matrix",
            version="candidate-profile-matrix.v1",
        )
        diagnostic_profiles = _verify_matrix_profiles(
            root, diagnostic_matrix, candidate_order=candidate_ids
        )
    elif task["task_type"] != "run":
        raise ValidationError("candidate authorization task is not a run")
    else:
        status_row = next(
            item for item in status["tasks"] if item["task_id"] == intent.task_id
        )
        for key in (
            "evidence_kind",
            "evidence_sha256",
            "evidence_physical_sha256",
            "evidence_path",
            "started_at",
            "completed_at",
            "ineligibility_reason",
        ):
            if status_row[key] is not None:
                raise ValidationError(
                    "pending candidate run authorization carries evidence state"
                )

    authorization_task = task if task is not None else diagnostic_task
    assert authorization_task is not None
    baseline_task_id = _candidate_timeout_baseline_task_id(authorization_task)
    baseline_run: Mapping[str, Any] | None = None
    if baseline_task_id is None:
        if intent.baseline_timeout_path is not None:
            raise ValidationError(
                "initial candidate timeout authorization forbids a baseline run"
            )
    else:
        raw_baseline = next(
            (
                item
                for item in current_raw_registry["runs"]
                if item["task_id"] == baseline_task_id
            ),
            None,
        )
        if raw_baseline is None:
            raise ValidationError(
                "candidate timeout baseline is absent from current raw evidence"
            )
        baseline_physical = _require_authorized_regular_path(
            workspace_root=root,
            observed_path=intent.baseline_timeout_path,
            expected_path=raw_baseline["run_record"]["path"],
            label="candidate authorization timeout baseline",
        )
        baseline_run = _load_version(
            baseline_physical,
            "run-record.v1",
            label="candidate authorization timeout baseline",
        )
        if (
            sha256_json(baseline_run)
            != raw_baseline["run_record"]["canonical_sha256"]
            or sha256_file(baseline_physical)
            != raw_baseline["run_record"]["physical_sha256"]
        ):
            raise ValidationError(
                "candidate timeout baseline differs from current raw evidence"
            )
        baseline_main_task = main_tasks.get(baseline_task_id)
        if baseline_main_task is not None:
            baseline_status = next(
                item
                for item in status["tasks"]
                if item["task_id"] == baseline_task_id
            )
            _validate_campaign_run_static(
                plan,
                baseline_main_task,
                baseline_run,
                profiles=profiles,
            )
        else:
            diagnostic_status = status["diagnostic_plan"]
            if diagnostic_status is None or diagnostic_profiles is None:
                raise ValidationError(
                    "candidate diagnostic timeout baseline lacks its frozen plan"
                )
            baseline_status = next(
                (
                    item
                    for item in diagnostic_status["tasks"]
                    if item["task_id"] == baseline_task_id
                ),
                None,
            )
            if baseline_status is None:
                raise ValidationError(
                    "candidate diagnostic timeout baseline task is absent"
                )
            baseline_static_task = {
                "task_id": baseline_status["task_id"],
                "run_id": baseline_status["run_id"],
                "kind": baseline_status["kind"],
                "measurement_mode": baseline_status["measurement_mode"],
                "candidate_ids": baseline_status["candidate_ids"],
            }
            _validate_candidate_diagnostic_run_static(
                task=baseline_static_task,
                run=baseline_run,
                profile=diagnostic_profiles[baseline_status["logical_profile_id"]],
                candidate_registry_sha256=sha256_json(catalog),
                pass_registry_sha256=sha256_json(pass_registry),
                b3_suite=next(
                    item for item in plan["suites"] if item["data_role"] == "B3"
                ),
                freeze={
                    "repository": plan["repository"],
                    "measurement_protocols": plan["measurement_protocols"],
                    "reference_toolchain": plan["reference_toolchain"],
                    "analyzer": plan["analyzer"],
                    "execution_environment_sha256": plan[
                        "execution_environment_sha256"
                    ],
                },
            )
        if (
            baseline_status["status"] != "completed"
            or baseline_status["evidence_sha256"] != sha256_json(baseline_run)
            or baseline_status["evidence_physical_sha256"]
            != sha256_file(baseline_physical)
        ):
            raise ValidationError(
                "candidate timeout baseline is not exact completed campaign evidence"
            )

    data_role = (
        task["data_role"] if task is not None else diagnostic_task["data_role"]
    )
    suite = next(
        item for item in plan["suites"] if item["data_role"] == data_role
    )
    manifest_physical = _require_authorized_regular_path(
        workspace_root=root,
        observed_path=intent.manifest_path,
        expected_path=suite["manifest"]["path"],
        label="candidate run authorization manifest",
    )
    _, suite_root_physical, _ = _workspace_directory_path(
        root, intent.suite_root, label="candidate run authorization suite root"
    )
    manifest = load_and_validate(
        manifest_physical,
        suite_root=suite_root_physical,
        verify_files=True,
    )
    if (
        manifest["schema_version"] != "benchmark-manifest.v1"
        or manifest["provenance"]["data_role"] != data_role
        or manifest["suite_id"] != suite["suite_id"]
        or len(manifest["cases"]) != suite["case_count"]
        or sha256_json(manifest) != suite["manifest"]["canonical_sha256"]
        or sha256_file(manifest_physical)
        != suite["manifest"]["physical_sha256"]
    ):
        raise ValidationError("candidate run authorization manifest differs")
    require_formal_suite_contract(
        role=data_role, manifest=manifest, manifest_path=manifest_physical
    )

    run = {
        "run_id": intent.run_id,
        "suite_id": manifest["suite_id"],
        "manifest_sha256": sha256_json(manifest),
        "configuration": intent.configuration,
        "provenance": intent.provenance,
    }
    if task is not None:
        _validate_campaign_run_static(
            plan,
            task,
            run,
            profiles=profiles,
            baseline_run=baseline_run,
        )
        expected_mode = None if data_role == "B1" else "standard_proxy"
        if task["kind"] == "reference":
            if any(
                item is not None
                for item in (
                    intent.pipeline_profile_path,
                    intent.candidate_registry_path,
                    intent.candidate_pass_registry_path,
                )
            ) or any(
                intent.configuration.get(key) is not None
                for key in (
                    "pipeline_profile_file_sha256",
                    "candidate_registry_sha256",
                    "candidate_pass_registry_sha256",
                )
            ):
                raise ValidationError(
                    "candidate reference authorization carries candidate profile artifacts"
                )
            reference_snapshot = plan["reference_toolchain"]["snapshot"]
            _require_authorized_regular_path(
                workspace_root=root,
                observed_path=intent.compiler_artifact_path,
                expected_path=reference_snapshot["path"],
                label="candidate reference compiler artifact",
            )
        else:
            profile = profiles[task["logical_profile_id"]]
            if (
                task["candidate_profile_sha256"] != profile["profile_sha256"]
                or task["candidate_profile_path"] != profile["path"]
            ):
                raise ValidationError(
                    "candidate authorization task profile differs from matrix"
                )
            _require_authorized_regular_path(
                workspace_root=root,
                observed_path=intent.pipeline_profile_path,
                expected_path=profile["path"],
                label="candidate authorization pipeline profile",
            )
    else:
        assert diagnostic_task is not None and diagnostic_profiles is not None
        profile = diagnostic_profiles[diagnostic_task["logical_profile_id"]]
        if (
            diagnostic_task["profile_sha256"] != profile["profile_sha256"]
            or diagnostic_task["profile_path"] != profile["path"]
        ):
            raise ValidationError(
                "candidate diagnostic authorization profile differs from matrix"
            )
        _require_authorized_regular_path(
            workspace_root=root,
            observed_path=intent.pipeline_profile_path,
            expected_path=profile["path"],
            label="candidate diagnostic authorization pipeline profile",
        )
        diagnostic_static_task = {
            "task_id": diagnostic_task["task_id"],
            "run_id": diagnostic_task["run_id"],
            "kind": diagnostic_task["kind"],
            "measurement_mode": diagnostic_task["measurement_mode"],
            "candidate_ids": diagnostic_task["candidate_ids"],
        }
        _validate_candidate_diagnostic_run_static(
            task=diagnostic_static_task,
            run=run,
            profile=profile,
            candidate_registry_sha256=sha256_json(catalog),
            pass_registry_sha256=sha256_json(pass_registry),
            b3_suite=suite,
            freeze={
                "repository": plan["repository"],
                "measurement_protocols": plan["measurement_protocols"],
                "reference_toolchain": plan["reference_toolchain"],
                "analyzer": plan["analyzer"],
                "execution_environment_sha256": plan[
                    "execution_environment_sha256"
                ],
            },
            baseline_run=baseline_run,
        )
        expected_mode = diagnostic_task["measurement_mode"]

    if task is None or task["kind"] != "reference":
        if _frozen_compiler_artifact(root, intent.compiler_artifact_path) != plan[
            "repository"
        ]["compiler_artifact"]:
            raise ValidationError(
                "candidate authorization compiler artifact path/hash differs"
            )
        for observed_path, artifact, label in (
            (
                intent.candidate_registry_path,
                plan["artifacts"]["candidate_registry"],
                "candidate authorization registry",
            ),
            (
                intent.candidate_pass_registry_path,
                plan["artifacts"]["executable_pass_registry"],
                "candidate authorization PassRegistry",
            ),
        ):
            _require_authorized_regular_path(
                workspace_root=root,
                observed_path=observed_path,
                expected_path=artifact["path"],
                label=label,
            )

    if expected_mode is None:
        if intent.measurement_protocol_path is not None:
            raise ValidationError(
                "B1 candidate authorization forbids a measurement protocol"
            )
    else:
        protocol = plan["measurement_protocols"][expected_mode]
        protocol_physical = _require_authorized_regular_path(
            workspace_root=root,
            observed_path=intent.measurement_protocol_path,
            expected_path=protocol["path"],
            label="candidate authorization measurement protocol",
        )
        protocol_document = _load_version(
            protocol_physical,
            "measurement-protocol.v1",
            label="candidate authorization measurement protocol",
        )
        if (
            sha256_json(protocol_document) != protocol["protocol_sha256"]
            or sha256_file(protocol_physical) != protocol["physical_sha256"]
        ):
            raise ValidationError(
                "candidate authorization measurement protocol differs"
            )

    protected_files = {
        plan_physical,
        status_physical,
        *ledger_physical_paths,
        manifest_physical,
    }
    for supplied in (
        intent.pipeline_profile_path,
        intent.candidate_registry_path,
        intent.candidate_pass_registry_path,
        intent.measurement_protocol_path,
        intent.baseline_timeout_path,
    ):
        if supplied is not None:
            protected_files.add(
                _workspace_regular_path(
                    root,
                    supplied,
                    label="candidate authorization protected input",
                )[1]
            )
    artifact_paths = {
        plan["base_pipeline_profile"]["path"],
        plan["reference_toolchain"]["snapshot"]["path"],
        *(item["manifest"]["path"] for item in plan["suites"]),
        *(item["path"] for item in plan["measurement_protocols"].values()),
        *(item["path"] for item in plan["artifacts"].values()),
        *(item["path"] for item in matrix["profiles"]),
        *(item["path"] for item in screening["sources"].values()),
        *(
            item["run_record_path"]
            for item in oracle_capture["raw_evidence"].values()
        ),
    }
    artifact_paths.update(
        item["run_record"]["path"]
        for item in current_raw_registry["runs"]
    )
    if diagnostic_matrix is not None:
        artifact_paths.update(item["path"] for item in diagnostic_matrix["profiles"])
        assert status["diagnostic_plan"] is not None
        artifact_paths.add(status["diagnostic_plan"]["matrix"]["path"])
    for ledger_path in ledger_physical_paths:
        ledger_entry = _load_version(
            ledger_path,
            "candidate-campaign-status.v1",
            label="candidate authorization protected ledger entry",
        )
        artifact_paths.add(ledger_entry["raw_evidence_registry"]["path"])
        artifact_paths.update(
            row["evidence_path"]
            for row in ledger_entry["tasks"]
            if row["evidence_path"] is not None
        )
        if ledger_entry["diagnostic_plan"] is not None:
            artifact_paths.update(
                row["evidence_path"]
                for row in ledger_entry["diagnostic_plan"]["tasks"]
                if row["evidence_path"] is not None
            )
            diagnostic_study_path = ledger_entry["diagnostic_plan"]["study"][
                "evidence_path"
            ]
            if diagnostic_study_path is not None:
                artifact_paths.add(diagnostic_study_path)
    for relative in artifact_paths:
        protected_files.add(
            _workspace_regular_path(
                root,
                root / relative,
                label="candidate authorization protected artifact",
            )[1]
        )

    compiler_physical = resolve_without_symlinks(
        intent.compiler_artifact_path,
        label="candidate authorization compiler artifact",
    )
    plan_compiler_physical = resolve_without_symlinks(
        root / plan["repository"]["compiler_artifact"]["path"],
        label="candidate authorization frozen compiler artifact",
    )
    protected_directories = {
        state_physical,
        suite_root_physical,
        oracle_state_physical,
        compiler_physical if compiler_physical.is_dir() else compiler_physical.parent,
        (
            plan_compiler_physical
            if plan_compiler_physical.is_dir()
            else plan_compiler_physical.parent
        ),
    }
    git_control = root / ".git"
    if git_control.is_dir():
        protected_directories.add(git_control.absolute())
    elif git_control.is_file():
        protected_files.add(git_control.absolute())
    _require_candidate_output_separation(
        output_path=output_physical,
        protected_files=tuple(protected_files),
        protected_directories=tuple(protected_directories),
    )
    raw_snapshot_cache.assert_unchanged()


def _candidate_remark_totals(
    run: Mapping[str, Any],
    *,
    candidate_ids: list[str],
) -> tuple[int, dict[str, dict[str, int]]]:
    totals = {
        candidate_id: {
            "paired_candidate_count": 0,
            "applied_count": 0,
            "rejected_count": 0,
        }
        for candidate_id in candidate_ids
    }
    case_count = 0
    for case in run["cases"]:
        summary = case.get("candidate_remark_summary")
        if summary is None:
            continue
        case_count += 1
        by_id = {
            item["candidate_id"]: item for item in summary["candidates"]
        }
        if set(by_id) != set(candidate_ids):
            raise ValidationError(
                f"candidate raw remark summary enablement differs: {case['case_id']}"
            )
        for candidate_id in candidate_ids:
            for field in (
                "paired_candidate_count",
                "applied_count",
                "rejected_count",
            ):
                totals[candidate_id][field] += by_id[candidate_id][field]
    return case_count, totals


def _candidate_static_text_summary(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    comparison: Any,
) -> tuple[float | None, float | None, float | None]:
    baseline_cases = {case["case_id"]: case for case in baseline["cases"]}
    candidate_cases = {case["case_id"]: case for case in candidate["cases"]}
    pairs = [
        (
            _measured_case_value(baseline_cases[item.case_id], "elf_text_bytes"),
            _measured_case_value(candidate_cases[item.case_id], "elf_text_bytes"),
        )
        for item in comparison.pairs
    ]
    if not pairs or not all(
        left is not None and right is not None for left, right in pairs
    ):
        return None, None, None
    full = sum(float(left) for left, _ in pairs if left is not None)
    variant = sum(float(right) for _, right in pairs if right is not None)
    if full <= 0 or variant <= 0:
        return None, None, None
    return full, variant, full / variant


def _candidate_per_case_rows(comparison: Any) -> list[dict[str, Any]]:
    return [
        {
            "case_id": item.case_id,
            "source_group": item.source_group_id,
            "family": item.family,
            "target": item.target,
            "weight": item.weight,
            "metric_full": item.baseline_value,
            "metric_full_plus_candidate": item.candidate_value,
            "speedup": item.speedup,
        }
        for item in comparison.pairs
    ]


def _validate_study_against_raw_runs(
    *,
    study: Mapping[str, Any],
    baseline: Mapping[str, Any],
    candidate_runs: Mapping[str, Mapping[str, Any]],
    interaction_runs: Mapping[
        frozenset[str], tuple[Mapping[str, Any], Mapping[str, Any]]
    ],
    catalog: Mapping[str, Any],
    raw_verifications: Mapping[str, Mapping[str, Any]],
) -> None:
    if study["baseline"] != _run_ref(baseline):
        raise ValidationError("candidate study baseline differs from its raw FULL run")
    descriptor_by_id = _candidate_map(catalog)
    result_by_id = {item["candidate_id"]: item for item in study["candidates"]}
    if set(result_by_id) != set(candidate_runs):
        raise ValidationError("candidate study singles differ from raw candidate runs")
    try:
        expected_raw_evidence = {
            "baseline": _raw_run_ref_document(
                raw_verifications[baseline["run_id"]]
            ),
            "candidates": [
                {
                    "candidate_id": candidate_id,
                    "run": _raw_run_ref_document(
                        raw_verifications[candidate_runs[candidate_id]["run_id"]]
                    ),
                }
                for candidate_id in result_by_id
            ],
            "interactions": [
                {
                    "candidate_ids": list(task["candidate_ids"]),
                    "run": _raw_run_ref_document(
                        raw_verifications[run["run_id"]]
                    ),
                }
                for task, run in interaction_runs.values()
            ],
        }
    except KeyError as exc:
        raise ValidationError(
            "candidate study raw run is absent from the verified registry"
        ) from exc
    if study["raw_evidence"] != expected_raw_evidence:
        raise ValidationError(
            "candidate study raw-evidence refs differ from replayed journals/raw files"
        )
    for candidate_id, run in candidate_runs.items():
        result = result_by_id[candidate_id]
        comparison = compare_runs(baseline, run)
        interval = bootstrap_geometric_mean_ci(
            comparison.pairs,
            samples=study["bootstrap_samples"],
            seed=study["seed"],
        )
        remark_case_count, totals = _candidate_remark_totals(
            run, candidate_ids=[candidate_id]
        )
        candidate_totals = totals[candidate_id]
        reason = _terminal_candidate_reason(
            correctness_failures=comparison.correctness_failures,
            excluded_cases=comparison.excluded_cases,
            censored_cases=comparison.censored_cases,
            comparable_cases=len(comparison.pairs),
            paired_candidate_count=candidate_totals["paired_candidate_count"],
        )
        static_full, static_variant, static_ratio = _candidate_static_text_summary(
            baseline, run, comparison
        )
        expected = {
            "candidate_id": candidate_id,
            "logical_profile_id": run["provenance"]["pipeline_profile_id"],
            "run_id": run["run_id"],
            "run_sha256": sha256_json(run),
            "configuration_sha256": run["configuration_sha256"],
            "enabled_candidate_ids": [candidate_id],
            "comparable_cases": len(comparison.pairs),
            "comparable_source_groups": len(
                {item.source_group_id for item in comparison.pairs}
            ),
            "correctness_failures": comparison.correctness_failures,
            "censored_cases": comparison.censored_cases,
            "excluded_cases": comparison.excluded_cases,
            "eligible_for_ranking": reason is None,
            "ineligibility_reason": reason,
            "case_geometric_mean_speedup": comparison.geometric_mean_speedup,
            "source_group_geometric_mean_speedup": (
                comparison.source_group_geometric_mean_speedup
            ),
            "confidence_interval_95": (
                None if interval is None else {"low": interval[0], "high": interval[1]}
            ),
            "static_text_bytes_full": static_full,
            "static_text_bytes_full_plus_candidate": static_variant,
            "static_text_ratio": static_ratio,
            "remarks": {
                "case_count": remark_case_count,
                **candidate_totals,
                "legality_obligation_ids": [
                    item["obligation_id"]
                    for item in descriptor_by_id[candidate_id][
                        "legality_obligations"
                    ]
                ],
            },
            "per_cases": _candidate_per_case_rows(comparison),
            "families": family_geometric_means(comparison.pairs),
        }
        if result != expected:
            raise ValidationError(
                f"candidate study metrics differ from raw run: {candidate_id}"
            )
    interaction_by_pair = {
        frozenset(item["candidate_ids"]): item for item in study["interactions"]
    }
    if set(interaction_by_pair) != set(interaction_runs):
        raise ValidationError("candidate study interactions differ from raw pair runs")
    for pair, (task, run) in interaction_runs.items():
        interaction = interaction_by_pair[pair]
        candidate_ids = task["candidate_ids"]
        comparison = compare_runs(baseline, run)
        _, totals = _candidate_remark_totals(run, candidate_ids=candidate_ids)
        observations = [
            {
                "candidate_id": candidate_id,
                "paired_candidate_count": totals[candidate_id][
                    "paired_candidate_count"
                ],
            }
            for candidate_id in candidate_ids
        ]
        reason = _terminal_candidate_reason(
            correctness_failures=comparison.correctness_failures,
            excluded_cases=comparison.excluded_cases,
            censored_cases=comparison.censored_cases,
            comparable_cases=len(comparison.pairs),
            paired_candidate_count=min(
                item["paired_candidate_count"] for item in observations
            ),
        )
        constituent_results = [result_by_id[item] for item in candidate_ids]
        if reason is None and any(
            not item["eligible_for_ranking"] for item in constituent_results
        ):
            reason = "constituent_ineligible"
        pair_gm = comparison.geometric_mean_speedup
        expected_multiplicative = (
            math.prod(
                float(item["case_geometric_mean_speedup"])
                for item in constituent_results
            )
            if reason is None
            else None
        )
        expected = {
            "candidate_ids": candidate_ids,
            "logical_profile_id": task["logical_profile_id"],
            "run_id": run["run_id"],
            "run_sha256": sha256_json(run),
            "configuration_sha256": run["configuration_sha256"],
            "enabled_candidate_ids": candidate_ids,
            "comparable_cases": len(comparison.pairs),
            "correctness_failures": comparison.correctness_failures,
            "censored_cases": comparison.censored_cases,
            "excluded_cases": comparison.excluded_cases,
            "candidate_observations": observations,
            "eligible_for_ranking": reason is None,
            "ineligibility_reason": reason,
            "pair_case_geometric_mean_speedup": (
                pair_gm if reason is None else None
            ),
            "expected_multiplicative_speedup": expected_multiplicative,
            "delta_ln_geometric_mean": (
                math.log(float(pair_gm))
                - sum(
                    math.log(float(item["case_geometric_mean_speedup"]))
                    for item in constituent_results
                )
                if reason is None and pair_gm is not None
                else None
            ),
            "per_cases": _candidate_per_case_rows(comparison),
        }
        if interaction != expected:
            raise ValidationError(
                "candidate interaction metrics differ from raw pair run"
            )


def _validate_candidate_diagnostic_study(
    *,
    study: Mapping[str, Any],
    formal_b3_study: Mapping[str, Any],
    baseline: Mapping[str, Any],
    candidate_runs: Mapping[str, Mapping[str, Any]],
    pair_runs: Mapping[frozenset[str], tuple[Mapping[str, Any], Mapping[str, Any]]],
    top3_candidate_ids: list[str],
    matrix_sha256: str,
    catalog: Mapping[str, Any],
    raw_verifications: Mapping[str, Mapping[str, Any]],
) -> None:
    formal_by_id = {
        item["candidate_id"]: item for item in formal_b3_study["candidates"]
    }
    if (
        study["data_role"] != "B3"
        or study["matrix_sha256"] != matrix_sha256
        or study["candidate_registry_sha256"]
        != formal_b3_study["candidate_registry_sha256"]
        or study["pass_registry_sha256"]
        != formal_b3_study["pass_registry_sha256"]
        or study["suite_id"] != formal_b3_study["suite_id"]
        or study["manifest_sha256"] != formal_b3_study["manifest_sha256"]
        or study["bindings"] != formal_b3_study["bindings"]
        or study["baseline"] != _run_ref(baseline)
        or study["primary_metric_id"] != formal_b3_study["primary_metric_id"]
        or study["metric_unit"] != formal_b3_study["metric_unit"]
        or study["bootstrap_samples"] != formal_b3_study["bootstrap_samples"]
        or study["seed"] != formal_b3_study["seed"]
    ):
        raise ValidationError("candidate diagnostic study differs from exact formal B3 inputs")
    diagnostic_by_id = {item["candidate_id"]: item for item in study["candidates"]}
    formal_order = [
        item["candidate_id"]
        for item in formal_b3_study["candidates"]
        if item["candidate_id"] in set(top3_candidate_ids)
    ]
    if (
        [item["candidate_id"] for item in study["candidates"]] != formal_order
        or set(candidate_runs) != set(top3_candidate_ids)
        or any(
            diagnostic_by_id[candidate_id] != formal_by_id[candidate_id]
            for candidate_id in top3_candidate_ids
        )
    ):
        raise ValidationError(
            "candidate diagnostic study singles differ from exact formal B3 Top3"
        )
    interaction_by_pair = {
        frozenset(item["candidate_ids"]): item for item in study["interactions"]
    }
    expected_pairs = {
        frozenset(pair) for pair in combinations(top3_candidate_ids, 2)
    }
    if set(interaction_by_pair) != expected_pairs or set(pair_runs) != expected_pairs:
        raise ValidationError("candidate diagnostic study differs from exact Top3 pairs")
    _validate_study_against_raw_runs(
        study=study,
        baseline=baseline,
        candidate_runs=candidate_runs,
        interaction_runs=pair_runs,
        catalog=catalog,
        raw_verifications=raw_verifications,
    )


def _candidate_final_study_ref(
    study: Mapping[str, Any],
    *,
    study_path: Path,
    workspace_root: Path,
) -> dict[str, Any]:
    artifact = _frozen_artifact_digest(
        workspace_root,
        study_path,
        study,
        label=f"candidate final study {study['data_role']}",
    )
    return {
        "study_id": study["study_id"],
        "path": artifact["path"],
        "study_sha256": artifact["canonical_sha256"],
        "physical_sha256": artifact["physical_sha256"],
        "suite_id": study["suite_id"],
        "manifest_sha256": study["manifest_sha256"],
        "candidate_count": len(study["candidates"]),
        "baseline_run_id": study["baseline"]["run_id"],
        "baseline_run_sha256": study["baseline"]["run_sha256"],
        "baseline_configuration_sha256": study["baseline"][
            "configuration_sha256"
        ],
    }


def _candidate_reference_toolchain_context(
    freeze: Mapping[str, Any],
) -> dict[str, Any]:
    toolchain = freeze["reference_toolchain"]
    return {
        "snapshot": toolchain["snapshot"],
        "compile_driver_sha256": toolchain["compile_driver_sha256"],
        "source_adapter_sha256": toolchain["source_adapter_sha256"],
        "builtin_header_sha256": toolchain["builtin_header_sha256"],
        "image_id": toolchain["image_id"],
        "named_volume_contract": toolchain["named_volume_contract"],
        "candidate_toolchain": toolchain["candidate_toolchain"],
        "common_tool_versions": toolchain["common_tool_versions"],
        "accela_jdk_version": toolchain["accela_jdk_version"],
        "baselines": [
            {
                key: baseline[key]
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
            for baseline in toolchain["baselines"]
        ],
    }


def _candidate_final_diagnostic_ref(
    diagnostic_plan: Mapping[str, Any],
    *,
    campaign_id: str,
    study_path: Path | None,
    workspace_root: Path,
) -> dict[str, Any]:
    study = diagnostic_plan["study"]
    if (study["status"] == "completed") != (study_path is not None):
        raise ValidationError(
            "candidate final diagnostic study path/status binding differs"
        )
    return {
        "source_freeze_sha256": diagnostic_plan["source_freeze_sha256"],
        "source_study_sha256": diagnostic_plan["source_study_sha256"],
        "matrix": diagnostic_plan["matrix"],
        "top3_candidate_ids": diagnostic_plan["top3_candidate_ids"],
        "tasks": [
            {
                key: task[key]
                for key in (
                    "task_id",
                    "kind",
                    "candidate_ids",
                    "run_id",
                    "data_role",
                    "measurement_mode",
                    "logical_profile_id",
                    "profile_sha256",
                    "configuration_sha256",
                    "profile_path",
                    "ranking_evidence",
                    "dependencies",
                    "status",
                    "evidence_sha256",
                    "evidence_physical_sha256",
                    "started_at",
                    "completed_at",
                    "failure_reason",
                )
            }
            for task in diagnostic_plan["tasks"]
        ],
        "study_status": study["status"],
        "study_ineligibility_reason": study["ineligibility_reason"],
        "study": (
            None
            if study["status"] != "completed"
            else {
                "study_id": f"{campaign_id}:study:diagnostic",
                "path": _workspace_regular_path(
                    workspace_root,
                    study_path,
                    label="candidate final diagnostic study",
                )[2].as_posix(),
                "canonical_sha256": study["evidence_sha256"],
                "physical_sha256": study["evidence_physical_sha256"],
            }
        ),
    }


def _require_candidate_final_campaign_identity(
    *,
    final: Mapping[str, Any],
    plan: Mapping[str, Any],
    previous: Mapping[str, Any],
    freeze: Mapping[str, Any],
) -> None:
    previous_by_id = {item["task_id"]: item for item in previous["tasks"]}
    plan_sha256 = sha256_json(plan)
    previous_sha256 = sha256_json(previous)
    if (
        previous["campaign_id"] != plan["campaign_id"]
        or previous["plan_sha256"] != plan_sha256
        or previous["execution_environment_sha256"]
        != plan["execution_environment_sha256"]
        or previous["analyzer"] != plan["analyzer"]
        or previous["ready_tasks"] != ["final"]
        or previous_by_id.get("final", {}).get("status") != "pending"
        or final["campaign"]["plan_sha256"] != plan_sha256
        or final["campaign"]["status_sha256"] != previous_sha256
        or final["campaign"]["status_ledger_head_sha256"] != previous_sha256
        or final["campaign"]["raw_evidence_registry"]
        != previous["raw_evidence_registry"]
        or final["freeze"]["freeze_id"] != freeze["freeze_id"]
        or final["freeze"]["freeze_sha256"] != sha256_json(freeze)
        or final["freeze"]["campaign_id"] != plan["campaign_id"]
        or final["freeze"]["run_namespace"] != plan["run_namespace"]
        or final["freeze"]["repo_commit"]
        != plan["repository"]["repo_commit"]
        or final["freeze"]["repo_tree"] != plan["repository"]["repo_tree"]
        or final["freeze"]["compiler_artifact"]
        != plan["repository"]["compiler_artifact"]
        or final["freeze"]["execution_environment_sha256"]
        != plan["execution_environment_sha256"]
        or final["freeze"]["analyzer"] != plan["analyzer"]
        or final["freeze"]["raw_evidence_registry"]
        != freeze["b2_campaign"]["raw_evidence_registry"]
    ):
        raise ValidationError(
            "candidate campaign final differs from its exact campaign/freeze/status"
        )


def _require_exact_candidate_final_derivation(
    final: Mapping[str, Any], expected: Mapping[str, Any]
) -> None:
    if final != expected:
        raise ValidationError(
            "candidate campaign final differs from exact raw-study derivation"
        )


def _materialize_candidate_campaign_ineligibility(
    *,
    tasks: Sequence[Mapping[str, Any]],
    statuses_by_id: dict[str, dict[str, Any]],
    loaded_studies: Mapping[str, Mapping[str, Any]],
    promoted: set[str],
) -> None:
    """Apply false gates only after their complete causal dependency prefix."""

    terminal_states = {"completed", "failed", "interrupted", "ineligible"}
    b3_study = loaded_studies.get("B3")
    for task in tasks:
        row = statuses_by_id[task["task_id"]]
        gate = task["gate"]
        gate_false_reason: str | None = None
        gate_decided_at: str | None = None
        if gate["kind"] == "b1_completed":
            source = statuses_by_id[f"run.B1.{gate['candidate_id']}"]
            if source["status"] in {"failed", "interrupted"}:
                gate_false_reason = "b1_not_completed_correct"
                gate_decided_at = source["completed_at"]
        elif gate["kind"] == "b3_promoted" and b3_study is not None:
            if gate["candidate_id"] not in promoted:
                gate_false_reason = "b3_not_strictly_above_one"
                gate_decided_at = b3_study["generated_at"]
        elif gate["kind"] == "b3_has_promoted" and b3_study is not None:
            if not promoted:
                gate_false_reason = "no_b3_promotion"
                gate_decided_at = b3_study["generated_at"]
        elif gate["kind"] == "b2_formal_complete" and "B2" in loaded_studies:
            if any(
                not item["eligible_for_ranking"]
                for item in loaded_studies["B2"]["candidates"]
            ):
                gate_false_reason = "b2_not_formal_complete"
                gate_decided_at = loaded_studies["B2"]["generated_at"]
        if gate_false_reason is None:
            continue
        if row["evidence_sha256"] is not None:
            raise ValidationError(
                f"candidate campaign leapfrog supplied gated evidence: {task['task_id']}"
            )
        prerequisite_rows = [
            statuses_by_id[item]
            for item in [*task["dependencies"], *task["terminal_dependencies"]]
        ]
        if any(item["status"] not in terminal_states for item in prerequisite_rows):
            continue
        completion_times = [gate_decided_at]
        completion_times.extend(item["completed_at"] for item in prerequisite_rows)
        if any(item is None for item in completion_times):
            raise ValidationError(
                f"candidate campaign terminal prerequisite lacks completion time: {task['task_id']}"
            )
        statuses_by_id[task["task_id"]] = {
            **row,
            "status": "ineligible",
            "completed_at": _latest_evidence_timestamp(
                [item for item in completion_times if item is not None]
            ),
            "ineligibility_reason": gate_false_reason,
        }


def _validate_candidate_diagnostic_sequence(
    *,
    source_status: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
) -> None:
    """Reject diagnostic evidence that skips its immediate serial predecessor."""

    terminal_states = {"completed", "failed", "interrupted", "ineligible"}
    previous = source_status
    for task in tasks:
        has_evidence = task["evidence_sha256"] is not None
        if has_evidence or task["status"] in terminal_states:
            if previous["status"] not in terminal_states:
                raise ValidationError(
                    f"candidate diagnostic evidence leapfrogs unfinished predecessor: {task['task_id']}"
                )
            previous_completed_at = previous["completed_at"]
            observed_at = task["started_at"] or task["completed_at"]
            if previous_completed_at is None or observed_at is None:
                raise ValidationError(
                    f"candidate diagnostic terminal lacks causal timestamps: {task['task_id']}"
                )
            if datetime.fromisoformat(observed_at.replace("Z", "+00:00")) < (
                datetime.fromisoformat(previous_completed_at.replace("Z", "+00:00"))
            ):
                raise ValidationError(
                    f"candidate diagnostic task starts before its dependency: {task['task_id']}"
                )
        previous = task


def update_candidate_campaign_status(
    *,
    plan_path: Path,
    run_paths: Mapping[str, Path],
    study_paths: Mapping[str, Path],
    diagnostic_matrix_path: Path | None = None,
    freeze_path: Path | None = None,
    final_path: Path | None = None,
    raw_evidence_registry: Mapping[str, Any],
    raw_evidence_registry_output_path: Path,
    status_output_path: Path,
    workspace_root: Path,
    previous_status_path: Path | None = None,
    status_ledger_paths: Sequence[Path] = (),
    started_at: str | None = None,
    as_of: str | None = None,
    _raw_snapshot_cache: _ReadOnlyRawEvidenceCache | None = None,
    _raw_evidence_verifier: Any | None = None,
) -> dict[str, Any]:
    """Recompute the sole dependency-safe B1-through-final scheduler view."""

    raw_snapshot_cache, raw_evidence_verifier = (
        _candidate_read_only_raw_verifier(
            raw_snapshot_cache=_raw_snapshot_cache,
            raw_evidence_verifier=_raw_evidence_verifier,
        )
    )
    plan = _load_version(
        plan_path, "candidate-campaign-plan.v1", label="candidate campaign plan"
    )
    if (
        plan["run_record_schema_sha256"] != schema_sha256("run-record.v1")
        or plan["candidate_study_schema_sha256"]
        != schema_sha256("candidate-study.v1")
        or plan["candidate_freeze_schema_sha256"]
        != schema_sha256("candidate-freeze.v1")
        or plan["candidate_raw_evidence_schema_sha256"]
        != schema_sha256("candidate-raw-evidence.v1")
    ):
        raise ValidationError("candidate campaign plan binds a stale active schema")
    candidate_ids = plan["qualified_candidate_ids"]
    root = _candidate_workspace_root(workspace_root)
    artifact_versions = {
        "candidate_registry": "candidate-catalog.v1",
        "executable_pass_registry": "pass-registry.v2",
        "screening_base_pass_registry": "pass-registry.v2",
        "matrix": "candidate-profile-matrix.v1",
        "screening": "candidate-screening.v1",
    }
    documents = {
        key: _load_frozen_artifact(
            root,
            plan["artifacts"][key],
            label=f"candidate campaign {key}",
            version=version,
        )
        for key, version in artifact_versions.items()
    }
    catalog = documents["candidate_registry"]
    pass_registry = documents["executable_pass_registry"]
    base_pass_registry = documents["screening_base_pass_registry"]
    matrix = documents["matrix"]
    screening = documents["screening"]
    if (
        _load_and_reverify_candidate_screening(
            screening_path=root / plan["artifacts"]["screening"]["path"],
            workspace_root=root,
            raw_evidence_verifier=raw_evidence_verifier,
        )
        != screening
        or
        screening["base_pass_registry"]
        != plan["artifacts"]["screening_base_pass_registry"]
        or _require_executable_registry_bridge(
            screening=screening,
            catalog=catalog,
            executable_registry=pass_registry,
            workspace_root=root,
        )
        != base_pass_registry
    ):
        raise ValidationError("candidate campaign base PassRegistry artifact drifted")
    qualified_screening = [
        item["implementation_candidate_id"]
        for item in screening["candidates"]
        if item["qualification_status"] == "qualified"
    ]
    if (
        [item["candidate_id"] for item in catalog["candidates"]] != candidate_ids
        or qualified_screening != candidate_ids
        or matrix["candidate_registry_sha256"] != sha256_json(catalog)
        or matrix["pass_registry_sha256"] != sha256_json(pass_registry)
        or screening["pass_registry_sha256"] != sha256_json(base_pass_registry)
    ):
        raise ValidationError("candidate campaign frozen registry/matrix/screening drifted")
    profiles = _verify_matrix_profiles(
        root, matrix, candidate_order=candidate_ids
    )
    _, observed_tree = _clean_repository_identity(
        root, plan["repository"]["repo_commit"]
    )
    if observed_tree != plan["repository"]["repo_tree"] or _frozen_compiler_artifact(
        root, root / plan["repository"]["compiler_artifact"]["path"]
    ) != plan["repository"]["compiler_artifact"]:
        raise ValidationError("candidate campaign repository/compiler artifact drifted")
    _verify_candidate_plan_execution_environment(plan, workspace_root=root)
    base = plan["base_pipeline_profile"]
    if (
        base["profile_id"] != "candidate-empty"
        or base["profile_sha256"] != profiles["candidate-empty"]["profile_sha256"]
        or base["path"] != profiles["candidate-empty"]["path"]
        or base["physical_sha256"]
        != sha256_file(root / base["path"])
    ):
        raise ValidationError("candidate campaign base profile physical identity drifted")
    suite_by_role = {item["data_role"]: item for item in plan["suites"]}
    for role, suite in suite_by_role.items():
        manifest = _load_frozen_artifact(
            root,
            suite["manifest"],
            label=f"candidate campaign {role} manifest",
            version="benchmark-manifest.v1",
        )
        require_formal_suite_contract(
            role=role,
            manifest=manifest,
            manifest_path=root / suite["manifest"]["path"],
        )
        if (
            manifest["suite_id"] != suite["suite_id"]
            or len(manifest["cases"]) != suite["case_count"]
        ):
            raise ValidationError(f"candidate campaign {role} suite metadata drifted")

    tasks = {item["task_id"]: item for item in plan["tasks"]}
    previous = (
        _load_version(
            previous_status_path,
            "candidate-campaign-status.v1",
            label="previous candidate campaign status",
        )
        if previous_status_path is not None
        else None
    )
    if (
        previous is not None
        and (
            previous["execution_environment_sha256"]
            != plan["execution_environment_sha256"]
            or previous["analyzer"] != plan["analyzer"]
        )
    ):
        raise ValidationError(
            "previous candidate status execution environment differs from plan"
        )
    publication_inputs = [
        plan_path,
        *run_paths.values(),
        *study_paths.values(),
        *status_ledger_paths,
        *(
            [path]
            if (path := diagnostic_matrix_path) is not None
            else []
        ),
        *([path] if (path := freeze_path) is not None else []),
        *([path] if (path := final_path) is not None else []),
        *(
            [path]
            if (path := previous_status_path) is not None
            else []
        ),
    ]
    status_inputs = [
        *(
            [previous]
            if previous is not None
            else []
        ),
        *[
            _load_version(
                path,
                "candidate-campaign-status.v1",
                label=f"candidate publication ledger entry {index}",
            )
            for index, path in enumerate(status_ledger_paths)
        ],
    ]
    publication_inputs.extend(
        root / item["raw_evidence_registry"]["path"] for item in status_inputs
    )
    _require_candidate_campaign_publication_paths(
        plan=plan,
        matrix=matrix,
        workspace_root=root,
        raw_output_path=raw_evidence_registry_output_path,
        status_output_path=status_output_path,
        protected_input_paths=publication_inputs,
    )
    plan_sha256 = sha256_json(plan)
    chain_started_at, observation_time, previous_sha256 = campaign_status_chain(
        campaign_id=plan["campaign_id"],
        plan_sha256=plan_sha256,
        previous=previous,
        started_at=started_at,
        as_of=as_of,
    )
    main_run_paths = {key: value for key, value in run_paths.items() if key in tasks}
    diagnostic_run_paths = {
        key: value for key, value in run_paths.items() if key.startswith("diagnostic.")
    }
    unknown = sorted(set(run_paths) - set(main_run_paths) - set(diagnostic_run_paths))
    if unknown:
        raise ConfigurationError("unknown candidate campaign run task: " + ", ".join(unknown))
    if any(tasks[item]["task_type"] != "run" for item in main_run_paths):
        raise ConfigurationError("candidate campaign --run may bind only run tasks")
    raw_evidence_registry = _reverify_candidate_raw_evidence_registry_document(
        plan=plan,
        registry=raw_evidence_registry,
        workspace_root=root,
        expected_run_paths=run_paths,
        raw_evidence_verifier=raw_evidence_verifier,
    )
    raw_evidence_registry_artifact = candidate_raw_evidence_registry_artifact(
        registry=raw_evidence_registry,
        output_path=raw_evidence_registry_output_path,
        workspace_root=root,
    )
    if previous is not None:
        _, previous_raw_registry_artifact = (
            _load_and_reverify_candidate_raw_evidence_registry(
                plan=plan,
                registry_path=root / previous["raw_evidence_registry"]["path"],
                workspace_root=root,
                raw_evidence_verifier=raw_evidence_verifier,
            )
        )
        if previous_raw_registry_artifact != previous["raw_evidence_registry"]:
            raise ValidationError(
                "previous candidate status raw-evidence artifact differs"
            )
    raw_verifications = _raw_verifications_by_run_id(raw_evidence_registry)
    def status_evidence_path(path: Path, *, label: str) -> str:
        return _workspace_regular_path(root, path, label=label)[2].as_posix()

    unknown_studies = sorted(
        set(study_paths) - set(plan["study_ids"]) - {"diagnostic"}
    )
    if unknown_studies:
        raise ConfigurationError(
            "unknown candidate campaign study role: " + ", ".join(unknown_studies)
        )
    statuses_by_id: dict[str, dict[str, Any]] = {}
    loaded_runs: dict[str, dict[str, Any]] = {}
    for task_id, task in tasks.items():
        if task["task_type"] != "run":
            continue
        path = main_run_paths.get(task_id)
        if path is None:
            statuses_by_id[task_id] = {
                "task_id": task_id,
                "status": "pending",
                "evidence_kind": None,
                "evidence_sha256": None,
                "evidence_physical_sha256": None,
                "evidence_path": None,
                "started_at": None,
                "completed_at": None,
                "ineligibility_reason": None,
            }
        else:
            run = _load_version(path, "run-record.v1", label=f"campaign run {task_id}")
            baseline_task_id = _candidate_timeout_baseline_task_id(task)
            _validate_campaign_run(
                plan,
                task,
                run,
                profiles=profiles,
                baseline_run=(
                    None
                    if baseline_task_id is None
                    else loaded_runs.get(baseline_task_id)
                ),
            )
            binding = _binding_record(
                run, include_execution_environment=True
            )
            if (
                binding["repo_commit"] != plan["repository"]["repo_commit"]
                or binding["repo_dirty"]
                or binding["tracked_diff_sha256"] is not None
                or (
                    task["kind"] != "reference"
                    and binding["compiler_artifact_sha256"]
                    != plan["repository"]["compiler_artifact"]["physical_sha256"]
                )
            ):
                raise ValidationError(
                    f"candidate campaign run differs from frozen revision/artifact/protocol: {task_id}"
                )
            loaded_runs[task_id] = run
            status, _ = campaign_run_status(run)
            statuses_by_id[task_id] = {
                "task_id": task_id,
                "status": status,
                "evidence_kind": "run-record.v1",
                "evidence_sha256": sha256_json(run),
                "evidence_physical_sha256": sha256_file(path),
                "evidence_path": status_evidence_path(
                    path, label=f"campaign run evidence {task_id}"
                ),
                "started_at": run["started_at"],
                "completed_at": raw_verifications[run["run_id"]][
                    "terminal_observed_at"
                ],
                "ineligibility_reason": None,
            }
    b1_passed = {
        candidate_id
        for candidate_id in candidate_ids
        if statuses_by_id[f"run.B1.{candidate_id}"]["status"] == "completed"
    }

    loaded_studies: dict[str, dict[str, Any]] = {}
    promoted: set[str] = set()
    for role in ("B2", "B3", "B4", "B5", "B6"):
        task_id = f"study.{role}"
        path = study_paths.get(role)
        if path is None:
            statuses_by_id[task_id] = {
                "task_id": task_id,
                "status": "pending",
                "evidence_kind": None,
                "evidence_sha256": None,
                "evidence_physical_sha256": None,
                "evidence_path": None,
                "started_at": None,
                "completed_at": None,
                "ineligibility_reason": None,
            }
            continue
        study = _load_version(
            path, "candidate-study.v1", label=f"candidate campaign {role} study"
        )
        suite = suite_by_role[role]
        if (
            study["study_id"] != plan["study_ids"][role]
            or study["data_role"] != role
            or study["matrix_sha256"] != plan["artifacts"]["matrix"]["canonical_sha256"]
            or study["candidate_registry_sha256"]
            != plan["artifacts"]["candidate_registry"]["canonical_sha256"]
            or study["pass_registry_sha256"]
            != plan["artifacts"]["executable_pass_registry"]["canonical_sha256"]
            or study["suite_id"] != suite["suite_id"]
            or study["manifest_sha256"] != suite["manifest"]["canonical_sha256"]
        ):
            raise ValidationError(f"candidate campaign {role} study binding differs")
        expected_ids = (
            b1_passed if role in {"B2", "B3"} else promoted
        )
        result_by_id = {item["candidate_id"]: item for item in study["candidates"]}
        if set(result_by_id) != expected_ids:
            raise ValidationError(
                f"candidate campaign {role} study candidate gate differs"
            )
        baseline_task_id = f"run.{role}.full"
        baseline_run = loaded_runs.get(baseline_task_id)
        if baseline_run is None or study["baseline"] != _run_ref(baseline_run):
            raise ValidationError(
                f"candidate campaign {role} study does not bind its FULL run"
            )
        for candidate_id, result in result_by_id.items():
            run_task_id = f"run.{role}.{candidate_id}"
            run = loaded_runs.get(run_task_id)
            if run is None or (
                result["run_id"] != run["run_id"]
                or result["run_sha256"] != sha256_json(run)
                or result["configuration_sha256"] != run["configuration_sha256"]
            ):
                raise ValidationError(
                    f"candidate campaign study/run SHA binding differs: {run_task_id}"
                )
        _validate_study_against_raw_runs(
            study=study,
            baseline=baseline_run,
            candidate_runs={
                candidate_id: loaded_runs[f"run.{role}.{candidate_id}"]
                for candidate_id in result_by_id
            },
            interaction_runs={},
            catalog=catalog,
            raw_verifications=raw_verifications,
        )
        if role == "B3":
            if study["interactions"]:
                raise ValidationError(
                    "formal B3 study cannot contain post-B3 diagnostic pair evidence"
                )
            promoted = {
                candidate_id
                for candidate_id, result in result_by_id.items()
                if result["eligible_for_ranking"]
                and result["case_geometric_mean_speedup"] is not None
                and result["case_geometric_mean_speedup"] > 1.0
            }
        loaded_studies[role] = study
        statuses_by_id[task_id] = {
            "task_id": task_id,
            "status": "completed",
            "evidence_kind": "candidate-study.v1",
            "evidence_sha256": sha256_json(study),
            "evidence_physical_sha256": sha256_file(path),
            "evidence_path": status_evidence_path(
                path, label=f"candidate campaign {role} study evidence"
            ),
            "started_at": study["generated_at"],
            "completed_at": study["generated_at"],
            "ineligibility_reason": None,
        }

    freeze: dict[str, Any] | None = None
    if freeze_path is None:
        statuses_by_id["freeze"] = {
            "task_id": "freeze",
            "status": "pending",
            "evidence_kind": None,
            "evidence_sha256": None,
            "evidence_physical_sha256": None,
            "evidence_path": None,
            "started_at": None,
            "completed_at": None,
            "ineligibility_reason": None,
        }
    else:
        freeze = _load_version(
            freeze_path, "candidate-freeze.v1", label="candidate campaign freeze"
        )
        _verify_candidate_freeze_inputs(
            freeze,
            workspace_root=root,
            candidate_order=candidate_ids,
            raw_evidence_verifier=raw_evidence_verifier,
        )
        if (
            freeze["campaign_id"] != plan["campaign_id"]
            or freeze["b2_campaign"]["plan_sha256"] != plan_sha256
            or freeze["execution_environment_sha256"]
            != plan["execution_environment_sha256"]
            or freeze["b2_campaign"]["study_sha256"]
            != statuses_by_id["study.B2"]["evidence_sha256"]
        ):
            raise ValidationError("candidate campaign freeze differs from plan/B2 study")
        _, frozen_raw_registry_artifact = (
            _load_and_reverify_candidate_raw_evidence_registry(
                plan=plan,
                registry_path=root
                / freeze["b2_campaign"]["raw_evidence_registry"]["path"],
                workspace_root=root,
                raw_evidence_verifier=raw_evidence_verifier,
            )
        )
        if (
            frozen_raw_registry_artifact
            != freeze["b2_campaign"]["raw_evidence_registry"]
            or (
                previous is not None
                and previous["tasks"][
                    next(
                        index
                        for index, row in enumerate(previous["tasks"])
                        if row["task_id"] == "freeze"
                    )
                ]["status"]
                == "pending"
                and previous["raw_evidence_registry"]
                != frozen_raw_registry_artifact
            )
        ):
            raise ValidationError(
                "candidate campaign freeze raw-evidence binding differs"
            )
        statuses_by_id["freeze"] = {
            "task_id": "freeze",
            "status": "completed",
            "evidence_kind": "candidate-freeze.v1",
            "evidence_sha256": sha256_json(freeze),
            "evidence_physical_sha256": sha256_file(freeze_path),
            "evidence_path": status_evidence_path(
                freeze_path, label="candidate campaign freeze evidence"
            ),
            "started_at": freeze["frozen_at"],
            "completed_at": freeze["frozen_at"],
            "ineligibility_reason": None,
        }
        for task_id, run in loaded_runs.items():
            task = tasks[task_id]
            if task["kind"] == "reference":
                _require_frozen_reference_run(run, task, freeze)

    final: dict[str, Any] | None = None
    if final_path is None:
        statuses_by_id["final"] = {
            "task_id": "final",
            "status": "pending",
            "evidence_kind": None,
            "evidence_sha256": None,
            "evidence_physical_sha256": None,
            "evidence_path": None,
            "started_at": None,
            "completed_at": None,
            "ineligibility_reason": None,
        }
    else:
        final = _load_version(
            final_path, "candidate-final.v1", label="candidate campaign final"
        )
        if freeze is None or previous is None:
            raise ValidationError(
                "candidate campaign final requires its freeze and exact pre-final status"
            )
        if raw_evidence_registry_artifact != previous["raw_evidence_registry"]:
            raise ValidationError(
                "candidate final registration must reuse the exact pre-final raw snapshot"
            )
        _require_candidate_final_campaign_identity(
            final=final,
            plan=plan,
            previous=previous,
            freeze=freeze,
        )
        final_freeze = final["freeze"]
        frozen_snapshots = freeze["snapshots"]
        frozen_suites = {
            item["data_role"]: item for item in freeze["suites"]
        }
        if (
            final["screening_sha256"] != sha256_json(screening)
            or final["candidate_registry_sha256"] != sha256_json(catalog)
            or final["matrix_sha256"] != sha256_json(matrix)
            or final_freeze["candidate_registry"]
            != frozen_snapshots["candidate_registry"]
            or final_freeze["executable_pass_registry"]
            != frozen_snapshots["executable_pass_registry"]
            or final_freeze["screening_base_pass_registry"]
            != frozen_snapshots["screening_base_pass_registry"]
            or final_freeze["matrix"] != frozen_snapshots["matrix"]
            or final_freeze["screening"] != frozen_snapshots["screening"]
            or final_freeze["oracle_capture"]
            != frozen_snapshots["oracle_capture"]
            or final_freeze["run_record_schema"]
            != frozen_snapshots["run_record_schema"]
            or final_freeze["candidate_study_schema"]
            != frozen_snapshots["candidate_study_schema"]
            or final_freeze["base_pipeline_profile"]
            != freeze["base_pipeline_profile"]["artifact"]
            or final_freeze["standard_measurement_protocol"]
            != {
                "path": freeze["measurement_protocols"]["standard_proxy"]["path"],
                "canonical_sha256": freeze["measurement_protocols"][
                    "standard_proxy"
                ]["protocol_sha256"],
                "physical_sha256": freeze["measurement_protocols"][
                    "standard_proxy"
                ]["physical_sha256"],
            }
            or final_freeze["hotblock_measurement_protocol"]
            != {
                "path": freeze["measurement_protocols"]["cache_hotblock"]["path"],
                "canonical_sha256": freeze["measurement_protocols"][
                    "cache_hotblock"
                ]["protocol_sha256"],
                "physical_sha256": freeze["measurement_protocols"][
                    "cache_hotblock"
                ]["physical_sha256"],
            }
            or final_freeze["suite_manifests"]
            != {
                role: frozen_suites[role]["manifest"]
                for role in _CANDIDATE_SUITE_CASE_COUNTS
            }
            or final_freeze["reference_toolchain"]
            != _candidate_reference_toolchain_context(freeze)
            or final_freeze["execution_environment_sha256"]
            != freeze["execution_environment_sha256"]
            or final_freeze["frozen_candidate_ids_sha256"]
            != freeze["frozen_candidate_ids_sha256"]
            or final_freeze["freeze_status_ledger_entry_count"]
            != freeze["b2_campaign"]["status_ledger_entry_count"]
            or final_freeze["freeze_status_ledger_head_sha256"]
            != freeze["b2_campaign"]["status_ledger_head_sha256"]
            or final_freeze["freeze_status_ledger_sha256"]
            != freeze["b2_campaign"]["status_ledger_sha256"]
            or final_freeze["b1_full_run_sha256"]
            != freeze["b2_campaign"]["b1_full_run_sha256"]
            or final_freeze["b1_passed_candidate_ids"]
            != freeze["b2_campaign"]["b1_passed_candidate_ids"]
            or final_freeze["b1_failed_candidate_ids"]
            != freeze["b2_campaign"]["b1_failed_candidate_ids"]
            or final_freeze["oracle_threshold"]
            != freeze["gates"]["oracle_structure_geometric_mean_minimum"]
            or final_freeze["b3_strict_threshold"]
            != freeze["gates"]["b3_geometric_mean_strictly_above"]
            or final_freeze["combined_case_count"]
            != freeze["gates"]["combined_case_count"]
            or final_freeze["ranking_rule"]
            != [
                freeze["ranking_rule"]["primary"],
                freeze["ranking_rule"]["secondary"],
                freeze["ranking_rule"]["tertiary"],
                freeze["ranking_rule"]["quaternary"],
            ]
        ):
            raise ValidationError(
                "candidate campaign final differs from its exact campaign/freeze/status"
            )
        statuses_by_id["final"] = {
            "task_id": "final",
            "status": "completed",
            "evidence_kind": "candidate-final.v1",
            "evidence_sha256": sha256_json(final),
            "evidence_physical_sha256": sha256_file(final_path),
            "evidence_path": status_evidence_path(
                final_path, label="candidate campaign final evidence"
            ),
            "started_at": final["generated_at"],
            "completed_at": final["generated_at"],
            "ineligibility_reason": None,
        }

    b3_study = loaded_studies.get("B3")
    _materialize_candidate_campaign_ineligibility(
        tasks=plan["tasks"],
        statuses_by_id=statuses_by_id,
        loaded_studies=loaded_studies,
        promoted=promoted,
    )

    terminal_states = {"completed", "failed", "interrupted", "ineligible"}
    for task in plan["tasks"]:
        row = statuses_by_id[task["task_id"]]
        if row["evidence_sha256"] is None:
            continue
        if any(
            statuses_by_id[item]["status"] != "completed"
            for item in task["dependencies"]
        ) or any(
            statuses_by_id[item]["status"] not in terminal_states
            for item in task["terminal_dependencies"]
        ):
            raise ValidationError(
                f"candidate campaign evidence leapfrogs dependencies: {task['task_id']}"
            )

    statuses = [statuses_by_id[item["task_id"]] for item in plan["tasks"]]
    def parse_time(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    campaign_start = parse_time(chain_started_at)
    observation = parse_time(observation_time)
    for row in statuses:
        started_value = row["started_at"]
        completed_value = row["completed_at"]
        if started_value is not None:
            started_time = parse_time(started_value)
            if started_time < campaign_start or started_time > observation:
                raise ValidationError(
                    f"candidate campaign task start lies outside the ledger clock: {row['task_id']}"
                )
        else:
            started_time = None
        if completed_value is not None:
            completed_time = parse_time(completed_value)
            if completed_time > observation or (
                started_time is not None and completed_time < started_time
            ):
                raise ValidationError(
                    f"candidate campaign task completion violates the ledger clock: {row['task_id']}"
                )
    for task in plan["tasks"]:
        row = statuses_by_id[task["task_id"]]
        if row["started_at"] is None:
            continue
        started_time = parse_time(row["started_at"])
        for dependency in [*task["dependencies"], *task["terminal_dependencies"]]:
            dependency_completed = statuses_by_id[dependency]["completed_at"]
            if dependency_completed is None or parse_time(dependency_completed) > started_time:
                raise ValidationError(
                    f"candidate campaign dependency completes after dependent start: {task['task_id']}"
                )
    if previous is not None:
        enforce_terminal_task_immutability(
            previous_rows=previous["tasks"],
            current_rows=statuses,
            label="candidate campaign",
        )
    ready_tasks = ready_campaign_task_ids(tasks=plan["tasks"], statuses=statuses)

    diagnostic_plan: dict[str, Any] | None = None
    diagnostic_loaded_runs: dict[str, dict[str, Any]] = {}
    if b3_study is not None:
        if freeze is None or diagnostic_matrix_path is None:
            raise ConfigurationError(
                "completed B3 requires its freeze-bound diagnostic profile matrix"
            )
        diagnostic_matrix = _load_version(
            diagnostic_matrix_path,
            "candidate-profile-matrix.v1",
            label="candidate B3 diagnostic matrix",
        )
        if (
            diagnostic_matrix["candidate_registry_sha256"] != sha256_json(catalog)
            or diagnostic_matrix["pass_registry_sha256"] != sha256_json(pass_registry)
        ):
            raise ValidationError("candidate diagnostic matrix registry binding differs")
        diagnostic_profiles = _verify_matrix_profiles(
            root, diagnostic_matrix, candidate_order=candidate_ids
        )
        top3 = [
            item["candidate_id"]
            for item in sorted(
                (
                    item
                    for item in b3_study["candidates"]
                    if item["eligible_for_ranking"]
                ),
                key=lambda item: (
                    -float(item["case_geometric_mean_speedup"]),
                    item["candidate_id"],
                ),
            )[:3]
        ]
        expected_pairs = {
            frozenset(pair) for pair in combinations(top3, 2)
        }
        pair_records = {
            frozenset(item["candidate_ids"]): item
            for item in diagnostic_matrix["profiles"]
            if item["kind"] == "pair"
        }
        scheduled_pairs = {
            frozenset(item["candidate_ids"])
            for item in diagnostic_matrix["schedule"]
            if item["kind"] == "pair"
        }
        if set(pair_records) != expected_pairs or scheduled_pairs != expected_pairs:
            raise ValidationError(
                "candidate diagnostic matrix must contain all and only exact B3 Top3 pairs"
            )
        single_records = {
            item["candidate_ids"][0]: item
            for item in diagnostic_matrix["profiles"]
            if item["kind"] == "single"
        }
        if set(single_records) != set(candidate_ids):
            raise ValidationError("candidate diagnostic matrix single profiles differ")
        diagnostic_specs: list[tuple[str, str, list[str]]] = []
        for left, right in combinations(top3, 2):
            profile_record = pair_records[frozenset((left, right))]
            profile_candidate_ids = list(profile_record["candidate_ids"])
            stable_pair_id = "+".join(sorted((left, right)))
            diagnostic_specs.append(
                (
                    f"diagnostic.pair.{stable_pair_id}",
                    "pair",
                    profile_candidate_ids,
                )
            )
        diagnostic_specs.extend(
            (
                "diagnostic.cache.full"
                if candidate_id is None
                else f"diagnostic.cache.{candidate_id}",
                "cache_hotblock",
                [] if candidate_id is None else [candidate_id],
            )
            for candidate_id in [None, *top3]
        )
        diagnostic_tasks: list[dict[str, Any]] = []
        diagnostic_dependency = "study.B3"
        for task_id, kind, diagnostic_candidate_ids in diagnostic_specs:
            if kind == "pair":
                profile_record = pair_records[frozenset(diagnostic_candidate_ids)]
                measurement_mode = "standard_proxy"
            elif diagnostic_candidate_ids:
                profile_record = single_records[diagnostic_candidate_ids[0]]
                measurement_mode = "cache_hotblock"
            else:
                profile_record = diagnostic_matrix["profiles"][0]
                if profile_record["kind"] != "candidate_empty":
                    raise ValidationError("candidate diagnostic matrix lacks leading FULL")
                measurement_mode = "cache_hotblock"
            path = diagnostic_run_paths.get(task_id)
            if path is None:
                status = "pending"
                evidence_sha256 = None
                evidence_physical_sha256 = None
                evidence_path = None
                configuration_sha256 = None
                started_at_value = None
                completed_at_value = None
                failure_reason = None
            else:
                run = _load_version(
                    path,
                    "run-record.v1",
                    label=f"candidate diagnostic run {task_id}",
                )
                diagnostic_validation_task = {
                    "task_id": task_id,
                    "run_id": f"{plan['run_namespace']}{task_id}",
                    "kind": kind,
                    "measurement_mode": measurement_mode,
                    "candidate_ids": diagnostic_candidate_ids,
                }
                baseline_task_id = _candidate_timeout_baseline_task_id(
                    diagnostic_validation_task
                )
                _validate_candidate_diagnostic_run(
                    task=diagnostic_validation_task,
                    run=run,
                    profile=diagnostic_profiles[profile_record["profile_id"]],
                    candidate_registry_sha256=sha256_json(catalog),
                    pass_registry_sha256=sha256_json(pass_registry),
                    b3_suite=suite_by_role["B3"],
                    freeze=freeze,
                    baseline_run=(
                        None
                        if baseline_task_id is None
                        else (
                            loaded_runs.get(baseline_task_id)
                            or diagnostic_loaded_runs.get(baseline_task_id)
                        )
                    ),
                )
                status, failure_reason = campaign_run_status(run)
                diagnostic_loaded_runs[task_id] = run
                evidence_sha256 = sha256_json(run)
                evidence_physical_sha256 = sha256_file(path)
                evidence_path = status_evidence_path(
                    path, label=f"candidate diagnostic run evidence {task_id}"
                )
                configuration_sha256 = run["configuration_sha256"]
                started_at_value = run["started_at"]
                completed_at_value = raw_verifications[run["run_id"]][
                    "terminal_observed_at"
                ]
            diagnostic_tasks.append(
                {
                    "task_id": task_id,
                    "kind": kind,
                    "candidate_ids": diagnostic_candidate_ids,
                    "run_id": f"{plan['run_namespace']}{task_id}",
                    "data_role": "B3",
                    "measurement_mode": measurement_mode,
                    "logical_profile_id": profile_record["profile_id"],
                    "profile_sha256": profile_record["profile_sha256"],
                    "configuration_sha256": configuration_sha256,
                    "profile_path": profile_record["path"],
                    "ranking_evidence": False,
                    "dependencies": [diagnostic_dependency],
                    "status": status,
                    "evidence_sha256": evidence_sha256,
                    "evidence_physical_sha256": evidence_physical_sha256,
                    "evidence_path": evidence_path,
                    "started_at": started_at_value,
                    "completed_at": completed_at_value,
                    "failure_reason": failure_reason,
                }
            )
            diagnostic_dependency = task_id
        unknown_diagnostic = sorted(set(diagnostic_run_paths) - {
            item["task_id"] for item in diagnostic_tasks
        })
        if unknown_diagnostic:
            raise ConfigurationError(
                "unknown candidate diagnostic task: " + ", ".join(unknown_diagnostic)
            )
        diagnostic_study_path = study_paths.get("diagnostic")
        raw_diagnostics_terminal = all(
            item["status"] in {"completed", "failed", "interrupted"}
            for item in diagnostic_tasks
        )
        pair_tasks = [item for item in diagnostic_tasks if item["kind"] == "pair"]
        automatic_study_ineligibility = (
            "fewer_than_two_top3"
            if not pair_tasks
            else None
        )
        if automatic_study_ineligibility is not None:
            if diagnostic_study_path is not None:
                raise ValidationError(
                    "candidate diagnostic study is forbidden without analyzable pair evidence"
                )
            diagnostic_study_task = {
                "task_id": "diagnostic.study",
                "dependencies": [diagnostic_dependency],
                "status": "ineligible" if raw_diagnostics_terminal else "pending",
                "evidence_kind": None,
                "evidence_sha256": None,
                "evidence_physical_sha256": None,
                "evidence_path": None,
                "started_at": None,
                "completed_at": (
                    diagnostic_tasks[-1]["completed_at"]
                    if raw_diagnostics_terminal
                    else None
                ),
                "ineligibility_reason": (
                    automatic_study_ineligibility
                    if raw_diagnostics_terminal
                    else None
                ),
            }
        elif diagnostic_study_path is None:
            diagnostic_study_task = {
                "task_id": "diagnostic.study",
                "dependencies": [diagnostic_dependency],
                "status": "pending",
                "evidence_kind": None,
                "evidence_sha256": None,
                "evidence_physical_sha256": None,
                "evidence_path": None,
                "started_at": None,
                "completed_at": None,
                "ineligibility_reason": None,
            }
        else:
            if not raw_diagnostics_terminal:
                raise ValidationError(
                    "candidate diagnostic study leapfrogs unfinished raw diagnostics"
                )
            diagnostic_study = _load_version(
                diagnostic_study_path,
                "candidate-study.v1",
                label="candidate diagnostic interaction study",
            )
            if diagnostic_study["study_id"] != f"{plan['campaign_id']}:study:diagnostic":
                raise ValidationError("candidate diagnostic study id differs")
            pair_runs = {
                frozenset(task["candidate_ids"]): (
                    task,
                    diagnostic_loaded_runs[task["task_id"]],
                )
                for task in pair_tasks
            }
            _validate_candidate_diagnostic_study(
                study=diagnostic_study,
                formal_b3_study=b3_study,
                baseline=loaded_runs["run.B3.full"],
                candidate_runs={
                    candidate_id: loaded_runs[f"run.B3.{candidate_id}"]
                    for candidate_id in top3
                },
                pair_runs=pair_runs,
                top3_candidate_ids=top3,
                matrix_sha256=sha256_json(diagnostic_matrix),
                catalog=catalog,
                raw_verifications=raw_verifications,
            )
            diagnostic_study_task = {
                "task_id": "diagnostic.study",
                "dependencies": [diagnostic_dependency],
                "status": "completed",
                "evidence_kind": "candidate-study.v1",
                "evidence_sha256": sha256_json(diagnostic_study),
                "evidence_physical_sha256": sha256_file(diagnostic_study_path),
                "evidence_path": status_evidence_path(
                    diagnostic_study_path,
                    label="candidate diagnostic study evidence",
                ),
                "started_at": diagnostic_study["generated_at"],
                "completed_at": diagnostic_study["generated_at"],
                "ineligibility_reason": None,
            }
        _validate_candidate_diagnostic_sequence(
            source_status=statuses_by_id["study.B3"],
            tasks=[*diagnostic_tasks, diagnostic_study_task],
        )
        diagnostic_plan = {
            "source_freeze_sha256": statuses_by_id["freeze"]["evidence_sha256"],
            "source_study_sha256": sha256_json(b3_study),
            "matrix": _frozen_artifact_digest(
                root,
                diagnostic_matrix_path,
                diagnostic_matrix,
                label="candidate diagnostic matrix",
            ),
            "top3_candidate_ids": top3,
            "tasks": diagnostic_tasks,
            "study": diagnostic_study_task,
        }
    elif diagnostic_matrix_path is not None or diagnostic_run_paths:
        raise ConfigurationError("candidate diagnostics require an exact completed B3 study")

    if previous is not None:
        previous_diagnostic = previous["diagnostic_plan"]
        if previous_diagnostic is not None and diagnostic_plan is None:
            raise ValidationError("candidate diagnostic child plan cannot disappear")
        if previous_diagnostic is not None and diagnostic_plan is not None:
            if any(
                previous_diagnostic[key] != diagnostic_plan[key]
                for key in (
                    "source_freeze_sha256",
                    "source_study_sha256",
                    "matrix",
                    "top3_candidate_ids",
                )
            ):
                raise ValidationError("candidate diagnostic child plan identity is immutable")
            enforce_terminal_task_immutability(
                previous_rows=previous_diagnostic["tasks"],
                current_rows=diagnostic_plan["tasks"],
                label="candidate diagnostic campaign",
            )
            enforce_terminal_task_immutability(
                previous_rows=[previous_diagnostic["study"]],
                current_rows=[diagnostic_plan["study"]],
                label="candidate diagnostic study",
            )

    if final is not None:
        if diagnostic_plan is None:
            raise ValidationError(
                "candidate campaign final lacks its exact diagnostic child plan"
            )
        if (
            previous_status_path is None
            or not status_ledger_paths
            or freeze_path is None
            or "B2" not in study_paths
        ):
            raise ConfigurationError(
                "candidate campaign final registration requires the ordered "
                "pre-final ledger and exact B2/freeze inputs"
            )
        ordered_run_ids = [
            *[
                task["task_id"]
                for task in plan["tasks"]
                if task["task_id"] in loaded_runs
            ],
            *[
                task["task_id"]
                for task in diagnostic_plan["tasks"]
                if task["task_id"] in diagnostic_loaded_runs
            ],
        ]
        all_loaded_runs = {**loaded_runs, **diagnostic_loaded_runs}
        expected_run_records = [
            {
                "task_id": task_id,
                "run_id": all_loaded_runs[task_id]["run_id"],
                "run_sha256": sha256_json(all_loaded_runs[task_id]),
                "run_physical_sha256": sha256_file(run_paths[task_id]),
                "state": all_loaded_runs[task_id]["state"],
            }
            for task_id in ordered_run_ids
        ]
        expected_studies = {
            role: (
                None
                if role not in loaded_studies
                else _candidate_final_study_ref(
                    loaded_studies[role],
                    study_path=study_paths[role],
                    workspace_root=root,
                )
            )
            for role in ("B2", "B3", "B4", "B5", "B6")
        }
        b1_full = loaded_runs.get("run.B1.full")
        if b1_full is None:
            raise ValidationError("candidate campaign final lacks B1 FULL evidence")
        expected_b1_full = {
            "run_id": b1_full["run_id"],
            "run_sha256": sha256_json(b1_full),
            "configuration_sha256": b1_full["configuration_sha256"],
            "suite_id": b1_full["suite_id"],
            "manifest_sha256": b1_full["manifest_sha256"],
            "case_count": b1_full["manifest_case_count"],
            "evidence_level": b1_full["configuration"]["evidence_level"],
            "state": b1_full["state"],
            "passed_cases": b1_full["summary"]["passed_cases"],
            "failed_cases": b1_full["summary"]["failed_cases"],
            "pending_cases": b1_full["summary"]["pending_cases"],
            "censored_cases": b1_full["summary"]["censored_cases"],
            "all_correct": (
                b1_full["state"] == "completed"
                and b1_full["summary"]["failed_cases"] == 0
                and b1_full["summary"]["pending_cases"] == 0
            ),
            "failure_reason": None,
        }
        if (
            final["campaign"]["run_records"] != expected_run_records
            or final["studies"] != expected_studies
            or final["freeze"]["b2_study_sha256"]
            != sha256_json(loaded_studies["B2"])
            or final["freeze"]["artifact"]
            != _frozen_artifact_digest(
                root,
                freeze_path,
                freeze,
                label="candidate final freeze",
            )
            or final["diagnostics"]
            != _candidate_final_diagnostic_ref(
                diagnostic_plan,
                campaign_id=plan["campaign_id"],
                study_path=diagnostic_study_path,
                workspace_root=root,
            )
            or final["b1_full_correctness"] != expected_b1_full
        ):
            raise ValidationError(
                "candidate campaign final raw/study/diagnostic bindings differ"
            )
        expected_final = build_candidate_final(
            screening_path=root / plan["artifacts"]["screening"]["path"],
            catalog_path=root
            / plan["artifacts"]["candidate_registry"]["path"],
            matrix_path=root / plan["artifacts"]["matrix"]["path"],
            workspace_root=root,
            campaign_plan_path=plan_path,
            campaign_status_path=previous_status_path,
            status_ledger_paths=status_ledger_paths,
            run_paths=run_paths,
            b2_study_path=study_paths["B2"],
            study_paths={
                role: study_paths[role]
                for role in ("B3", "B4", "B5", "B6")
                if role in study_paths
            },
            diagnostic_study_path=study_paths.get("diagnostic"),
            freeze_path=freeze_path,
            final_id=final["final_id"],
            _raw_evidence_verifier=raw_evidence_verifier,
        )
        _require_exact_candidate_final_derivation(final, expected_final)

    if diagnostic_plan is not None:
        diagnostic_ready: list[str] = []
        diagnostic_active = False
        for task in [*diagnostic_plan["tasks"], diagnostic_plan["study"]]:
            if task["status"] in {
                "completed",
                "failed",
                "interrupted",
                "ineligible",
            }:
                continue
            diagnostic_active = True
            if task["status"] == "pending" and not diagnostic_ready:
                diagnostic_ready = [task["task_id"]]
            break
        if diagnostic_active:
            for task in plan["tasks"]:
                if task["stage"] in {"B4", "B5", "B6", "final"} and statuses_by_id[
                    task["task_id"]
                ]["evidence_sha256"] is not None:
                    raise ValidationError(
                        "candidate ranking evidence leapfrogs unfinished diagnostics"
                    )
            ready_tasks = diagnostic_ready

    final_status = statuses_by_id["final"]["status"]
    if final_status == "completed":
        state = "completed"
        ready_tasks = []
    elif statuses_by_id["freeze"]["status"] == "ineligible":
        state = "failed"
    elif not any(row["evidence_sha256"] is not None for row in statuses):
        state = "pending"
    elif ready_tasks:
        state = "running"
    else:
        pending = {row["task_id"] for row in statuses if row["status"] == "pending"}
        blockers = {
            dependency
            for task in plan["tasks"]
            if task["task_id"] in pending
            for dependency in [*task["dependencies"], *task["terminal_dependencies"]]
            if statuses_by_id[dependency]["status"] in {"failed", "interrupted"}
        }
        if any(statuses_by_id[item]["status"] == "interrupted" for item in blockers):
            state = "interrupted"
        elif blockers:
            state = "failed"
        else:
            state = "running"
    if state in {"completed", "failed", "interrupted"}:
        ready_tasks = []
    status = validate_document(
        {
            "schema_version": "candidate-campaign-status.v1",
            "campaign_id": plan["campaign_id"],
            "plan_sha256": plan_sha256,
            "execution_environment_sha256": plan[
                "execution_environment_sha256"
            ],
            "analyzer": plan["analyzer"],
            "previous_status_sha256": previous_sha256,
            "state": state,
            "started_at": chain_started_at,
            "as_of": observation_time,
            "raw_evidence_registry": raw_evidence_registry_artifact,
            "tasks": statuses,
            "ready_tasks": ready_tasks,
            "diagnostic_plan": diagnostic_plan,
        }
    )
    if raw_snapshot_cache is not None:
        raw_snapshot_cache.assert_unchanged()
    return status


def _clean_repository_identity(
    workspace_root: Path,
    expected_commit: str | None = None,
) -> tuple[str, str]:
    root = _candidate_workspace_root(workspace_root)

    def git(*arguments: str) -> str:
        try:
            result = subprocess.run(
                ("git", "--no-optional-locks", "-C", str(root), *arguments),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConfigurationError(
                "candidate freeze Git provenance command did not complete"
            ) from exc
        if result.returncode != 0:
            raise ConfigurationError(
                "candidate freeze Git provenance command failed"
            )
        try:
            return result.stdout.decode("utf-8", errors="strict").strip()
        except UnicodeError as exc:
            raise ValidationError(
                "candidate freeze Git provenance output is not UTF-8"
            ) from exc

    head = git("rev-parse", "--verify", "HEAD").lower()
    if expected_commit is not None and head != expected_commit.lower():
        raise ValidationError("candidate freeze workspace HEAD differs from B2 evidence")
    if git("status", "--porcelain=v1", "--untracked-files=normal"):
        raise ValidationError("candidate freeze requires a clean workspace")
    tree = git("rev-parse", "--verify", "HEAD^{tree}").lower()
    if len(tree) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in tree
    ):
        raise ValidationError("candidate freeze repository tree id is malformed")
    return head, tree


def _reverify_candidate_status_evidence(
    *,
    workspace_root: Path,
    row: Mapping[str, Any],
    version: str,
    label: str,
    cache: dict[tuple[str, str], dict[str, str]] | None = None,
) -> dict[str, str] | None:
    evidence_path = row["evidence_path"]
    if evidence_path is None:
        return None
    root = _candidate_workspace_root(workspace_root)
    _, physical, relative = _workspace_regular_path(
        root,
        root / evidence_path,
        label=label,
    )
    key = (relative.as_posix(), version)
    artifact = None if cache is None else cache.get(key)
    if artifact is None:
        document = _load_version(physical, version, label=label)
        artifact = {
            "path": relative.as_posix(),
            "canonical_sha256": sha256_json(document),
            "physical_sha256": sha256_file(physical),
        }
        if cache is not None:
            cache[key] = artifact
    if (
        artifact["canonical_sha256"] != row["evidence_sha256"]
        or artifact["physical_sha256"] != row["evidence_physical_sha256"]
    ):
        raise ValidationError(f"{label} canonical/physical identity differs")
    return artifact


def _validate_candidate_status_ledger(
    *,
    plan: Mapping[str, Any],
    status: Mapping[str, Any],
    ledger_paths: Sequence[Path],
    workspace_root: Path,
    raw_evidence_verifier: Any | None = None,
) -> tuple[
    int,
    str,
    str,
    tuple[str, ...],
    tuple[dict[str, str], ...],
]:
    if not ledger_paths:
        raise ConfigurationError("candidate campaign requires the complete status ledger")
    plan_digest = sha256_json(plan)
    previous: dict[str, Any] | None = None
    started_at: str | None = None
    entries: list[dict[str, Any]] = []
    identities: list[dict[str, str]] = []
    evidence_cache: dict[tuple[str, str], dict[str, str]] = {}
    for index, path in enumerate(ledger_paths):
        physical_path = resolve_without_symlinks(
            path, label=f"candidate status ledger entry {index}"
        )
        if not physical_path.is_file():
            raise ValidationError(
                f"candidate status ledger entry {index} must be a regular file"
            )
        entry = _load_version(
            physical_path,
            "candidate-campaign-status.v1",
            label=f"candidate status ledger entry {index}",
        )
        if (
            entry["campaign_id"] != plan["campaign_id"]
            or entry["plan_sha256"] != plan_digest
            or entry["execution_environment_sha256"]
            != plan["execution_environment_sha256"]
            or entry["analyzer"] != plan["analyzer"]
        ):
            raise ValidationError("candidate status ledger entry binds another plan")
        raw_registry, raw_registry_artifact = (
            _load_and_reverify_candidate_raw_evidence_registry(
                plan=plan,
                registry_path=_candidate_workspace_root(workspace_root)
                / entry["raw_evidence_registry"]["path"],
                workspace_root=workspace_root,
                raw_evidence_verifier=raw_evidence_verifier,
            )
        )
        if raw_registry_artifact != entry["raw_evidence_registry"]:
            raise ValidationError(
                "candidate status ledger raw-evidence artifact differs"
            )
        raw_runs_by_task = {
            item["task_id"]: item["run_record"] for item in raw_registry["runs"]
        }
        for row in entry["tasks"]:
            evidence_kind = row["evidence_kind"]
            if evidence_kind is None:
                continue
            artifact = _reverify_candidate_status_evidence(
                workspace_root=workspace_root,
                row=row,
                version=evidence_kind,
                label=f"candidate status ledger evidence {row['task_id']}",
                cache=evidence_cache,
            )
            assert artifact is not None
            if evidence_kind == "run-record.v1" and (
                raw_runs_by_task.get(row["task_id"]) != artifact
            ):
                raise ValidationError(
                    "candidate status ledger run evidence differs from raw registry"
                )
        diagnostic = entry["diagnostic_plan"]
        if diagnostic is not None:
            for row in diagnostic["tasks"]:
                if row["evidence_path"] is None:
                    continue
                artifact = _reverify_candidate_status_evidence(
                    workspace_root=workspace_root,
                    row=row,
                    version="run-record.v1",
                    label=(
                        "candidate status ledger diagnostic evidence "
                        + row["task_id"]
                    ),
                    cache=evidence_cache,
                )
                assert artifact is not None
                if raw_runs_by_task.get(row["task_id"]) != artifact:
                    raise ValidationError(
                        "candidate status ledger diagnostic evidence differs "
                        "from raw registry"
                    )
            diagnostic_study = diagnostic["study"]
            if diagnostic_study["evidence_path"] is not None:
                _reverify_candidate_status_evidence(
                    workspace_root=workspace_root,
                    row=diagnostic_study,
                    version="candidate-study.v1",
                    label="candidate status ledger diagnostic study evidence",
                    cache=evidence_cache,
                )
        if previous is None:
            if entry["previous_status_sha256"] is not None:
                raise ValidationError("candidate status ledger does not start at genesis")
            started_at = entry["started_at"]
        else:
            if (
                entry["previous_status_sha256"] != sha256_json(previous)
                or entry["started_at"] != started_at
                or datetime.fromisoformat(entry["as_of"].replace("Z", "+00:00"))
                <= datetime.fromisoformat(previous["as_of"].replace("Z", "+00:00"))
            ):
                raise ValidationError("candidate status ledger hash/time chain differs")
            enforce_terminal_task_immutability(
                previous_rows=previous["tasks"],
                current_rows=entry["tasks"],
                label="candidate status ledger",
            )
        previous = entry
        entries.append(entry)
        identities.append(
            {
                "canonical_sha256": sha256_json(entry),
                "physical_sha256": sha256_file(physical_path),
            }
        )
    assert previous is not None
    if previous != status:
        raise ValidationError("candidate status ledger head differs from --status")
    for (relative, _), artifact in evidence_cache.items():
        _, physical, _ = _workspace_regular_path(
            _candidate_workspace_root(workspace_root),
            _candidate_workspace_root(workspace_root) / relative,
            label="candidate status ledger cached evidence",
        )
        if sha256_file(physical) != artifact["physical_sha256"]:
            raise ValidationError(
                "candidate status ledger evidence changed during validation"
            )
    return (
        len(entries),
        sha256_json(previous),
        sha256_json(identities),
        tuple(sha256_json(entry) for entry in entries),
        tuple(identities),
    )


def _require_candidate_ledger_prefix(
    *,
    binding: Mapping[str, Any],
    ledger_identities: Sequence[Mapping[str, str]],
    label: str,
) -> list[Mapping[str, str]]:
    prefix_count = binding["status_ledger_entry_count"]
    prefix = list(ledger_identities[:prefix_count])
    if (
        len(prefix) != prefix_count
        or not prefix
        or binding["status_sha256"] != prefix[-1]["canonical_sha256"]
        or binding["status_ledger_head_sha256"]
        != prefix[-1]["canonical_sha256"]
        or binding["status_ledger_sha256"] != sha256_json(prefix)
    ):
        raise ValidationError(
            f"{label} canonical/physical status-ledger prefix differs"
        )
    return prefix


def validate_candidate_final_completion(
    *,
    campaign_plan_path: Path,
    candidate_final_path: Path,
    completed_status_path: Path,
    status_ledger_paths: Sequence[Path],
    workspace_root: Path,
    _raw_snapshot_cache: _ReadOnlyRawEvidenceCache | None = None,
    _raw_evidence_verifier: Any | None = None,
) -> dict[str, Any]:
    """Verify the post-final terminal ledger before publishing a final report."""

    raw_snapshot_cache, raw_evidence_verifier = (
        _candidate_read_only_raw_verifier(
            raw_snapshot_cache=_raw_snapshot_cache,
            raw_evidence_verifier=_raw_evidence_verifier,
        )
    )
    root = _candidate_workspace_root(workspace_root)
    _, plan_physical, _ = _workspace_regular_path(
        root, campaign_plan_path, label="candidate report campaign plan"
    )
    _, final_physical, final_relative = _workspace_regular_path(
        root, candidate_final_path, label="candidate report final"
    )
    _, status_physical, _ = _workspace_regular_path(
        root, completed_status_path, label="candidate report completed status"
    )
    ledger_physical = [
        _workspace_regular_path(
            root, path, label=f"candidate report status ledger entry {index}"
        )[1]
        for index, path in enumerate(status_ledger_paths)
    ]
    plan = _load_version(
        plan_physical,
        "candidate-campaign-plan.v1",
        label="candidate report campaign plan",
    )
    final = _load_version(
        final_physical, "candidate-final.v1", label="candidate report final"
    )
    completed_status = _load_version(
        status_physical,
        "candidate-campaign-status.v1",
        label="candidate report completed status",
    )
    completed_raw_registry, completed_raw_registry_artifact = (
        _load_and_reverify_candidate_raw_evidence_registry(
            plan=plan,
            registry_path=root
            / completed_status["raw_evidence_registry"]["path"],
            workspace_root=root,
            raw_evidence_verifier=raw_evidence_verifier,
        )
    )
    if (
        completed_raw_registry_artifact
        != completed_status["raw_evidence_registry"]
        or completed_raw_registry_artifact
        != final["campaign"]["raw_evidence_registry"]
    ):
        raise ValidationError(
            "candidate final report raw evidence differs from terminal campaign state"
        )
    (
        ledger_entry_count,
        ledger_head_sha256,
        ledger_sha256,
        _,
        ledger_identities,
    ) = _validate_candidate_status_ledger(
        plan=plan,
        status=completed_status,
        ledger_paths=ledger_physical,
        workspace_root=root,
        raw_evidence_verifier=raw_evidence_verifier,
    )
    plan_sha256 = sha256_json(plan)
    final_sha256 = sha256_json(final)
    final_physical_sha256 = sha256_file(final_physical)
    status_by_id = {
        item["task_id"]: item for item in completed_status["tasks"]
    }
    final_task = status_by_id["final"]
    prefix_count = final["campaign"]["status_ledger_entry_count"]
    prefix = _require_candidate_ledger_prefix(
        binding=final["campaign"],
        ledger_identities=ledger_identities,
        label="candidate final report",
    )
    if (
        completed_status["campaign_id"] != plan["campaign_id"]
        or completed_status["plan_sha256"] != plan_sha256
        or completed_status["state"] != "completed"
        or completed_status["ready_tasks"]
        or final_task["status"] != "completed"
        or final_task["evidence_kind"] != "candidate-final.v1"
        or final_task["evidence_sha256"] != final_sha256
        or final_task["evidence_physical_sha256"] != final_physical_sha256
        or final_task["evidence_path"] != final_relative.as_posix()
        or final["campaign"]["plan_sha256"] != plan_sha256
        or final["freeze"]["campaign_id"] != plan["campaign_id"]
        or final["freeze"]["run_namespace"] != plan["run_namespace"]
        or ledger_entry_count != prefix_count + 1
        or completed_status["previous_status_sha256"]
        != prefix[-1]["canonical_sha256"]
        or ledger_identities[-1]["canonical_sha256"]
        != sha256_json(completed_status)
        or ledger_identities[-1]["physical_sha256"]
        != sha256_file(status_physical)
    ):
        raise ValidationError(
            "candidate final report requires the exact terminal final/ledger closure"
        )
    run_paths = {
        item["task_id"]: root / item["run_record"]["path"]
        for item in completed_raw_registry["runs"]
    }
    b2_study_path = root / final["studies"]["B2"]["path"]
    main_study_paths = {
        role: root / study["path"]
        for role in ("B3", "B4", "B5", "B6")
        if (study := final["studies"][role]) is not None
    }
    diagnostic_study = final["diagnostics"]["study"]
    expected_final = build_candidate_final(
        screening_path=root / plan["artifacts"]["screening"]["path"],
        catalog_path=root / plan["artifacts"]["candidate_registry"]["path"],
        matrix_path=root / plan["artifacts"]["matrix"]["path"],
        workspace_root=root,
        campaign_plan_path=plan_physical,
        campaign_status_path=ledger_physical[prefix_count - 1],
        status_ledger_paths=ledger_physical[:prefix_count],
        run_paths=run_paths,
        b2_study_path=b2_study_path,
        study_paths=main_study_paths,
        diagnostic_study_path=(
            None if diagnostic_study is None else root / diagnostic_study["path"]
        ),
        freeze_path=root / final["freeze"]["artifact"]["path"],
        final_id=final["final_id"],
        _raw_evidence_verifier=raw_evidence_verifier,
    )
    if expected_final != final:
        raise ValidationError(
            "candidate final report content differs from the exact replayed campaign"
        )
    completion = {
        "campaign_id": plan["campaign_id"],
        "plan_sha256": plan_sha256,
        "candidate_final_sha256": final_sha256,
        "candidate_final_physical_sha256": final_physical_sha256,
        "completed_status_sha256": ledger_head_sha256,
        "completed_status_physical_sha256": sha256_file(status_physical),
        "status_ledger_entry_count": ledger_entry_count,
        "status_ledger_head_sha256": ledger_head_sha256,
        "status_ledger_sha256": ledger_sha256,
        "raw_evidence_registry_sha256": completed_raw_registry_artifact[
            "canonical_sha256"
        ],
        "raw_evidence_registry_physical_sha256": completed_raw_registry_artifact[
            "physical_sha256"
        ],
    }
    if raw_snapshot_cache is not None:
        raw_snapshot_cache.assert_unchanged()
    return completion


def finalize_candidate_campaign(
    *,
    plan_path: Path,
    status_path: Path,
    status_ledger_paths: Sequence[Path],
    study_path: Path,
    catalog_path: Path,
    pass_registry_path: Path,
    matrix_path: Path,
    screening_path: Path,
    oracle_capture_path: Path,
    suite_paths: Mapping[str, Path],
    measurement_protocol_path: Path,
    hotblock_measurement_protocol_path: Path,
    reference_toolchain_path: Path,
    compiler_artifact_path: Path,
    workspace_root: Path,
    freeze_id: str,
    _raw_snapshot_cache: _ReadOnlyRawEvidenceCache | None = None,
    _raw_evidence_verifier: Any | None = None,
) -> dict[str, Any]:
    """Seal the completed B2 campaign into the immutable pre-B3 freeze."""

    raw_snapshot_cache, raw_evidence_verifier = (
        _candidate_read_only_raw_verifier(
            raw_snapshot_cache=_raw_snapshot_cache,
            raw_evidence_verifier=_raw_evidence_verifier,
        )
    )
    plan = _load_version(
        plan_path, "candidate-campaign-plan.v1", label="candidate campaign plan"
    )
    status = _load_version(
        status_path, "candidate-campaign-status.v1", label="candidate campaign status"
    )
    study = _load_version(
        study_path, "candidate-study.v1", label="candidate B2 study"
    )
    catalog = _load_version(
        catalog_path, "candidate-catalog.v1", label="candidate registry"
    )
    pass_registry = _load_version(
        pass_registry_path, "pass-registry.v2", label="pass registry"
    )
    matrix = _load_version(
        matrix_path, "candidate-profile-matrix.v1", label="candidate matrix"
    )
    plan_artifacts = plan["artifacts"]
    if (
        _frozen_artifact_digest(
            workspace_root, catalog_path, catalog, label="candidate registry"
        ) != plan_artifacts["candidate_registry"]
        or _frozen_artifact_digest(
            workspace_root,
            pass_registry_path,
            pass_registry,
            label="pass registry",
        ) != plan_artifacts["executable_pass_registry"]
        or _frozen_artifact_digest(
            workspace_root, matrix_path, matrix, label="candidate matrix"
        ) != plan_artifacts["matrix"]
        or matrix["candidate_registry_sha256"] != sha256_json(catalog)
        or matrix["pass_registry_sha256"] != sha256_json(pass_registry)
    ):
        raise ValidationError(
            "candidate freeze registry/matrix inputs differ from the B2 plan"
        )
    matrix_profiles = _verify_matrix_profiles(
        workspace_root,
        matrix,
        candidate_order=[item["candidate_id"] for item in catalog["candidates"]],
    )
    screening = _load_and_reverify_candidate_screening(
        screening_path=screening_path,
        workspace_root=workspace_root,
        raw_evidence_verifier=raw_evidence_verifier,
    )
    base_pass_registry = _require_executable_registry_bridge(
        screening=screening,
        catalog=catalog,
        executable_registry=pass_registry,
        workspace_root=workspace_root,
    )
    if _frozen_artifact_digest(
        workspace_root,
        screening_path,
        screening,
        label="candidate screening",
    ) != plan_artifacts["screening"] or screening["base_pass_registry"] != (
        plan_artifacts["screening_base_pass_registry"]
    ):
        raise ValidationError("candidate freeze screening differs from the campaign plan")
    oracle_capture = _load_and_reverify_candidate_oracle_capture(
        capture_path=oracle_capture_path,
        workspace_root=workspace_root,
        raw_evidence_verifier=raw_evidence_verifier,
    )
    _require_screening_capture_binding(screening, oracle_capture)
    plan_digest = sha256_json(plan)
    study_digest = sha256_json(study)
    (
        ledger_entry_count,
        ledger_head_sha256,
        ledger_sha256,
        _,
        _,
    ) = _validate_candidate_status_ledger(
        plan=plan,
        status=status,
        ledger_paths=status_ledger_paths,
        workspace_root=workspace_root,
        raw_evidence_verifier=raw_evidence_verifier,
    )
    _, raw_evidence_registry_artifact = (
        _load_and_reverify_candidate_raw_evidence_registry(
            plan=plan,
            registry_path=_candidate_workspace_root(workspace_root)
            / status["raw_evidence_registry"]["path"],
            workspace_root=workspace_root,
            raw_evidence_verifier=raw_evidence_verifier,
        )
    )
    if raw_evidence_registry_artifact != status["raw_evidence_registry"]:
        raise ValidationError(
            "candidate freeze raw-evidence registry differs from pre-B3 status"
        )
    status_by_id = {item["task_id"]: item for item in status["tasks"]}
    if (
        status["campaign_id"] != plan["campaign_id"]
        or status["plan_sha256"] != plan_digest
        or status["execution_environment_sha256"]
        != plan["execution_environment_sha256"]
        or status["analyzer"] != plan["analyzer"]
        or status_by_id["freeze"]["status"] != "pending"
        or status["ready_tasks"] != ["freeze"]
    ):
        raise ValidationError("only the exact dependency-ready pre-B3 task may freeze")
    if (
        status_by_id["study.B2"]["evidence_sha256"] != study_digest
        or study["study_id"] != plan["study_ids"]["B2"]
        or study["data_role"] != "B2"
    ):
        raise ValidationError("candidate freeze B2 study binding differs")
    if set(suite_paths) != set(_CANDIDATE_SUITE_CASE_COUNTS):
        raise ConfigurationError("candidate freeze requires exactly B1 through B6 manifests")
    suites: list[dict[str, Any]] = []
    suite_by_role: dict[str, dict[str, Any]] = {}
    for role, expected_count in _CANDIDATE_SUITE_CASE_COUNTS.items():
        manifest = _load_version(
            suite_paths[role],
            "benchmark-manifest.v1",
            label=f"candidate {role} manifest",
        )
        if (
            manifest["provenance"]["data_role"] != role
            or len(manifest["cases"]) != expected_count
            or {case["target"] for case in manifest["cases"]} != {"rv64gc"}
        ):
            raise ValidationError(
                f"candidate freeze {role} manifest must contain exactly {expected_count} RV64GC cases"
            )
        require_formal_suite_contract(
            role=role,
            manifest=manifest,
            manifest_path=suite_paths[role],
        )
        row = {
            "data_role": role,
            "suite_id": manifest["suite_id"],
            "manifest": _frozen_artifact_digest(
                workspace_root,
                suite_paths[role],
                manifest,
                label=f"candidate {role} manifest",
            ),
            "case_count": expected_count,
        }
        suites.append(row)
        suite_by_role[role] = row
    b2_suite = suite_by_role["B2"]
    if (
        study["suite_id"] != b2_suite["suite_id"]
        or study["manifest_sha256"]
        != b2_suite["manifest"]["canonical_sha256"]
        or next(item for item in plan["suites"] if item["data_role"] == "B2")
        != b2_suite
    ):
        raise ValidationError("candidate freeze B2 manifest differs from plan/study")
    qualified_ids = [
        item["implementation_candidate_id"]
        for item in screening["candidates"]
        if item["qualification_status"] == "qualified"
    ]
    assert all(item is not None for item in qualified_ids)
    frozen_candidate_ids = plan["qualified_candidate_ids"]
    b1_passed_ids = [
        candidate_id
        for candidate_id in frozen_candidate_ids
        if status_by_id[f"run.B1.{candidate_id}"]["status"] == "completed"
    ]
    b1_failed_ids = [
        candidate_id
        for candidate_id in frozen_candidate_ids
        if status_by_id[f"run.B1.{candidate_id}"]["status"]
        in {"failed", "interrupted"}
    ]
    if (
        frozen_candidate_ids != qualified_ids
        or set(b1_passed_ids).isdisjoint(b1_failed_ids) is False
        or set(b1_passed_ids) | set(b1_failed_ids) != set(frozen_candidate_ids)
        or [item["candidate_id"] for item in study["candidates"]]
        != b1_passed_ids
        or any(not item["eligible_for_ranking"] for item in study["candidates"])
        or status_by_id["run.B1.full"]["status"] != "completed"
    ):
        raise ValidationError(
            "candidate freeze B1/B2 selection differs from completed-correct candidates"
        )
    planned_candidate_ids = [
        task["candidate_ids"][0]
        for task in plan["tasks"]
        if task["kind"] == "single" and task["data_role"] == "B2"
    ]
    if planned_candidate_ids != frozen_candidate_ids:
        raise ValidationError("candidate freeze B2 plan differs from frozen candidates")
    if oracle_capture["pair_count"] != 99:
        raise ValidationError("candidate freeze requires the complete 99-pair Oracle capture")
    environment = build_campaign_environment_contract(
        measurement_protocol_path=measurement_protocol_path,
        hotblock_measurement_protocol_path=hotblock_measurement_protocol_path,
        reference_toolchain_path=reference_toolchain_path,
        workspace_root=workspace_root,
        include_candidate_workspace_bootstrap=True,
    )
    standard_protocol = environment["measurement_protocols"]["standard_proxy"]
    standard_protocol_document = _load_version(
        measurement_protocol_path,
        "measurement-protocol.v1",
        label="standard measurement protocol",
    )
    binding = study["bindings"]
    if (
        binding["repo_dirty"]
        or binding["tracked_diff_sha256"] is not None
        or binding["execution_environment_sha256"]
        != plan["execution_environment_sha256"]
        or binding["measurement_protocol_id"] != standard_protocol["protocol_id"]
        or binding["measurement_protocol_sha256"]
        != standard_protocol["protocol_sha256"]
    ):
        raise ValidationError(
            "candidate freeze B2 study is not clean standard-proxy evidence"
        )
    _, repository_tree = _clean_repository_identity(
        workspace_root, binding["repo_commit"]
    )
    compiler_artifact = _frozen_compiler_artifact(
        workspace_root, compiler_artifact_path
    )
    if compiler_artifact["physical_sha256"] != binding["compiler_artifact_sha256"]:
        raise ValidationError(
            "candidate freeze compiler artifact bytes differ from B2 provenance"
        )
    if (
        plan["repository"]["repo_commit"] != binding["repo_commit"]
        or plan["repository"]["repo_tree"] != repository_tree
        or plan["repository"]["compiler_artifact"] != compiler_artifact
    ):
        raise ValidationError("candidate freeze repository/artifact differs from plan")
    base_profile = plan["base_pipeline_profile"]
    if (
        base_profile["profile_id"] != "candidate-empty"
        or {
            "profile_id": base_profile["profile_id"],
            "profile_sha256": base_profile["profile_sha256"],
        }
        != matrix["base_pipeline_profile"]
    ):
        raise ValidationError("candidate freeze base profile is not exact FULL")
    base_profile_record = matrix_profiles["candidate-empty"]
    base_profile_path = (
        _candidate_workspace_root(workspace_root) / base_profile_record["path"]
    )
    base_profile_document = validate_pipeline_profile_v2(
        read_json(base_profile_path),
        candidate_order=[item["candidate_id"] for item in catalog["candidates"]],
    )
    base_profile_artifact = _frozen_artifact_digest(
        workspace_root,
        base_profile_path,
        base_profile_document,
        label="candidate-empty profile",
    )
    if (
        base_profile_artifact["physical_sha256"] != base_profile["profile_sha256"]
        or base_profile_artifact["physical_sha256"] != base_profile["physical_sha256"]
        or base_profile_artifact["path"] != base_profile["path"]
    ):
        raise ValidationError("candidate freeze base profile physical hash differs")

    protocol_documents = {
        "standard_proxy": standard_protocol_document,
        "cache_hotblock": _load_version(
            hotblock_measurement_protocol_path,
            "measurement-protocol.v1",
            label="cache-hotblock measurement protocol",
        ),
    }
    protocol_paths = {
        "standard_proxy": measurement_protocol_path,
        "cache_hotblock": hotblock_measurement_protocol_path,
    }
    frozen_protocols: dict[str, dict[str, Any]] = {}
    for mode in ("standard_proxy", "cache_hotblock"):
        contract = dict(environment["measurement_protocols"][mode])
        artifact = _frozen_artifact_digest(
            workspace_root,
            protocol_paths[mode],
            protocol_documents[mode],
            label=f"candidate {mode} protocol",
        )
        if artifact["canonical_sha256"] != contract["protocol_sha256"]:
            raise ValidationError(
                f"candidate {mode} protocol canonical hash differs"
            )
        contract["path"] = artifact["path"]
        contract["physical_sha256"] = artifact["physical_sha256"]
        frozen_protocols[mode] = contract
    if frozen_protocols != plan["measurement_protocols"]:
        raise ValidationError("candidate freeze measurement protocols differ from plan")
    if environment["analyzer"] != plan["analyzer"]:
        raise ValidationError("candidate freeze analyzer contract differs from plan")
    toolchain_document = read_json(reference_toolchain_path)
    if not isinstance(toolchain_document, dict):
        raise ValidationError("candidate reference toolchain must be a JSON object")
    toolchain_contract = dict(environment["reference_toolchain"])
    expected_toolchain_physical = toolchain_contract.pop("snapshot_sha256")
    toolchain_artifact = _frozen_artifact_digest(
        workspace_root,
        reference_toolchain_path,
        toolchain_document,
        label="candidate reference toolchain",
    )
    if toolchain_artifact["physical_sha256"] != expected_toolchain_physical:
        raise ValidationError("candidate reference toolchain physical hash differs")
    toolchain_contract["snapshot"] = toolchain_artifact
    if toolchain_contract != plan["reference_toolchain"]:
        raise ValidationError("candidate freeze toolchain contract differs from plan")
    execution_environment_sha256 = candidate_execution_environment_sha256(
        environment={
            **environment,
            "measurement_protocols": frozen_protocols,
            "reference_toolchain": toolchain_contract,
        },
        compiler_artifact_sha256=compiler_artifact["physical_sha256"],
    )
    if plan["execution_environment_sha256"] != execution_environment_sha256:
        raise ValidationError("candidate freeze execution environment differs from plan")

    schema_paths = {
        "run_record_schema": workspace_root
        / "tools/benchmark/schemas/run-record.v1.json",
        "candidate_study_schema": workspace_root
        / "tools/benchmark/schemas/candidate-study.v1.json",
    }
    schema_documents = {
        key: read_json(path) for key, path in schema_paths.items()
    }
    frozen_snapshots = {
        "candidate_registry": _frozen_artifact_digest(
            workspace_root, catalog_path, catalog, label="candidate registry"
        ),
        "executable_pass_registry": _frozen_artifact_digest(
            workspace_root,
            pass_registry_path,
            pass_registry,
            label="candidate executable PassRegistry v2",
        ),
        "screening_base_pass_registry": _frozen_artifact_digest(
            workspace_root,
            workspace_root / screening["base_pass_registry"]["path"],
            base_pass_registry,
            label="candidate screening base PassRegistry v2",
        ),
        "matrix": _frozen_artifact_digest(
            workspace_root, matrix_path, matrix, label="candidate matrix"
        ),
        "screening": _frozen_artifact_digest(
            workspace_root, screening_path, screening, label="candidate screening"
        ),
        "oracle_capture": _frozen_artifact_digest(
            workspace_root,
            oracle_capture_path,
            oracle_capture,
            label="candidate Oracle capture",
        ),
        "run_record_schema": _frozen_artifact_digest(
            workspace_root,
            schema_paths["run_record_schema"],
            schema_documents["run_record_schema"],
            label="run-record schema",
        ),
        "candidate_study_schema": _frozen_artifact_digest(
            workspace_root,
            schema_paths["candidate_study_schema"],
            schema_documents["candidate_study_schema"],
            label="candidate-study schema",
        ),
        "candidate_evidence_sha256": screening["candidate_evidence_sha256"],
        "screening_spec_sha256": screening["screening_spec_sha256"],
        "oracle_plan_sha256": oracle_capture["oracle_plan_sha256"],
    }
    run_namespace = plan["run_namespace"]
    if any(
        not task["run_id"].startswith(run_namespace)
        for task in plan["tasks"]
        if task["run_id"] is not None
    ):
        raise ValidationError("candidate freeze plan run id escapes its namespace")
    freeze = validate_document(
        {
            "schema_version": "candidate-freeze.v1",
            "freeze_id": freeze_id,
            "frozen_at": status["as_of"],
            "campaign_id": plan["campaign_id"],
            "run_namespace": run_namespace,
            "repository": {
                "repo_commit": binding["repo_commit"],
                "repo_tree": repository_tree,
                "repo_dirty": False,
                "tracked_diff_sha256": None,
                "compiler_artifact": compiler_artifact,
            },
            "snapshots": frozen_snapshots,
            "base_pipeline_profile": {
                "profile_id": "candidate-empty",
                "artifact": base_profile_artifact,
            },
            "suites": suites,
            "measurement_protocols": frozen_protocols,
            "reference_toolchain": toolchain_contract,
            "analyzer": environment["analyzer"],
            "execution_environment_sha256": execution_environment_sha256,
            "gates": {
                "oracle_structure_geometric_mean_minimum": 1.10,
                "b3_geometric_mean_strictly_above": 1.0,
                "combined_case_count": 267,
                "evidence_claim": "qemu_proxy_only",
            },
            "ranking_rule": {
                "primary": "combined_geometric_mean_desc",
                "secondary": "b3_geometric_mean_desc",
                "tertiary": "static_text_bytes_asc",
                "quaternary": "stable_candidate_id_asc",
            },
            "frozen_candidate_ids": frozen_candidate_ids,
            "frozen_candidate_ids_sha256": sha256_json(frozen_candidate_ids),
            "b2_campaign": {
                "plan_sha256": plan_digest,
                "status_sha256": sha256_json(status),
                "status_ledger_entry_count": ledger_entry_count,
                "status_ledger_head_sha256": ledger_head_sha256,
                "status_ledger_sha256": ledger_sha256,
                "raw_evidence_registry": raw_evidence_registry_artifact,
                "study_id": study["study_id"],
                "study_sha256": study_digest,
                "b1_full_run_id": next(
                    task["run_id"]
                    for task in plan["tasks"]
                    if task["task_id"] == "run.B1.full"
                ),
                "b1_full_run_sha256": status_by_id["run.B1.full"][
                    "evidence_sha256"
                ],
                "b1_passed_candidate_ids": b1_passed_ids,
                "b1_failed_candidate_ids": b1_failed_ids,
            },
        }
    )
    if raw_snapshot_cache is not None:
        raw_snapshot_cache.assert_unchanged()
    return freeze


def build_candidate_final(
    *,
    screening_path: Path,
    catalog_path: Path,
    matrix_path: Path,
    workspace_root: Path,
    campaign_plan_path: Path,
    campaign_status_path: Path,
    status_ledger_paths: Sequence[Path],
    run_paths: Mapping[str, Path],
    b2_study_path: Path,
    study_paths: Mapping[str, Path],
    diagnostic_study_path: Path | None,
    freeze_path: Path,
    final_id: str,
    _raw_snapshot_cache: _ReadOnlyRawEvidenceCache | None = None,
    _raw_evidence_verifier: Any | None = None,
) -> dict[str, Any]:
    """Build the B3/B4/B5/B6 final using equal weight for all 267 cases."""

    raw_snapshot_cache, raw_evidence_verifier = (
        _candidate_read_only_raw_verifier(
            raw_snapshot_cache=_raw_snapshot_cache,
            raw_evidence_verifier=_raw_evidence_verifier,
        )
    )
    if "B3" not in study_paths or not set(study_paths).issubset({"B3", "B4", "B5", "B6"}):
        raise ConfigurationError("candidate final requires B3 and only B3-B6 studies")
    plan = _load_version(
        campaign_plan_path,
        "candidate-campaign-plan.v1",
        label="candidate full-stage campaign plan",
    )
    campaign_status = _load_version(
        campaign_status_path,
        "candidate-campaign-status.v1",
        label="candidate full-stage campaign status",
    )
    plan_sha256 = sha256_json(plan)
    (
        ledger_entry_count,
        ledger_head_sha256,
        ledger_sha256,
        _,
        ledger_identities,
    ) = _validate_candidate_status_ledger(
        plan=plan,
        status=campaign_status,
        ledger_paths=status_ledger_paths,
        workspace_root=workspace_root,
        raw_evidence_verifier=raw_evidence_verifier,
    )
    status_by_id = {item["task_id"]: item for item in campaign_status["tasks"]}
    if (
        campaign_status["campaign_id"] != plan["campaign_id"]
        or campaign_status["plan_sha256"] != plan_sha256
        or campaign_status["execution_environment_sha256"]
        != plan["execution_environment_sha256"]
        or campaign_status["analyzer"] != plan["analyzer"]
        or campaign_status["ready_tasks"] != ["final"]
        or status_by_id["final"]["status"] != "pending"
    ):
        raise ValidationError("candidate final requires the exact terminal full-stage status")
    raw_evidence_registry, raw_evidence_registry_artifact = (
        _load_and_reverify_candidate_raw_evidence_registry(
            plan=plan,
            registry_path=_candidate_workspace_root(workspace_root)
            / campaign_status["raw_evidence_registry"]["path"],
            workspace_root=workspace_root,
            expected_run_paths=run_paths,
            raw_evidence_verifier=raw_evidence_verifier,
        )
    )
    if raw_evidence_registry_artifact != campaign_status["raw_evidence_registry"]:
        raise ValidationError(
            "candidate final raw-evidence registry differs from pre-final status"
        )
    raw_verifications = _raw_verifications_by_run_id(raw_evidence_registry)
    screening = _load_and_reverify_candidate_screening(
        screening_path=screening_path,
        workspace_root=workspace_root,
        raw_evidence_verifier=raw_evidence_verifier,
    )
    catalog = _load_version(
        catalog_path, "candidate-catalog.v1", label="candidate registry"
    )
    matrix = _load_version(
        matrix_path, "candidate-profile-matrix.v1", label="candidate profile matrix"
    )
    freeze = _load_version(
        freeze_path, "candidate-freeze.v1", label="candidate pre-B3 freeze"
    )
    frozen_documents = _verify_candidate_freeze_inputs(
        freeze,
        workspace_root=workspace_root,
        candidate_order=[item["candidate_id"] for item in catalog["candidates"]],
        raw_evidence_verifier=raw_evidence_verifier,
    )
    _require_candidate_ledger_prefix(
        binding=freeze["b2_campaign"],
        ledger_identities=ledger_identities,
        label="candidate final freeze",
    )
    registry_sha256 = sha256_json(catalog)
    if matrix["candidate_registry_sha256"] != registry_sha256:
        raise ValidationError("candidate final matrix/registry binding differs")
    if (
        _require_executable_registry_bridge(
            screening=screening,
            catalog=catalog,
            executable_registry=frozen_documents["executable_pass_registry"],
            workspace_root=workspace_root,
        )
        != frozen_documents["screening_base_pass_registry"]
        or screening["pass_registry_sha256"]
        != sha256_json(frozen_documents["screening_base_pass_registry"])
    ):
        raise ValidationError("candidate final screening/PassRegistry binding differs")
    profiles = _verify_matrix_profiles(
        workspace_root,
        matrix,
        candidate_order=[item["candidate_id"] for item in catalog["candidates"]],
    )
    qualified_ids = [
        item["implementation_candidate_id"]
        for item in screening["candidates"]
        if item["qualification_status"] == "qualified"
    ]
    assert all(item is not None for item in qualified_ids)
    catalog_ids = [item["candidate_id"] for item in catalog["candidates"]]
    if catalog_ids != qualified_ids:
        raise ValidationError(
            "executable candidate catalog must contain the qualified screening candidates in order"
        )
    if plan["qualified_candidate_ids"] != catalog_ids:
        raise ValidationError("candidate final campaign candidate order differs")
    diagnostic_status = campaign_status["diagnostic_plan"]
    if diagnostic_status is None or any(
        item["status"] not in {"completed", "failed", "interrupted"}
        for item in diagnostic_status["tasks"]
    ):
        raise ValidationError(
            "candidate final requires terminal freeze-bound post-B3 diagnostics"
        )
    diagnostic_study_status = diagnostic_status["study"]
    if diagnostic_study_status["status"] not in {"completed", "ineligible"}:
        raise ValidationError("candidate final requires terminal diagnostic study state")
    if (diagnostic_study_path is not None) != (
        diagnostic_study_status["status"] == "completed"
    ):
        raise ValidationError(
            "candidate final diagnostic study path/status binding differs"
        )
    diagnostic_matrix = _load_frozen_artifact(
        workspace_root,
        diagnostic_status["matrix"],
        label="candidate final diagnostic matrix",
        version="candidate-profile-matrix.v1",
    )
    if (
        diagnostic_matrix["candidate_registry_sha256"] != registry_sha256
        or diagnostic_matrix["pass_registry_sha256"]
        != sha256_json(frozen_documents["executable_pass_registry"])
    ):
        raise ValidationError("candidate final diagnostic matrix registry binding differs")
    diagnostic_profiles = _verify_matrix_profiles(
        workspace_root,
        diagnostic_matrix,
        candidate_order=catalog_ids,
    )
    diagnostic_tasks = {
        item["task_id"]: item for item in diagnostic_status["tasks"]
    }
    run_statuses = {
        task_id: row
        for task_id, row in status_by_id.items()
        if row["evidence_kind"] == "run-record.v1"
    }
    run_statuses.update(
        {
            task_id: row
            for task_id, row in diagnostic_tasks.items()
            if row["evidence_sha256"] is not None
        }
    )
    if set(run_paths) != set(run_statuses):
        raise ValidationError(
            "candidate final requires every and only hash-bound full-stage raw run"
        )
    plan_tasks = {item["task_id"]: item for item in plan["tasks"]}
    raw_runs: dict[str, dict[str, Any]] = {
        task_id: _load_version(
            path,
            "run-record.v1",
            label=f"candidate final raw run {task_id}",
        )
        for task_id, path in run_paths.items()
    }
    for task_id, path in run_paths.items():
        run = raw_runs[task_id]
        if (
            sha256_json(run) != run_statuses[task_id]["evidence_sha256"]
            or sha256_file(path)
            != run_statuses[task_id]["evidence_physical_sha256"]
        ):
            raise ValidationError(f"candidate final raw run/status hash differs: {task_id}")
        if task_id in plan_tasks:
            task = plan_tasks[task_id]
            if task["task_type"] != "run":
                raise ValidationError(f"candidate final task is not a run: {task_id}")
            baseline_task_id = _candidate_timeout_baseline_task_id(task)
            _validate_campaign_run(
                plan,
                task,
                run,
                profiles=profiles,
                baseline_run=(
                    None
                    if baseline_task_id is None
                    else raw_runs.get(baseline_task_id)
                ),
            )
            if task["kind"] == "reference":
                _require_frozen_reference_run(run, task, freeze)
            binding = _binding_record(
                run, include_execution_environment=True
            )
            if (
                binding["repo_commit"] != plan["repository"]["repo_commit"]
                or binding["repo_dirty"]
                or binding["tracked_diff_sha256"] is not None
                or (
                    task["kind"] != "reference"
                    and binding["compiler_artifact_sha256"]
                    != plan["repository"]["compiler_artifact"]["physical_sha256"]
                )
            ):
                raise ValidationError(
                    f"candidate final raw run differs from campaign freeze: {task_id}"
                )
        else:
            task = diagnostic_tasks[task_id]
            baseline_task_id = _candidate_timeout_baseline_task_id(task)
            _validate_candidate_diagnostic_run(
                task=task,
                run=run,
                profile=diagnostic_profiles[task["logical_profile_id"]],
                candidate_registry_sha256=registry_sha256,
                pass_registry_sha256=sha256_json(
                    frozen_documents["executable_pass_registry"]
                ),
                b3_suite=next(
                    item for item in plan["suites"] if item["data_role"] == "B3"
                ),
                freeze=freeze,
                baseline_run=(
                    None
                    if baseline_task_id is None
                    else raw_runs.get(baseline_task_id)
                ),
            )
    freeze_snapshots = freeze["snapshots"]
    if (
        status_by_id["freeze"]["status"] != "completed"
        or status_by_id["freeze"]["evidence_sha256"] != sha256_json(freeze)
        or status_by_id["freeze"]["evidence_physical_sha256"]
        != sha256_file(freeze_path)
        or frozen_documents["candidate_registry"] != catalog
        or frozen_documents["matrix"] != matrix
        or frozen_documents["screening"] != screening
        or sha256_json(frozen_documents["executable_pass_registry"])
        != catalog["pass_registry_sha256"]
        or _frozen_artifact_digest(
            workspace_root,
            catalog_path,
            catalog,
            label="candidate registry",
        )
        != freeze_snapshots["candidate_registry"]
        or _frozen_artifact_digest(
            workspace_root,
            matrix_path,
            matrix,
            label="candidate matrix",
        )
        != freeze_snapshots["matrix"]
        or _frozen_artifact_digest(
            workspace_root,
            screening_path,
            screening,
            label="candidate screening",
        )
        != freeze_snapshots["screening"]
        or freeze["frozen_candidate_ids"] != catalog_ids
        or freeze["base_pipeline_profile"]["profile_id"]
        != matrix["base_pipeline_profile"]["profile_id"]
        or freeze["base_pipeline_profile"]["artifact"]["physical_sha256"]
        != matrix["base_pipeline_profile"]["profile_sha256"]
        or plan["artifacts"]["candidate_registry"]
        != freeze_snapshots["candidate_registry"]
        or plan["artifacts"]["executable_pass_registry"]
        != freeze_snapshots["executable_pass_registry"]
        or plan["artifacts"]["screening_base_pass_registry"]
        != freeze_snapshots["screening_base_pass_registry"]
        or plan["artifacts"]["matrix"] != freeze_snapshots["matrix"]
        or plan["artifacts"]["screening"] != freeze_snapshots["screening"]
        or plan["repository"] != {
            "repo_commit": freeze["repository"]["repo_commit"],
            "repo_tree": freeze["repository"]["repo_tree"],
            "compiler_artifact": freeze["repository"]["compiler_artifact"],
        }
        or plan["execution_environment_sha256"]
        != freeze["execution_environment_sha256"]
        or plan["measurement_protocols"] != freeze["measurement_protocols"]
        or plan["analyzer"] != freeze["analyzer"]
    ):
        raise ValidationError(
            "candidate final inputs differ from the immutable pre-B3 freeze"
        )
    b1_full_run = raw_runs.get("run.B1.full")
    if b1_full_run is None or (
        b1_full_run["state"] != "completed"
        or sha256_json(b1_full_run) != freeze["b2_campaign"]["b1_full_run_sha256"]
        or b1_full_run["run_id"] != freeze["b2_campaign"]["b1_full_run_id"]
    ):
        raise ValidationError("candidate final lacks the exact completed B1 FULL baseline")
    b1_run_paths = {
        candidate_id: run_paths[f"run.B1.{candidate_id}"]
        for candidate_id in catalog_ids
    }

    studies: dict[str, dict[str, Any]] = {}
    results_by_role: dict[str, dict[str, Mapping[str, Any]]] = {}
    common_binding: Mapping[str, Any] | None = None
    common_keys = (
        "repo_commit",
        "repo_dirty",
        "tracked_diff_sha256",
        "compiler_artifact_sha256",
        "execution_environment_sha256",
        "measurement_protocol_id",
        "measurement_protocol_sha256",
    )
    all_study_paths = {"B2": b2_study_path, **study_paths}
    frozen_suites = {
        item["data_role"]: item for item in freeze["suites"]
    }
    for role in ("B2", "B3", "B4", "B5", "B6"):
        if role not in all_study_paths:
            results_by_role[role] = {}
            continue
        study = _load_version(
            all_study_paths[role], "candidate-study.v1", label=f"{role} candidate study"
        )
        if (
            study["data_role"] != role
            or study["candidate_registry_sha256"] != registry_sha256
            or study["matrix_sha256"] != sha256_json(matrix)
            or study["pass_registry_sha256"] != catalog["pass_registry_sha256"]
            or study["suite_id"] != frozen_suites[role]["suite_id"]
            or study["manifest_sha256"]
            != frozen_suites[role]["manifest"]["canonical_sha256"]
        ):
            raise ValidationError(f"{role} candidate study binding differs")
        if common_binding is None:
            common_binding = study["bindings"]
        elif any(study["bindings"][key] != common_binding[key] for key in common_keys):
            raise ValidationError(f"{role} candidate study artifact/commit/protocol differs")
        by_id = {item["candidate_id"]: item for item in study["candidates"]}
        if not set(by_id).issubset(set(catalog_ids)):
            raise ValidationError(f"{role} candidate study contains an unqualified candidate")
        _validate_study_against_raw_runs(
            study=study,
            baseline=raw_runs[f"run.{role}.full"],
            candidate_runs={
                candidate_id: raw_runs[f"run.{role}.{candidate_id}"]
                for candidate_id in by_id
            },
            interaction_runs={},
            catalog=catalog,
            raw_verifications=raw_verifications,
        )
        if not study["baseline"]["run_id"].startswith(
            freeze["run_namespace"]
        ) or any(
            not item["run_id"].startswith(freeze["run_namespace"])
            for item in [*study["candidates"], *study["interactions"]]
        ):
            raise ValidationError(
                f"{role} candidate study run id escapes the frozen namespace"
            )
        studies[role] = study
        results_by_role[role] = by_id
        study_status = status_by_id[f"study.{role}"]
        if (
            study_status["status"] != "completed"
            or study_status["evidence_sha256"] != sha256_json(study)
            or study_status["evidence_physical_sha256"]
            != sha256_file(all_study_paths[role])
        ):
            raise ValidationError(f"candidate final {role} study/status hash differs")

    if (
        common_binding is None
        or common_binding["execution_environment_sha256"]
        != freeze["execution_environment_sha256"]
        or common_binding["compiler_artifact_sha256"]
        != freeze["repository"]["compiler_artifact"]["physical_sha256"]
    ):
        raise ValidationError(
            "candidate final study execution environment differs from freeze"
        )

    if sha256_json(studies["B2"]) != freeze["b2_campaign"]["study_sha256"]:
        raise ValidationError("candidate final B2 study differs from pre-B3 freeze")
    diagnostic_study: dict[str, Any] | None = None
    if diagnostic_study_path is not None:
        diagnostic_study = _load_version(
            diagnostic_study_path,
            "candidate-study.v1",
            label="candidate final diagnostic interaction study",
        )
        if (
            diagnostic_study_status["evidence_kind"] != "candidate-study.v1"
            or diagnostic_study_status["evidence_sha256"]
            != sha256_json(diagnostic_study)
            or diagnostic_study_status["evidence_physical_sha256"]
            != sha256_file(diagnostic_study_path)
        ):
            raise ValidationError(
                "candidate final diagnostic study/status hash differs"
            )
        _validate_candidate_diagnostic_study(
            study=diagnostic_study,
            formal_b3_study=studies["B3"],
            baseline=raw_runs["run.B3.full"],
            candidate_runs={
                candidate_id: raw_runs[f"run.B3.{candidate_id}"]
                for candidate_id in diagnostic_status["top3_candidate_ids"]
            },
            pair_runs={
                frozenset(task["candidate_ids"]): (
                    task,
                    raw_runs[task["task_id"]],
                )
                for task in diagnostic_status["tasks"]
                if task["kind"] == "pair"
            },
            top3_candidate_ids=diagnostic_status["top3_candidate_ids"],
            matrix_sha256=sha256_json(diagnostic_matrix),
            catalog=catalog,
            raw_verifications=raw_verifications,
        )
    assert common_binding is not None
    frozen_repository = freeze["repository"]
    standard_protocol = freeze["measurement_protocols"]["standard_proxy"]
    if (
        common_binding["repo_commit"] != frozen_repository["repo_commit"]
        or common_binding["repo_dirty"]
        or common_binding["tracked_diff_sha256"] is not None
        or common_binding["compiler_artifact_sha256"]
        != frozen_repository["compiler_artifact"]["physical_sha256"]
        or common_binding["measurement_protocol_id"]
        != standard_protocol["protocol_id"]
        or common_binding["measurement_protocol_sha256"]
        != standard_protocol["protocol_sha256"]
    ):
        raise ValidationError(
            "candidate final studies differ from frozen code/artifact/protocol"
        )

    b1_runs: dict[str, dict[str, Any]] = {}
    b1_passed_ids: set[str] = set()
    b1_run_ids: set[str] = set()
    b1_run_hashes: set[str] = set()
    for candidate_id in catalog_ids:
        run = _load_version(
            b1_run_paths[candidate_id],
            "run-record.v1",
            label=f"B1 correctness run {candidate_id}",
        )
        run_hash = sha256_json(run)
        if run["run_id"] in b1_run_ids or run_hash in b1_run_hashes:
            raise ValidationError("B1 correctness runs must be physically distinct")
        b1_run_ids.add(run["run_id"])
        b1_run_hashes.add(run_hash)
        if (
            run["state"] not in {"completed", "failed", "interrupted"}
            or run["configuration"]["evidence_level"] != "qemu_correctness"
            or run["suite_id"] != frozen_suites["B1"]["suite_id"]
            or run["manifest_sha256"]
            != frozen_suites["B1"]["manifest"]["canonical_sha256"]
            or not run["run_id"].startswith(freeze["run_namespace"])
        ):
            raise ValidationError(
                f"B1 candidate run is not terminal qemu_correctness evidence: {candidate_id}"
            )
        _require_candidate_run_protocol(run, data_role="B1")
        _require_candidate_correctness_run(
            run, label=f"B1 candidate {candidate_id}"
        )
        binding = _binding_record(
            run, include_execution_environment=True
        )
        for key in (
            "repo_commit",
            "repo_dirty",
            "tracked_diff_sha256",
            "compiler_artifact_sha256",
            "execution_environment_sha256",
        ):
            if binding[key] != common_binding[key]:
                raise ValidationError(
                    f"B1 candidate run differs from the frozen artifact/revision: {candidate_id}"
                )
        profile = next(
            item
            for item in matrix["profiles"]
            if item["kind"] == "single" and item["candidate_ids"] == [candidate_id]
        )
        _require_candidate_configuration(
            run,
            registry_sha256=registry_sha256,
            pass_registry_sha256=catalog["pass_registry_sha256"],
            enabled_candidate_ids=[candidate_id],
            profile=profiles[profile["profile_id"]],
            label=f"B1 candidate {candidate_id}",
        )
        b1_runs[candidate_id] = run
        if run["state"] == "completed":
            b1_passed_ids.add(candidate_id)

    if (
        b1_passed_ids != set(freeze["b2_campaign"]["b1_passed_candidate_ids"])
        or set(catalog_ids) - b1_passed_ids
        != set(freeze["b2_campaign"]["b1_failed_candidate_ids"])
    ):
        raise ValidationError("candidate final B1 pass/fail partition differs from freeze")

    if set(results_by_role["B2"]) != b1_passed_ids:
        raise ValidationError("B2 tuning must contain exactly the B1-correct candidates")
    if set(results_by_role["B3"]) != b1_passed_ids:
        raise ValidationError("B3 must contain exactly the B1-correct candidates")

    b3_results = results_by_role["B3"]
    promoted_ids = {
        candidate_id
        for candidate_id, result in b3_results.items()
        if result["eligible_for_ranking"]
        and result["case_geometric_mean_speedup"] is not None
        and result["case_geometric_mean_speedup"] > 1.0
    }
    expected_validation_roles = {"B4", "B5", "B6"} if promoted_ids else set()
    if set(study_paths) - {"B3"} != expected_validation_roles:
        raise ValidationError(
            "candidate final B4-B6 study presence differs from the B3 promotion gate"
        )
    for role in ("B4", "B5", "B6"):
        if set(results_by_role[role]) != promoted_ids:
            raise ValidationError(
                f"{role} must contain exactly all B3 candidates with GM strictly greater than 1"
            )

    candidate_rows: list[dict[str, Any]] = []
    for screened in screening["candidates"]:
        candidate_id = screened["candidate_id"]
        implementation_id = screened["implementation_candidate_id"]
        outcomes: dict[str, dict[str, Any] | None] = {}
        raw_results: dict[str, Mapping[str, Any] | None] = {}
        for role in ("B3", "B4", "B5", "B6"):
            result = results_by_role[role].get(implementation_id)
            raw_results[role] = result
            outcomes[role] = (
                None
                if result is None
                else {
                    "study_id": studies[role]["study_id"],
                    "run_id": result["run_id"],
                    "run_sha256": result["run_sha256"],
                    "configuration_sha256": result["configuration_sha256"],
                    "suite_id": studies[role]["suite_id"],
                    "manifest_sha256": studies[role]["manifest_sha256"],
                    "expected_case_count": _CANDIDATE_SUITE_CASE_COUNTS[role],
                    "eligible_for_ranking": result["eligible_for_ranking"],
                    "ineligibility_reason": result["ineligibility_reason"],
                    "comparable_cases": result["comparable_cases"],
                    "comparable_source_groups": result[
                        "comparable_source_groups"
                    ],
                    "correctness_failures": result["correctness_failures"],
                    "censored_cases": result["censored_cases"],
                    "excluded_cases": result["excluded_cases"],
                    "case_geometric_mean_speedup": result[
                        "case_geometric_mean_speedup"
                    ],
                    "source_group_geometric_mean_speedup": result[
                        "source_group_geometric_mean_speedup"
                    ],
                    "confidence_interval_95": result["confidence_interval_95"],
                    "static_text_bytes_full": result["static_text_bytes_full"],
                    "static_text_bytes_full_plus_candidate": result[
                        "static_text_bytes_full_plus_candidate"
                    ],
                    "static_text_ratio": result["static_text_ratio"],
                }
            )
        reasons: list[str] = []
        b3 = raw_results["B3"]
        b1_run = b1_runs.get(implementation_id)
        b2_result = results_by_role["B2"].get(implementation_id)
        if screened["qualification_status"] != "qualified":
            reasons.append("screening_not_qualified")
        elif b1_run is None or b1_run["state"] != "completed":
            reasons.append("b1_not_correct")
        elif b3 is None:
            reasons.append("missing_b3_evidence")
        elif not b3["eligible_for_ranking"]:
            reasons.append("suite_ineligible")
        elif b3["case_geometric_mean_speedup"] <= 1.0:
            reasons.append("b3_not_above_one")
        else:
            validation = [raw_results[role] for role in ("B4", "B5", "B6")]
            if any(item is None for item in validation):
                reasons.append("missing_validation_suite")
            elif any(not item["eligible_for_ranking"] for item in validation if item is not None):
                reasons.append("suite_ineligible")

        complete_results = [
            raw_results[role]
            for role in ("B3", "B4", "B5", "B6")
            if raw_results[role] is not None
        ]
        combined_case_count = sum(item["comparable_cases"] for item in complete_results)
        combined_gm: float | None = None
        b3_gm = None if b3 is None else b3["case_geometric_mean_speedup"]
        static_candidate_bytes: float | None = None
        static_ratio: float | None = None
        if len(complete_results) == 4 and all(
            item["eligible_for_ranking"] for item in complete_results
        ):
            per_case_speedups = [
                float(case["speedup"])
                for item in complete_results
                for case in item["per_cases"]
            ]
            if per_case_speedups:
                combined_gm = math.exp(
                    sum(math.log(value) for value in per_case_speedups)
                    / len(per_case_speedups)
                )
            static_complete = all(
                item["static_text_bytes_full"] is not None
                and item["static_text_bytes_full_plus_candidate"] is not None
                for item in complete_results
            )
            if static_complete:
                static_full = sum(
                    float(item["static_text_bytes_full"]) for item in complete_results
                )
                static_candidate = sum(
                    float(item["static_text_bytes_full_plus_candidate"])
                    for item in complete_results
                )
                if static_full > 0 and static_candidate > 0:
                    static_candidate_bytes = static_candidate
                    static_ratio = static_full / static_candidate
        if not reasons and combined_case_count != 267:
            reasons.append("combined_case_count_not_267")
        if not reasons and static_ratio is None:
            reasons.append("missing_static_text_evidence")
        candidate_rows.append(
            {
                "candidate_id": candidate_id,
                "oracle_family_id": screened["oracle_family_id"],
                "implementation_candidate_id": implementation_id,
                "screening_status": screened["qualification_status"],
                "screening_rejection_reasons": screened["rejection_reasons"],
                "b1_correctness": (
                    None
                    if b1_run is None
                    else {
                        "run_id": b1_run["run_id"],
                        "run_sha256": sha256_json(b1_run),
                        "configuration_sha256": b1_run["configuration_sha256"],
                        "suite_id": b1_run["suite_id"],
                        "manifest_sha256": b1_run["manifest_sha256"],
                        "case_count": b1_run["manifest_case_count"],
                        "evidence_level": b1_run["configuration"]["evidence_level"],
                        "state": b1_run["state"],
                        "passed_cases": b1_run["summary"]["passed_cases"],
                        "failed_cases": b1_run["summary"]["failed_cases"],
                        "pending_cases": b1_run["summary"]["pending_cases"],
                        "censored_cases": b1_run["summary"]["censored_cases"],
                        "all_correct": b1_run["state"] == "completed",
                        "failure_reason": campaign_run_status(b1_run)[1],
                    }
                ),
                "b2_tuning": (
                    None
                    if b2_result is None
                    else {
                        "study_id": studies["B2"]["study_id"],
                        "run_id": b2_result["run_id"],
                        "run_sha256": b2_result["run_sha256"],
                        "configuration_sha256": b2_result[
                            "configuration_sha256"
                        ],
                        "suite_id": studies["B2"]["suite_id"],
                        "manifest_sha256": studies["B2"]["manifest_sha256"],
                        "expected_case_count": _CANDIDATE_SUITE_CASE_COUNTS[
                            "B2"
                        ],
                        "eligible_for_ranking": b2_result["eligible_for_ranking"],
                        "ineligibility_reason": b2_result["ineligibility_reason"],
                        "comparable_cases": b2_result["comparable_cases"],
                        "comparable_source_groups": b2_result[
                            "comparable_source_groups"
                        ],
                        "correctness_failures": b2_result[
                            "correctness_failures"
                        ],
                        "censored_cases": b2_result["censored_cases"],
                        "excluded_cases": b2_result["excluded_cases"],
                        "case_geometric_mean_speedup": b2_result[
                            "case_geometric_mean_speedup"
                        ],
                        "source_group_geometric_mean_speedup": b2_result[
                            "source_group_geometric_mean_speedup"
                        ],
                        "confidence_interval_95": b2_result[
                            "confidence_interval_95"
                        ],
                        "static_text_bytes_full": b2_result["static_text_bytes_full"],
                        "static_text_bytes_full_plus_candidate": b2_result[
                            "static_text_bytes_full_plus_candidate"
                        ],
                        "static_text_ratio": b2_result["static_text_ratio"],
                    }
                ),
                "suite_outcomes": outcomes,
                "eligible_for_final": not reasons,
                "final_ineligibility_reasons": reasons,
                "combined_case_count": combined_case_count,
                "combined_case_geometric_mean_speedup": combined_gm,
                "b3_case_geometric_mean_speedup": b3_gm,
                "combined_static_text_bytes_full_plus_candidate": static_candidate_bytes,
                "combined_static_text_ratio": static_ratio,
                "rank": None,
            }
        )

    eligible = [item for item in candidate_rows if item["eligible_for_final"]]
    eligible.sort(
        key=lambda item: (
            -float(item["combined_case_geometric_mean_speedup"]),
            -float(item["b3_case_geometric_mean_speedup"]),
            float(item["combined_static_text_bytes_full_plus_candidate"]),
            item["implementation_candidate_id"],
        )
    )
    rank_by_id = {
        item["implementation_candidate_id"]: index
        for index, item in enumerate(eligible, 1)
    }
    for item in candidate_rows:
        item["rank"] = rank_by_id.get(item["implementation_candidate_id"])
    ranking = [
        {
            "rank": index,
            "candidate_id": item["implementation_candidate_id"],
            "combined_case_geometric_mean_speedup": item[
                "combined_case_geometric_mean_speedup"
            ],
            "b3_case_geometric_mean_speedup": item[
                "b3_case_geometric_mean_speedup"
            ],
            "combined_static_text_bytes_full_plus_candidate": item[
                "combined_static_text_bytes_full_plus_candidate"
            ],
            "combined_static_text_ratio": item["combined_static_text_ratio"],
            "stable_id_tiebreak": item["implementation_candidate_id"],
        }
        for index, item in enumerate(eligible, 1)
    ]
    winner_candidate_id = (
        ranking[0]["candidate_id"]
        if ranking and ranking[0]["combined_case_geometric_mean_speedup"] > 1.0
        else None
    )
    final = validate_document(
        {
            "schema_version": "candidate-final.v1",
            "final_id": final_id,
            "generated_at": campaign_status["as_of"],
            "screening_sha256": sha256_json(screening),
            "candidate_registry_sha256": registry_sha256,
            "executable_pass_registry_sha256": catalog[
                "pass_registry_sha256"
            ],
            "matrix_sha256": sha256_json(matrix),
            "expected_combined_case_count": 267,
            "campaign": {
                "plan_sha256": plan_sha256,
                "status_sha256": sha256_json(campaign_status),
                "status_ledger_entry_count": ledger_entry_count,
                "status_ledger_head_sha256": ledger_head_sha256,
                "status_ledger_sha256": ledger_sha256,
                "raw_evidence_registry": raw_evidence_registry_artifact,
                "run_records": [
                    {
                        "task_id": task_id,
                        "run_id": run["run_id"],
                        "run_sha256": sha256_json(run),
                        "run_physical_sha256": sha256_file(run_paths[task_id]),
                        "state": run["state"],
                    }
                    for task_id in [
                        *[
                            item["task_id"]
                            for item in plan["tasks"]
                            if item["task_id"] in raw_runs
                        ],
                        *[
                            item["task_id"]
                            for item in diagnostic_status["tasks"]
                            if item["task_id"] in raw_runs
                        ],
                    ]
                    for run in [raw_runs[task_id]]
                ],
            },
            "b1_full_correctness": {
                "run_id": b1_full_run["run_id"],
                "run_sha256": sha256_json(b1_full_run),
                "configuration_sha256": b1_full_run["configuration_sha256"],
                "suite_id": b1_full_run["suite_id"],
                "manifest_sha256": b1_full_run["manifest_sha256"],
                "case_count": b1_full_run["manifest_case_count"],
                "evidence_level": b1_full_run["configuration"]["evidence_level"],
                "state": b1_full_run["state"],
                "passed_cases": b1_full_run["summary"]["passed_cases"],
                "failed_cases": b1_full_run["summary"]["failed_cases"],
                "pending_cases": b1_full_run["summary"]["pending_cases"],
                "censored_cases": b1_full_run["summary"]["censored_cases"],
                "all_correct": True,
                "failure_reason": None,
            },
            "freeze": {
                "freeze_id": freeze["freeze_id"],
                "freeze_sha256": sha256_json(freeze),
                "artifact": _frozen_artifact_digest(
                    workspace_root,
                    freeze_path,
                    freeze,
                    label="candidate final freeze",
                ),
                "campaign_id": freeze["campaign_id"],
                "run_namespace": freeze["run_namespace"],
                "repo_commit": freeze["repository"]["repo_commit"],
                "repo_tree": freeze["repository"]["repo_tree"],
                "compiler_artifact": freeze["repository"]["compiler_artifact"],
                "execution_environment_sha256": freeze[
                    "execution_environment_sha256"
                ],
                "analyzer": freeze["analyzer"],
                "candidate_registry": freeze["snapshots"]["candidate_registry"],
                "executable_pass_registry": freeze["snapshots"][
                    "executable_pass_registry"
                ],
                "screening_base_pass_registry": freeze["snapshots"][
                    "screening_base_pass_registry"
                ],
                "matrix": freeze["snapshots"]["matrix"],
                "screening": freeze["snapshots"]["screening"],
                "oracle_capture": freeze["snapshots"]["oracle_capture"],
                "run_record_schema": freeze["snapshots"]["run_record_schema"],
                "candidate_study_schema": freeze["snapshots"][
                    "candidate_study_schema"
                ],
                "base_pipeline_profile": freeze["base_pipeline_profile"][
                    "artifact"
                ],
                "suite_manifests": {
                    role: frozen_suites[role]["manifest"]
                    for role in _CANDIDATE_SUITE_CASE_COUNTS
                },
                "standard_measurement_protocol": {
                    "path": freeze["measurement_protocols"]["standard_proxy"][
                        "path"
                    ],
                    "canonical_sha256": freeze["measurement_protocols"][
                        "standard_proxy"
                    ]["protocol_sha256"],
                    "physical_sha256": freeze["measurement_protocols"][
                        "standard_proxy"
                    ]["physical_sha256"],
                },
                "hotblock_measurement_protocol": {
                    "path": freeze["measurement_protocols"]["cache_hotblock"][
                        "path"
                    ],
                    "canonical_sha256": freeze["measurement_protocols"][
                        "cache_hotblock"
                    ]["protocol_sha256"],
                    "physical_sha256": freeze["measurement_protocols"][
                        "cache_hotblock"
                    ]["physical_sha256"],
                },
                "reference_toolchain": _candidate_reference_toolchain_context(
                    freeze
                ),
                "raw_evidence_registry": freeze["b2_campaign"][
                    "raw_evidence_registry"
                ],
                "frozen_candidate_ids_sha256": freeze[
                    "frozen_candidate_ids_sha256"
                ],
                "b2_study_sha256": sha256_json(studies["B2"]),
                "freeze_status_ledger_entry_count": freeze["b2_campaign"][
                    "status_ledger_entry_count"
                ],
                "freeze_status_ledger_head_sha256": freeze["b2_campaign"][
                    "status_ledger_head_sha256"
                ],
                "freeze_status_ledger_sha256": freeze["b2_campaign"][
                    "status_ledger_sha256"
                ],
                "b1_full_run_sha256": freeze["b2_campaign"][
                    "b1_full_run_sha256"
                ],
                "b1_passed_candidate_ids": freeze["b2_campaign"][
                    "b1_passed_candidate_ids"
                ],
                "b1_failed_candidate_ids": freeze["b2_campaign"][
                    "b1_failed_candidate_ids"
                ],
                "oracle_threshold": freeze["gates"][
                    "oracle_structure_geometric_mean_minimum"
                ],
                "b3_strict_threshold": freeze["gates"][
                    "b3_geometric_mean_strictly_above"
                ],
                "combined_case_count": freeze["gates"][
                    "combined_case_count"
                ],
                "ranking_rule": [
                    freeze["ranking_rule"]["primary"],
                    freeze["ranking_rule"]["secondary"],
                    freeze["ranking_rule"]["tertiary"],
                    freeze["ranking_rule"]["quaternary"],
                ],
            },
            "studies": {
                role: (
                    None
                    if role not in studies
                    else _candidate_final_study_ref(
                        studies[role],
                        study_path=all_study_paths[role],
                        workspace_root=workspace_root,
                    )
                )
                for role in ("B2", "B3", "B4", "B5", "B6")
            },
            "diagnostics": {
                "source_freeze_sha256": diagnostic_status[
                    "source_freeze_sha256"
                ],
                "source_study_sha256": diagnostic_status[
                    "source_study_sha256"
                ],
                "matrix": diagnostic_status["matrix"],
                "top3_candidate_ids": diagnostic_status[
                    "top3_candidate_ids"
                ],
                "tasks": diagnostic_status["tasks"],
                "study_status": diagnostic_study_status["status"],
                "study_ineligibility_reason": diagnostic_study_status[
                    "ineligibility_reason"
                ],
                "study": (
                    None
                    if diagnostic_study is None
                    else {
                        "study_id": diagnostic_study["study_id"],
                        "path": _workspace_regular_path(
                            workspace_root,
                            diagnostic_study_path,
                            label="candidate final diagnostic study",
                        )[2].as_posix(),
                        "canonical_sha256": sha256_json(diagnostic_study),
                        "physical_sha256": sha256_file(
                            diagnostic_study_path
                        ),
                    }
                ),
            },
            "candidates": candidate_rows,
            "ranking": ranking,
            "winner_candidate_id": winner_candidate_id,
            "winner_reason": (
                "top_combined_gm_above_one"
                if winner_candidate_id is not None
                else "no_winning_candidate"
            ),
        }
    )
    if raw_snapshot_cache is not None:
        raw_snapshot_cache.assert_unchanged()
    return final
