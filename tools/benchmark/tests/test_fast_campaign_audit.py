from __future__ import annotations

from pathlib import Path

import pytest

from tools.benchmark.errors import ValidationError
from tools.benchmark.fast_campaign import (
    build_fast_audit,
    build_fast_campaign_plan,
    build_fast_campaign_status,
    build_fast_run_index,
    publish_fast_run_receipt,
    publish_immutable_fast_document,
)
from tools.benchmark.tests.test_fast_campaign import (
    NOW,
    _integration_pseudo_task,
    bootstrap_static_bindings,
    prepare_campaign,
)


def prepare_audit(root: Path) -> dict[str, Path]:
    intent, _, _ = prepare_campaign(root)
    receipt_path = root / "receipt.json"
    publish_fast_run_receipt(
        intent=intent,
        run_record_path=root / "run.json",
        receipt_output_path=receipt_path,
    )
    index = build_fast_run_index(
        plan_path=intent.plan_path,
        receipt_paths=[receipt_path],
        workspace_root=root,
        generated_at=NOW,
    )
    index_path = root / "index-1.json"
    publish_immutable_fast_document(index_path, index)
    status = build_fast_campaign_status(
        plan_path=intent.plan_path,
        index_path=index_path,
        workspace_root=root,
        generation=1,
        generated_at=NOW,
    )
    status_path = root / "status-1.json"
    publish_immutable_fast_document(status_path, status)
    return {
        "bootstrap_path": root / "bootstrap.json",
        "plan_path": intent.plan_path,
        "index_path": index_path,
        "status_path": status_path,
        "workspace_root": root,
    }


def test_fast_audit_hashes_only_normalized_run_and_receipt_bindings(tmp_path: Path) -> None:
    paths = prepare_audit(tmp_path)
    audit = build_fast_audit(checkpoint="B2", generated_at=NOW, **paths)
    assert audit["passed"] is True
    assert [row["task_id"] for row in audit["scope_receipts"]] == ["run.B2.full"]
    assert [row["check_id"] for row in audit["checks"]] == [
        "bootstrap.bindings",
        "receipt.0",
    ]
    assert not (tmp_path / "state").exists()


def test_fast_bootstrap_audit_has_no_receipt_scope(tmp_path: Path) -> None:
    paths = prepare_audit(tmp_path)
    audit = build_fast_audit(checkpoint="bootstrap", generated_at=NOW, **paths)
    assert audit["scope_receipts"] == []
    assert len(audit["checks"]) == 1


def test_fast_audit_fails_closed_on_receipt_drift(tmp_path: Path) -> None:
    paths = prepare_audit(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_bytes(receipt_path.read_bytes() + b" \n")
    with pytest.raises(ValidationError, match="canonical or physical hash differs"):
        build_fast_audit(checkpoint="B2", generated_at=NOW, **paths)


def test_fast_final_audit_binds_only_the_pre_audit_ready_status(tmp_path: Path) -> None:
    prepare_campaign(tmp_path)
    task = _integration_pseudo_task(
        ordinal=0,
        task_id="audit.final",
        kind="audit",
        stage="final",
        static_bindings=bootstrap_static_bindings(tmp_path),
    )
    plan = build_fast_campaign_plan(
        plan_id="final-audit-plan",
        bootstrap_path=tmp_path / "bootstrap.json",
        workspace_root=tmp_path,
        tasks=[task],
        candidate_ids=["candidate-a"],
        created_at=NOW,
    )
    plan_path = tmp_path / "final-audit-plan.json"
    publish_immutable_fast_document(plan_path, plan)
    index = build_fast_run_index(
        plan_path=plan_path,
        receipt_paths=[],
        workspace_root=tmp_path,
        generated_at=NOW,
    )
    index_path = tmp_path / "final-audit-index.json"
    publish_immutable_fast_document(index_path, index)
    status = build_fast_campaign_status(
        plan_path=plan_path,
        index_path=index_path,
        workspace_root=tmp_path,
        generation=0,
        generated_at=NOW,
    )
    status_path = tmp_path / "final-audit-ready.json"
    publish_immutable_fast_document(status_path, status)
    assert status["ready_tasks"] == ["audit.final"]

    audit = build_fast_audit(
        checkpoint="final",
        bootstrap_path=tmp_path / "bootstrap.json",
        plan_path=plan_path,
        index_path=index_path,
        status_path=status_path,
        workspace_root=tmp_path,
        generated_at=NOW,
    )
    audit_path = tmp_path / "final-audit.json"
    publish_immutable_fast_document(audit_path, audit)
    after = build_fast_campaign_status(
        plan_path=plan_path,
        index_path=index_path,
        workspace_root=tmp_path,
        generation=1,
        audit_paths=[audit_path],
        generated_at=NOW,
    )
    after_path = tmp_path / "final-audit-complete.json"
    publish_immutable_fast_document(after_path, after)
    assert after["tasks"][0]["state"] == "completed"
    with pytest.raises(ValidationError, match="pre-audit ready status"):
        build_fast_audit(
            checkpoint="final",
            bootstrap_path=tmp_path / "bootstrap.json",
            plan_path=plan_path,
            index_path=index_path,
            status_path=after_path,
            workspace_root=tmp_path,
            generated_at=NOW,
        )
