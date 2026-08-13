from __future__ import annotations

from pathlib import Path

import pytest

from tools.benchmark import fast_driver, fast_plan, fast_scheduler
from tools.benchmark.cli import build_parser
from tools.benchmark.errors import ExecutionError
from tools.benchmark.fast_campaign import (
    build_fast_campaign_status,
    build_fast_run_index,
    publish_fast_current_head,
    publish_fast_run_receipt,
    publish_immutable_fast_document,
)
from tools.benchmark.fast_scheduler import FastWaveTaskFailure
from tools.benchmark.execution import _summary
from tools.benchmark.lease import ExclusiveFileLease, candidate_wave_lease_path
from tools.benchmark.schema import validate_document
from tools.benchmark.tests.test_fast_campaign import prepare_campaign
from tools.benchmark.tests.test_fast_plan import _fixture
from tools.benchmark.tests.test_stats_ablation_report import (
    _rebind_synthetic_run_configuration,
    make_run,
)
from tools.benchmark.util import atomic_write_json, read_json, sha256_file, sha256_json


NOW = "2026-08-13T00:00:00Z"


def _artifact(root: Path, path: Path) -> dict[str, str]:
    document = read_json(path)
    return {
        "path": path.relative_to(root).as_posix(),
        "canonical_sha256": sha256_json(document),
        "physical_sha256": sha256_file(path),
    }


def _campaign(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, Path]:
    bootstrap_path, source_path, blueprints_path = _fixture(root)
    bootstrap = read_json(bootstrap_path)
    for row in bootstrap["imported_receipts"]:
        run_path = root / row["run_artifact"]["path"]
        if row["task_id"] == "run.B2.full":
            run = read_json(run_path)
            _rebind_synthetic_run_configuration(run)
        else:
            run = make_run(
                row["run_id"],
                {"case": ("family", 1.0)},
                profile_id="candidate-empty",
                profile_sha256="a" * 64,
            )
        atomic_write_json(run_path, run)
        row["run_artifact"] = _artifact(root, run_path)
        row["verification_commitment_sha256"] = sha256_json(
            {
                key: value
                for key, value in row.items()
                if key != "verification_commitment_sha256"
            }
        )
    bootstrap["bootstrap_commitment_sha256"] = sha256_json(
        {
            key: value
            for key, value in bootstrap.items()
            if key != "bootstrap_commitment_sha256"
        }
    )
    atomic_write_json(bootstrap_path, bootstrap)

    monkeypatch.setattr(fast_plan, "load_and_validate", read_json)
    source = read_json(source_path)
    oracle_bundle = {
        row["artifact_id"]: {
            "artifact": row["artifact"],
            "document": read_json(root / row["artifact"]["path"]),
        }
        for row in bootstrap["static_artifacts"]
        if row["artifact_id"] in fast_plan.FAST_ORACLE_STATIC_ARTIFACT_VERSIONS
    }
    monkeypatch.setattr(
        fast_plan,
        "verify_fast_oracle_static_artifacts",
        lambda **_: oracle_bundle,
    )
    plan, _ = fast_plan.build_fast_plan_factory(
        workspace_root=root,
        bootstrap_path=bootstrap_path,
        source_plan_path=source_path,
        blueprint_path=blueprints_path,
        plan_id="driver-plan",
        plan_output_path=Path("control/plan.json"),
        launch_template_output_path=Path("control/templates.json"),
        campaign_output_root=Path("fast-output"),
        campaign_state_root=Path("fast-state"),
        diagnostic_profile_root=Path("fast-output/diagnostic-profiles"),
        created_at=NOW,
    )
    plan_path = root / "control/plan.json"
    index = build_fast_run_index(
        plan_path=plan_path,
        receipt_paths=[],
        workspace_root=root,
        generated_at=NOW,
    )
    index_path = root / "control/index-0.json"
    publish_immutable_fast_document(index_path, index)
    status = build_fast_campaign_status(
        plan_path=plan_path,
        index_path=index_path,
        workspace_root=root,
        generation=0,
        generated_at=NOW,
    )
    status_path = root / "control/status-0.json"
    publish_immutable_fast_document(status_path, status)
    head_path = root / "control/current-head.json"
    publish_fast_current_head(
        bootstrap_path=bootstrap_path,
        plan_path=plan_path,
        status_path=status_path,
        index_path=index_path,
        workspace_root=root,
        head_path=head_path,
        initial=True,
        updated_at=NOW,
    )
    assert plan["tasks"][0]["task_id"] == "audit.bootstrap"
    return head_path, root / "control/templates.json", plan_path, bootstrap_path


def test_fast_driver_materializes_bootstrap_audit_and_advances_cas_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head_path, templates, _, _ = _campaign(tmp_path, monkeypatch)
    snapshot = fast_driver._load_snapshot(tmp_path, head_path)
    with ExclusiveFileLease(
        candidate_wave_lease_path(tmp_path, snapshot.head["campaign_id"]),
        "fast campaign wave",
        {"campaign_id": snapshot.head["campaign_id"], "generation": 0},
    ):
        result = fast_driver._advance_generation_owned(
            snapshot=snapshot,
            launch_template_path=templates,
            generation_root=Path("control/generations"),
            report_directory=Path("fast-output/report"),
        )

    assert result["generation"] == 1
    advanced = fast_driver._load_snapshot(tmp_path, head_path)
    assert advanced.status["tasks"][0]["state"] == "completed"
    assert advanced.status["audits"][0]["artifact_id"] == "fast:audit:bootstrap"
    assert len(advanced.status["ready_tasks"]) == 4
    assert all(task_id.startswith("run.B2.") for task_id in advanced.status["ready_tasks"])


def test_fast_driver_generation_is_idempotent_after_pre_cas_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head_path, templates, _, _ = _campaign(tmp_path, monkeypatch)
    snapshot = fast_driver._load_snapshot(tmp_path, head_path)
    real_publish = fast_driver.publish_fast_current_head_owned

    def crash_before_cas(**_: object) -> dict[str, object]:
        raise ExecutionError("synthetic pre-CAS crash")

    monkeypatch.setattr(fast_driver, "publish_fast_current_head_owned", crash_before_cas)
    with ExclusiveFileLease(
        candidate_wave_lease_path(tmp_path, snapshot.head["campaign_id"]),
        "fast campaign wave",
        {"campaign_id": snapshot.head["campaign_id"], "generation": 0},
    ):
        with pytest.raises(ExecutionError, match="pre-CAS"):
            fast_driver._advance_generation_owned(
                snapshot=snapshot,
                launch_template_path=templates,
                generation_root=Path("control/generations"),
                report_directory=Path("fast-output/report"),
            )
    assert read_json(head_path)["generation"] == 0

    monkeypatch.setattr(fast_driver, "publish_fast_current_head_owned", real_publish)
    recovered = fast_driver._load_snapshot(tmp_path, head_path)
    with ExclusiveFileLease(
        candidate_wave_lease_path(tmp_path, recovered.head["campaign_id"]),
        "fast campaign wave",
        {"campaign_id": recovered.head["campaign_id"], "generation": 0},
    ):
        result = fast_driver._advance_generation_owned(
            snapshot=recovered,
            launch_template_path=templates,
            generation_root=Path("control/generations"),
            report_directory=Path("fast-output/report"),
        )
    assert result["generation"] == 1


def test_fast_driver_clean_layout_creates_only_run_parents_before_prelease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head_path, templates, _, _ = _campaign(tmp_path, monkeypatch)
    initial = fast_driver._load_snapshot(tmp_path, head_path)
    with ExclusiveFileLease(
        candidate_wave_lease_path(tmp_path, initial.head["campaign_id"]),
        "fast campaign wave",
        {"campaign_id": initial.head["campaign_id"], "generation": 0},
    ):
        fast_driver._advance_generation_owned(
            snapshot=initial,
            launch_template_path=templates,
            generation_root=Path("control/generations"),
            report_directory=Path("fast-output/report"),
        )
    ready = fast_driver._load_snapshot(tmp_path, head_path)
    ready_tasks = {
        task["task_id"]: task
        for task in ready.plan["tasks"]
        if task["task_id"] in ready.status["ready_tasks"]
    }
    assert ready_tasks
    assert not (tmp_path / "fast-output/runs").exists()
    assert not (tmp_path / "fast-output/receipts").exists()
    assert not (tmp_path / "fast-state").exists()

    def stop_at_child_start(*_: object, **__: object) -> None:
        assert (tmp_path / "fast-output/runs").is_dir()
        assert (tmp_path / "fast-output/receipts").is_dir()
        assert not (tmp_path / "fast-state").exists()
        for task in ready_tasks.values():
            assert not (tmp_path / task["output_path"]).exists()
            assert not (tmp_path / task["receipt_path"]).exists()
        raise OSError("synthetic child start boundary")

    monkeypatch.setattr(fast_scheduler.subprocess, "Popen", stop_at_child_start)
    with ExclusiveFileLease(
        candidate_wave_lease_path(tmp_path, ready.head["campaign_id"]),
        "fast campaign wave",
        {"campaign_id": ready.head["campaign_id"], "generation": 1},
    ):
        with pytest.raises(ExecutionError, match="failed to start"):
            fast_driver._advance_generation_owned(
                snapshot=ready,
                launch_template_path=templates,
                generation_root=Path("control/generations"),
                report_directory=Path("fast-output/report"),
            )

    assert read_json(head_path)["generation"] == 1


def test_fast_driver_fails_before_work_when_campaign_lease_is_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head_path, templates, _, _ = _campaign(tmp_path, monkeypatch)
    campaign_id = read_json(head_path)["campaign_id"]
    with ExclusiveFileLease(
        candidate_wave_lease_path(tmp_path, campaign_id),
        "fast campaign wave",
        {"campaign_id": campaign_id, "generation": 99},
    ):
        with pytest.raises(ExecutionError, match="already owned"):
            fast_driver.drive_fast_campaign(
                workspace_root=tmp_path,
                head_path=head_path,
                launch_template_path=templates,
                generation_root=Path("control/generations"),
                report_directory=Path("fast-output/report"),
            )
    assert not (tmp_path / "control/generations").exists()


def test_fast_driver_keeps_campaign_lease_through_generation_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head_path, templates, _, _ = _campaign(tmp_path, monkeypatch)

    def stop_inside_advance(*, snapshot: fast_driver._CampaignSnapshot, **_: object) -> None:
        with pytest.raises(ExecutionError, match="already owned"):
            with ExclusiveFileLease(
                candidate_wave_lease_path(snapshot.root, snapshot.head["campaign_id"]),
                "fast campaign wave",
                {},
            ):
                pass
        raise RuntimeError("generation advance observed campaign lease")

    monkeypatch.setattr(fast_driver, "_advance_generation_owned", stop_inside_advance)
    with pytest.raises(RuntimeError, match="observed campaign lease"):
        fast_driver.drive_fast_campaign(
            workspace_root=tmp_path,
            head_path=head_path,
            launch_template_path=templates,
            generation_root=Path("control/generations"),
            report_directory=Path("fast-output/report"),
        )


def test_fast_driver_commits_valid_failed_receipt_before_continuing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intent, _, run = prepare_campaign(tmp_path)
    head_path = tmp_path / "head.json"
    publish_fast_current_head(
        bootstrap_path=tmp_path / "bootstrap.json",
        plan_path=tmp_path / "plan.json",
        status_path=tmp_path / "status.json",
        index_path=tmp_path / "index.json",
        workspace_root=tmp_path,
        head_path=head_path,
        initial=True,
        updated_at=NOW,
    )
    templates = tmp_path / "templates.json"
    atomic_write_json(templates, {})

    def fail_after_terminal_receipt(**_: object) -> None:
        case = run["cases"][0]
        case["status"] = "wrong_output"
        case["diagnostic"] = "synthetic expected failure"
        case["samples"][0]["status"] = "wrong_output"
        case["samples"][0]["diagnostic"] = "synthetic expected failure"
        case["samples"][0]["first_mismatch_offset"] = 0
        case["consistency_passed"] = None
        run["state"] = "failed"
        run["summary"] = _summary(run["cases"])
        atomic_write_json(intent.output_path, validate_document(run))
        publish_fast_run_receipt(
            intent=intent,
            run_record_path=intent.output_path,
            receipt_output_path=intent.receipt_path,
        )
        raise FastWaveTaskFailure("synthetic task exit after terminal receipt")

    monkeypatch.setattr(
        fast_driver,
        "materialize_fast_launch_spec",
        lambda **_: [],
    )
    monkeypatch.setattr(fast_driver, "run_fast_wave_owned", fail_after_terminal_receipt)
    snapshot = fast_driver._load_snapshot(tmp_path, head_path)
    with ExclusiveFileLease(
        candidate_wave_lease_path(tmp_path, snapshot.head["campaign_id"]),
        "fast campaign wave",
        {"campaign_id": snapshot.head["campaign_id"], "generation": 0},
    ):
        result = fast_driver._advance_generation_owned(
            snapshot=snapshot,
            launch_template_path=templates,
            generation_root=Path("generations"),
            report_directory=Path("report"),
        )

    assert result["generation"] == 1
    assert result["indexed_receipts"] == 1
    advanced = fast_driver._load_snapshot(tmp_path, head_path)
    assert advanced.status["tasks"][0]["state"] == "failed"
    assert advanced.status["state"] == "failed"


def test_fast_driver_uses_planned_pair_ids_when_b3_rank_order_is_not_lexical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair_ids = [
        "diagnostic.pair.candidate-a+candidate-b",
        "diagnostic.pair.candidate-a+candidate-c",
        "diagnostic.pair.candidate-b+candidate-c",
    ]
    snapshot = fast_driver._CampaignSnapshot(
        root=tmp_path,
        head_path=tmp_path / "head.json",
        head={},
        bootstrap_path=tmp_path / "bootstrap.json",
        plan_path=tmp_path / "plan.json",
        plan={
            "tasks": [
                {
                    "task_id": task_id,
                    "kind": "diagnostic",
                    "measurement_mode": "standard_proxy",
                    "candidate_ids": task_id.removeprefix("diagnostic.pair.").split("+"),
                }
                for task_id in pair_ids
            ]
        },
        status_path=tmp_path / "status.json",
        status={},
        index_path=tmp_path / "index.json",
        index={},
    )
    b3 = {
        "candidates": [
            {
                "candidate_id": candidate_id,
                "eligible": True,
                "geometric_mean_speedup": speedup,
            }
            for candidate_id, speedup in (
                ("candidate-b", 1.3),
                ("candidate-a", 1.2),
                ("candidate-c", 1.1),
            )
        ]
    }
    observed: dict[str, object] = {}
    monkeypatch.setattr(fast_driver, "_load_version", lambda *_, **__: b3)
    monkeypatch.setattr(
        fast_driver,
        "_receipt_path",
        lambda _snapshot, _index, task_id: Path(task_id),
    )
    monkeypatch.setattr(
        fast_driver,
        "build_fast_diagnostic_study",
        lambda **keywords: observed.update(keywords) or {},
    )
    monkeypatch.setattr(
        fast_driver, "publish_immutable_fast_document", lambda _path, document: document
    )

    fast_driver._materialize_diagnostic_study(
        snapshot=snapshot,
        index_path=snapshot.index_path,
        index={},
        study_paths={"B3": tmp_path / "study.json"},
        output_path=tmp_path / "diagnostic.json",
        generated_at=NOW,
    )

    assert set(observed["pair_receipt_paths"]) == set(pair_ids)


def test_cli_registers_fast_driver() -> None:
    args = build_parser().parse_args(
        [
            "candidates",
            "fast-drive",
            "--workspace-root",
            ".",
            "--head",
            "head.json",
            "--templates",
            "templates.json",
            "--generation-directory",
            "generations",
            "--report-directory",
            "report",
        ]
    )
    assert args.candidates_command == "fast-drive"
