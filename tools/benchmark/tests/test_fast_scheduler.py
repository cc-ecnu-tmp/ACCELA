from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tools.benchmark import fast_scheduler
from tools.benchmark.cli import build_parser, main
from tools.benchmark.errors import ConfigurationError, ExecutionError, ValidationError
from tools.benchmark.lease import ExclusiveFileLease, candidate_wave_lease_path
from tools.benchmark.util import read_json, sha256_file, sha256_json


class _FakeProcess:
    def __init__(
        self,
        argv: tuple[str, ...],
        *,
        stdout: object,
        stderr: object,
        shell: bool,
        **_: object,
    ) -> None:
        assert shell is False
        task_id = argv[argv.index("--candidate-fast-task-id") + 1]
        exit_code = int(argv[argv.index("--scheduler-test-exit-code") + 1])
        stdout.write(f"stdout-{task_id}\n".encode())  # type: ignore[attr-defined]
        stderr.write(f"stderr-{task_id}\n".encode())  # type: ignore[attr-defined]
        self.returncode = exit_code

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -1

    def kill(self) -> None:
        self.returncode = -9


def _write(path: Path, document: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _artifact(root: Path, path: Path) -> dict[str, str]:
    document = read_json(path)
    return {
        "path": path.relative_to(root).as_posix(),
        "canonical_sha256": sha256_json(document),
        "physical_sha256": sha256_file(path),
    }


def _campaign(
    root: Path, *, ready_kinds: tuple[str, ...] = ("run", "diagnostic", "study")
) -> tuple[Path, Path, dict[str, dict[str, object]]]:
    tasks: list[dict[str, object]] = []
    for ordinal, kind in enumerate(ready_kinds):
        task_id = f"task-{ordinal}"
        tasks.append(
            {
                "ordinal": ordinal,
                "task_id": task_id,
                "kind": kind,
                "output_path": (
                    f"runs/{task_id}.json"
                    if kind in {"run", "diagnostic"}
                    else f"studies/{task_id}.json"
                ),
                "receipt_path": (
                    f"receipts/{task_id}.json"
                    if kind in {"run", "diagnostic"}
                    else None
                ),
            }
        )
    bootstrap_path = _write(
        root / "control/bootstrap.json",
        {"schema_version": "candidate-fast-bootstrap.v1", "campaign_id": "campaign"},
    )
    bootstrap = _artifact(root, bootstrap_path)
    bootstrap_sha256 = bootstrap["canonical_sha256"]
    plan = {
        "schema_version": "candidate-fast-campaign-plan.v1",
        "campaign_id": "campaign",
        "bootstrap": bootstrap,
        "max_parallel_runs": 4,
        "jobs_per_run": 4,
        "tasks": tasks,
    }
    plan_path = _write(root / "control/plan.json", plan)
    plan_sha256 = sha256_json(plan)
    index = {
        "schema_version": "candidate-fast-run-index.v1",
        "index_id": "index-0",
        "campaign_id": "campaign",
        "bootstrap_sha256": bootstrap_sha256,
        "plan_sha256": plan_sha256,
    }
    index_path = _write(root / "control/index.json", index)
    status = {
        "schema_version": "candidate-fast-status.v1",
        "status_id": "status-0",
        "campaign_id": "campaign",
        "generation": 0,
        "bootstrap": bootstrap,
        "plan": _artifact(root, plan_path),
        "index": _artifact(root, index_path),
        "ready_tasks": [task["task_id"] for task in tasks],
        "tasks": [
            {"task_id": task["task_id"], "state": "ready"} for task in tasks
        ],
    }
    status_path = _write(root / "control/status.json", status)
    head = {
        "schema_version": "candidate-fast-current-head.v1",
        "campaign_id": "campaign",
        "generation": 0,
        "bootstrap_sha256": bootstrap_sha256,
        "plan_sha256": plan_sha256,
        "status_id": "status-0",
        "status": _artifact(root, status_path),
        "index_id": "index-0",
        "index": _artifact(root, index_path),
    }
    head_path = _write(root / "control/head.json", head)
    return head_path, plan_path, {task["task_id"]: task for task in tasks}  # type: ignore[index]


def _argv(
    root: Path,
    *,
    task_id: str,
    receipt_path: str,
    exit_code: int = 0,
) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-m",
        "tools.benchmark",
        "run",
        "--candidate-fast-plan",
        "control/plan.json",
        "--candidate-fast-status",
        "control/status.json",
        "--candidate-fast-index",
        "control/index.json",
        "--candidate-fast-task-id",
        task_id,
        "--candidate-fast-receipt",
        receipt_path,
        "--jobs",
        "4",
        "--scheduler-test-exit-code",
        str(exit_code),
    ]


@pytest.fixture(autouse=True)
def _accept_minimal_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fast_scheduler, "load_and_validate", read_json)
    monkeypatch.setattr(fast_scheduler.subprocess, "Popen", _FakeProcess)


def test_fast_wave_launches_exact_runnable_ready_set_and_reports_materializer(
    tmp_path: Path,
) -> None:
    head_path, _, tasks = _campaign(tmp_path)
    launch = [
        {
            "task_id": task_id,
            "argv": _argv(
                tmp_path,
                task_id=task_id,
                receipt_path=str(task["receipt_path"]),
            ),
        }
        for task_id, task in tasks.items()
        if task["kind"] in {"run", "diagnostic"}
    ]
    launch_path = _write(tmp_path / "control/launch.json", launch)

    result = fast_scheduler.run_fast_wave(
        workspace_root=tmp_path,
        head_path=head_path,
        launch_spec_path=launch_path,
        log_directory=Path("logs/wave-0"),
    )

    assert [row["task_id"] for row in result["launched"]] == ["task-0", "task-1"]
    assert all(row["returncode"] == 0 for row in result["launched"])
    assert result["materialization_required"] == ["task-2"]
    for row in result["launched"]:
        assert (tmp_path / row["stdout_log"]).read_text(encoding="utf-8")
        assert (tmp_path / row["stderr_log"]).read_text(encoding="utf-8")


def test_fast_wave_creates_only_planned_parents_before_clean_layout_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head_path, _, tasks = _campaign(tmp_path, ready_kinds=("run",))
    task = tasks["task-0"]
    output = tmp_path / str(task["output_path"])
    receipt = tmp_path / str(task["receipt_path"])
    assert not output.parent.exists()
    assert not receipt.parent.exists()

    class InspectPreleaseProcess(_FakeProcess):
        def __init__(self, *args: object, **kwargs: object) -> None:
            assert output.parent.is_dir()
            assert receipt.parent.is_dir()
            assert not output.exists()
            assert not receipt.exists()
            assert not (tmp_path / "fast-state").exists()
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fast_scheduler.subprocess, "Popen", InspectPreleaseProcess)
    launch_path = _write(
        tmp_path / "control/launch.json",
        [
            {
                "task_id": "task-0",
                "argv": _argv(
                    tmp_path,
                    task_id="task-0",
                    receipt_path=str(task["receipt_path"]),
                ),
            }
        ],
    )
    result = fast_scheduler.run_fast_wave(
        workspace_root=tmp_path,
        head_path=head_path,
        launch_spec_path=launch_path,
        log_directory=Path("logs/wave-0"),
    )

    assert result["launched"][0]["task_id"] == "task-0"
    assert output.parent.is_dir() and receipt.parent.is_dir()
    assert not output.exists() and not receipt.exists()


def test_fast_wave_rejects_non_directory_planned_parent_before_launch(
    tmp_path: Path,
) -> None:
    head_path, _, tasks = _campaign(tmp_path, ready_kinds=("run",))
    task = tasks["task-0"]
    (tmp_path / "runs").write_text("not a directory", encoding="utf-8")
    launch_path = _write(
        tmp_path / "control/launch.json",
        [
            {
                "task_id": "task-0",
                "argv": _argv(
                    tmp_path,
                    task_id="task-0",
                    receipt_path=str(task["receipt_path"]),
                ),
            }
        ],
    )

    with pytest.raises(ValidationError, match="parent must be a directory"):
        fast_scheduler.run_fast_wave(
            workspace_root=tmp_path,
            head_path=head_path,
            launch_spec_path=launch_path,
            log_directory=Path("logs/wave-0"),
        )


def test_fast_wave_rejects_symlinked_planned_parent_before_launch(
    tmp_path: Path,
) -> None:
    head_path, _, tasks = _campaign(tmp_path, ready_kinds=("run",))
    task = tasks["task-0"]
    target = tmp_path / "elsewhere"
    target.mkdir()
    try:
        (tmp_path / "runs").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    launch_path = _write(
        tmp_path / "control/launch.json",
        [
            {
                "task_id": "task-0",
                "argv": _argv(
                    tmp_path,
                    task_id="task-0",
                    receipt_path=str(task["receipt_path"]),
                ),
            }
        ],
    )

    with pytest.raises(ValidationError, match="symbolic link"):
        fast_scheduler.run_fast_wave(
            workspace_root=tmp_path,
            head_path=head_path,
            launch_spec_path=launch_path,
            log_directory=Path("logs/wave-0"),
        )


def test_fast_wave_rejects_launch_outside_ready_set(tmp_path: Path) -> None:
    head_path, _, tasks = _campaign(tmp_path, ready_kinds=("run",))
    task = tasks["task-0"]
    launch_path = _write(
        tmp_path / "control/launch.json",
        [
            {
                "task_id": "not-ready",
                "argv": _argv(
                    tmp_path,
                    task_id="not-ready",
                    receipt_path=str(task["receipt_path"]),
                ),
            }
        ],
    )
    with pytest.raises(ConfigurationError, match="exactly equal"):
        fast_scheduler.run_fast_wave(
            workspace_root=tmp_path,
            head_path=head_path,
            launch_spec_path=launch_path,
            log_directory=Path("logs/wave-0"),
        )


def test_fast_wave_rejects_non_benchmark_entrypoint(tmp_path: Path) -> None:
    head_path, _, tasks = _campaign(tmp_path, ready_kinds=("run",))
    task = tasks["task-0"]
    argv = _argv(
        tmp_path, task_id="task-0", receipt_path=str(task["receipt_path"])
    )
    argv[1:5] = ["-c", "print('bypass')", "ignored", "ignored"]
    launch_path = _write(
        tmp_path / "control/launch.json",
        [{"task_id": "task-0", "argv": argv}],
    )
    with pytest.raises(ConfigurationError, match="isolated tools.benchmark run"):
        fast_scheduler.run_fast_wave(
            workspace_root=tmp_path,
            head_path=head_path,
            launch_spec_path=launch_path,
            log_directory=Path("logs/wave-0"),
        )


def test_fast_wave_rejects_other_python_executable(tmp_path: Path) -> None:
    head_path, _, tasks = _campaign(tmp_path, ready_kinds=("run",))
    task = tasks["task-0"]
    argv = _argv(
        tmp_path, task_id="task-0", receipt_path=str(task["receipt_path"])
    )
    argv[0] = str(tmp_path / "other-python")
    (tmp_path / "other-python").write_bytes(b"not the scheduler interpreter")
    launch_path = _write(
        tmp_path / "control/launch.json",
        [{"task_id": "task-0", "argv": argv}],
    )
    with pytest.raises(ConfigurationError, match="scheduler interpreter"):
        fast_scheduler.run_fast_wave(
            workspace_root=tmp_path,
            head_path=head_path,
            launch_spec_path=launch_path,
            log_directory=Path("logs/wave-0"),
        )


def test_fast_wave_fails_fast_while_another_generation_owns_campaign(
    tmp_path: Path,
) -> None:
    head_path, _, tasks = _campaign(tmp_path, ready_kinds=("run",))
    task = tasks["task-0"]
    launch_path = _write(
        tmp_path / "control/launch.json",
        [
            {
                "task_id": "task-0",
                "argv": _argv(
                    tmp_path,
                    task_id="task-0",
                    receipt_path=str(task["receipt_path"]),
                ),
            }
        ],
    )
    with ExclusiveFileLease(
        candidate_wave_lease_path(tmp_path, "campaign"),
        "fast campaign wave",
        {"campaign_id": "campaign", "generation": 1},
    ):
        with pytest.raises(ExecutionError, match="already owned"):
            fast_scheduler.run_fast_wave(
                workspace_root=tmp_path,
                head_path=head_path,
                launch_spec_path=launch_path,
                log_directory=Path("logs/wave-locked"),
            )
    assert not (tmp_path / "logs/wave-locked").exists()


def test_fast_wave_rejects_head_drift_before_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head_path, _, tasks = _campaign(tmp_path, ready_kinds=("run",))
    task = tasks["task-0"]
    launch_path = _write(
        tmp_path / "control/launch.json",
        [
            {
                "task_id": "task-0",
                "argv": _argv(
                    tmp_path,
                    task_id="task-0",
                    receipt_path=str(task["receipt_path"]),
                ),
            }
        ],
    )
    original_enter = fast_scheduler.ExclusiveFileLease.__enter__

    def enter_and_advance(self: ExclusiveFileLease) -> ExclusiveFileLease:
        lease = original_enter(self)
        head = read_json(head_path)
        head["generation"] = 1
        _write(head_path, head)
        return lease

    monkeypatch.setattr(
        fast_scheduler.ExclusiveFileLease, "__enter__", enter_and_advance
    )
    with pytest.raises(ValidationError, match="changed before.*claimed"):
        fast_scheduler.run_fast_wave(
            workspace_root=tmp_path,
            head_path=head_path,
            launch_spec_path=launch_path,
            log_directory=Path("logs/wave-drift"),
        )
    assert not (tmp_path / "logs/wave-drift").exists()


def test_fast_wave_interrupt_terminates_children_before_releasing_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head_path, _, tasks = _campaign(tmp_path, ready_kinds=("run", "run"))
    launch_path = _write(
        tmp_path / "control/launch.json",
        [
            {
                "task_id": task_id,
                "argv": _argv(
                    tmp_path,
                    task_id=task_id,
                    receipt_path=str(task["receipt_path"]),
                ),
            }
            for task_id, task in tasks.items()
        ],
    )
    processes: list[object] = []

    class InterruptProcess(_FakeProcess):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            self.interrupted = False
            processes.append(self)

        def wait(self, timeout: float | None = None) -> int:
            if self.returncode >= 0 and not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt
            return self.returncode

    monkeypatch.setattr(fast_scheduler.subprocess, "Popen", InterruptProcess)
    with pytest.raises(KeyboardInterrupt):
        fast_scheduler.run_fast_wave(
            workspace_root=tmp_path,
            head_path=head_path,
            launch_spec_path=launch_path,
            log_directory=Path("logs/wave-interrupt"),
        )
    assert processes and all(process.returncode == -1 for process in processes)  # type: ignore[attr-defined]
    with ExclusiveFileLease(
        candidate_wave_lease_path(tmp_path, "campaign"),
        "fast campaign wave",
        {"campaign_id": "campaign", "generation": 0},
    ):
        pass


def test_fast_wave_bounds_cleanup_when_later_start_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head_path, _, tasks = _campaign(tmp_path, ready_kinds=("run", "run"))
    launch_path = _write(
        tmp_path / "control/launch.json",
        [
            {
                "task_id": task_id,
                "argv": _argv(
                    tmp_path,
                    task_id=task_id,
                    receipt_path=str(task["receipt_path"]),
                ),
            }
            for task_id, task in tasks.items()
        ],
    )
    state = {"starts": 0, "terminate": 0, "kill": 0, "wait_timeouts": []}

    class HungProcess:
        def __init__(self, *_: object, **__: object) -> None:
            state["starts"] += 1
            if state["starts"] == 2:
                raise OSError("synthetic start failure")

        def terminate(self) -> None:
            state["terminate"] += 1

        def kill(self) -> None:
            state["kill"] += 1

        def wait(self, timeout: float | None = None) -> int:
            state["wait_timeouts"].append(timeout)
            if len(state["wait_timeouts"]) == 1:
                raise fast_scheduler.subprocess.TimeoutExpired("synthetic", timeout)
            return -9

    monkeypatch.setattr(fast_scheduler.subprocess, "Popen", HungProcess)
    with pytest.raises(ExecutionError, match="failed to start"):
        fast_scheduler.run_fast_wave(
            workspace_root=tmp_path,
            head_path=head_path,
            launch_spec_path=launch_path,
            log_directory=Path("logs/wave-0"),
        )
    assert state == {
        "starts": 2,
        "terminate": 1,
        "kill": 1,
        "wait_timeouts": [
            fast_scheduler._START_FAILURE_TERMINATE_TIMEOUT_SECONDS,
            fast_scheduler._START_FAILURE_KILL_TIMEOUT_SECONDS,
        ],
    }


@pytest.mark.parametrize(
    ("option", "wrong_value", "message"),
    [
        ("--candidate-fast-status", "control/other.json", "status"),
        ("--candidate-fast-task-id", "other-task", "task ID"),
        ("--candidate-fast-receipt", "receipts/other.json", "receipt"),
        ("--jobs", "3", "exactly 4"),
    ],
)
def test_fast_wave_rejects_prelease_binding_drift(
    tmp_path: Path, option: str, wrong_value: str, message: str
) -> None:
    head_path, _, tasks = _campaign(tmp_path, ready_kinds=("run",))
    task = tasks["task-0"]
    argv = _argv(
        tmp_path, task_id="task-0", receipt_path=str(task["receipt_path"])
    )
    argv[argv.index(option) + 1] = wrong_value
    launch_path = _write(
        tmp_path / "control/launch.json",
        [{"task_id": "task-0", "argv": argv}],
    )
    with pytest.raises(ConfigurationError, match=message):
        fast_scheduler.run_fast_wave(
            workspace_root=tmp_path,
            head_path=head_path,
            launch_spec_path=launch_path,
            log_directory=Path("logs/wave-0"),
        )


def test_fast_wave_fails_as_a_wave_on_nonzero_process(tmp_path: Path) -> None:
    head_path, _, tasks = _campaign(tmp_path, ready_kinds=("run",))
    task = tasks["task-0"]
    launch_path = _write(
        tmp_path / "control/launch.json",
        [
            {
                "task_id": "task-0",
                "argv": _argv(
                    tmp_path,
                    task_id="task-0",
                    receipt_path=str(task["receipt_path"]),
                    exit_code=7,
                ),
            }
        ],
    )
    with pytest.raises(ExecutionError, match="task-0"):
        fast_scheduler.run_fast_wave(
            workspace_root=tmp_path,
            head_path=head_path,
            launch_spec_path=launch_path,
            log_directory=Path("logs/wave-0"),
        )
    assert len(list((tmp_path / "logs/wave-0").glob("*.stdout.log"))) == 1
    assert len(list((tmp_path / "logs/wave-0").glob("*.stderr.log"))) == 1


def test_fast_wave_rejects_tampered_head_artifact(tmp_path: Path) -> None:
    head_path, plan_path, _ = _campaign(tmp_path, ready_kinds=("study",))
    plan_path.write_text("{}\n", encoding="utf-8")
    launch_path = _write(tmp_path / "control/launch.json", [])
    with pytest.raises(ValidationError, match="hash differs"):
        fast_scheduler.run_fast_wave(
            workspace_root=tmp_path,
            head_path=head_path,
            launch_spec_path=launch_path,
            log_directory=Path("logs/wave-0"),
        )


def test_fast_wave_cli_is_registered_and_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parser = build_parser()
    candidates = next(
        action
        for action in parser._actions
        if action.dest == "command"
    ).choices["candidates"]
    commands = next(
        action for action in candidates._actions if action.dest == "candidates_command"
    )
    assert "fast-wave" in commands.choices

    head = _write(tmp_path / "head.json", {})
    launch = _write(tmp_path / "launch.json", [])
    monkeypatch.setattr(
        "tools.benchmark.cli.run_fast_wave",
        lambda **_: {
            "campaign_id": "campaign",
            "generation": 0,
            "launched": [],
            "materialization_required": [],
        },
    )
    assert main(
        [
            "candidates",
            "fast-wave",
            "--workspace-root",
            str(tmp_path),
            "--head",
            str(head),
            "--launch-spec",
            str(launch),
            "--log-directory",
            "logs/wave-0",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["campaign_id"] == "campaign"
