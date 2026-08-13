from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import ConfigurationError, ExecutionError, ValidationError
from .lease import ExclusiveFileLease, candidate_wave_lease_path
from .schema import load_and_validate
from .util import (
    read_json,
    resolve_without_symlinks,
    sha256_file,
    sha256_json,
    validate_relative_path,
)


_HEAD_VERSION = "candidate-fast-current-head.v1"
_STATUS_VERSION = "candidate-fast-status.v1"
_INDEX_VERSION = "candidate-fast-run-index.v1"
_PLAN_VERSION = "candidate-fast-campaign-plan.v1"
_RUN_TASK_KINDS = frozenset({"run", "diagnostic"})
_REQUIRED_RUN_OPTIONS = (
    "--candidate-fast-plan",
    "--candidate-fast-status",
    "--candidate-fast-index",
    "--candidate-fast-task-id",
    "--candidate-fast-receipt",
    "--jobs",
)
_START_FAILURE_TERMINATE_TIMEOUT_SECONDS = 5.0
_START_FAILURE_KILL_TIMEOUT_SECONDS = 5.0


class FastWaveTaskFailure(ExecutionError):
    """Every child started, but at least one task exited nonzero."""


@dataclass(frozen=True)
class _PreparedLaunch:
    task_id: str
    argv: tuple[str, ...]
    stdout_path: Path
    stderr_path: Path


def _workspace_root(path: Path) -> Path:
    root = resolve_without_symlinks(path, label="fast scheduler workspace")
    if not root.is_dir():
        raise ConfigurationError("fast scheduler workspace must be a directory")
    return root


def _workspace_existing_file(root: Path, path: Path, *, label: str) -> Path:
    candidate = path if path.is_absolute() else root / path
    resolved = resolve_without_symlinks(candidate, label=label)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"{label} must remain inside the fast campaign workspace") from exc
    if not resolved.is_file():
        raise ValidationError(f"{label} must be a regular file")
    return resolved


def _workspace_log_directory(root: Path, path: Path) -> Path:
    candidate = (path if path.is_absolute() else root / path).absolute()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ConfigurationError("fast scheduler log directory must remain inside the workspace") from exc
    validate_relative_path(relative.as_posix(), label="fast scheduler log directory")
    cursor = root
    for component in relative.parts:
        cursor /= component
        if cursor.is_symlink():
            raise ConfigurationError(
                "fast scheduler log directory must not traverse a symbolic link"
            )
        try:
            cursor.mkdir()
        except FileExistsError:
            pass
        if cursor.is_symlink() or not cursor.is_dir():
            raise ConfigurationError(
                "fast scheduler log directory parent must be a directory"
            )
    resolved = resolve_without_symlinks(candidate, label="fast scheduler log directory")
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ConfigurationError("fast scheduler log directory resolves outside the workspace") from exc
    if not resolved.is_dir():
        raise ConfigurationError("fast scheduler log directory must be a directory")
    return resolved


def _ensure_planned_output_parent(root: Path, path: str, *, label: str) -> None:
    relative = validate_relative_path(path, label=label)
    cursor = root
    for component in relative.parts[:-1]:
        cursor /= component
        if cursor.is_symlink():
            raise ValidationError(f"{label} parent must not traverse a symbolic link")
        try:
            cursor.mkdir()
        except FileExistsError:
            pass
        if cursor.is_symlink() or not cursor.is_dir():
            raise ValidationError(f"{label} parent must be a directory")
    target = root.joinpath(*relative.parts)
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValidationError(f"{label} target must be absent or a regular file")


def _artifact_path(
    root: Path, artifact: Mapping[str, str], *, label: str
) -> Path:
    relative = validate_relative_path(artifact["path"], label=f"{label} path")
    path = _workspace_existing_file(root, root.joinpath(*relative.parts), label=label)
    document = read_json(path)
    if not isinstance(document, (dict, list)):
        raise ValidationError(f"{label} must contain a JSON object or array")
    if (
        sha256_json(document) != artifact["canonical_sha256"]
        or sha256_file(path) != artifact["physical_sha256"]
    ):
        raise ValidationError(f"{label} canonical or physical hash differs")
    return path


def _load_version(path: Path, version: str, *, label: str) -> dict[str, Any]:
    document = load_and_validate(path)
    if document.get("schema_version") != version:
        raise ValidationError(f"{label} has an unexpected schema version")
    return document


def _extract_options(argv: Sequence[str]) -> dict[str, str]:
    if len(argv) < 5 or tuple(argv[1:5]) != (
        "-I",
        "-m",
        "tools.benchmark",
        "run",
    ):
        raise ConfigurationError(
            "fast launch must invoke the isolated tools.benchmark run entry point"
        )
    values: dict[str, str] = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        matched: str | None = None
        value: str | None = None
        for option in _REQUIRED_RUN_OPTIONS:
            if token == option:
                if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
                    raise ConfigurationError(f"fast launch {option} requires one value")
                matched = option
                value = argv[index + 1]
                index += 1
                break
            prefix = f"{option}="
            if token.startswith(prefix):
                matched = option
                value = token[len(prefix) :]
                if not value:
                    raise ConfigurationError(f"fast launch {option} requires one value")
                break
        if matched is not None:
            if matched in values:
                raise ConfigurationError(f"fast launch repeats {matched}")
            assert value is not None
            values[matched] = value
        index += 1
    missing = [option for option in _REQUIRED_RUN_OPTIONS if option not in values]
    if missing:
        raise ConfigurationError(
            "fast launch is missing required prelease options: " + ", ".join(missing)
        )
    return values


def _validate_python_launcher(argv: Sequence[str]) -> None:
    launcher = Path(argv[0])
    if not launcher.is_absolute():
        raise ConfigurationError("fast launch Python executable must be an absolute path")
    try:
        launcher_physical = launcher.resolve(strict=True)
        scheduler_physical = Path(sys.executable).resolve(strict=True)
    except OSError as exc:
        raise ConfigurationError("fast launch Python executable is missing or unreadable") from exc
    if launcher_physical != scheduler_physical:
        raise ConfigurationError(
            "fast launch Python executable must be the scheduler interpreter"
        )


def _terminate_started_processes(
    processes: Sequence[tuple[_PreparedLaunch, subprocess.Popen[bytes], Any, Any]],
) -> None:
    for _, process, _, _ in processes:
        try:
            process.terminate()
        except OSError:
            pass
    for _, process, stdout, stderr in processes:
        try:
            process.wait(timeout=_START_FAILURE_TERMINATE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=_START_FAILURE_KILL_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            pass
        finally:
            stdout.close()
            stderr.close()


def _argument_path(root: Path, value: str, *, label: str) -> Path:
    path = Path(value)
    candidate = (path if path.is_absolute() else root / path).absolute()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ConfigurationError(f"{label} must remain inside the workspace") from exc
    return candidate


def _load_launch_specs(path: Path) -> list[dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("cannot read fast launch-spec JSON") from exc
    if not isinstance(document, list):
        raise ConfigurationError("fast launch-spec must be a JSON array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ordinal, row in enumerate(document):
        if not isinstance(row, dict) or set(row) != {"task_id", "argv"}:
            raise ConfigurationError(
                f"fast launch-spec row {ordinal} must contain exactly task_id and argv"
            )
        task_id = row["task_id"]
        argv = row["argv"]
        if not isinstance(task_id, str) or not task_id or task_id in seen:
            raise ConfigurationError(f"fast launch-spec row {ordinal} has an invalid task_id")
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(token, str) or not token for token in argv)
        ):
            raise ConfigurationError(f"fast launch-spec row {ordinal} has an invalid argv")
        seen.add(task_id)
        result.append({"task_id": task_id, "argv": argv})
    return result


def run_fast_wave_owned(
    *,
    workspace_root: Path,
    head_path: Path,
    launch_spec_path: Path,
    log_directory: Path,
) -> dict[str, Any]:
    """Launch one immutable ready wave while the caller owns its campaign lease."""

    root = _workspace_root(workspace_root)
    head_physical = _workspace_existing_file(root, head_path, label="fast current head")
    launch_physical = _workspace_existing_file(
        root, launch_spec_path, label="fast launch specification"
    )
    head = _load_version(head_physical, _HEAD_VERSION, label="fast current head")
    status_path = _artifact_path(root, head["status"], label="fast head status")
    index_path = _artifact_path(root, head["index"], label="fast head index")
    status = _load_version(status_path, _STATUS_VERSION, label="fast head status")
    index = _load_version(index_path, _INDEX_VERSION, label="fast head index")
    plan_path = _artifact_path(root, status["plan"], label="fast status plan")
    plan = _load_version(plan_path, _PLAN_VERSION, label="fast status plan")
    bootstrap_path = _artifact_path(root, plan["bootstrap"], label="fast plan bootstrap")
    bootstrap_sha256 = sha256_json(read_json(bootstrap_path))

    if sha256_json(plan) != head["plan_sha256"]:
        raise ValidationError("fast head plan commitment differs from the status plan")
    if status["campaign_id"] != head["campaign_id"] or index["campaign_id"] != head["campaign_id"]:
        raise ValidationError("fast head campaign binding differs")
    if status["generation"] != head["generation"] or status["status_id"] != head["status_id"]:
        raise ValidationError("fast head status generation or identity differs")
    if index["index_id"] != head["index_id"] or status["index"] != head["index"]:
        raise ValidationError("fast head index binding differs")
    if plan["campaign_id"] != head["campaign_id"] or index["plan_sha256"] != head["plan_sha256"]:
        raise ValidationError("fast plan/index campaign binding differs")
    if (
        bootstrap_sha256 != head["bootstrap_sha256"]
        or index["bootstrap_sha256"] != bootstrap_sha256
        or status["bootstrap"] != plan["bootstrap"]
    ):
        raise ValidationError("fast bootstrap binding differs across the current head")
    if plan["max_parallel_runs"] != 4 or plan["jobs_per_run"] != 4:
        raise ValidationError("fast scheduler requires the fixed four-by-four concurrency contract")

    ready = status["ready_tasks"]
    if len(ready) > plan["max_parallel_runs"]:
        raise ValidationError("fast current head exceeds its parallel-run bound")
    task_by_id = {task["task_id"]: task for task in plan["tasks"]}
    task_state_by_id = {row["task_id"]: row for row in status["tasks"]}
    if (
        len(task_by_id) != len(plan["tasks"])
        or len(task_state_by_id) != len(status["tasks"])
        or set(task_state_by_id) != set(task_by_id)
    ):
        raise ValidationError("fast status task projection differs from the plan")
    if any(task_id not in task_by_id for task_id in ready):
        raise ValidationError("fast current head names an unknown ready task")
    if any(task_state_by_id[task_id]["state"] != "ready" for task_id in ready):
        raise ValidationError("fast ready task is not marked ready in the status projection")
    runnable = [task_id for task_id in ready if task_by_id[task_id]["kind"] in _RUN_TASK_KINDS]
    materialization = [task_id for task_id in ready if task_by_id[task_id]["kind"] not in _RUN_TASK_KINDS]

    specifications = _load_launch_specs(launch_physical)
    if {row["task_id"] for row in specifications} != set(runnable):
        raise ConfigurationError(
            "fast launch-spec task IDs must exactly equal the runnable ready-task set"
        )
    specification_by_id = {row["task_id"]: row for row in specifications}
    logs = _workspace_log_directory(root, log_directory)
    prepared: list[_PreparedLaunch] = []
    for task_id in runnable:
        task = task_by_id[task_id]
        output = task.get("output_path")
        receipt = task["receipt_path"]
        if not isinstance(output, str) or not isinstance(receipt, str):
            raise ValidationError(
                "runnable fast task must declare output and receipt paths"
            )
        argv = tuple(specification_by_id[task_id]["argv"])
        _validate_python_launcher(argv)
        options = _extract_options(argv)
        expected_paths = {
            "--candidate-fast-plan": plan_path,
            "--candidate-fast-status": status_path,
            "--candidate-fast-index": index_path,
            "--candidate-fast-receipt": (root / validate_relative_path(receipt, label="fast task receipt path")).absolute(),
        }
        for option, expected in expected_paths.items():
            observed = _argument_path(root, options[option], label=f"fast launch {option}")
            if observed != expected.absolute():
                raise ConfigurationError(f"fast launch {option} differs from the current head or plan")
        if options["--candidate-fast-task-id"] != task_id:
            raise ConfigurationError("fast launch task ID differs from its plan task")
        if options["--jobs"] != "4":
            raise ConfigurationError("fast launch --jobs must be exactly 4")
        stem = f"{task['ordinal']:04d}-{sha256_json(task_id)[:16]}"
        stdout_path = logs / f"{stem}.stdout.log"
        stderr_path = logs / f"{stem}.stderr.log"
        if stdout_path.exists() or stderr_path.exists():
            raise ConfigurationError("fast scheduler refuses to overwrite an existing task log")
        prepared.append(_PreparedLaunch(task_id, argv, stdout_path, stderr_path))

    for task_id in runnable:
        task = task_by_id[task_id]
        _ensure_planned_output_parent(
            root, task["output_path"], label=f"fast task output {task_id}"
        )
        _ensure_planned_output_parent(
            root, task["receipt_path"], label=f"fast task receipt {task_id}"
        )

    processes: list[tuple[_PreparedLaunch, subprocess.Popen[bytes], Any, Any]] = []
    try:
        for launch in prepared:
            stdout = launch.stdout_path.open("xb")
            try:
                stderr = launch.stderr_path.open("xb")
            except BaseException:
                stdout.close()
                raise
            try:
                process = subprocess.Popen(
                    launch.argv,
                    cwd=root,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    shell=False,
                )
            except BaseException:
                stdout.close()
                stderr.close()
                raise
            processes.append((launch, process, stdout, stderr))
    except BaseException as exc:
        _terminate_started_processes(processes)
        if isinstance(exc, (OSError, ValueError, subprocess.SubprocessError)):
            raise ExecutionError("failed to start the complete fast ready wave") from exc
        raise

    results: list[dict[str, Any]] = []
    try:
        for launch, process, stdout, stderr in processes:
            returncode = process.wait()
            stdout.close()
            stderr.close()
            results.append(
                {
                    "task_id": launch.task_id,
                    "returncode": returncode,
                    "stdout_log": launch.stdout_path.relative_to(root).as_posix(),
                    "stderr_log": launch.stderr_path.relative_to(root).as_posix(),
                }
            )
    except BaseException:
        _terminate_started_processes(processes)
        raise
    failed = [row["task_id"] for row in results if row["returncode"] != 0]
    if failed:
        raise FastWaveTaskFailure(
            "fast ready wave failed for tasks: " + ", ".join(failed)
        )
    return {
        "campaign_id": head["campaign_id"],
        "generation": head["generation"],
        "launched": results,
        "materialization_required": materialization,
    }


def run_fast_wave(
    *,
    workspace_root: Path,
    head_path: Path,
    launch_spec_path: Path,
    log_directory: Path,
) -> dict[str, Any]:
    """Claim and execute one complete wave under the campaign-wide OS lease."""

    root = _workspace_root(workspace_root)
    head_physical = _workspace_existing_file(root, head_path, label="fast current head")
    observed_head = _load_version(
        head_physical, _HEAD_VERSION, label="fast current head"
    )
    observed_physical_sha256 = sha256_file(head_physical)
    observed_canonical_sha256 = sha256_json(observed_head)
    campaign_id = observed_head["campaign_id"]
    with ExclusiveFileLease(
        candidate_wave_lease_path(root, campaign_id),
        "fast campaign wave",
        {
            "campaign_id": campaign_id,
            "generation": observed_head["generation"],
            "head_canonical_sha256": observed_canonical_sha256,
            "head_physical_sha256": observed_physical_sha256,
        },
    ):
        claimed_head_path = _workspace_existing_file(
            root, head_path, label="claimed fast current head"
        )
        claimed_head = _load_version(
            claimed_head_path, _HEAD_VERSION, label="claimed fast current head"
        )
        if (
            claimed_head_path != head_physical
            or sha256_json(claimed_head) != observed_canonical_sha256
            or sha256_file(claimed_head_path) != observed_physical_sha256
        ):
            raise ValidationError(
                "fast current head changed before the ready wave was claimed"
            )
        return run_fast_wave_owned(
            workspace_root=root,
            head_path=claimed_head_path,
            launch_spec_path=launch_spec_path,
            log_directory=log_directory,
        )
