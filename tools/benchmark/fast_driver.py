from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import ConfigurationError, ExecutionError, ValidationError
from .fast_campaign import (
    build_fast_audit,
    build_fast_campaign_status,
    build_fast_diagnostic_study,
    build_fast_final,
    build_fast_run_index,
    build_fast_study,
    publish_fast_current_head_owned,
    publish_immutable_fast_document,
)
from .fast_plan import materialize_fast_launch_spec
from .fast_report import build_fast_report
from .fast_scheduler import FastWaveTaskFailure, run_fast_wave_owned
from .journal import durable_create_json
from .lease import ExclusiveFileLease, candidate_wave_lease_path
from .schema import load_and_validate
from .util import (
    canonical_json_bytes,
    read_json,
    resolve_without_symlinks,
    sha256_file,
    sha256_json,
    utc_now,
    validate_relative_path,
)


_DRIVER_INTENT_VERSION = "candidate-fast-driver-generation-intent.v1"
_RUN_KINDS = frozenset({"run", "diagnostic"})


@dataclass(frozen=True)
class _CampaignSnapshot:
    root: Path
    head_path: Path
    head: dict[str, Any]
    bootstrap_path: Path
    plan_path: Path
    plan: dict[str, Any]
    status_path: Path
    status: dict[str, Any]
    index_path: Path
    index: dict[str, Any]


def _root(path: Path) -> Path:
    root = resolve_without_symlinks(path, label="fast driver workspace")
    if not root.is_dir():
        raise ConfigurationError("fast driver workspace must be a directory")
    return root


def _inside(
    root: Path,
    path: Path,
    *,
    label: str,
    exists: bool,
    directory: bool = False,
) -> tuple[Path, str]:
    candidate = (path if path.is_absolute() else root / path).absolute()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ConfigurationError(f"{label} must remain inside the workspace") from exc
    validate_relative_path(relative.as_posix(), label=label)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.exists() and cursor.is_symlink():
            raise ValidationError(f"{label} must not traverse a symbolic link")
    if exists:
        if directory and not candidate.is_dir():
            raise ValidationError(f"{label} must be a directory")
        if not directory and not candidate.is_file():
            raise ValidationError(f"{label} must be a regular file")
    return candidate, relative.as_posix()


def _artifact_path(
    root: Path, artifact: Mapping[str, str], *, label: str
) -> Path:
    relative = validate_relative_path(artifact["path"], label=f"{label} path")
    physical, _ = _inside(root, Path(relative), label=label, exists=True)
    document = read_json(physical)
    if not isinstance(document, (dict, list)):
        raise ValidationError(f"{label} must contain JSON")
    if (
        sha256_json(document) != artifact["canonical_sha256"]
        or sha256_file(physical) != artifact["physical_sha256"]
    ):
        raise ValidationError(f"{label} canonical or physical hash differs")
    return physical


def _load_version(path: Path, version: str, *, label: str) -> dict[str, Any]:
    document = load_and_validate(path)
    if document.get("schema_version") != version:
        raise ValidationError(f"{label} has an unexpected schema version")
    return document


def _load_snapshot(workspace_root: Path, head_path: Path) -> _CampaignSnapshot:
    root = _root(workspace_root)
    head_physical, _ = _inside(
        root, head_path, label="fast driver current head", exists=True
    )
    head = _load_version(
        head_physical,
        "candidate-fast-current-head.v1",
        label="fast driver current head",
    )
    status_path = _artifact_path(root, head["status"], label="fast driver status")
    index_path = _artifact_path(root, head["index"], label="fast driver index")
    status = _load_version(
        status_path, "candidate-fast-status.v1", label="fast driver status"
    )
    index = _load_version(
        index_path, "candidate-fast-run-index.v1", label="fast driver index"
    )
    plan_path = _artifact_path(root, status["plan"], label="fast driver plan")
    plan = _load_version(
        plan_path, "candidate-fast-campaign-plan.v1", label="fast driver plan"
    )
    bootstrap_path = _artifact_path(
        root, plan["bootstrap"], label="fast driver bootstrap"
    )
    bootstrap = _load_version(
        bootstrap_path,
        "candidate-fast-bootstrap.v1",
        label="fast driver bootstrap",
    )
    plan_sha256 = sha256_json(plan)
    bootstrap_sha256 = sha256_json(bootstrap)
    if (
        head["campaign_id"] != plan["campaign_id"]
        or head["generation"] != status["generation"]
        or head["status_id"] != status["status_id"]
        or head["index_id"] != index["index_id"]
        or head["plan_sha256"] != plan_sha256
        or head["bootstrap_sha256"] != bootstrap_sha256
        or status["campaign_id"] != plan["campaign_id"]
        or status["index"] != head["index"]
        or status["bootstrap"] != plan["bootstrap"]
        or index["campaign_id"] != plan["campaign_id"]
        or index["plan_sha256"] != plan_sha256
        or index["bootstrap_sha256"] != bootstrap_sha256
        or bootstrap["campaign_id"] != plan["campaign_id"]
        or plan["max_parallel_runs"] != 4
        or plan["jobs_per_run"] != 4
    ):
        raise ValidationError("fast driver current-head closure is inconsistent")
    return _CampaignSnapshot(
        root=root,
        head_path=head_physical,
        head=head,
        bootstrap_path=bootstrap_path,
        plan_path=plan_path,
        plan=plan,
        status_path=status_path,
        status=status,
        index_path=index_path,
        index=index,
    )


def _publish_plain_json(path: Path, document: Mapping[str, Any], *, label: str) -> None:
    payload = canonical_json_bytes(document) + b"\n"
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise ValidationError(f"{label} already exists with different bytes")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    resolve_without_symlinks(path.parent, label=f"{label} parent")
    durable_create_json(path, document)


def _generation_intent(
    *,
    snapshot: _CampaignSnapshot,
    launch_template_path: Path,
    generation_root: Path,
    report_directory: Path,
) -> tuple[Path, Path, dict[str, Any]]:
    next_generation = snapshot.head["generation"] + 1
    generations, generations_relative = _inside(
        snapshot.root,
        generation_root,
        label="fast driver generation root",
        exists=False,
        directory=True,
    )
    report, report_relative = _inside(
        snapshot.root,
        report_directory,
        label="fast driver report directory",
        exists=False,
        directory=True,
    )
    template, template_relative = _inside(
        snapshot.root,
        launch_template_path,
        label="fast driver launch templates",
        exists=True,
    )
    generation_directory = generations / f"{next_generation:06d}"
    generation_relative = f"{generations_relative}/{next_generation:06d}"
    parent_head_path = generation_directory / "parent-head.json"
    publish_immutable_fast_document(parent_head_path, snapshot.head)
    template_document = read_json(template)
    if not isinstance(template_document, dict):
        raise ValidationError("fast driver launch templates must be a JSON object")
    intent_path = generation_directory / "intent.json"
    expected_without_time = {
        "schema_version": _DRIVER_INTENT_VERSION,
        "campaign_id": snapshot.head["campaign_id"],
        "parent_generation": snapshot.head["generation"],
        "generation": next_generation,
        "parent_head_canonical_sha256": sha256_json(snapshot.head),
        "parent_head_physical_sha256": sha256_file(snapshot.head_path),
        "parent_status_sha256": sha256_json(snapshot.status),
        "parent_index_sha256": sha256_json(snapshot.index),
        "ready_tasks": list(snapshot.status["ready_tasks"]),
        "launch_templates": {
            "path": template_relative,
            "canonical_sha256": sha256_json(template_document),
            "physical_sha256": sha256_file(template),
        },
        "generation_directory": generation_relative,
        "report_directory": report_relative,
    }
    if intent_path.exists():
        intent = read_json(intent_path)
        if not isinstance(intent, dict):
            raise ValidationError("fast driver generation intent must be a JSON object")
        expected = {
            **expected_without_time,
            "created_at": intent.get("created_at"),
            "intent_commitment_sha256": intent.get("intent_commitment_sha256"),
        }
        if (
            intent != expected
            or not isinstance(intent.get("created_at"), str)
            or intent.get("intent_commitment_sha256")
            != sha256_json(
                {
                    key: value
                    for key, value in intent.items()
                    if key != "intent_commitment_sha256"
                }
            )
        ):
            raise ValidationError("fast driver generation intent differs")
    else:
        intent = {
            **expected_without_time,
            "created_at": utc_now(),
            "intent_commitment_sha256": "0" * 64,
        }
        intent["intent_commitment_sha256"] = sha256_json(
            {
                key: value
                for key, value in intent.items()
                if key != "intent_commitment_sha256"
            }
        )
        _publish_plain_json(intent_path, intent, label="fast driver generation intent")
    return generation_directory, parent_head_path, intent


def _paths_from_named(
    root: Path, rows: Sequence[Mapping[str, Any]], *, label: str
) -> list[Path]:
    return [
        _artifact_path(root, row["artifact"], label=f"{label} {row['artifact_id']}")
        for row in rows
    ]


def _receipt_path(
    snapshot: _CampaignSnapshot,
    index: Mapping[str, Any],
    task_id: str,
) -> Path:
    matches = [row for row in index["receipts"] if row["task_id"] == task_id]
    if len(matches) != 1:
        raise ValidationError(f"fast driver requires one indexed receipt for {task_id}")
    return _artifact_path(
        snapshot.root, matches[0]["receipt"], label=f"fast driver receipt {task_id}"
    )


def _artifact_document(
    root: Path, artifact: Mapping[str, str], *, version: str, label: str
) -> tuple[Path, dict[str, Any]]:
    path = _artifact_path(root, artifact, label=label)
    return path, _load_version(path, version, label=label)


def _study_paths_by_stage(snapshot: _CampaignSnapshot) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for row in snapshot.status["studies"]:
        path, study = _artifact_document(
            snapshot.root,
            row["artifact"],
            version="candidate-fast-study.v1",
            label=f"fast driver study {row['artifact_id']}",
        )
        stage = study["stage"]
        if stage in result:
            raise ValidationError("fast driver status contains duplicate study stages")
        result[stage] = path
    return result


def _audit_paths_by_checkpoint(snapshot: _CampaignSnapshot) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for row in snapshot.status["audits"]:
        path, audit = _artifact_document(
            snapshot.root,
            row["artifact"],
            version="candidate-fast-audit.v1",
            label=f"fast driver audit {row['artifact_id']}",
        )
        checkpoint = audit["checkpoint"]
        if checkpoint in result:
            raise ValidationError("fast driver status contains duplicate audit checkpoints")
        result[checkpoint] = path
    return result


def _top3(study: Mapping[str, Any]) -> list[str]:
    return [
        row["candidate_id"]
        for row in sorted(
            (
                row
                for row in study["candidates"]
                if row["eligible"] and row["geometric_mean_speedup"] is not None
            ),
            key=lambda row: (-float(row["geometric_mean_speedup"]), row["candidate_id"]),
        )[:3]
    ]


def _materialize_study(
    *,
    snapshot: _CampaignSnapshot,
    index_path: Path,
    index: Mapping[str, Any],
    task: Mapping[str, Any],
    study_paths: Mapping[str, Path],
    output_path: Path,
    generated_at: str,
) -> dict[str, Any]:
    stage = task["stage"]
    task_by_id = {row["task_id"]: row for row in snapshot.plan["tasks"]}
    if stage == "B2":
        baseline_task = task_by_id["run.B2.full"] if "run.B2.full" in task_by_id else None
        if baseline_task is not None:
            raise ValidationError("fast driver B2 FULL must remain a bootstrap import")
        bootstrap = _load_version(
            snapshot.bootstrap_path,
            "candidate-fast-bootstrap.v1",
            label="fast driver bootstrap",
        )
        imported = next(
            (row for row in bootstrap["imported_receipts"] if row["task_id"] == "run.B2.full"),
            None,
        )
        if imported is None:
            raise ValidationError("fast driver bootstrap lacks imported B2 FULL")
        baseline_path = _artifact_path(
            snapshot.root, imported["run_artifact"], label="fast driver B2 FULL"
        )
    else:
        baseline_path = _receipt_path(snapshot, index, f"run.{stage}.full")
    candidate_ids = [
        row["candidate_ids"][0]
        for row in snapshot.plan["tasks"]
        if row["kind"] == "run"
        and row["stage"] == stage
        and row.get("run_kind") == "single"
    ]
    if stage in {"B4", "B5", "B6"}:
        promotion_path = study_paths.get("B3")
        if promotion_path is None:
            raise ValidationError("fast driver validation study lacks B3 promotion evidence")
        promotion = _load_version(
            promotion_path,
            "candidate-fast-study.v1",
            label="fast driver B3 promotion study",
        )
        promoted = {
            row["candidate_id"]
            for row in promotion["candidates"]
            if row["eligible"]
            and row["geometric_mean_speedup"] is not None
            and row["geometric_mean_speedup"] > 1.0
        }
        candidate_ids = [item for item in candidate_ids if item in promoted]
    else:
        promotion_path = None
    document = build_fast_study(
        stage=stage,
        bootstrap_path=snapshot.bootstrap_path,
        plan_path=snapshot.plan_path,
        index_path=index_path,
        baseline_receipt_path=baseline_path,
        candidate_receipt_paths={
            candidate_id: _receipt_path(
                snapshot, index, f"run.{stage}.{candidate_id}"
            )
            for candidate_id in candidate_ids
        },
        workspace_root=snapshot.root,
        promotion_study_path=promotion_path,
        generated_at=generated_at,
    )
    return publish_immutable_fast_document(output_path, document)


def _materialize_diagnostic_study(
    *,
    snapshot: _CampaignSnapshot,
    index_path: Path,
    index: Mapping[str, Any],
    study_paths: Mapping[str, Path],
    output_path: Path,
    generated_at: str,
) -> dict[str, Any]:
    b3_path = study_paths.get("B3")
    if b3_path is None:
        raise ValidationError("fast driver diagnostic study lacks B3 study")
    b3 = _load_version(
        b3_path, "candidate-fast-study.v1", label="fast driver B3 study"
    )
    top3 = _top3(b3)
    pair_ids = [
        task["task_id"]
        for task in snapshot.plan["tasks"]
        if task["kind"] == "diagnostic"
        and task["measurement_mode"] == "standard_proxy"
        and len(task["candidate_ids"]) == 2
        and set(task["candidate_ids"]) <= set(top3)
    ]
    document = build_fast_diagnostic_study(
        bootstrap_path=snapshot.bootstrap_path,
        plan_path=snapshot.plan_path,
        index_path=index_path,
        b3_study_path=b3_path,
        pair_receipt_paths={
            task_id: _receipt_path(snapshot, index, task_id) for task_id in pair_ids
        },
        cache_full_receipt_path=_receipt_path(
            snapshot, index, "diagnostic.cache.full"
        ),
        cache_candidate_receipt_paths={
            candidate_id: _receipt_path(
                snapshot, index, f"diagnostic.cache.{candidate_id}"
            )
            for candidate_id in top3
        },
        workspace_root=snapshot.root,
        generated_at=generated_at,
    )
    return publish_immutable_fast_document(output_path, document)


def _diagnostic_receipt_paths(
    snapshot: _CampaignSnapshot, index: Mapping[str, Any]
) -> list[Path]:
    task_by_id = {task["task_id"]: task for task in snapshot.plan["tasks"]}
    return [
        _artifact_path(
            snapshot.root,
            row["receipt"],
            label=f"fast driver diagnostic {row['task_id']}",
        )
        for row in index["receipts"]
        if task_by_id[row["task_id"]]["kind"] == "diagnostic"
    ]


def _materialize_final(
    *,
    snapshot: _CampaignSnapshot,
    index_path: Path,
    index: Mapping[str, Any],
    study_paths: Mapping[str, Path],
    audit_paths: Mapping[str, Path],
    diagnostic_study_path: Path,
    report_directory: Path,
    output_path: Path,
    generated_at: str,
) -> dict[str, Any]:
    diagnostics = _diagnostic_receipt_paths(snapshot, index)
    build_fast_report(
        bootstrap_path=snapshot.bootstrap_path,
        plan_path=snapshot.plan_path,
        index_path=index_path,
        status_path=snapshot.status_path,
        audit_paths=audit_paths,
        study_paths=study_paths,
        diagnostic_study_path=diagnostic_study_path,
        output_directory=report_directory,
        workspace_root=snapshot.root,
    )
    report_manifest = report_directory / "manifest.json"
    document = build_fast_final(
        bootstrap_path=snapshot.bootstrap_path,
        plan_path=snapshot.plan_path,
        index_path=index_path,
        status_path=snapshot.status_path,
        audit_paths=audit_paths,
        study_paths=study_paths,
        diagnostic_paths=diagnostics,
        diagnostic_study_path=diagnostic_study_path,
        report_manifest_path=report_manifest,
        workspace_root=snapshot.root,
        generated_at=generated_at,
    )
    return publish_immutable_fast_document(output_path, document)


def _materialize_ready_pseudo_tasks(
    *,
    snapshot: _CampaignSnapshot,
    generated_at: str,
    report_directory: Path,
) -> tuple[list[Path], list[Path], Path | None, Path | None]:
    task_by_id = {task["task_id"]: task for task in snapshot.plan["tasks"]}
    studies = _study_paths_by_stage(snapshot)
    audits = _audit_paths_by_checkpoint(snapshot)
    diagnostic_study = (
        None
        if snapshot.status["diagnostic_study"] is None
        else _artifact_path(
            snapshot.root,
            snapshot.status["diagnostic_study"],
            label="fast driver diagnostic study",
        )
    )
    final_path = (
        None
        if snapshot.status["final"] is None
        else _artifact_path(
            snapshot.root, snapshot.status["final"], label="fast driver final"
        )
    )
    new_studies: list[Path] = []
    new_audits: list[Path] = []
    for task_id in snapshot.status["ready_tasks"]:
        task = task_by_id[task_id]
        if task["kind"] in _RUN_KINDS:
            continue
        output, _ = _inside(
            snapshot.root,
            Path(task["output_path"]),
            label=f"fast driver pseudo output {task_id}",
            exists=False,
        )
        if task["kind"] == "audit":
            document = build_fast_audit(
                checkpoint=task["stage"],
                bootstrap_path=snapshot.bootstrap_path,
                plan_path=snapshot.plan_path,
                index_path=snapshot.index_path,
                status_path=snapshot.status_path,
                workspace_root=snapshot.root,
                generated_at=generated_at,
            )
            publish_immutable_fast_document(output, document)
            audits[task["stage"]] = output
            new_audits.append(output)
        elif task["kind"] == "study" and task["stage"] != "diagnostic":
            _materialize_study(
                snapshot=snapshot,
                index_path=snapshot.index_path,
                index=snapshot.index,
                task=task,
                study_paths=studies,
                output_path=output,
                generated_at=generated_at,
            )
            studies[task["stage"]] = output
            new_studies.append(output)
        elif task["kind"] == "study" and task["stage"] == "diagnostic":
            _materialize_diagnostic_study(
                snapshot=snapshot,
                index_path=snapshot.index_path,
                index=snapshot.index,
                study_paths=studies,
                output_path=output,
                generated_at=generated_at,
            )
            diagnostic_study = output
        elif task["kind"] == "final":
            if diagnostic_study is None:
                raise ValidationError("fast driver final lacks diagnostic study")
            _materialize_final(
                snapshot=snapshot,
                index_path=snapshot.index_path,
                index=snapshot.index,
                study_paths=studies,
                audit_paths=audits,
                diagnostic_study_path=diagnostic_study,
                report_directory=report_directory,
                output_path=output,
                generated_at=generated_at,
            )
            final_path = output
        else:
            raise ValidationError(f"fast driver cannot materialize task kind {task['kind']}")
    return new_studies, new_audits, diagnostic_study, final_path


def _next_log_directory(generation_directory: Path) -> Path:
    logs = generation_directory / "logs"
    for attempt in range(10_000):
        candidate = logs / f"attempt-{attempt:04d}"
        if not candidate.exists():
            return candidate
    raise ExecutionError("fast driver exhausted bounded wave recovery log attempts")


def _all_ready_run_receipts(
    snapshot: _CampaignSnapshot,
) -> tuple[list[Path], bool]:
    task_by_id = {task["task_id"]: task for task in snapshot.plan["tasks"]}
    paths: list[Path] = []
    for task_id in snapshot.status["ready_tasks"]:
        task = task_by_id[task_id]
        if task["kind"] not in _RUN_KINDS:
            continue
        relative = task.get("receipt_path")
        if not isinstance(relative, str):
            raise ValidationError("fast driver ready run lacks a receipt path")
        path, _ = _inside(
            snapshot.root,
            Path(relative),
            label=f"fast driver receipt output {task_id}",
            exists=False,
        )
        paths.append(path)
    return paths, bool(paths) and all(path.is_file() and not path.is_symlink() for path in paths)


def _advance_generation_owned(
    *,
    snapshot: _CampaignSnapshot,
    launch_template_path: Path,
    generation_root: Path,
    report_directory: Path,
) -> dict[str, Any]:
    generation_directory, parent_head_path, intent = _generation_intent(
        snapshot=snapshot,
        launch_template_path=launch_template_path,
        generation_root=generation_root,
        report_directory=report_directory,
    )
    generated_at = intent["created_at"]
    new_studies, new_audits, diagnostic_study, final_path = (
        _materialize_ready_pseudo_tasks(
            snapshot=snapshot,
            generated_at=generated_at,
            report_directory=report_directory,
        )
    )
    ready_receipts, receipts_complete = _all_ready_run_receipts(snapshot)
    wave_error: FastWaveTaskFailure | None = None
    runnable_count = len(ready_receipts)
    if runnable_count and not receipts_complete:
        launch_path = generation_directory / "launch.json"
        materialize_fast_launch_spec(
            workspace_root=snapshot.root,
            template_path=launch_template_path,
            head_path=snapshot.head_path,
            output_path=launch_path,
        )
        try:
            run_fast_wave_owned(
                workspace_root=snapshot.root,
                head_path=snapshot.head_path,
                launch_spec_path=launch_path,
                log_directory=_next_log_directory(generation_directory),
            )
        except FastWaveTaskFailure as exc:
            wave_error = exc
        ready_receipts, receipts_complete = _all_ready_run_receipts(snapshot)
        if not receipts_complete:
            if wave_error is not None:
                raise wave_error
            raise ExecutionError("successful fast wave omitted a planned terminal receipt")

    index_path = snapshot.index_path
    index = snapshot.index
    if receipts_complete:
        previous_paths = [
            _artifact_path(
                snapshot.root,
                row["receipt"],
                label=f"fast driver indexed receipt {row['task_id']}",
            )
            for row in snapshot.index["receipts"]
        ]
        index_document = build_fast_run_index(
            plan_path=snapshot.plan_path,
            receipt_paths=[*previous_paths, *ready_receipts],
            workspace_root=snapshot.root,
            previous_index_path=snapshot.index_path,
            generated_at=generated_at,
        )
        index_path = generation_directory / "index.json"
        index = publish_immutable_fast_document(index_path, index_document)

    progressed = bool(
        receipts_complete
        or new_studies
        or new_audits
        or diagnostic_study != (
            None
            if snapshot.status["diagnostic_study"] is None
            else _artifact_path(
                snapshot.root,
                snapshot.status["diagnostic_study"],
                label="fast driver prior diagnostic study",
            )
        )
        or final_path != (
            None
            if snapshot.status["final"] is None
            else _artifact_path(
                snapshot.root,
                snapshot.status["final"],
                label="fast driver prior final",
            )
        )
    )
    if not progressed:
        if wave_error is not None:
            raise wave_error
        raise ValidationError("fast driver generation made no evidence progress")

    study_paths = [
        *_paths_from_named(snapshot.root, snapshot.status["studies"], label="fast driver prior study"),
        *new_studies,
    ]
    audit_paths = [
        *_paths_from_named(snapshot.root, snapshot.status["audits"], label="fast driver prior audit"),
        *new_audits,
    ]
    diagnostic_paths = _diagnostic_receipt_paths(snapshot, index)
    status_document = build_fast_campaign_status(
        plan_path=snapshot.plan_path,
        index_path=index_path,
        workspace_root=snapshot.root,
        generation=intent["generation"],
        study_paths=study_paths,
        audit_paths=audit_paths,
        diagnostic_paths=diagnostic_paths,
        diagnostic_study_path=diagnostic_study,
        final_path=final_path,
        generated_at=generated_at,
    )
    status_path = generation_directory / "status.json"
    status = publish_immutable_fast_document(status_path, status_document)
    head = publish_fast_current_head_owned(
        bootstrap_path=snapshot.bootstrap_path,
        plan_path=snapshot.plan_path,
        status_path=status_path,
        index_path=index_path,
        workspace_root=snapshot.root,
        head_path=snapshot.head_path,
        previous_head_path=parent_head_path,
        expected_previous_head_sha256=sha256_json(snapshot.head),
        updated_at=generated_at,
    )
    return {
        "campaign_id": head["campaign_id"],
        "generation": head["generation"],
        "state": status["state"],
        "ready_tasks": status["ready_tasks"],
        "indexed_receipts": len(index["receipts"]),
    }


def drive_fast_campaign(
    *,
    workspace_root: Path,
    head_path: Path,
    launch_template_path: Path,
    generation_root: Path,
    report_directory: Path,
) -> dict[str, Any]:
    """Drive a fast campaign to one terminal head under a single OS lease."""

    initial = _load_snapshot(workspace_root, head_path)
    observed_canonical = sha256_json(initial.head)
    observed_physical = sha256_file(initial.head_path)
    with ExclusiveFileLease(
        candidate_wave_lease_path(initial.root, initial.head["campaign_id"]),
        "fast campaign wave",
        {
            "campaign_id": initial.head["campaign_id"],
            "generation": initial.head["generation"],
            "head_canonical_sha256": observed_canonical,
            "head_physical_sha256": observed_physical,
            "owner": "fast-driver",
        },
    ) as lease:
        claimed = _load_snapshot(initial.root, initial.head_path)
        if (
            sha256_json(claimed.head) != observed_canonical
            or sha256_file(claimed.head_path) != observed_physical
        ):
            raise ValidationError("fast current head changed before driver ownership")
        while True:
            snapshot = _load_snapshot(initial.root, initial.head_path)
            lease.bind(
                {
                    "campaign_id": snapshot.head["campaign_id"],
                    "generation": snapshot.head["generation"],
                    "head_canonical_sha256": sha256_json(snapshot.head),
                    "head_physical_sha256": sha256_file(snapshot.head_path),
                    "owner": "fast-driver",
                }
            )
            if snapshot.status["state"] == "complete":
                return {
                    "campaign_id": snapshot.head["campaign_id"],
                    "generation": snapshot.head["generation"],
                    "state": "complete",
                    "indexed_receipts": len(snapshot.index["receipts"]),
                    "final": snapshot.status["final"],
                }
            if snapshot.status["state"] == "failed":
                raise ExecutionError("fast campaign reached a failed terminal head")
            if snapshot.status["state"] != "running" or not snapshot.status["ready_tasks"]:
                raise ValidationError("fast campaign is running without a ready task")
            _advance_generation_owned(
                snapshot=snapshot,
                launch_template_path=launch_template_path,
                generation_root=generation_root,
                report_directory=report_directory,
            )


__all__ = ["drive_fast_campaign"]
