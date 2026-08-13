from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass
from fnmatch import fnmatchcase
from itertools import combinations
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .errors import ConfigurationError, ValidationError
from .journal import durable_create_json
from .lease import (
    ExclusiveFileLease,
    candidate_wave_lease_path,
    output_lease_path,
)
from .schema import validate_document
from .stats import PairedCase, case_geometric_mean, metric_spec, run_case_metrics
from .util import (
    atomic_write_json,
    canonical_json_bytes,
    read_json,
    resolve_without_symlinks,
    sha256_artifact,
    sha256_file,
    sha256_json,
    utc_now,
    validate_relative_path,
)


_BOOTSTRAP_VERSION = "candidate-fast-bootstrap.v1"
_PLAN_VERSION = "candidate-fast-campaign-plan.v1"
_RECEIPT_VERSION = "candidate-fast-run-receipt.v1"
_INDEX_VERSION = "candidate-fast-run-index.v1"
_STATUS_VERSION = "candidate-fast-status.v1"
_HEAD_VERSION = "candidate-fast-current-head.v1"
_AUDIT_VERSION = "candidate-fast-audit.v1"
_STUDY_VERSION = "candidate-fast-study.v1"
_DIAGNOSTIC_STUDY_VERSION = "candidate-fast-diagnostic-study.v1"
_FINAL_VERSION = "candidate-fast-final.v1"

FAST_ORACLE_STATIC_ARTIFACT_VERSIONS = {
    "candidate-screening": "candidate-screening.v1",
    "candidate-oracle-capture": "candidate-oracle-capture.v1",
    "candidate-evidence": "candidate-evidence.v1",
    "candidate-screening-spec": "candidate-screening-spec.v1",
}

FAST_ORACLE_STATIC_ARTIFACT_PATHS = {
    artifact_id: Path("docs/optimization/data/candidates") / f"{artifact_id}.v1.json"
    for artifact_id in FAST_ORACLE_STATIC_ARTIFACT_VERSIONS
}

_FAST_ORCHESTRATION_ALLOWLIST = (
    "AGENTS.md",
    "docs/optimization/README.md",
    "tools/benchmark/cli.py",
    "tools/benchmark/execution.py",
    "tools/benchmark/fast_campaign.py",
    "tools/benchmark/fast_driver.py",
    "tools/benchmark/fast_plan.py",
    "tools/benchmark/fast_report.py",
    "tools/benchmark/fast_scheduler.py",
    "tools/benchmark/journal.py",
    "tools/benchmark/lease.py",
    "tools/benchmark/schema.py",
    "tools/benchmark/schemas/candidate-fast-*.json",
    "tools/benchmark/tests/test_cli.py",
    "tools/benchmark/tests/test_execution.py",
    "tools/benchmark/tests/test_fast_*.py",
    "tools/benchmark/tests/test_lease.py",
)

_STAGES = ("B2", "B3", "B4", "B5", "B6")
_TERMINAL_TASK_STATES = frozenset({"completed", "failed", "cancelled"})
@dataclass(frozen=True)
class FastRunAuthorizationIntent:
    plan_path: Path
    status_path: Path
    index_path: Path
    task_id: str
    workspace_root: Path
    manifest_path: Path
    output_path: Path
    compiler_artifact_path: Path
    pipeline_profile_path: Path | None
    measurement_protocol_path: Path | None
    baseline_run_path: Path | None
    configuration: Mapping[str, Any]
    provenance: Mapping[str, Any]
    run_id: str
    receipt_path: Path


def _commit(document: dict[str, Any], field: str) -> dict[str, Any]:
    document[field] = sha256_json(
        {key: value for key, value in document.items() if key != field}
    )
    return validate_document(document)


def _workspace_root(path: Path) -> Path:
    root = resolve_without_symlinks(path, label="fast campaign workspace")
    if not root.is_dir():
        raise ConfigurationError("fast campaign workspace must be a directory")
    return root


def _workspace_existing_path(
    root: Path, path: Path, *, label: str, regular_only: bool = False
) -> tuple[Path, str]:
    candidate = path if path.is_absolute() else root / path
    resolved = resolve_without_symlinks(candidate, label=label)
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"{label} must remain inside the fast campaign workspace") from exc
    if regular_only and not resolved.is_file():
        raise ValidationError(f"{label} must be a regular file")
    return resolved, relative.as_posix()


def _workspace_output_path(
    root: Path,
    path: Path,
    *,
    label: str,
    create_parent: bool = True,
) -> tuple[Path, str]:
    candidate = path if path.is_absolute() else root / path
    try:
        relative = candidate.absolute().relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"{label} must remain inside the fast campaign workspace") from exc
    validate_relative_path(relative.as_posix(), label=label)
    parent = candidate.parent
    if create_parent:
        parent.mkdir(parents=True, exist_ok=True)
    elif not parent.is_dir():
        raise ValidationError(f"{label} parent must already exist before authorization")
    resolved_parent = resolve_without_symlinks(parent, label=f"{label} parent")
    try:
        resolved_parent.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"{label} parent must remain inside the workspace") from exc
    if candidate.exists() and candidate.is_symlink():
        raise ValidationError(f"{label} must not be a symbolic link")
    return candidate.absolute(), relative.as_posix()


def _load_version(path: Path, version: str, *, label: str) -> dict[str, Any]:
    document = read_json(path)
    if not isinstance(document, dict):
        raise ValidationError(f"{label} must be a JSON object")
    validate_document(document)
    if document.get("schema_version") != version:
        raise ValidationError(f"{label} has an unexpected schema version")
    return document


def _artifact_ref(root: Path, path: Path, *, label: str) -> dict[str, str]:
    physical, relative = _workspace_existing_path(root, path, label=label)
    if physical.is_file():
        physical_sha256 = sha256_file(physical)
        if physical.suffix.lower() == ".json":
            document = read_json(physical)
            if not isinstance(document, (dict, list)):
                raise ValidationError(f"{label} JSON artifact must be an object or array")
            canonical_sha256 = sha256_json(document)
        else:
            canonical_sha256 = physical_sha256
    else:
        physical_sha256 = sha256_artifact(physical)
        canonical_sha256 = physical_sha256
    return {
        "path": relative,
        "canonical_sha256": canonical_sha256,
        "physical_sha256": physical_sha256,
    }


def _verify_artifact(
    root: Path, artifact: Mapping[str, str], *, label: str
) -> Path:
    relative = validate_relative_path(artifact["path"], label=f"{label} path")
    physical, _ = _workspace_existing_path(
        root, root.joinpath(*relative.parts), label=label
    )
    observed = _artifact_ref(root, physical, label=label)
    if observed != dict(artifact):
        raise ValidationError(f"{label} canonical or physical hash differs")
    return physical


def verify_fast_oracle_static_artifacts(
    *,
    workspace_root: Path,
    named_artifacts: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Verify the immutable four-document Oracle qualification closure.

    This intentionally validates only normalized JSON artifacts.  It never opens
    the legacy Oracle raw state, journal, or ledger namespaces.
    """

    root = _workspace_root(workspace_root)
    by_id = {row["artifact_id"]: row for row in named_artifacts}
    if len(by_id) != len(named_artifacts):
        raise ValidationError("fast Oracle static artifact ids must be unique")
    missing = sorted(set(FAST_ORACLE_STATIC_ARTIFACT_VERSIONS) - set(by_id))
    if missing:
        raise ValidationError(
            "fast Oracle static artifact set is incomplete: " + ", ".join(missing)
        )

    documents: dict[str, dict[str, Any]] = {}
    artifacts: dict[str, dict[str, str]] = {}
    for artifact_id, version in FAST_ORACLE_STATIC_ARTIFACT_VERSIONS.items():
        row = by_id[artifact_id]
        artifact = dict(row["artifact"])
        path = _verify_artifact(
            root, artifact, label=f"fast Oracle static artifact {artifact_id}"
        )
        documents[artifact_id] = _load_version(
            path, version, label=f"fast Oracle static artifact {artifact_id}"
        )
        artifacts[artifact_id] = artifact

    screening = documents["candidate-screening"]
    capture = documents["candidate-oracle-capture"]
    evidence = documents["candidate-evidence"]
    spec = documents["candidate-screening-spec"]
    evidence_sha256 = sha256_json(evidence)
    spec_sha256 = sha256_json(spec)
    capture_sha256 = sha256_json(capture)
    if (
        screening["sources"]["candidate_evidence"]
        != artifacts["candidate-evidence"]
        or screening["sources"]["screening_spec"]
        != artifacts["candidate-screening-spec"]
        or screening["sources"]["oracle_capture"]
        != artifacts["candidate-oracle-capture"]
        or capture["sources"]["candidate_evidence"]
        != artifacts["candidate-evidence"]
        or screening["candidate_evidence_sha256"] != evidence_sha256
        or capture["candidate_evidence_sha256"] != evidence_sha256
        or screening["screening_spec_sha256"] != spec_sha256
        or screening["oracle_capture_sha256"] != capture_sha256
        or capture["pair_count"] != 99
    ):
        raise ValidationError(
            "fast Oracle screening/capture/evidence/spec artifact closure differs"
        )
    candidate_orders = [
        [row["candidate_id"] for row in document["candidates"]]
        for document in (screening, capture, evidence, spec)
    ]
    if any(order != candidate_orders[0] for order in candidate_orders[1:]):
        raise ValidationError(
            "fast Oracle qualification documents bind different candidate orders"
        )
    pair_ids = [
        structure["sizes"][size]["pair_id"]
        for candidate in capture["candidates"]
        for structure in candidate["structures"]
        for size in ("small", "medium", "large")
    ]
    if len(pair_ids) != 99 or len(set(pair_ids)) != 99:
        raise ValidationError("fast Oracle capture must bind 99 distinct pairs")
    return {
        artifact_id: {
            "artifact": artifacts[artifact_id],
            "document": documents[artifact_id],
        }
        for artifact_id in FAST_ORACLE_STATIC_ARTIFACT_VERSIONS
    }


def _named_artifact(
    root: Path, artifact_id: str, path: Path, *, verified: bool
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "artifact_id": artifact_id,
        "artifact": _artifact_ref(root, path, label=f"fast artifact {artifact_id}"),
    }
    if verified:
        row["verification_commitment_sha256"] = sha256_json(row)
    return row


def _artifact_matches_path(
    root: Path, artifact: Mapping[str, str], path: Path | None, *, label: str
) -> bool:
    if path is None:
        return False
    return _artifact_ref(root, path, label=label) == dict(artifact)


def _repository_identity(value: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    result = {
        "commit": value.get("commit", value.get("repo_commit")),
        "tree": value.get("tree", value.get("repo_tree")),
        "dirty": value.get("dirty", False),
    }
    if (
        not isinstance(result["commit"], str)
        or len(result["commit"]) not in {40, 64}
        or not isinstance(result["tree"], str)
        or len(result["tree"]) not in {40, 64}
        or result["dirty"] is not False
    ):
        raise ValidationError(f"{label} must identify one clean repository revision")
    return result


def _git_output(root: Path, *arguments: str, binary: bool = False) -> str | bytes:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
            text=not binary,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValidationError("fast bootstrap could not prove the local Git revision") from exc


def _verify_git_revision_transition(
    *,
    root: Path,
    source_revision: Mapping[str, Any],
    evaluation_revision: Mapping[str, Any],
) -> list[str]:
    top_level = Path(str(_git_output(root, "rev-parse", "--show-toplevel")).strip())
    if resolve_without_symlinks(top_level, label="fast bootstrap Git root") != root:
        raise ValidationError("fast bootstrap workspace must be the Git top-level")
    observed_head = str(_git_output(root, "rev-parse", "HEAD")).strip()
    observed_tree = str(_git_output(root, "rev-parse", "HEAD^{tree}")).strip()
    if (
        observed_head != evaluation_revision["commit"]
        or observed_tree != evaluation_revision["tree"]
        or bytes(
            _git_output(
                root,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                binary=True,
            )
        )
    ):
        raise ValidationError(
            "fast bootstrap evaluation revision differs from a clean checked-out HEAD/tree"
        )
    source_commit = source_revision["commit"]
    _git_output(root, "cat-file", "-e", f"{source_commit}^{{commit}}")
    if (
        str(_git_output(root, "rev-parse", f"{source_commit}^{{tree}}")).strip()
        != source_revision["tree"]
    ):
        raise ValidationError("fast bootstrap source revision tree differs from Git")
    raw = bytes(
        _git_output(
            root,
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            f"{source_commit}..{evaluation_revision['commit']}",
            "--",
            binary=True,
        )
    )
    try:
        changed = [
            validate_relative_path(
                item.decode("utf-8", errors="strict"),
                label="fast bootstrap changed path",
            ).as_posix()
            for item in raw.split(b"\0")
            if item
        ]
    except UnicodeDecodeError as exc:
        raise ValidationError("fast bootstrap Git changes contain a non-UTF-8 path") from exc
    changed = sorted(set(changed))
    disallowed = [
        path
        for path in changed
        if not any(fnmatchcase(path, pattern) for pattern in _FAST_ORCHESTRATION_ALLOWLIST)
    ]
    if disallowed:
        raise ValidationError("fast bootstrap source-to-evaluation diff is not orchestration-only")
    return changed


def _paths_overlap(left: str, right: str) -> bool:
    left_path, right_path = PurePosixPath(left), PurePosixPath(right)
    return (
        left_path == right_path
        or left_path in right_path.parents
        or right_path in left_path.parents
    )


def publish_immutable_fast_document(path: Path, document: Mapping[str, Any]) -> dict[str, Any]:
    """Create one immutable fast-campaign JSON document, or prove idempotence."""

    value = dict(document)
    validate_document(value)
    payload = canonical_json_bytes(value) + b"\n"
    if path.exists():
        resolved = resolve_without_symlinks(path, label="fast immutable publication")
        if not resolved.is_file() or resolved.read_bytes() != payload:
            raise ValidationError("fast immutable publication target already differs")
        return value
    path.parent.mkdir(parents=True, exist_ok=True)
    resolve_without_symlinks(path.parent, label="fast immutable publication parent")
    durable_create_json(path, value)
    return value


def build_fast_bootstrap(
    *,
    bootstrap_id: str,
    campaign_id: str,
    source_plan_path: Path,
    source_status_path: Path,
    source_raw_registry_path: Path,
    source_run_paths: Mapping[str, Path],
    workspace_root: Path,
    evaluation_revision: Mapping[str, Any],
    measurement_component_paths: Mapping[str, Path],
    created_at: str | None = None,
) -> dict[str, Any]:
    """Import the sealed B1 x7 and B2 FULL normalized closure without raw replay."""

    root = _workspace_root(workspace_root)
    plan_path, _ = _workspace_existing_path(
        root, source_plan_path, label="fast bootstrap source plan", regular_only=True
    )
    status_path, _ = _workspace_existing_path(
        root, source_status_path, label="fast bootstrap source status", regular_only=True
    )
    registry_path, _ = _workspace_existing_path(
        root,
        source_raw_registry_path,
        label="fast bootstrap source raw registry",
        regular_only=True,
    )
    source_plan = _load_version(
        plan_path, "candidate-campaign-plan.v1", label="fast bootstrap source plan"
    )
    source_status = _load_version(
        status_path,
        "candidate-campaign-status.v1",
        label="fast bootstrap source status",
    )
    registry = _load_version(
        registry_path,
        "candidate-raw-evidence.v1",
        label="fast bootstrap source raw registry",
    )
    plan_sha256 = sha256_json(source_plan)
    if (
        source_status["campaign_id"] != source_plan["campaign_id"]
        or source_status["plan_sha256"] != plan_sha256
        or source_status["execution_environment_sha256"]
        != source_plan["execution_environment_sha256"]
        or source_status["analyzer"] != source_plan["analyzer"]
    ):
        raise ValidationError("fast bootstrap source status binds another plan")
    registry_artifact = _artifact_ref(
        root, registry_path, label="fast bootstrap source raw registry"
    )
    if source_status["raw_evidence_registry"] != registry_artifact:
        raise ValidationError("fast bootstrap source status binds another raw registry")
    if (
        registry["campaign_id"] != source_plan["campaign_id"]
        or registry["plan_sha256"] != plan_sha256
        or registry["raw_state_root"] != source_plan["raw_state_root"]
    ):
        raise ValidationError("fast bootstrap raw registry binds another source campaign")

    expected_tasks = {
        task["task_id"]
        for task in source_plan["tasks"]
        if task["task_type"] == "run"
        and (
            task["stage"] == "B1"
            or (task["stage"] == "B2" and task["kind"] == "candidate_empty")
        )
    }
    if len(expected_tasks) != 8 or "run.B1.full" not in expected_tasks or "run.B2.full" not in expected_tasks:
        raise ValidationError("fast bootstrap source plan is not the sealed B1 x7 plus B2 FULL prefix")
    if set(source_run_paths) != expected_tasks:
        raise ValidationError("fast bootstrap run set differs from the sealed source prefix")
    registry_by_task = {row["task_id"]: row for row in registry["runs"]}
    if len(registry_by_task) != len(registry["runs"]) or set(registry_by_task) != expected_tasks:
        raise ValidationError("fast bootstrap raw registry run set differs from the source prefix")
    status_by_task = {row["task_id"]: row for row in source_status["tasks"]}
    imported: list[dict[str, Any]] = []
    for task in source_plan["tasks"]:
        task_id = task["task_id"]
        if task_id not in expected_tasks:
            continue
        run_path, relative = _workspace_existing_path(
            root,
            source_run_paths[task_id],
            label=f"fast bootstrap normalized run {task_id}",
            regular_only=True,
        )
        run = _load_version(run_path, "run-record.v1", label="fast bootstrap normalized run")
        raw_row = registry_by_task[task_id]
        artifact = _artifact_ref(root, run_path, label="fast bootstrap normalized run")
        verification = raw_row["verification"]
        status_row = status_by_task.get(task_id)
        if (
            artifact != raw_row["run_record"]
            or verification["run_id"] != run["run_id"]
            or verification["run_canonical_sha256"] != artifact["canonical_sha256"]
            or verification["run_physical_sha256"] != artifact["physical_sha256"]
            or status_row is None
            or status_row["status"] != "completed"
            or status_row["evidence_kind"] != "run-record.v1"
            or status_row["evidence_path"] != relative
            or status_row["evidence_sha256"] != artifact["canonical_sha256"]
            or status_row["evidence_physical_sha256"] != artifact["physical_sha256"]
            or run["state"] != "completed"
        ):
            raise ValidationError(f"fast bootstrap source run binding differs: {task_id}")
        row: dict[str, Any] = {
            "task_id": task_id,
            "run_id": run["run_id"],
            "run_artifact": artifact,
            "terminal_commitment_sha256": verification["raw_evidence_sha256"],
        }
        row["verification_commitment_sha256"] = sha256_json(row)
        imported.append(row)

    source_revision = _repository_identity(
        source_plan["repository"], label="fast bootstrap source repository"
    )
    evaluation = _repository_identity(
        evaluation_revision, label="fast bootstrap evaluation repository"
    )
    changed_paths = _verify_git_revision_transition(
        root=root,
        source_revision=source_revision,
        evaluation_revision=evaluation,
    )
    component_paths: list[str] = []
    static_artifacts: list[dict[str, Any]] = []
    for artifact_id, path in sorted(measurement_component_paths.items()):
        if artifact_id in FAST_ORACLE_STATIC_ARTIFACT_PATHS:
            raise ConfigurationError(
                f"fast Oracle artifact id is reserved: {artifact_id}"
            )
        _, relative = _workspace_existing_path(
            root, path, label=f"fast bootstrap measurement component {artifact_id}"
        )
        component_paths.append(relative)
        static_artifacts.append(_named_artifact(root, artifact_id, path, verified=True))
    if not static_artifacts:
        raise ConfigurationError("fast bootstrap requires measurement component hashes")
    static_artifacts.extend(
        _named_artifact(root, artifact_id, root / relative_path, verified=True)
        for artifact_id, relative_path in FAST_ORACLE_STATIC_ARTIFACT_PATHS.items()
    )
    oracle_bundle = verify_fast_oracle_static_artifacts(
        workspace_root=root,
        named_artifacts=static_artifacts,
    )
    if any(
        _paths_overlap(changed, component)
        for changed in changed_paths
        for component in component_paths
    ):
        raise ValidationError(
            "fast bootstrap orchestration diff overlaps a measurement component"
        )
    qualified_candidate_ids = [
        row["implementation_candidate_id"]
        for row in oracle_bundle["candidate-screening"]["document"]["candidates"]
        if row["qualification_status"] == "qualified"
    ]
    if (
        any(candidate_id is None for candidate_id in qualified_candidate_ids)
        or source_plan["qualified_candidate_ids"] != qualified_candidate_ids
        or expected_tasks
        != {
            "run.B1.full",
            "run.B2.full",
            *(f"run.B1.{candidate_id}" for candidate_id in qualified_candidate_ids),
        }
    ):
        raise ValidationError(
            "fast bootstrap source candidate set differs from normalized Oracle qualification"
        )

    source_artifacts = [
        _named_artifact(root, "source-plan", plan_path, verified=True),
        _named_artifact(root, "source-status", status_path, verified=True),
        _named_artifact(root, "source-raw-registry", registry_path, verified=True),
    ]
    document: dict[str, Any] = {
        "schema_version": _BOOTSTRAP_VERSION,
        "bootstrap_id": bootstrap_id,
        "campaign_id": campaign_id,
        "created_at": created_at or utc_now(),
        "source_revision": source_revision,
        "evaluation_revision": evaluation,
        "source_artifacts": source_artifacts,
        "static_artifacts": static_artifacts,
        "imported_receipts": imported,
        "bootstrap_commitment_sha256": "0" * 64,
    }
    return _commit(document, "bootstrap_commitment_sha256")


def build_fast_campaign_plan(
    *,
    plan_id: str,
    bootstrap_path: Path,
    workspace_root: Path,
    tasks: Sequence[Mapping[str, Any]],
    candidate_ids: Sequence[str] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    root = _workspace_root(workspace_root)
    bootstrap_physical, _ = _workspace_existing_path(
        root, bootstrap_path, label="fast campaign bootstrap", regular_only=True
    )
    bootstrap = _load_version(
        bootstrap_physical, _BOOTSTRAP_VERSION, label="fast campaign bootstrap"
    )
    oracle_static = {
        artifact_id: row["artifact"]
        for artifact_id in FAST_ORACLE_STATIC_ARTIFACT_VERSIONS
        for row in bootstrap["static_artifacts"]
        if row["artifact_id"] == artifact_id
    }
    if set(oracle_static) != set(FAST_ORACLE_STATIC_ARTIFACT_VERSIONS):
        raise ValidationError("fast campaign bootstrap lacks the Oracle static closure")
    task_rows = [dict(task) for task in tasks]
    if not task_rows:
        raise ConfigurationError("fast campaign plan requires tasks")
    selected_candidates = (
        sorted(
            {
                candidate_id
                for task in task_rows
                for candidate_id in task.get("candidate_ids", [])
            }
        )
        if candidate_ids is None
        else list(candidate_ids)
    )
    if not selected_candidates:
        raise ConfigurationError("fast campaign plan requires candidate ids")
    for task in task_rows:
        task_oracle_static = {
            binding["artifact_id"]: binding["artifact"]
            for binding in task.get("static_bindings", [])
            if binding.get("artifact_id") in FAST_ORACLE_STATIC_ARTIFACT_VERSIONS
        }
        if task_oracle_static != oracle_static:
            raise ValidationError(
                f"fast campaign task {task.get('task_id')} Oracle static binding differs"
            )
        for field in (
            "manifest",
            "profile",
            "measurement_protocol",
            "compiler_artifact",
            "baseline_artifact",
        ):
            artifact = task.get(field)
            if artifact is not None:
                _verify_artifact(
                    root,
                    artifact,
                    label=f"fast campaign task {task.get('task_id')} {field}",
                )
        for binding in task.get("static_bindings", []):
            _verify_artifact(
                root,
                binding["artifact"],
                label=(
                    f"fast campaign task {task.get('task_id')} static "
                    f"{binding.get('artifact_id')}"
                ),
            )
    document: dict[str, Any] = {
        "schema_version": _PLAN_VERSION,
        "plan_id": plan_id,
        "campaign_id": bootstrap["campaign_id"],
        "created_at": created_at or utc_now(),
        "bootstrap_id": bootstrap["bootstrap_id"],
        "bootstrap": _artifact_ref(root, bootstrap_physical, label="fast campaign bootstrap"),
        "repository": bootstrap["evaluation_revision"],
        "max_parallel_runs": 4,
        "jobs_per_run": 4,
        "candidate_ids": selected_candidates,
        "tasks": task_rows,
        "plan_commitment_sha256": "0" * 64,
    }
    return _commit(document, "plan_commitment_sha256")


def _load_plan_status_index(
    intent: FastRunAuthorizationIntent,
) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any], Path]:
    root = _workspace_root(intent.workspace_root)
    plan_path, _ = _workspace_existing_path(
        root, intent.plan_path, label="fast authorization plan", regular_only=True
    )
    status_path, _ = _workspace_existing_path(
        root, intent.status_path, label="fast authorization status", regular_only=True
    )
    index_path, _ = _workspace_existing_path(
        root, intent.index_path, label="fast authorization index", regular_only=True
    )
    plan = _load_version(plan_path, _PLAN_VERSION, label="fast authorization plan")
    status = _load_version(status_path, _STATUS_VERSION, label="fast authorization status")
    index = _load_version(index_path, _INDEX_VERSION, label="fast authorization index")
    plan_artifact = _artifact_ref(root, plan_path, label="fast authorization plan")
    index_artifact = _artifact_ref(root, index_path, label="fast authorization index")
    if (
        status["campaign_id"] != plan["campaign_id"]
        or status["plan"] != plan_artifact
        or status["bootstrap"] != plan["bootstrap"]
        or status["index"] != index_artifact
        or index["campaign_id"] != plan["campaign_id"]
        or index["bootstrap_sha256"] != plan["bootstrap"]["canonical_sha256"]
        or index["plan_sha256"] != plan_artifact["canonical_sha256"]
    ):
        raise ValidationError("fast authorization inputs bind different campaign identities")
    _verify_artifact(root, plan["bootstrap"], label="fast authorization bootstrap")
    return root, plan, status, index, plan_path


def _reference_task_contract(
    root: Path, task: Mapping[str, Any]
) -> Mapping[str, Any]:
    source_rows = [
        row
        for row in task["static_bindings"]
        if row["artifact_id"] == "source-campaign-plan"
    ]
    snapshot_rows = [
        row
        for row in task["static_bindings"]
        if row["artifact_id"] == "reference-toolchain-snapshot"
    ]
    if len(source_rows) != 1 or len(snapshot_rows) != 1:
        raise ValidationError(
            "fast reference run requires exact source-plan and toolchain-snapshot bindings"
        )
    source_path = _verify_artifact(
        root, source_rows[0]["artifact"], label="fast reference source campaign plan"
    )
    source = _load_version(
        source_path,
        "candidate-campaign-plan.v1",
        label="fast reference source campaign plan",
    )
    baseline = next(
        (
            row
            for row in source["reference_toolchain"]["baselines"]
            if row["profile_id"] == task.get("reference_profile_id")
        ),
        None,
    )
    if (
        baseline is None
        or baseline["profile_sha256"] != task.get("reference_profile_sha256")
        or source["reference_toolchain"]["snapshot"] != snapshot_rows[0]["artifact"]
        or task.get("compiler_artifact") != snapshot_rows[0]["artifact"]
    ):
        raise ValidationError(
            "fast reference run differs from the frozen source toolchain baseline"
        )
    return baseline


def fast_configuration_template_sha256(
    configuration: Mapping[str, Any],
    provenance: Mapping[str, Any],
    baseline_task_id: str | None,
) -> str:
    projected_configuration = {
        key: value
        for key, value in configuration.items()
        if key not in {"baseline_timeout_run_id", "baseline_timeout_run_sha256"}
    }
    return sha256_json(
        {
            "configuration": projected_configuration,
            "provenance": dict(provenance),
            "baseline_task_id": baseline_task_id,
        }
    )


def _require_usable_baseline_run(run: Mapping[str, Any]) -> None:
    correctness = _correctness_summary(run)
    if (
        run["state"] != "completed"
        or not correctness["all_correct"]
        or not _metrics_summary(run, correctness)["complete"]
    ):
        raise ValidationError("fast task baseline is not complete correct metric evidence")


def _resolve_baseline_binding(
    *,
    root: Path,
    plan: Mapping[str, Any],
    index: Mapping[str, Any],
    task: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]] | None:
    baseline_task_id = task.get("baseline_task_id")
    baseline_artifact = task.get("baseline_artifact")
    if baseline_task_id is None:
        if baseline_artifact is not None:
            raise ValidationError("fast task has a baseline artifact without a baseline task")
        return None
    if baseline_artifact is not None:
        path = _verify_artifact(root, baseline_artifact, label="fast task imported baseline")
        run = _load_version(path, "run-record.v1", label="fast task imported baseline")
        bootstrap_path = _verify_artifact(root, plan["bootstrap"], label="fast task bootstrap")
        bootstrap = _load_version(bootstrap_path, _BOOTSTRAP_VERSION, label="fast task bootstrap")
        imported = next(
            (
                row
                for row in bootstrap["imported_receipts"]
                if row["task_id"] == baseline_task_id
            ),
            None,
        )
        if (
            imported is None
            or imported["run_artifact"] != baseline_artifact
            or imported["run_id"] != run["run_id"]
        ):
            raise ValidationError("fast task baseline differs from the bootstrap import")
        _require_usable_baseline_run(run)
        return path, run
    ref = next(
        (row for row in index["receipts"] if row["task_id"] == baseline_task_id), None
    )
    if ref is None:
        raise ValidationError("fast task baseline receipt is absent from the current index")
    receipt_path = _verify_artifact(root, ref["receipt"], label="fast task baseline receipt")
    receipt = _load_version(receipt_path, _RECEIPT_VERSION, label="fast task baseline receipt")
    if (
        receipt["task_id"] != baseline_task_id
        or receipt["run_id"] != ref["run_id"]
        or receipt["terminal"]["commitment_sha256"]
        != ref["terminal_commitment_sha256"]
        or receipt["terminal"]["state"] != "completed"
    ):
        raise ValidationError("fast task baseline receipt is not completed exact evidence")
    run_path = _verify_artifact(root, receipt["run_artifact"], label="fast task baseline run")
    run = _load_version(run_path, "run-record.v1", label="fast task baseline run")
    if run["run_id"] != receipt["run_id"] or run["state"] != "completed":
        raise ValidationError("fast task baseline normalized run differs from its receipt")
    _require_usable_baseline_run(run)
    return run_path, run


def _require_baseline_matches_task(
    *,
    baseline: Mapping[str, Any],
    task: Mapping[str, Any],
    configuration: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> None:
    primary = configuration.get("primary_metric_id")
    metric = next(
        (
            row
            for row in configuration.get("metrics", [])
            if row.get("metric_id") == primary
        ),
        None,
    )
    if (
        baseline["suite_id"] != task["suite_id"]
        or baseline["manifest_sha256"] != task["manifest"]["canonical_sha256"]
        or baseline["configuration"]["primary_metric_id"] != primary
        or metric is None
        or metric_spec(baseline)["unit"] != metric.get("unit")
        or baseline["provenance"].get("compiler_artifact_sha256")
        != task["compiler_artifact"]["physical_sha256"]
        or baseline["provenance"].get("measurement_protocol_sha256")
        != task["measurement_protocol"]["canonical_sha256"]
        or baseline["provenance"].get("execution_environment_sha256")
        != task["execution_environment_sha256"]
        or any(
            baseline["provenance"].get(key) != provenance.get(key)
            for key in (
                "compiler_artifact_sha256",
                "measurement_protocol_sha256",
                "execution_environment_sha256",
            )
        )
    ):
        raise ValidationError("fast run baseline differs from the exact measured contract")


def authorize_fast_candidate_run_prelease(
    intent: FastRunAuthorizationIntent,
) -> dict[str, Any]:
    """Authorize one bounded-parallel run using only immutable normalized metadata."""

    root, plan, status, index, _ = _load_plan_status_index(intent)
    if status["state"] != "running":
        raise ValidationError("fast candidate campaign is not running")
    tasks = {task["task_id"]: task for task in plan["tasks"]}
    task = tasks.get(intent.task_id)
    if task is None or task["kind"] not in {"run", "diagnostic"}:
        raise ValidationError("fast authorization task is not a planned run")
    status_row = next(
        (row for row in status["tasks"] if row["task_id"] == intent.task_id), None
    )
    if (
        status_row is None
        or status_row["state"] != "ready"
        or intent.task_id not in status["ready_tasks"]
    ):
        raise ValidationError("fast authorization task is not ready")
    if any(row["task_id"] == intent.task_id for row in index["receipts"]):
        raise ValidationError("fast authorization task already has a terminal receipt")
    output_path, output_relative = _workspace_output_path(
        root,
        intent.output_path,
        label="fast run output",
        create_parent=False,
    )
    if output_relative != task["output_path"]:
        raise ValidationError("fast run output differs from the planned output")
    if output_path.exists() and not output_path.is_file():
        raise ValidationError("fast run output target must be a regular file")
    if task.get("run_id") != f"{plan['campaign_id']}:{task['task_id']}":
        raise ValidationError("fast run id differs from the planned campaign identity")
    if intent.run_id != task["run_id"]:
        raise ValidationError("fast run id differs from the authorized execution intent")
    receipt_relative = task.get("receipt_path")
    if receipt_relative is None:
        raise ValidationError("fast task lacks its planned receipt path")
    validate_relative_path(receipt_relative, label="fast task receipt path")
    _, observed_receipt_relative = _workspace_output_path(
        root,
        intent.receipt_path,
        label="fast run receipt",
        create_parent=False,
    )
    if observed_receipt_relative != receipt_relative:
        raise ValidationError("fast run receipt path differs from the planned receipt")
    for binding in task["static_bindings"]:
        _verify_artifact(
            root,
            binding["artifact"],
            label=f"fast task static binding {binding['artifact_id']}",
        )
    explicit_paths = (
        ("manifest", task.get("manifest"), intent.manifest_path),
        ("compiler", task.get("compiler_artifact"), intent.compiler_artifact_path),
        (
            "measurement protocol",
            task.get("measurement_protocol"),
            intent.measurement_protocol_path,
        ),
    )
    for label, artifact, path in explicit_paths:
        if artifact is None or path is None or not _artifact_matches_path(
            root, artifact, path, label=f"fast authorization {label}"
        ):
            raise ValidationError(f"fast run {label} differs from the planned artifact")
    manifest = read_json(intent.manifest_path)
    if not isinstance(manifest, dict):
        raise ValidationError("fast run manifest must be a JSON object")
    validate_document(manifest)
    if (
        manifest.get("schema_version") != "benchmark-manifest.v1"
        or manifest.get("suite_id") != task.get("suite_id")
        or len(manifest.get("cases", [])) != task.get("expected_case_count")
        or manifest.get("provenance", {}).get("data_role") != task.get("data_role")
    ):
        raise ValidationError("fast run manifest suite, role, or case count differs from the plan")
    is_reference = task.get("run_kind") == "reference"
    if is_reference:
        if task.get("profile") is not None or intent.pipeline_profile_path is not None:
            raise ValidationError(
                "fast reference run must not consume an ACCELA pipeline profile"
            )
        expected_profile_sha256 = task.get("reference_profile_sha256")
        if (
            not isinstance(expected_profile_sha256, str)
            or expected_profile_sha256 == "0" * 64
        ):
            raise ValidationError("fast reference run lacks its frozen profile identity")
    else:
        if (
            task.get("profile") is None
            or intent.pipeline_profile_path is None
            or not _artifact_matches_path(
                root,
                task["profile"],
                intent.pipeline_profile_path,
                label="fast authorization profile",
            )
        ):
            raise ValidationError("fast run profile differs from the planned artifact")
        expected_profile_sha256 = sha256_file(intent.pipeline_profile_path)
    baseline = _resolve_baseline_binding(root=root, plan=plan, index=index, task=task)
    if baseline is None:
        if intent.baseline_run_path is not None:
            raise ValidationError("fast run supplied an unplanned baseline")
    else:
        baseline_path, baseline_run = baseline
        if intent.baseline_run_path is None:
            raise ValidationError("fast run omitted its planned baseline")
        observed_baseline, _ = _workspace_existing_path(
            root,
            intent.baseline_run_path,
            label="fast authorization baseline",
            regular_only=True,
        )
        if observed_baseline != baseline_path:
            raise ValidationError("fast run baseline path differs from the exact plan/index binding")

    configuration = intent.configuration
    required_configuration = {
        "compile_repetitions": 5,
        "reuse_compile_cache": False,
        "repetitions": 1,
        "max_workers": plan["jobs_per_run"],
        "keep_going": False,
        "retry_failures": False,
        "seed": 20260809,
        "consistency_fraction": 0.1,
        "consistency_repetitions": 3,
    }
    if any(configuration.get(key) != value for key, value in required_configuration.items()):
        raise ValidationError("fast run configuration differs from the fixed speed/correctness contract")
    if sorted(configuration.get("enabled_candidate_ids", [])) != sorted(task["candidate_ids"]):
        raise ValidationError("fast run enabled candidates differ from the planned task")
    provenance = intent.provenance
    if baseline is not None:
        _require_baseline_matches_task(
            baseline=baseline[1],
            task=task,
            configuration=configuration,
            provenance=provenance,
        )
    if is_reference and (
        configuration.get("pipeline_profile_file_sha256") is not None
        or configuration.get("candidate_registry_sha256") is not None
        or configuration.get("candidate_pass_registry_sha256") is not None
        or configuration.get("enabled_candidate_ids", [])
    ):
        raise ValidationError(
            "fast reference run must not carry ACCELA profile or candidate configuration"
        )
    reference_contract = _reference_task_contract(root, task) if is_reference else None
    if reference_contract is not None:
        compiler = configuration.get("compiler")
        if (
            compiler
            != {
                "kind": "external",
                "adapter": "host",
                "command_sha256": reference_contract["compiler_command_sha256"],
                "executable": reference_contract["compiler_executable"],
                "environment_keys": [],
            }
        ):
            raise ValidationError(
                "fast reference compiler command differs from the frozen baseline"
            )
    if (
        provenance.get("repo_commit") != plan["repository"]["commit"]
        or provenance.get("repo_dirty") is not False
        or provenance.get("tracked_diff_sha256") is not None
    ):
        raise ValidationError("fast run repository provenance differs from the plan")
    expected_template = task.get("expected_configuration_template_sha256")
    if expected_template != fast_configuration_template_sha256(
        configuration, provenance, task.get("baseline_task_id")
    ):
        raise ValidationError("fast run configuration/provenance template differs from the plan")
    if configuration.get("max_workers") != 4 or task.get("expected_case_count") is None:
        raise ValidationError("fast run jobs or expected case count differs from the plan")
    if baseline is None:
        if (
            configuration.get("baseline_timeout_run_id") is not None
            or configuration.get("baseline_timeout_run_sha256") is not None
        ):
            raise ValidationError("fast run contains unplanned baseline timeout provenance")
    else:
        _, baseline_run = baseline
        if (
            configuration.get("baseline_timeout_run_id") != baseline_run["run_id"]
            or configuration.get("baseline_timeout_run_sha256")
            != sha256_json(baseline_run)
        ):
            raise ValidationError("fast run timeout provenance differs from the exact baseline")
    compiler_sha256 = sha256_artifact(intent.compiler_artifact_path)
    if provenance.get("compiler_artifact_sha256") != compiler_sha256:
        raise ValidationError("fast run compiler artifact provenance differs")
    if is_reference and compiler_sha256 != task["compiler_artifact"]["physical_sha256"]:
        raise ValidationError(
            "fast reference compiler provenance differs from the toolchain snapshot"
        )
    if intent.measurement_protocol_path is None:
        raise ValidationError("fast run requires protocol provenance")
    protocol = read_json(intent.measurement_protocol_path)
    if (
        provenance.get("pipeline_profile_sha256") != expected_profile_sha256
        or provenance.get("measurement_protocol_sha256") != sha256_json(protocol)
        or provenance.get("execution_environment_sha256")
        != task.get("execution_environment_sha256")
        or provenance.get("pipeline_profile_id") != task.get("logical_profile_id")
    ):
        raise ValidationError("fast run profile, protocol, or environment provenance differs")
    return dict(task)


def _correctness_summary(run: Mapping[str, Any]) -> dict[str, Any]:
    statuses = [case["status"] for case in run["cases"]]
    passed = statuses.count("passed")
    timed_out = statuses.count("timeout")
    pending = statuses.count("pending")
    failed = len(statuses) - passed - timed_out - pending
    return {
        "expected_cases": run["manifest_case_count"],
        "passed_cases": passed,
        "failed_cases": failed,
        "timed_out_cases": timed_out,
        "pending_cases": pending,
        "all_correct": passed == run["manifest_case_count"],
    }


def _metrics_summary(run: Mapping[str, Any], correctness: Mapping[str, Any]) -> dict[str, Any]:
    primary = run["configuration"]["primary_metric_id"]
    spec = metric_spec(run, primary)
    values = run_case_metrics(run)
    aggregate = [
        {"case_id": case_id, "value": value}
        for case_id, value in sorted(values.items())
    ]
    complete = bool(correctness["all_correct"] and len(values) == correctness["expected_cases"])
    return {
        "primary_metric_id": primary,
        "unit": spec["unit"],
        "sample_count": len(values),
        "aggregate_sha256": sha256_json(aggregate),
        "complete": complete,
    }


def _receipt_configuration(
    plan: Mapping[str, Any], run: Mapping[str, Any]
) -> dict[str, Any]:
    provenance = run["provenance"]
    required = (
        "compiler_artifact_sha256",
        "execution_environment_sha256",
        "pipeline_profile_sha256",
        "measurement_protocol_sha256",
    )
    if any(not isinstance(provenance.get(key), str) for key in required):
        raise ValidationError("fast receipt run lacks complete provenance hashes")
    configuration: dict[str, Any] = {
        "configuration_sha256": "0" * 64,
        "repository": plan["repository"],
        "compiler_artifact_sha256": provenance["compiler_artifact_sha256"],
        "execution_environment_sha256": provenance["execution_environment_sha256"],
        "manifest_sha256": run["manifest_sha256"],
        "profile_sha256": provenance["pipeline_profile_sha256"],
        "protocol_sha256": provenance["measurement_protocol_sha256"],
        "jobs": plan["jobs_per_run"],
    }
    configuration["configuration_sha256"] = sha256_json(
        {key: value for key, value in configuration.items() if key != "configuration_sha256"}
    )
    return configuration


def _terminal_summary(run: Mapping[str, Any]) -> dict[str, Any]:
    state = {
        "completed": "completed",
        "failed": "failed",
        "interrupted": "cancelled",
    }.get(run["state"])
    if state is None:
        raise ValidationError("fast receipt requires a terminal normalized run")
    if run["state"] == "interrupted":
        if run["completed_at"] is not None or not any(
            case.get("cancellation_reason") == "infrastructure_failure"
            for case in run["cases"]
        ):
            raise ValidationError(
                "fast interrupted receipt requires a sealed non-resumable infrastructure failure"
            )
        terminal_at = run["updated_at"]
    else:
        terminal_at = run["completed_at"]
        if terminal_at is None:
            raise ValidationError("fast receipt requires a terminal normalized run")
    return {
        "state": state,
        "completed_at": terminal_at,
        "reason": None if state == "completed" else f"run_{run['state']}",
        "commitment_sha256": "0" * 64,
    }


def _validate_receipt_run_binding(
    *,
    receipt: Mapping[str, Any],
    run: Mapping[str, Any],
    task: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> None:
    correctness = _correctness_summary(run)
    metrics = _metrics_summary(run, correctness)
    terminal = _terminal_summary(run)
    if (
        receipt["campaign_id"] != plan["campaign_id"]
        or receipt["bootstrap_sha256"] != plan["bootstrap"]["canonical_sha256"]
        or receipt["plan_sha256"] != sha256_json(plan)
        or receipt["ordinal"] != task["ordinal"]
        or receipt["task_id"] != task["task_id"]
        or receipt["run_id"] != run["run_id"]
        or receipt["stage"] != task["stage"]
        or receipt["candidate_ids"] != task["candidate_ids"]
        or receipt["configuration"] != _receipt_configuration(plan, run)
        or receipt["correctness"] != correctness
        or receipt["metrics"] != metrics
        or any(receipt["terminal"][key] != terminal[key] for key in ("state", "completed_at", "reason"))
    ):
        raise ValidationError("fast receipt differs from its normalized run and plan binding")


def publish_fast_run_receipt(
    *,
    intent: FastRunAuthorizationIntent,
    run_record_path: Path,
    receipt_output_path: Path,
    state_root: Path | None = None,
) -> dict[str, Any]:
    """Publish an idempotent terminal receipt. ``state_root`` is never accessed."""

    del state_root
    root, plan, _, _, _ = _load_plan_status_index(intent)
    # Status snapshots are immutable. Even if current-head advances while this
    # task executes, reauthorization against the exact pre-run snapshot remains
    # stable and prevents callers from bypassing the ready gate at publication.
    task = authorize_fast_candidate_run_prelease(intent)
    output_intent, output_relative = _workspace_output_path(
        root,
        intent.output_path,
        label="fast receipt authorized output",
        create_parent=False,
    )
    if output_relative != task["output_path"] or output_intent != run_record_path.absolute():
        raise ValidationError("fast receipt run output differs from the authorized plan")
    run_path, run_relative = _workspace_existing_path(
        root, run_record_path, label="fast receipt normalized run", regular_only=True
    )
    if run_relative != task["output_path"]:
        raise ValidationError("fast receipt run path differs from the planned output")
    run = _load_version(run_path, "run-record.v1", label="fast receipt normalized run")
    if run["configuration"] != dict(intent.configuration) or run["provenance"] != dict(intent.provenance):
        raise ValidationError("fast receipt normalized run differs from the authorized intent")
    expected_run_id = task["run_id"]
    if run["run_id"] != expected_run_id:
        raise ValidationError("fast receipt run id differs from the campaign task identity")
    if (
        run["suite_id"] != task.get("suite_id")
        or run["manifest_case_count"] != task.get("expected_case_count")
    ):
        raise ValidationError("fast receipt run suite or case count differs from the plan")
    correctness = _correctness_summary(run)
    metrics = _metrics_summary(run, correctness)
    terminal = _terminal_summary(run)
    document: dict[str, Any] = {
        "schema_version": _RECEIPT_VERSION,
        "receipt_id": f"{plan['campaign_id']}:receipt:{task['task_id']}",
        "campaign_id": plan["campaign_id"],
        "bootstrap_sha256": plan["bootstrap"]["canonical_sha256"],
        "plan_sha256": sha256_json(plan),
        "ordinal": task["ordinal"],
        "task_id": task["task_id"],
        "run_id": run["run_id"],
        "stage": task["stage"],
        "candidate_ids": task["candidate_ids"],
        "configuration": _receipt_configuration(plan, run),
        "run_artifact": _artifact_ref(root, run_path, label="fast receipt normalized run"),
        "correctness": correctness,
        "metrics": metrics,
        "terminal": terminal,
    }
    payload = dict(document)
    payload_terminal = dict(terminal)
    payload_terminal.pop("commitment_sha256")
    payload["terminal"] = payload_terminal
    document["terminal"]["commitment_sha256"] = sha256_json(payload)
    validate_document(document)
    output, _ = _workspace_output_path(
        root, receipt_output_path, label="fast receipt output"
    )
    if (
        output != intent.receipt_path.absolute()
        or output.relative_to(root).as_posix() != task.get("receipt_path")
    ):
        raise ValidationError("fast receipt output differs from the planned receipt path")
    return publish_immutable_fast_document(output, document)


def _receipt_ref(
    root: Path, receipt_path: Path, receipt: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "ordinal": receipt["ordinal"],
        "task_id": receipt["task_id"],
        "run_id": receipt["run_id"],
        "receipt": _artifact_ref(root, receipt_path, label="fast receipt artifact"),
        "terminal_commitment_sha256": receipt["terminal"]["commitment_sha256"],
    }


def build_fast_run_index(
    *,
    plan_path: Path,
    receipt_paths: Sequence[Path],
    workspace_root: Path,
    previous_index_path: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    root = _workspace_root(workspace_root)
    plan_physical, _ = _workspace_existing_path(
        root, plan_path, label="fast index plan", regular_only=True
    )
    plan = _load_version(plan_physical, _PLAN_VERSION, label="fast index plan")
    plan_tasks = {task["task_id"]: task for task in plan["tasks"]}
    rows: list[dict[str, Any]] = []
    for path in receipt_paths:
        physical, _ = _workspace_existing_path(
            root, path, label="fast index receipt", regular_only=True
        )
        receipt = _load_version(physical, _RECEIPT_VERSION, label="fast index receipt")
        task = plan_tasks.get(receipt["task_id"])
        run_path = _verify_artifact(root, receipt["run_artifact"], label="fast index normalized run")
        run = _load_version(run_path, "run-record.v1", label="fast index normalized run")
        if task is None:
            raise ValidationError("fast index receipt references an unknown task")
        _validate_receipt_run_binding(receipt=receipt, run=run, task=task, plan=plan)
        rows.append(_receipt_ref(root, physical, receipt))
    rows.sort(key=lambda row: row["ordinal"])
    if previous_index_path is not None:
        previous_physical, _ = _workspace_existing_path(
            root, previous_index_path, label="previous fast index", regular_only=True
        )
        previous = _load_version(
            previous_physical, _INDEX_VERSION, label="previous fast index"
        )
        if (
            previous["campaign_id"] != plan["campaign_id"]
            or previous["bootstrap_sha256"] != plan["bootstrap"]["canonical_sha256"]
            or previous["plan_sha256"] != sha256_json(plan)
            or rows[: len(previous["receipts"])] != previous["receipts"]
        ):
            raise ValidationError("fast run index is not an append-only extension")
    document: dict[str, Any] = {
        "schema_version": _INDEX_VERSION,
        "index_id": f"{plan['campaign_id']}:index:{len(rows)}",
        "campaign_id": plan["campaign_id"],
        "generated_at": generated_at or utc_now(),
        "bootstrap_sha256": plan["bootstrap"]["canonical_sha256"],
        "plan_sha256": sha256_json(plan),
        "receipts": rows,
        "index_commitment_sha256": "0" * 64,
    }
    return _commit(document, "index_commitment_sha256")


def _load_named_evidence(
    root: Path,
    paths: Sequence[Path],
    *,
    version: str,
    id_field: str,
    label: str,
) -> tuple[list[dict[str, Any]], dict[str, tuple[dict[str, Any], dict[str, str]]]]:
    rows: list[dict[str, Any]] = []
    by_id: dict[str, tuple[dict[str, Any], dict[str, str]]] = {}
    for path in paths:
        physical, _ = _workspace_existing_path(root, path, label=label, regular_only=True)
        document = _load_version(physical, version, label=label)
        artifact_id = document[id_field]
        if artifact_id in by_id:
            raise ValidationError(f"{label} ids must be unique")
        artifact = _artifact_ref(root, physical, label=label)
        rows.append({"artifact_id": artifact_id, "artifact": artifact})
        by_id[artifact_id] = (document, artifact)
    return rows, by_id


def _receipt_ref_is_bound(
    ref: Mapping[str, Any],
    *,
    index: Mapping[str, Any],
    bootstrap: Mapping[str, Any],
) -> bool:
    if any(dict(row) == dict(ref) for row in index["receipts"]):
        return True
    return any(
        row["task_id"] == ref["task_id"]
        and row["run_id"] == ref["run_id"]
        and row["run_artifact"] == ref["receipt"]
        and row["terminal_commitment_sha256"]
        == ref["terminal_commitment_sha256"]
        for row in bootstrap["imported_receipts"]
    )


def _b3_top3_candidate_ids(study: Mapping[str, Any] | None) -> list[str]:
    if study is None:
        return []
    return [
        row["candidate_id"]
        for row in sorted(
            (
                row
                for row in study["candidates"]
                if row["eligible"] and row["geometric_mean_speedup"] is not None
            ),
            key=lambda row: (
                -float(row["geometric_mean_speedup"]),
                row["candidate_id"],
            ),
        )[:3]
    ]


def _diagnostic_task_is_selected(
    task: Mapping[str, Any], top3_candidate_ids: Sequence[str]
) -> bool:
    candidates = list(task["candidate_ids"])
    top3 = set(top3_candidate_ids)
    if task["measurement_mode"] == "standard_proxy":
        if (
            len(candidates) != 2
            or candidates != sorted(candidates)
            or task["task_id"] != f"diagnostic.pair.{'+'.join(candidates)}"
        ):
            raise ValidationError("fast diagnostic pair task identity is malformed")
        return set(candidates) <= top3
    if task["measurement_mode"] == "cache_hotblock":
        if not candidates:
            if task["task_id"] != "diagnostic.cache.full":
                raise ValidationError("fast diagnostic cache FULL task identity is malformed")
            return True
        if (
            len(candidates) != 1
            or task["task_id"] != f"diagnostic.cache.{candidates[0]}"
        ):
            raise ValidationError("fast diagnostic cache task identity is malformed")
        return candidates[0] in top3
    raise ValidationError("fast diagnostic task has an unsupported measurement mode")


def build_fast_campaign_status(
    *,
    plan_path: Path,
    index_path: Path,
    workspace_root: Path,
    generation: int,
    study_paths: Sequence[Path] = (),
    audit_paths: Sequence[Path] = (),
    diagnostic_paths: Sequence[Path] = (),
    diagnostic_study_path: Path | None = None,
    final_path: Path | None = None,
    running_task_ids: Sequence[str] = (),
    generated_at: str | None = None,
) -> dict[str, Any]:
    root = _workspace_root(workspace_root)
    plan_physical, _ = _workspace_existing_path(root, plan_path, label="fast status plan", regular_only=True)
    index_physical, _ = _workspace_existing_path(root, index_path, label="fast status index", regular_only=True)
    plan = _load_version(plan_physical, _PLAN_VERSION, label="fast status plan")
    index = _load_version(index_physical, _INDEX_VERSION, label="fast status index")
    if (
        index["campaign_id"] != plan["campaign_id"]
        or index["plan_sha256"] != sha256_json(plan)
        or index["bootstrap_sha256"] != plan["bootstrap"]["canonical_sha256"]
    ):
        raise ValidationError("fast status index binds another plan")
    studies, study_by_id = _load_named_evidence(
        root, study_paths, version=_STUDY_VERSION, id_field="study_id", label="fast status study"
    )
    audits, audit_by_id = _load_named_evidence(
        root, audit_paths, version=_AUDIT_VERSION, id_field="audit_id", label="fast status audit"
    )
    diagnostics, diagnostic_by_id = _load_named_evidence(
        root,
        diagnostic_paths,
        version=_RECEIPT_VERSION,
        id_field="receipt_id",
        label="fast status diagnostic",
    )
    plan_sha256 = sha256_json(plan)
    bootstrap_path = _verify_artifact(
        root, plan["bootstrap"], label="fast status bootstrap"
    )
    bootstrap = _load_version(
        bootstrap_path, _BOOTSTRAP_VERSION, label="fast status bootstrap"
    )
    plan_artifact = _artifact_ref(root, plan_physical, label="fast status plan")
    index_artifact = _artifact_ref(root, index_physical, label="fast status index")
    for document, _ in study_by_id.values():
        if (
            document["campaign_id"] != plan["campaign_id"]
            or document["bootstrap_sha256"] != plan["bootstrap"]["canonical_sha256"]
            or document["plan_sha256"] != plan_sha256
            or not all(
                _receipt_ref_is_bound(ref, index=index, bootstrap=bootstrap)
                for ref in [
                    document["baseline"],
                    *[row["receipt"] for row in document["candidates"]],
                ]
            )
        ):
            raise ValidationError("fast status study binds another campaign snapshot")
    for document, _ in audit_by_id.values():
        audited_status_path = _verify_artifact(
            root, document["status"], label="fast status audited status"
        )
        audited_status = _load_version(
            audited_status_path,
            _STATUS_VERSION,
            label="fast status audited status",
        )
        if (
            document["campaign_id"] != plan["campaign_id"]
            or document["bootstrap_sha256"] != plan["bootstrap"]["canonical_sha256"]
            or document["plan_sha256"] != plan_sha256
            or document["status_sha256"] != sha256_json(audited_status)
            or audited_status["campaign_id"] != plan["campaign_id"]
            or audited_status["bootstrap"] != plan["bootstrap"]
            or audited_status["plan"] != plan_artifact
            or document["scope_receipts"]
            != _scope_receipts(index, plan, document["checkpoint"])
        ):
            raise ValidationError("fast status audit binds another campaign snapshot")
        if document["checkpoint"] == "final":
            audited_task = next(
                (
                    row
                    for row in audited_status["tasks"]
                    if row["task_id"] == "audit.final"
                ),
                None,
            )
            if (
                document["index_sha256"] != sha256_json(index)
                or audited_status["index"] != index_artifact
                or audited_task is None
                or audited_task["state"] != "ready"
                or "audit.final" not in audited_status["ready_tasks"]
                or any(
                    row["artifact_id"] == f"{plan['campaign_id']}:audit:final"
                    for row in audited_status["audits"]
                )
            ):
                raise ValidationError(
                    "fast final audit must bind the exact pre-audit ready status"
                )
    indexed_receipts = {
        row["task_id"]: row for row in index["receipts"]
    }
    for document, artifact in diagnostic_by_id.values():
        indexed = indexed_receipts.get(document["task_id"])
        if (
            document["campaign_id"] != plan["campaign_id"]
            or document["bootstrap_sha256"] != plan["bootstrap"]["canonical_sha256"]
            or document["plan_sha256"] != plan_sha256
            or indexed is None
            or indexed["receipt"] != artifact
            or indexed["terminal_commitment_sha256"]
            != document["terminal"]["commitment_sha256"]
        ):
            raise ValidationError("fast status diagnostic differs from the current run index")
    diagnostic_study: dict[str, Any] | None = None
    diagnostic_study_artifact: dict[str, str] | None = None
    if diagnostic_study_path is not None:
        diagnostic_study_physical, _ = _workspace_existing_path(
            root,
            diagnostic_study_path,
            label="fast status diagnostic study",
            regular_only=True,
        )
        diagnostic_study = _load_version(
            diagnostic_study_physical,
            _DIAGNOSTIC_STUDY_VERSION,
            label="fast status diagnostic study",
        )
        diagnostic_study_artifact = _artifact_ref(
            root, diagnostic_study_physical, label="fast status diagnostic study"
        )
        b3_evidence = next(
            (
                (document, artifact)
                for document, artifact in study_by_id.values()
                if document["stage"] == "B3"
            ),
            None,
        )
        diagnostic_refs = [
            *[row["receipt"] for row in diagnostic_study["pairs"]],
            diagnostic_study["cache_full"]["receipt"],
            *[row["receipt"] for row in diagnostic_study["cache_candidates"]],
        ]
        if (
            b3_evidence is None
            or diagnostic_study["campaign_id"] != plan["campaign_id"]
            or diagnostic_study["bootstrap_sha256"]
            != plan["bootstrap"]["canonical_sha256"]
            or diagnostic_study["plan_sha256"] != plan_sha256
            or diagnostic_study["b3_study"] != b3_evidence[1]
            or diagnostic_study["top3_candidate_ids"]
            != _b3_top3_candidate_ids(b3_evidence[0])
            or not all(
                _receipt_ref_is_bound(ref, index=index, bootstrap=bootstrap)
                for ref in diagnostic_refs
            )
        ):
            raise ValidationError(
                "fast status diagnostic study binds another campaign snapshot"
            )
        expected_diagnostic_artifacts = {
            ref["receipt"]["path"]: ref["receipt"] for ref in diagnostic_refs
        }
        observed_diagnostic_artifacts = {
            artifact["path"]: artifact
            for _, artifact in diagnostic_by_id.values()
        }
        if observed_diagnostic_artifacts != expected_diagnostic_artifacts:
            raise ValidationError(
                "fast status diagnostic receipts differ from the diagnostic study"
            )
    evidence_by_task: dict[str, tuple[str, dict[str, str], str]] = {}
    receipt_docs: dict[str, dict[str, Any]] = {}
    for ref in index["receipts"]:
        receipt_path = _verify_artifact(root, ref["receipt"], label="fast status receipt")
        receipt = _load_version(receipt_path, _RECEIPT_VERSION, label="fast status receipt")
        if (
            receipt["task_id"] != ref["task_id"]
            or receipt["run_id"] != ref["run_id"]
            or receipt["terminal"]["commitment_sha256"] != ref["terminal_commitment_sha256"]
        ):
            raise ValidationError("fast status receipt differs from index binding")
        receipt_docs[receipt["task_id"]] = receipt
        evidence_by_task[receipt["task_id"]] = (
            receipt["terminal"]["state"],
            ref["receipt"],
            receipt["terminal"]["commitment_sha256"],
        )
    for document, artifact in study_by_id.values():
        matching = [task for task in plan["tasks"] if task["kind"] == "study" and task["stage"] == document["stage"]]
        if len(matching) != 1:
            raise ValidationError("fast status study does not identify one plan task")
        evidence_by_task[matching[0]["task_id"]] = (
            "completed",
            artifact,
            document["study_commitment_sha256"],
        )
    for document, artifact in audit_by_id.values():
        matching = [task for task in plan["tasks"] if task["kind"] == "audit" and task["stage"] == document["checkpoint"]]
        if len(matching) != 1:
            raise ValidationError("fast status audit does not identify one plan task")
        evidence_by_task[matching[0]["task_id"]] = (
            "completed" if document["passed"] else "failed",
            artifact,
            document["audit_commitment_sha256"],
        )
    for document, artifact in diagnostic_by_id.values():
        evidence_by_task[document["task_id"]] = (
            document["terminal"]["state"], artifact, document["terminal"]["commitment_sha256"]
        )
    if diagnostic_study is not None and diagnostic_study_artifact is not None:
        matching = [
            task
            for task in plan["tasks"]
            if task["kind"] == "study" and task["stage"] == "diagnostic"
        ]
        if len(matching) != 1 or matching[0]["task_id"] != "study.diagnostic":
            raise ValidationError(
                "fast diagnostic study does not identify one exact plan task"
            )
        evidence_by_task[matching[0]["task_id"]] = (
            "completed",
            diagnostic_study_artifact,
            diagnostic_study["diagnostic_study_commitment_sha256"],
        )
    final_artifact: dict[str, str] | None = None
    if final_path is not None:
        final_physical, _ = _workspace_existing_path(root, final_path, label="fast status final", regular_only=True)
        final_document = _load_version(final_physical, _FINAL_VERSION, label="fast status final")
        matching = [task for task in plan["tasks"] if task["kind"] == "final"]
        prefinal_status_path = _verify_artifact(
            root, final_document["status"], label="fast status pre-final snapshot"
        )
        prefinal_status = _load_version(
            prefinal_status_path,
            _STATUS_VERSION,
            label="fast status pre-final snapshot",
        )
        prefinal_task = next(
            (
                row
                for row in prefinal_status["tasks"]
                if matching and row["task_id"] == matching[0]["task_id"]
            ),
            None,
        )
        if (
            len(matching) != 1
            or final_document["campaign_id"] != plan["campaign_id"]
            or final_document["bootstrap"] != plan["bootstrap"]
            or final_document["plan"]
            != _artifact_ref(root, plan_physical, label="fast status plan")
            or final_document["index"]
            != _artifact_ref(root, index_physical, label="fast status index")
            or final_document["diagnostic_study"] != diagnostic_study_artifact
            or prefinal_status["campaign_id"] != plan["campaign_id"]
            or prefinal_status["bootstrap"] != plan["bootstrap"]
            or prefinal_status["plan"] != final_document["plan"]
            or prefinal_status["index"] != final_document["index"]
            or prefinal_status["state"] != "running"
            or prefinal_task is None
            or prefinal_task["state"] != "ready"
            or matching[0]["task_id"] not in prefinal_status["ready_tasks"]
        ):
            raise ValidationError("fast status final does not identify the campaign final task")
        final_artifact = _artifact_ref(root, final_physical, label="fast status final")
        evidence_by_task[matching[0]["task_id"]] = (
            "completed", final_artifact, final_document["final_commitment_sha256"]
        )
    running = set(running_task_ids)
    if len(running) > plan["max_parallel_runs"]:
        raise ConfigurationError("fast status exceeds the four-run concurrency bound")
    unknown_running = running - {task["task_id"] for task in plan["tasks"]}
    if unknown_running:
        raise ValidationError("fast status names an unknown running task")

    states: dict[str, str] = {}
    for task in plan["tasks"]:
        task_id = task["task_id"]
        if task_id in evidence_by_task:
            states[task_id] = evidence_by_task[task_id][0]
        elif task_id in running:
            if task["kind"] not in {"run", "diagnostic"}:
                raise ValidationError("only run tasks may be marked running")
            states[task_id] = "running"
        else:
            states[task_id] = "pending"

    b3_study = next(
        (document for document, _ in study_by_id.values() if document["stage"] == "B3"),
        None,
    )
    b3_study_artifact = next(
        (
            artifact
            for document, artifact in study_by_id.values()
            if document["stage"] == "B3"
        ),
        None,
    )
    promoted = (
        set()
        if b3_study is None
        else {
            row["candidate_id"]
            for row in b3_study["candidates"]
            if row["eligible"]
            and row["geometric_mean_speedup"] is not None
            and row["geometric_mean_speedup"] > 1.0
        }
    )
    diagnostic_top3 = _b3_top3_candidate_ids(b3_study)
    if b3_study is not None and b3_study_artifact is not None:
        for task in plan["tasks"]:
            if task["task_id"] in evidence_by_task:
                continue
            cancel = False
            if task["stage"] in {"B4", "B5", "B6"}:
                if task["kind"] == "run" and task.get("run_kind") == "single":
                    cancel = (
                        not task["candidate_ids"]
                        or task["candidate_ids"][0] not in promoted
                    )
                elif not promoted and (
                    task["kind"] == "study"
                    or (
                        task["kind"] == "run"
                        and task.get("run_kind") == "candidate_empty"
                    )
                ):
                    cancel = True
            elif task["kind"] == "diagnostic":
                cancel = not _diagnostic_task_is_selected(task, diagnostic_top3)
            if cancel:
                evidence_by_task[task["task_id"]] = (
                    "cancelled",
                    b3_study_artifact,
                    b3_study["study_commitment_sha256"],
                )
                states[task["task_id"]] = "cancelled"

    eligible_ready: list[str] = []
    blocked: dict[str, list[str]] = {}
    for task in plan["tasks"]:
        task_id = task["task_id"]
        if states[task_id] != "pending":
            blocked[task_id] = []
            continue
        missing_success = [dependency for dependency in task["dependencies"] if states[dependency] != "completed"]
        missing_terminal = [dependency for dependency in task["terminal_dependencies"] if states[dependency] not in _TERMINAL_TASK_STATES]
        blockers = [*missing_success, *missing_terminal]
        gate_ok = True
        if task["gate"] == "candidate_eligible":
            gate_ok = b3_study is not None and all(
                candidate_id in promoted for candidate_id in task["candidate_ids"]
            )
            if not gate_ok and not blockers:
                blockers = list(task["dependencies"][-1:])
        elif task["gate"] == "diagnostic_top3":
            gate_ok = (
                b3_study is not None
                and task["kind"] == "diagnostic"
                and _diagnostic_task_is_selected(task, diagnostic_top3)
            )
            if not gate_ok and not blockers:
                blockers = list(task["dependencies"][-1:])
        elif task["gate"] == "final_ready":
            observed_stages = {
                study["stage"] for study, _ in study_by_id.values()
            }
            observed_checkpoints = {
                audit["checkpoint"] for audit, _ in audit_by_id.values()
            }
            gate_ok = (
                b3_study is not None
                and diagnostic_study is not None
                and {"bootstrap", "B2", "B3", "final"} <= observed_checkpoints
                and (
                    not promoted
                    or {"B2", "B3", "B4", "B5", "B6"} <= observed_stages
                )
            )
            if not gate_ok and not blockers:
                blockers = list(task["dependencies"][-1:])
        blocked[task_id] = list(dict.fromkeys(blockers))
        if not blockers and gate_ok:
            eligible_ready.append(task_id)
    capacity = max(0, plan["max_parallel_runs"] - len(running))
    ready = eligible_ready[:capacity]
    for task_id in ready:
        states[task_id] = "ready"

    task_rows: list[dict[str, Any]] = []
    for task in plan["tasks"]:
        task_id = task["task_id"]
        evidence = evidence_by_task.get(task_id)
        task_rows.append(
            {
                "ordinal": task["ordinal"],
                "task_id": task_id,
                "stage": task["stage"],
                "state": states[task_id],
                "receipt": None if evidence is None else evidence[1],
                "terminal_commitment_sha256": None if evidence is None else evidence[2],
                "blocked_by": blocked[task_id] if states[task_id] == "pending" else [],
            }
        )
    if final_artifact is not None and all(row["state"] in _TERMINAL_TASK_STATES for row in task_rows):
        campaign_state = "complete"
    elif any(row["state"] == "failed" for row in task_rows) and not ready and not running:
        campaign_state = "failed"
    else:
        campaign_state = "running"
    document: dict[str, Any] = {
        "schema_version": _STATUS_VERSION,
        "status_id": f"{plan['campaign_id']}:status:{generation}",
        "campaign_id": plan["campaign_id"],
        "generation": generation,
        "generated_at": generated_at or utc_now(),
        "bootstrap": plan["bootstrap"],
        "plan": _artifact_ref(root, plan_physical, label="fast status plan"),
        "index": _artifact_ref(root, index_physical, label="fast status index"),
        "state": campaign_state,
        "tasks": task_rows,
        "ready_tasks": ready,
        "studies": studies,
        "audits": audits,
        "diagnostics": diagnostics,
        "diagnostic_study": diagnostic_study_artifact,
        "final": final_artifact,
        "status_commitment_sha256": "0" * 64,
    }
    return _commit(document, "status_commitment_sha256")


def publish_fast_current_head(
    *,
    bootstrap_path: Path,
    plan_path: Path,
    status_path: Path,
    index_path: Path,
    workspace_root: Path,
    head_path: Path,
    previous_head_path: Path | None = None,
    expected_previous_head_sha256: str | None = None,
    initial: bool = False,
    updated_at: str | None = None,
) -> dict[str, Any]:
    root = _workspace_root(workspace_root)
    plan_physical, _ = _workspace_existing_path(
        root, plan_path, label="fast head plan", regular_only=True
    )
    plan = _load_version(plan_physical, _PLAN_VERSION, label="fast head plan")
    lease_metadata = {
        "campaign_id": plan["campaign_id"],
        "generation": _load_version(
            _workspace_existing_path(
                root, status_path, label="fast head status", regular_only=True
            )[0],
            _STATUS_VERSION,
            label="fast head status",
        )["generation"],
    }
    with ExclusiveFileLease(
        candidate_wave_lease_path(root, plan["campaign_id"]),
        "fast campaign wave",
        lease_metadata,
    ):
        return publish_fast_current_head_owned(
            bootstrap_path=bootstrap_path,
            plan_path=plan_physical,
            status_path=status_path,
            index_path=index_path,
            workspace_root=root,
            head_path=head_path,
            previous_head_path=previous_head_path,
            expected_previous_head_sha256=expected_previous_head_sha256,
            initial=initial,
            updated_at=updated_at,
        )


def publish_fast_current_head_owned(
    *,
    bootstrap_path: Path,
    plan_path: Path,
    status_path: Path,
    index_path: Path,
    workspace_root: Path,
    head_path: Path,
    previous_head_path: Path | None = None,
    expected_previous_head_sha256: str | None = None,
    initial: bool = False,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """Advance current-head while the caller owns the campaign wave lease."""

    if initial == (previous_head_path is not None):
        raise ConfigurationError(
            "fast current head requires exactly one of initial or previous_head_path"
        )
    if initial and expected_previous_head_sha256 is not None:
        raise ConfigurationError(
            "initial fast current head cannot declare a previous-head hash"
        )
    if not initial and (
        not isinstance(expected_previous_head_sha256, str)
        or len(expected_previous_head_sha256) != 64
    ):
        raise ConfigurationError(
            "fast current head update requires the exact previous-head SHA-256"
        )
    root = _workspace_root(workspace_root)
    bootstrap_physical, _ = _workspace_existing_path(root, bootstrap_path, label="fast head bootstrap", regular_only=True)
    plan_physical, _ = _workspace_existing_path(root, plan_path, label="fast head plan", regular_only=True)
    status_physical, _ = _workspace_existing_path(root, status_path, label="fast head status", regular_only=True)
    index_physical, _ = _workspace_existing_path(root, index_path, label="fast head index", regular_only=True)
    bootstrap = _load_version(bootstrap_physical, _BOOTSTRAP_VERSION, label="fast head bootstrap")
    plan = _load_version(plan_physical, _PLAN_VERSION, label="fast head plan")
    status = _load_version(status_physical, _STATUS_VERSION, label="fast head status")
    index = _load_version(index_physical, _INDEX_VERSION, label="fast head index")
    plan_sha256 = sha256_json(plan)
    if (
        plan["bootstrap"] != _artifact_ref(root, bootstrap_physical, label="fast head bootstrap")
        or status["plan"] != _artifact_ref(root, plan_physical, label="fast head plan")
        or status["index"] != _artifact_ref(root, index_physical, label="fast head index")
        or index["plan_sha256"] != plan_sha256
        or bootstrap["campaign_id"] != plan["campaign_id"]
        or status["campaign_id"] != plan["campaign_id"]
    ):
        raise ValidationError("fast current head inputs bind different identities")
    document: dict[str, Any] = {
        "schema_version": _HEAD_VERSION,
        "campaign_id": plan["campaign_id"],
        "generation": status["generation"],
        "updated_at": updated_at or utc_now(),
        "bootstrap_sha256": sha256_json(bootstrap),
        "plan_sha256": plan_sha256,
        "status_id": status["status_id"],
        "status": _artifact_ref(root, status_physical, label="fast head status"),
        "index_id": index["index_id"],
        "index": _artifact_ref(root, index_physical, label="fast head index"),
        "head_commitment_sha256": "0" * 64,
    }
    document = _commit(document, "head_commitment_sha256")
    output, _ = _workspace_output_path(root, head_path, label="fast current head")
    lease_metadata = {
        "campaign_id": plan["campaign_id"],
        "generation": status["generation"],
    }
    with ExclusiveFileLease(
        output_lease_path(output),
        "fast campaign head",
        lease_metadata,
    ):
        if initial:
            if output.exists() or document["generation"] != 0:
                raise ValidationError(
                    "initial fast current head requires an absent target and generation zero"
                )
        else:
            if not output.is_file():
                raise ValidationError(
                    "fast current head update requires an existing head"
                )
            current = _load_version(
                output, _HEAD_VERSION, label="fast current head"
            )
            assert previous_head_path is not None
            previous_physical, _ = _workspace_existing_path(
                root,
                previous_head_path,
                label="fast expected previous head",
                regular_only=True,
            )
            previous = _load_version(
                previous_physical,
                _HEAD_VERSION,
                label="fast expected previous head",
            )
            if (
                previous != current
                or sha256_json(current) != expected_previous_head_sha256
            ):
                raise ValidationError(
                    "fast current head compare-and-swap previous head differs"
                )
            if (
                current["campaign_id"] != document["campaign_id"]
                or current["plan_sha256"] != document["plan_sha256"]
                or current["bootstrap_sha256"] != document["bootstrap_sha256"]
                or document["generation"] != current["generation"] + 1
            ):
                raise ValidationError(
                    "fast current head must advance the same campaign by one generation"
                )
            current_index_path = _verify_artifact(
                root, current["index"], label="fast current head previous index"
            )
            current_status_path = _verify_artifact(
                root, current["status"], label="fast current head previous status"
            )
            current_index = _load_version(
                current_index_path,
                _INDEX_VERSION,
                label="fast current head previous index",
            )
            current_status = _load_version(
                current_status_path,
                _STATUS_VERSION,
                label="fast current head previous status",
            )
            if (
                index["receipts"][: len(current_index["receipts"])]
                != current_index["receipts"]
            ):
                raise ValidationError(
                    "fast current head index is not an append-only extension"
                )
            old_tasks = {
                row["task_id"]: row for row in current_status["tasks"]
            }
            new_tasks = {row["task_id"]: row for row in status["tasks"]}
            if list(old_tasks) != list(new_tasks):
                raise ValidationError(
                    "fast current head status task identity differs"
                )
            for task_id, old_row in old_tasks.items():
                if (
                    old_row["state"] in _TERMINAL_TASK_STATES
                    and new_tasks[task_id] != old_row
                ):
                    raise ValidationError(
                        "fast current head cannot rewrite terminal task evidence"
                    )
            for field in ("studies", "audits", "diagnostics"):
                old_refs = {
                    row["artifact_id"]: row["artifact"]
                    for row in current_status[field]
                }
                new_refs = {
                    row["artifact_id"]: row["artifact"]
                    for row in status[field]
                }
                if any(
                    new_refs.get(key) != value
                    for key, value in old_refs.items()
                ):
                    raise ValidationError(
                        f"fast current head cannot remove or rewrite {field} evidence"
                    )
            for field in ("diagnostic_study", "final"):
                if (
                    current_status.get(field) is not None
                    and status.get(field) != current_status[field]
                ):
                    raise ValidationError(
                        f"fast current head cannot remove or rewrite {field} evidence"
                    )
            if (
                current_status["state"] in {"complete", "failed"}
                and status != current_status
            ):
                raise ValidationError(
                    "fast current head cannot advance beyond a terminal campaign status"
                )
        atomic_write_json(output, document)
    return document


def _scope_receipts(index: Mapping[str, Any], plan: Mapping[str, Any], checkpoint: str) -> list[dict[str, Any]]:
    if checkpoint == "bootstrap":
        return []
    maximum = {"B2": 2, "B3": 3, "final": 99}[checkpoint]
    task_by_id = {task["task_id"]: task for task in plan["tasks"]}
    return [
        dict(ref)
        for ref in index["receipts"]
        if task_by_id[ref["task_id"]]["stage"].startswith("B")
        and int(task_by_id[ref["task_id"]]["stage"][1:]) <= maximum
        or checkpoint == "final" and task_by_id[ref["task_id"]]["stage"] == "diagnostic"
    ]


def build_fast_audit(
    *,
    checkpoint: str,
    bootstrap_path: Path,
    plan_path: Path,
    index_path: Path,
    status_path: Path,
    workspace_root: Path,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if checkpoint not in {"bootstrap", "B2", "B3", "final"}:
        raise ConfigurationError("unknown fast campaign audit checkpoint")
    root = _workspace_root(workspace_root)
    bootstrap_physical, _ = _workspace_existing_path(root, bootstrap_path, label="fast audit bootstrap", regular_only=True)
    plan_physical, _ = _workspace_existing_path(root, plan_path, label="fast audit plan", regular_only=True)
    index_physical, _ = _workspace_existing_path(root, index_path, label="fast audit index", regular_only=True)
    status_physical, _ = _workspace_existing_path(root, status_path, label="fast audit status", regular_only=True)
    bootstrap = _load_version(bootstrap_physical, _BOOTSTRAP_VERSION, label="fast audit bootstrap")
    plan = _load_version(plan_physical, _PLAN_VERSION, label="fast audit plan")
    index = _load_version(index_physical, _INDEX_VERSION, label="fast audit index")
    status = _load_version(status_physical, _STATUS_VERSION, label="fast audit status")
    if (
        plan["bootstrap"] != _artifact_ref(root, bootstrap_physical, label="fast audit bootstrap")
        or status["plan"] != _artifact_ref(root, plan_physical, label="fast audit plan")
        or status["index"] != _artifact_ref(root, index_physical, label="fast audit index")
        or index["plan_sha256"] != sha256_json(plan)
    ):
        raise ValidationError("fast audit inputs bind different campaign identities")
    if checkpoint == "final":
        audit_task = next(
            (row for row in status["tasks"] if row["task_id"] == "audit.final"),
            None,
        )
        if (
            audit_task is None
            or audit_task["state"] != "ready"
            or "audit.final" not in status["ready_tasks"]
            or any(
                row["artifact_id"] == f"{plan['campaign_id']}:audit:final"
                for row in status["audits"]
            )
        ):
            raise ValidationError(
                "fast final audit requires the exact pre-audit ready status"
            )
    checks: list[dict[str, str]] = []
    for imported in bootstrap["imported_receipts"]:
        run_path = _verify_artifact(root, imported["run_artifact"], label="fast audit imported run")
        run = _load_version(run_path, "run-record.v1", label="fast audit imported run")
        if run["run_id"] != imported["run_id"] or run["state"] != "completed":
            raise ValidationError("fast audit imported normalized run binding differs")
    checks.append(
        {
            "check_id": "bootstrap.bindings",
            "outcome": "passed",
            "details_sha256": sha256_json(
                {
                    "bootstrap": sha256_json(bootstrap),
                    "plan": sha256_json(plan),
                    "index": sha256_json(index),
                    "status": sha256_json(status),
                }
            ),
        }
    )
    scope = _scope_receipts(index, plan, checkpoint)
    tasks = {task["task_id"]: task for task in plan["tasks"]}
    for ref in scope:
        receipt_path = _verify_artifact(root, ref["receipt"], label="fast audit receipt")
        receipt = _load_version(receipt_path, _RECEIPT_VERSION, label="fast audit receipt")
        if (
            receipt["ordinal"] != ref["ordinal"]
            or receipt["task_id"] != ref["task_id"]
            or receipt["run_id"] != ref["run_id"]
            or receipt["terminal"]["commitment_sha256"] != ref["terminal_commitment_sha256"]
        ):
            raise ValidationError("fast audit receipt index binding differs")
        run_path = _verify_artifact(root, receipt["run_artifact"], label="fast audit normalized run")
        run = _load_version(run_path, "run-record.v1", label="fast audit normalized run")
        _validate_receipt_run_binding(receipt=receipt, run=run, task=tasks[receipt["task_id"]], plan=plan)
        checks.append(
            {
                "check_id": f"receipt.{ref['ordinal']}",
                "outcome": "passed",
                "details_sha256": sha256_json(
                    {
                        "receipt": ref["receipt"],
                        "terminal": ref["terminal_commitment_sha256"],
                        "run": receipt["run_artifact"],
                    }
                ),
            }
        )
    document: dict[str, Any] = {
        "schema_version": _AUDIT_VERSION,
        "audit_id": f"{plan['campaign_id']}:audit:{checkpoint}",
        "campaign_id": plan["campaign_id"],
        "generated_at": generated_at or utc_now(),
        "checkpoint": checkpoint,
        "bootstrap_sha256": sha256_json(bootstrap),
        "plan_sha256": sha256_json(plan),
        "index_sha256": sha256_json(index),
        "status": _artifact_ref(root, status_physical, label="fast audit status"),
        "status_sha256": sha256_json(status),
        "scope_receipts": scope,
        "checks": checks,
        "passed": True,
        "audit_commitment_sha256": "0" * 64,
    }
    return _commit(document, "audit_commitment_sha256")


def _load_study_receipt(
    *,
    root: Path,
    bootstrap: Mapping[str, Any],
    path: Path,
    label: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    physical, _ = _workspace_existing_path(root, path, label=label, regular_only=True)
    document = read_json(physical)
    if not isinstance(document, dict):
        raise ValidationError(f"{label} must be a JSON object")
    validate_document(document)
    if document["schema_version"] == _RECEIPT_VERSION:
        run_path = _verify_artifact(root, document["run_artifact"], label=f"{label} run")
        run = _load_version(run_path, "run-record.v1", label=f"{label} run")
        return _receipt_ref(root, physical, document), run, document
    if document["schema_version"] != "run-record.v1":
        raise ValidationError(f"{label} must be a fast receipt or imported normalized run")
    artifact = _artifact_ref(root, physical, label=label)
    imported = next(
        (row for row in bootstrap["imported_receipts"] if row["run_artifact"] == artifact),
        None,
    )
    if imported is None or imported["run_id"] != document["run_id"]:
        raise ValidationError(f"{label} is not a bootstrap-imported normalized run")
    return (
        {
            "ordinal": 0,
            "task_id": imported["task_id"],
            "run_id": imported["run_id"],
            "receipt": imported["run_artifact"],
            "terminal_commitment_sha256": imported["terminal_commitment_sha256"],
        },
        document,
        None,
    )


def _assert_comparable_configuration(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any]
) -> None:
    left = baseline["provenance"]
    right = candidate["provenance"]
    for key in (
        "compiler_artifact_sha256",
        "execution_environment_sha256",
        "measurement_protocol_sha256",
    ):
        if left.get(key) != right.get(key):
            raise ValidationError(f"fast study {key} differs between paired runs")
    if (
        baseline["manifest_sha256"] != candidate["manifest_sha256"]
        or baseline["manifest_case_ids_sha256"] != candidate["manifest_case_ids_sha256"]
        or baseline["configuration"]["primary_metric_id"]
        != candidate["configuration"]["primary_metric_id"]
        or metric_spec(baseline)["unit"] != metric_spec(candidate)["unit"]
    ):
        raise ValidationError("fast study manifest or primary metric binding differs")
    candidate_timeout = candidate["configuration"]
    if candidate_timeout["timeout_policy"] == "baseline_derived" and (
        candidate_timeout["baseline_timeout_run_id"] != baseline["run_id"]
        or candidate_timeout["baseline_timeout_run_sha256"] != sha256_json(baseline)
    ):
        raise ValidationError("fast study candidate timeout policy does not bind the baseline")


def _measured_case_value(
    case: Mapping[str, Any], metric_id: str
) -> float | None:
    for measurement in case["measurements"]:
        if measurement["metric_id"] == metric_id:
            if (
                measurement["availability"] != "measured"
                or measurement["value"] is None
            ):
                return None
            return float(measurement["value"])
    return None


def build_fast_study(
    *,
    stage: str,
    bootstrap_path: Path,
    plan_path: Path,
    index_path: Path,
    baseline_receipt_path: Path,
    candidate_receipt_paths: Mapping[str, Path],
    workspace_root: Path,
    promotion_study_path: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if stage not in _STAGES:
        raise ConfigurationError("fast study stage must be B2-B6")
    root = _workspace_root(workspace_root)
    bootstrap_physical, _ = _workspace_existing_path(root, bootstrap_path, label="fast study bootstrap", regular_only=True)
    plan_physical, _ = _workspace_existing_path(root, plan_path, label="fast study plan", regular_only=True)
    index_physical, _ = _workspace_existing_path(root, index_path, label="fast study index", regular_only=True)
    bootstrap = _load_version(bootstrap_physical, _BOOTSTRAP_VERSION, label="fast study bootstrap")
    plan = _load_version(plan_physical, _PLAN_VERSION, label="fast study plan")
    index = _load_version(index_physical, _INDEX_VERSION, label="fast study index")
    if (
        plan["bootstrap"] != _artifact_ref(root, bootstrap_physical, label="fast study bootstrap")
        or index["plan_sha256"] != sha256_json(plan)
        or index["bootstrap_sha256"] != sha256_json(bootstrap)
    ):
        raise ValidationError("fast study inputs bind different campaign identities")
    baseline_ref, baseline_run, baseline_receipt = _load_study_receipt(
        root=root, bootstrap=bootstrap, path=baseline_receipt_path, label="fast study baseline"
    )
    indexed_by_task = {row["task_id"]: row for row in index["receipts"]}
    plan_by_task = {task["task_id"]: task for task in plan["tasks"]}
    if baseline_receipt is None:
        if stage != "B2" or baseline_ref["task_id"] != "run.B2.full":
            raise ValidationError("only B2 may use a bootstrap-imported fast baseline")
    else:
        indexed_baseline = indexed_by_task.get(baseline_receipt["task_id"])
        baseline_task = plan_by_task.get(baseline_receipt["task_id"])
        if (
            indexed_baseline != baseline_ref
            or baseline_task is None
            or baseline_task["kind"] != "run"
            or baseline_task.get("run_kind") != "candidate_empty"
            or baseline_task["stage"] != stage
        ):
            raise ValidationError("fast study baseline differs from its exact plan/index binding")
    baseline_correctness = _correctness_summary(baseline_run)
    if baseline_run["state"] != "completed" or not baseline_correctness["all_correct"]:
        raise ValidationError("fast study baseline is not complete correct evidence")
    baseline_metrics = run_case_metrics(baseline_run)
    if len(baseline_metrics) != baseline_run["manifest_case_count"]:
        raise ValidationError("fast study baseline metrics are incomplete")
    baseline_cases = {case["case_id"]: case for case in baseline_run["cases"]}
    results: list[dict[str, Any]] = []
    expected_candidates = [
        task["candidate_ids"][0]
        for task in plan["tasks"]
        if task["kind"] == "run"
        and task["stage"] == stage
        and task.get("run_kind") == "single"
    ]
    evaluated_candidates = list(expected_candidates)
    if stage in {"B4", "B5", "B6"}:
        if promotion_study_path is None:
            raise ConfigurationError("fast B4-B6 study requires the exact B3 promotion study")
        promotion_path, _ = _workspace_existing_path(
            root,
            promotion_study_path,
            label="fast study B3 promotion",
            regular_only=True,
        )
        promotion = _load_version(
            promotion_path, _STUDY_VERSION, label="fast study B3 promotion"
        )
        if (
            promotion["stage"] != "B3"
            or promotion["campaign_id"] != plan["campaign_id"]
            or promotion["plan_sha256"] != sha256_json(plan)
        ):
            raise ValidationError("fast B4-B6 promotion study binds another campaign")
        promoted = {
            row["candidate_id"]
            for row in promotion["candidates"]
            if row["eligible"]
            and row["geometric_mean_speedup"] is not None
            and row["geometric_mean_speedup"] > 1.0
        }
        evaluated_candidates = [
            candidate_id for candidate_id in expected_candidates if candidate_id in promoted
        ]
    elif promotion_study_path is not None:
        raise ConfigurationError("fast B2/B3 study cannot accept a promotion study")
    if list(candidate_receipt_paths) != evaluated_candidates:
        raise ValidationError(
            "fast study candidate receipts differ from the stage's planned promoted subset"
        )
    for candidate_id in evaluated_candidates:
        path = candidate_receipt_paths[candidate_id]
        receipt_ref, run, receipt = _load_study_receipt(
            root=root, bootstrap=bootstrap, path=path, label="fast study candidate"
        )
        candidate_task = None if receipt is None else plan_by_task.get(receipt["task_id"])
        if (
            receipt is None
            or indexed_by_task.get(receipt["task_id"]) != receipt_ref
            or candidate_task is None
            or candidate_task["kind"] != "run"
            or candidate_task.get("run_kind") != "single"
            or candidate_task["stage"] != stage
            or candidate_task["candidate_ids"] != [candidate_id]
        ):
            raise ValidationError("fast study candidate receipt is absent from the run index")
        if receipt["stage"] != stage or receipt["candidate_ids"] != [candidate_id]:
            raise ValidationError("fast study candidate receipt differs from the stage selection")
        _assert_comparable_configuration(baseline_run, run)
        correctness = receipt["correctness"]["all_correct"]
        metrics_complete = receipt["metrics"]["complete"]
        pairs: list[PairedCase] = []
        if correctness and metrics_complete:
            candidate_metrics = run_case_metrics(run)
            if set(candidate_metrics) != set(baseline_metrics):
                raise ValidationError("fast study candidate case set differs from the baseline")
            for case_id in sorted(baseline_metrics):
                case = baseline_cases[case_id]
                pairs.append(
                    PairedCase(
                        case_id=case_id,
                        family=case["family"],
                        target=case["target"],
                        weight=float(case["weight"]),
                        baseline_value=baseline_metrics[case_id],
                        candidate_value=candidate_metrics[case_id],
                        source_group=case["source_group"],
                    )
                )
        speedup = (
            math.exp(sum(math.log(pair.speedup) for pair in pairs) / len(pairs))
            if pairs
            else None
        )
        eligible = correctness and metrics_complete and bool(pairs) and speedup is not None
        reason = None
        if not correctness:
            reason = "correctness_failure"
        elif not metrics_complete:
            reason = "metrics_incomplete"
        elif not pairs:
            reason = "no_comparable_cases"
        static_pairs = [
            (
                _measured_case_value(baseline_cases[pair.case_id], "elf_text_bytes"),
                _measured_case_value(
                    {case["case_id"]: case for case in run["cases"]}[pair.case_id],
                    "elf_text_bytes",
                ),
            )
            for pair in pairs
        ]
        if static_pairs and all(
            full is not None and candidate is not None
            for full, candidate in static_pairs
        ):
            static_full = sum(float(full) for full, _ in static_pairs if full is not None)
            static_candidate = sum(
                float(candidate)
                for _, candidate in static_pairs
                if candidate is not None
            )
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
                "receipt": receipt_ref,
                "correctness_passed": correctness,
                "metrics_complete": metrics_complete,
                "comparable_case_count": len(pairs),
                "geometric_mean_speedup": speedup,
                "eligible": eligible,
                "ineligibility_reason": reason,
                "per_cases": [
                    {
                        "case_id": pair.case_id,
                        "weight": pair.weight,
                        "speedup": pair.speedup,
                    }
                    for pair in pairs
                ],
                "static_text_bytes_full": static_full,
                "static_text_bytes_full_plus_candidate": static_candidate,
                "static_text_ratio": static_ratio,
            }
        )
    document: dict[str, Any] = {
        "schema_version": _STUDY_VERSION,
        "study_id": f"{plan['campaign_id']}:study:{stage}",
        "campaign_id": plan["campaign_id"],
        "generated_at": generated_at or utc_now(),
        "stage": stage,
        "bootstrap_sha256": sha256_json(bootstrap),
        "plan_sha256": sha256_json(plan),
        "index_sha256": sha256_json(index),
        "baseline": baseline_ref,
        "primary_metric_id": baseline_run["configuration"]["primary_metric_id"],
        "metric_unit": metric_spec(baseline_run)["unit"],
        "planned_candidate_ids": expected_candidates,
        "evaluated_candidate_ids": evaluated_candidates,
        "candidates": results,
        "study_commitment_sha256": "0" * 64,
    }
    return _commit(document, "study_commitment_sha256")


def _load_exact_indexed_receipt(
    *,
    root: Path,
    bootstrap: Mapping[str, Any],
    plan: Mapping[str, Any],
    index: Mapping[str, Any],
    path: Path,
    expected_task_id: str,
    expected_candidate_ids: Sequence[str],
    expected_mode: str,
    label: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    ref, run, receipt = _load_study_receipt(
        root=root, bootstrap=bootstrap, path=path, label=label
    )
    task = next(
        (task for task in plan["tasks"] if task["task_id"] == expected_task_id),
        None,
    )
    indexed = next(
        (row for row in index["receipts"] if row["task_id"] == expected_task_id),
        None,
    )
    if (
        receipt is None
        or indexed != ref
        or task is None
        or task["kind"] != "diagnostic"
        or task["stage"] != "diagnostic"
        or task["measurement_mode"] != expected_mode
        or task["candidate_ids"] != list(expected_candidate_ids)
        or receipt["task_id"] != expected_task_id
        or receipt["candidate_ids"] != list(expected_candidate_ids)
    ):
        raise ValidationError(f"{label} differs from its exact plan/index binding")
    return ref, run, receipt


def _diagnostic_cache_result(
    ref: Mapping[str, Any], receipt: Mapping[str, Any]
) -> dict[str, Any]:
    completed = receipt["terminal"]["state"] == "completed"
    return {
        "receipt": dict(ref),
        "terminal_state": receipt["terminal"]["state"],
        "correctness_passed": completed and receipt["correctness"]["all_correct"],
        "metrics_complete": completed and receipt["metrics"]["complete"],
    }


def build_fast_diagnostic_study(
    *,
    bootstrap_path: Path,
    plan_path: Path,
    index_path: Path,
    b3_study_path: Path,
    pair_receipt_paths: Mapping[str, Path],
    cache_full_receipt_path: Path,
    cache_candidate_receipt_paths: Mapping[str, Path],
    workspace_root: Path,
    generated_at: str | None = None,
) -> dict[str, Any]:
    root = _workspace_root(workspace_root)
    bootstrap_physical, _ = _workspace_existing_path(
        root, bootstrap_path, label="fast diagnostic bootstrap", regular_only=True
    )
    plan_physical, _ = _workspace_existing_path(
        root, plan_path, label="fast diagnostic plan", regular_only=True
    )
    index_physical, _ = _workspace_existing_path(
        root, index_path, label="fast diagnostic index", regular_only=True
    )
    b3_physical, _ = _workspace_existing_path(
        root, b3_study_path, label="fast diagnostic B3 study", regular_only=True
    )
    bootstrap = _load_version(
        bootstrap_physical, _BOOTSTRAP_VERSION, label="fast diagnostic bootstrap"
    )
    plan = _load_version(plan_physical, _PLAN_VERSION, label="fast diagnostic plan")
    index = _load_version(index_physical, _INDEX_VERSION, label="fast diagnostic index")
    b3_study = _load_version(
        b3_physical, _STUDY_VERSION, label="fast diagnostic B3 study"
    )
    bootstrap_sha256 = sha256_json(bootstrap)
    plan_sha256 = sha256_json(plan)
    if (
        plan["bootstrap"]
        != _artifact_ref(root, bootstrap_physical, label="fast diagnostic bootstrap")
        or index["campaign_id"] != plan["campaign_id"]
        or index["bootstrap_sha256"] != bootstrap_sha256
        or index["plan_sha256"] != plan_sha256
        or b3_study["campaign_id"] != plan["campaign_id"]
        or b3_study["stage"] != "B3"
        or b3_study["bootstrap_sha256"] != bootstrap_sha256
        or b3_study["plan_sha256"] != plan_sha256
        or not all(
            _receipt_ref_is_bound(ref, index=index, bootstrap=bootstrap)
            for ref in [
                b3_study["baseline"],
                *[row["receipt"] for row in b3_study["candidates"]],
            ]
        )
    ):
        raise ValidationError("fast diagnostic inputs bind different campaign evidence")
    top3 = _b3_top3_candidate_ids(b3_study)
    expected_pairs = [sorted(pair) for pair in combinations(top3, 2)]
    expected_pair_ids = [
        f"diagnostic.pair.{'+'.join(pair)}" for pair in expected_pairs
    ]
    if set(pair_receipt_paths) != set(expected_pair_ids):
        raise ValidationError("fast diagnostic pair receipts differ from exact B3 Top3")
    if set(cache_candidate_receipt_paths) != set(top3):
        raise ValidationError("fast diagnostic cache receipts differ from B3 Top3 order")

    baseline_path = Path(b3_study["baseline"]["receipt"]["path"])
    baseline_ref, baseline_run, baseline_receipt = _load_study_receipt(
        root=root,
        bootstrap=bootstrap,
        path=baseline_path,
        label="fast diagnostic B3 baseline",
    )
    if (
        baseline_ref != b3_study["baseline"]
        or baseline_receipt is None
        or not _receipt_ref_is_bound(baseline_ref, index=index, bootstrap=bootstrap)
        or baseline_receipt["terminal"]["state"] != "completed"
        or not baseline_receipt["correctness"]["all_correct"]
        or not baseline_receipt["metrics"]["complete"]
    ):
        raise ValidationError("fast diagnostic B3 baseline is not exact complete evidence")
    b3_by_candidate = {
        row["candidate_id"]: row for row in b3_study["candidates"]
    }
    baseline_metrics = run_case_metrics(baseline_run)
    baseline_cases = {case["case_id"]: case for case in baseline_run["cases"]}
    pair_rows: list[dict[str, Any]] = []
    for candidate_ids, task_id in zip(expected_pairs, expected_pair_ids):
        ref, run, receipt = _load_exact_indexed_receipt(
            root=root,
            bootstrap=bootstrap,
            plan=plan,
            index=index,
            path=pair_receipt_paths[task_id],
            expected_task_id=task_id,
            expected_candidate_ids=candidate_ids,
            expected_mode="standard_proxy",
            label=f"fast diagnostic pair {task_id}",
        )
        completed = receipt["terminal"]["state"] == "completed"
        correctness = completed and receipt["correctness"]["all_correct"]
        metrics_complete = completed and receipt["metrics"]["complete"]
        pairs: list[PairedCase] = []
        if correctness and metrics_complete:
            _assert_comparable_configuration(baseline_run, run)
            candidate_metrics = run_case_metrics(run)
            if set(candidate_metrics) != set(baseline_metrics):
                raise ValidationError("fast diagnostic pair case set differs from B3 FULL")
            for case_id in sorted(baseline_metrics):
                case = baseline_cases[case_id]
                pairs.append(
                    PairedCase(
                        case_id=case_id,
                        family=case["family"],
                        target=case["target"],
                        weight=float(case["weight"]),
                        baseline_value=baseline_metrics[case_id],
                        candidate_value=candidate_metrics[case_id],
                        source_group=case["source_group"],
                    )
                )
        pair_speedup = case_geometric_mean(pairs) if pairs else None
        eligible = correctness and metrics_complete and bool(pairs) and pair_speedup is not None
        reason = None
        if not completed:
            reason = "terminal_failure"
        elif not correctness:
            reason = "correctness_failure"
        elif not metrics_complete:
            reason = "metrics_incomplete"
        elif not pairs:
            reason = "no_comparable_cases"
        expected_speedup = (
            math.prod(
                float(b3_by_candidate[candidate_id]["geometric_mean_speedup"])
                for candidate_id in candidate_ids
            )
            if eligible
            else None
        )
        pair_rows.append(
            {
                "candidate_ids": candidate_ids,
                "receipt": ref,
                "terminal_state": receipt["terminal"]["state"],
                "correctness_passed": correctness,
                "metrics_complete": metrics_complete,
                "comparable_case_count": len(pairs),
                "pair_geometric_mean_speedup": pair_speedup if eligible else None,
                "expected_multiplicative_speedup": expected_speedup,
                "delta_ln_geometric_mean": (
                    math.log(float(pair_speedup)) - math.log(float(expected_speedup))
                    if eligible and pair_speedup is not None and expected_speedup is not None
                    else None
                ),
                "eligible": eligible,
                "ineligibility_reason": reason,
            }
        )

    cache_full_ref, _, cache_full_receipt = _load_exact_indexed_receipt(
        root=root,
        bootstrap=bootstrap,
        plan=plan,
        index=index,
        path=cache_full_receipt_path,
        expected_task_id="diagnostic.cache.full",
        expected_candidate_ids=[],
        expected_mode="cache_hotblock",
        label="fast diagnostic cache FULL",
    )
    cache_rows: list[dict[str, Any]] = []
    for candidate_id in top3:
        task_id = f"diagnostic.cache.{candidate_id}"
        ref, _, receipt = _load_exact_indexed_receipt(
            root=root,
            bootstrap=bootstrap,
            plan=plan,
            index=index,
            path=cache_candidate_receipt_paths[candidate_id],
            expected_task_id=task_id,
            expected_candidate_ids=[candidate_id],
            expected_mode="cache_hotblock",
            label=f"fast diagnostic cache {candidate_id}",
        )
        cache_rows.append(
            {"candidate_id": candidate_id, **_diagnostic_cache_result(ref, receipt)}
        )
    document: dict[str, Any] = {
        "schema_version": _DIAGNOSTIC_STUDY_VERSION,
        "diagnostic_study_id": f"{plan['campaign_id']}:study:diagnostic",
        "campaign_id": plan["campaign_id"],
        "generated_at": generated_at or utc_now(),
        "bootstrap_sha256": bootstrap_sha256,
        "plan_sha256": plan_sha256,
        "index_sha256": sha256_json(index),
        "b3_study": _artifact_ref(root, b3_physical, label="fast diagnostic B3 study"),
        "top3_candidate_ids": top3,
        "pairs": pair_rows,
        "cache_full": _diagnostic_cache_result(cache_full_ref, cache_full_receipt),
        "cache_candidates": cache_rows,
        "diagnostic_study_commitment_sha256": "0" * 64,
    }
    return _commit(document, "diagnostic_study_commitment_sha256")


def build_fast_final(
    *,
    bootstrap_path: Path,
    plan_path: Path,
    index_path: Path,
    status_path: Path,
    audit_paths: Mapping[str, Path],
    study_paths: Mapping[str, Path],
    diagnostic_paths: Sequence[Path],
    diagnostic_study_path: Path,
    report_manifest_path: Path,
    workspace_root: Path,
    generated_at: str | None = None,
) -> dict[str, Any]:
    root = _workspace_root(workspace_root)
    bootstrap_physical, _ = _workspace_existing_path(root, bootstrap_path, label="fast final bootstrap", regular_only=True)
    plan_physical, _ = _workspace_existing_path(root, plan_path, label="fast final plan", regular_only=True)
    index_physical, _ = _workspace_existing_path(root, index_path, label="fast final index", regular_only=True)
    status_physical, _ = _workspace_existing_path(root, status_path, label="fast final status", regular_only=True)
    bootstrap = _load_version(bootstrap_physical, _BOOTSTRAP_VERSION, label="fast final bootstrap")
    plan = _load_version(plan_physical, _PLAN_VERSION, label="fast final plan")
    index = _load_version(index_physical, _INDEX_VERSION, label="fast final index")
    status = _load_version(status_physical, _STATUS_VERSION, label="fast final status")
    bootstrap_artifact = _artifact_ref(root, bootstrap_physical, label="fast final bootstrap")
    plan_artifact = _artifact_ref(root, plan_physical, label="fast final plan")
    index_artifact = _artifact_ref(root, index_physical, label="fast final index")
    status_artifact = _artifact_ref(root, status_physical, label="fast final status")
    bootstrap_sha256 = sha256_json(bootstrap)
    plan_sha256 = sha256_json(plan)
    index_sha256 = sha256_json(index)
    status_sha256 = sha256_json(status)
    if (
        bootstrap["campaign_id"] != plan["campaign_id"]
        or plan["bootstrap"] != bootstrap_artifact
        or index["campaign_id"] != plan["campaign_id"]
        or index["bootstrap_sha256"] != bootstrap_sha256
        or index["plan_sha256"] != plan_sha256
        or status["campaign_id"] != plan["campaign_id"]
        or status["bootstrap"] != bootstrap_artifact
        or status["plan"] != plan_artifact
        or status["index"] != index_artifact
        or status["state"] != "running"
    ):
        raise ValidationError("fast final pre-final status binds another campaign snapshot")
    final_tasks = [task for task in plan["tasks"] if task["kind"] == "final"]
    if len(final_tasks) != 1:
        raise ValidationError("fast final plan must identify exactly one final task")
    final_status = next(
        (
            row
            for row in status["tasks"]
            if row["task_id"] == final_tasks[0]["task_id"]
        ),
        None,
    )
    if (
        final_status is None
        or final_status["state"] != "ready"
        or final_tasks[0]["task_id"] not in status["ready_tasks"]
    ):
        raise ValidationError("fast final input must be the exact final-ready status")
    if set(audit_paths) != {"bootstrap", "B2", "B3", "final"} or not {"B2", "B3"} <= set(study_paths) or not set(study_paths) <= set(_STAGES):
        raise ConfigurationError("fast final requires four audits and bound B2/B3 studies")
    audit_refs: dict[str, dict[str, str]] = {}
    status_audits = {
        row["artifact_id"]: row["artifact"] for row in status["audits"]
    }
    for checkpoint, path in audit_paths.items():
        physical, _ = _workspace_existing_path(root, path, label="fast final audit", regular_only=True)
        audit = _load_version(physical, _AUDIT_VERSION, label="fast final audit")
        audit_artifact = _artifact_ref(root, physical, label="fast final audit")
        if (
            audit["checkpoint"] != checkpoint
            or audit["campaign_id"] != plan["campaign_id"]
            or audit["bootstrap_sha256"] != bootstrap_sha256
            or audit["plan_sha256"] != plan_sha256
            or audit["scope_receipts"] != _scope_receipts(index, plan, checkpoint)
            or not audit["passed"]
        ):
            raise ValidationError("fast final audit binding differs or failed")
        if checkpoint == "final":
            audited_status_path = _verify_artifact(
                root, audit["status"], label="fast final pre-audit status"
            )
            audited_status = _load_version(
                audited_status_path,
                _STATUS_VERSION,
                label="fast final pre-audit status",
            )
            audited_task = next(
                (
                    row
                    for row in audited_status["tasks"]
                    if row["task_id"] == "audit.final"
                ),
                None,
            )
            if (
                audit["index_sha256"] != index_sha256
                or audit["status_sha256"] != sha256_json(audited_status)
                or audited_status["campaign_id"] != plan["campaign_id"]
                or audited_status["bootstrap"] != bootstrap_artifact
                or audited_status["plan"] != plan_artifact
                or audited_status["index"] != index_artifact
                or audited_status["state"] != "running"
                or audited_status["generation"] + 1 != status["generation"]
                or audited_task is None
                or audited_task["state"] != "ready"
                or "audit.final" not in audited_status["ready_tasks"]
                or any(
                    row["artifact_id"] == audit["audit_id"]
                    for row in audited_status["audits"]
                )
                or status_audits.get(audit["audit_id"]) != audit_artifact
            ):
                raise ValidationError(
                    "fast final checkpoint audit does not bind the prior pre-audit snapshot"
                )
        elif status_audits.get(audit["audit_id"]) != audit_artifact:
            raise ValidationError(
                "fast historical checkpoint audit is absent from the final-ready status"
            )
        audit_refs[checkpoint] = audit_artifact
    study_refs: dict[str, dict[str, str]] = {}
    study_docs: dict[str, dict[str, Any]] = {}
    for stage, path in study_paths.items():
        physical, _ = _workspace_existing_path(root, path, label="fast final study", regular_only=True)
        study = _load_version(physical, _STUDY_VERSION, label="fast final study")
        if (
            study["stage"] != stage
            or study["campaign_id"] != plan["campaign_id"]
            or study["bootstrap_sha256"] != bootstrap_sha256
            or study["plan_sha256"] != plan_sha256
            or not all(
                _receipt_ref_is_bound(ref, index=index, bootstrap=bootstrap)
                for ref in [
                    study["baseline"],
                    *[row["receipt"] for row in study["candidates"]],
                ]
            )
        ):
            raise ValidationError("fast final study binding differs")
        study_docs[stage] = study
        study_refs[stage] = _artifact_ref(root, physical, label="fast final study")
    by_stage_candidate = {
        stage: {row["candidate_id"]: row for row in study["candidates"]}
        for stage, study in study_docs.items()
    }
    b3_candidates = set(by_stage_candidate["B3"])
    promoted_candidates = [
        candidate_id
        for candidate_id in plan["candidate_ids"]
        if candidate_id in b3_candidates
        and by_stage_candidate["B3"][candidate_id]["eligible"]
        and by_stage_candidate["B3"][candidate_id]["geometric_mean_speedup"] is not None
        and by_stage_candidate["B3"][candidate_id]["geometric_mean_speedup"] > 1.0
    ]
    validation_present = {stage for stage in ("B4", "B5", "B6") if stage in study_docs}
    if promoted_candidates and validation_present != {"B4", "B5", "B6"}:
        raise ValidationError("fast final promoted candidates require all B4-B6 studies")
    if not promoted_candidates and validation_present:
        raise ValidationError("fast final without promotion cannot claim B4-B6 studies")
    if promoted_candidates and any(
        study_docs[stage]["evaluated_candidate_ids"] != promoted_candidates
        for stage in ("B4", "B5", "B6")
    ):
        raise ValidationError("fast final B4-B6 studies differ from the B3 promoted subset")
    studies_map: dict[str, dict[str, str] | None] = {
        stage: study_refs.get(stage) for stage in _STAGES
    }
    diagnostic_study_physical, _ = _workspace_existing_path(
        root,
        diagnostic_study_path,
        label="fast final diagnostic study",
        regular_only=True,
    )
    diagnostic_study = _load_version(
        diagnostic_study_physical,
        _DIAGNOSTIC_STUDY_VERSION,
        label="fast final diagnostic study",
    )
    diagnostic_study_artifact = _artifact_ref(
        root, diagnostic_study_physical, label="fast final diagnostic study"
    )
    diagnostic_refs = [
        *[row["receipt"] for row in diagnostic_study["pairs"]],
        diagnostic_study["cache_full"]["receipt"],
        *[row["receipt"] for row in diagnostic_study["cache_candidates"]],
    ]
    if (
        diagnostic_study["campaign_id"] != plan["campaign_id"]
        or diagnostic_study["bootstrap_sha256"] != bootstrap_sha256
        or diagnostic_study["plan_sha256"] != plan_sha256
        or diagnostic_study["b3_study"] != study_refs["B3"]
        or diagnostic_study["top3_candidate_ids"]
        != _b3_top3_candidate_ids(study_docs["B3"])
        or status.get("diagnostic_study") != diagnostic_study_artifact
        or not all(
            _receipt_ref_is_bound(ref, index=index, bootstrap=bootstrap)
            for ref in diagnostic_refs
        )
    ):
        raise ValidationError("fast final diagnostic study binding differs")
    expected_diagnostic_artifacts = {
        ref["receipt"]["path"]: ref["receipt"] for ref in diagnostic_refs
    }
    supplied_diagnostic_artifacts: dict[str, tuple[dict[str, Any], dict[str, str]]] = {}
    for path in diagnostic_paths:
        physical, _ = _workspace_existing_path(
            root, path, label="fast final diagnostic", regular_only=True
        )
        receipt = _load_version(
            physical, _RECEIPT_VERSION, label="fast final diagnostic"
        )
        artifact = _artifact_ref(root, physical, label="fast final diagnostic")
        supplied_diagnostic_artifacts[artifact["path"]] = (receipt, artifact)
    if (
        len(diagnostic_paths) != len(supplied_diagnostic_artifacts)
        or set(supplied_diagnostic_artifacts) != set(expected_diagnostic_artifacts)
        or any(
            artifact != expected_diagnostic_artifacts[path]
            for path, (_, artifact) in supplied_diagnostic_artifacts.items()
        )
    ):
        raise ValidationError(
            "fast final diagnostic receipts differ from the diagnostic study"
        )
    candidate_rows: list[dict[str, Any]] = []
    for candidate_id in plan["candidate_ids"]:
        if any(candidate_id not in by_stage_candidate[stage] for stage in ("B2", "B3")):
            raise ValidationError("fast final studies omit required candidate evidence")
        is_promoted = candidate_id in promoted_candidates
        reported_stages = _STAGES if is_promoted else ("B2", "B3")
        final_stages = ("B3", "B4", "B5", "B6") if is_promoted else ("B3",)
        evidence = [by_stage_candidate[stage][candidate_id] for stage in final_stages]
        stage_map: dict[str, dict[str, Any] | None] = {
            stage: (
                {
                "study_sha256": study_refs[stage]["canonical_sha256"],
                    "receipt_sha256": by_stage_candidate[stage][candidate_id]["receipt"]["receipt"]["canonical_sha256"],
                    "eligible": by_stage_candidate[stage][candidate_id]["eligible"],
                    "geometric_mean_speedup": by_stage_candidate[stage][candidate_id]["geometric_mean_speedup"],
                }
                if stage in reported_stages
                else None
            )
            for stage in _STAGES
        }
        reasons = (
            ["not_promoted_by_B3"]
            if not is_promoted
            else [
                f"{stage}:{by_stage_candidate[stage][candidate_id]['ineligibility_reason']}"
                for stage in ("B2", *final_stages)
                if not by_stage_candidate[stage][candidate_id]["eligible"]
            ]
        )
        combined_case_count = sum(len(row["per_cases"]) for row in evidence)
        if is_promoted and not reasons and combined_case_count != 267:
            reasons.append("combined_case_count_not_267")
        static_complete = all(
            row["static_text_bytes_full"] is not None
            and row["static_text_bytes_full_plus_candidate"] is not None
            for row in evidence
        )
        if is_promoted and not reasons and not static_complete:
            reasons.append("missing_static_text_evidence")
        eligible = is_promoted and not reasons
        combined = None
        combined_static_candidate = None
        combined_static_ratio = None
        if eligible:
            speedups = [
                float(case["speedup"])
                for row in evidence
                for case in row["per_cases"]
            ]
            combined = math.exp(
                sum(math.log(value) for value in speedups) / len(speedups)
            )
            static_full = sum(
                float(row["static_text_bytes_full"]) for row in evidence
            )
            combined_static_candidate = sum(
                float(row["static_text_bytes_full_plus_candidate"])
                for row in evidence
            )
            combined_static_ratio = static_full / combined_static_candidate
        candidate_rows.append(
            {
                "candidate_id": candidate_id,
                "stages": stage_map,
                "eligible_for_final": eligible,
                "ineligibility_reasons": reasons,
                "combined_case_count": combined_case_count,
                "combined_geometric_mean_speedup": combined,
                "b3_geometric_mean_speedup": by_stage_candidate["B3"][candidate_id][
                    "geometric_mean_speedup"
                ],
                "combined_static_text_bytes_full_plus_candidate": combined_static_candidate,
                "combined_static_text_ratio": combined_static_ratio,
                "rank": None,
            }
        )
    eligible_rows = sorted(
        (row for row in candidate_rows if row["eligible_for_final"]),
        key=lambda row: (
            -row["combined_geometric_mean_speedup"],
            -row["b3_geometric_mean_speedup"],
            row["combined_static_text_bytes_full_plus_candidate"],
            row["candidate_id"],
        ),
    )
    ranking: list[dict[str, Any]] = []
    for rank, row in enumerate(eligible_rows, start=1):
        row["rank"] = rank
        ranking.append(
            {
                "rank": rank,
                "candidate_id": row["candidate_id"],
                "combined_geometric_mean_speedup": row["combined_geometric_mean_speedup"],
                "b3_geometric_mean_speedup": row["b3_geometric_mean_speedup"],
                "combined_static_text_bytes_full_plus_candidate": row[
                    "combined_static_text_bytes_full_plus_candidate"
                ],
                "combined_static_text_ratio": row["combined_static_text_ratio"],
                "stable_id_tiebreak": row["candidate_id"],
            }
        )
    diagnostics = [
        {
            "artifact_id": supplied_diagnostic_artifacts[ref["receipt"]["path"]][0][
                "receipt_id"
            ],
            "artifact": ref["receipt"],
        }
        for ref in diagnostic_refs
    ]
    from .fast_report import (
        build_fast_report_projection,
        fast_report_input_commitments_from_projection,
        load_and_verify_fast_report_manifest,
    )

    report_projection = build_fast_report_projection(
        bootstrap_path=bootstrap_physical,
        plan_path=plan_physical,
        index_path=index_physical,
        status_path=status_physical,
        audit_paths=audit_paths,
        study_paths=study_paths,
        diagnostic_study_path=diagnostic_study_physical,
        workspace_root=root,
    )
    report_manifest_physical, _ = _workspace_existing_path(
        root,
        report_manifest_path,
        label="fast final report manifest",
        regular_only=True,
    )
    report_manifest = load_and_verify_fast_report_manifest(
        workspace_root=root,
        manifest_path=report_manifest_physical,
        expected_input_commitments=fast_report_input_commitments_from_projection(
            report_projection
        ),
        expected_projection=report_projection,
    )
    if (
        report_manifest["campaign_id"] != plan["campaign_id"]
        or report_manifest["evidence_level"] != "qemu_proxy"
        or report_manifest["ranking"] != ranking
    ):
        raise ValidationError(
            "fast final report manifest differs from the exact final ranking"
        )
    report_manifest_artifact = _artifact_ref(
        root, report_manifest_physical, label="fast final report manifest"
    )
    report_artifacts = {
        artifact_id: {
            "path": row["path"],
            "canonical_sha256": row["physical_sha256"],
            "physical_sha256": row["physical_sha256"],
        }
        for artifact_id, row in report_manifest["files"].items()
    }
    document: dict[str, Any] = {
        "schema_version": _FINAL_VERSION,
        "final_id": f"{plan['campaign_id']}:final",
        "campaign_id": plan["campaign_id"],
        "generated_at": generated_at or utc_now(),
        "evidence_level": "qemu_proxy",
        "bootstrap": bootstrap_artifact,
        "plan": plan_artifact,
        "index": index_artifact,
        "status": status_artifact,
        "audits": audit_refs,
        "studies": studies_map,
        "diagnostics": diagnostics,
        "diagnostic_study": diagnostic_study_artifact,
        "planned_candidate_ids": plan["candidate_ids"],
        "promoted_candidate_ids": promoted_candidates,
        "candidates": candidate_rows,
        "ranking": ranking,
        "winner_candidate_id": (
            ranking[0]["candidate_id"]
            if ranking and ranking[0]["combined_geometric_mean_speedup"] > 1.0
            else None
        ),
        "report_manifest": report_manifest_artifact,
        "report_artifacts": report_artifacts,
        "final_commitment_sha256": "0" * 64,
    }
    return _commit(document, "final_commitment_sha256")


__all__ = [
    "FAST_ORACLE_STATIC_ARTIFACT_PATHS",
    "FAST_ORACLE_STATIC_ARTIFACT_VERSIONS",
    "FastRunAuthorizationIntent",
    "authorize_fast_candidate_run_prelease",
    "build_fast_audit",
    "build_fast_bootstrap",
    "build_fast_campaign_plan",
    "build_fast_campaign_status",
    "build_fast_diagnostic_study",
    "build_fast_final",
    "build_fast_run_index",
    "build_fast_study",
    "fast_configuration_template_sha256",
    "publish_fast_current_head",
    "publish_fast_run_receipt",
    "publish_immutable_fast_document",
    "verify_fast_oracle_static_artifacts",
]
