from __future__ import annotations

from pathlib import Path

import pytest

from tools.benchmark.errors import ValidationError
from tools.benchmark.fast_campaign import (
    build_fast_run_index,
    publish_fast_run_receipt,
    publish_immutable_fast_document,
)
from tools.benchmark.tests.test_fast_campaign import NOW, prepare_campaign


def _publish_receipt(root: Path) -> tuple[Path, Path]:
    intent, _, _ = prepare_campaign(root)
    receipt_path = root / "receipt.json"
    publish_fast_run_receipt(
        intent=intent,
        run_record_path=root / "run.json",
        receipt_output_path=receipt_path,
    )
    return intent.plan_path, receipt_path


def test_fast_index_is_plan_ordered_and_append_only(tmp_path: Path) -> None:
    plan_path, receipt_path = _publish_receipt(tmp_path)
    empty = build_fast_run_index(
        plan_path=plan_path,
        receipt_paths=[],
        workspace_root=tmp_path,
        generated_at=NOW,
    )
    empty_path = tmp_path / "empty-index.json"
    publish_immutable_fast_document(empty_path, empty)
    index = build_fast_run_index(
        plan_path=plan_path,
        receipt_paths=[receipt_path],
        workspace_root=tmp_path,
        previous_index_path=empty_path,
        generated_at=NOW,
    )
    assert index["index_id"] == "fast-campaign:index:1"
    assert [row["task_id"] for row in index["receipts"]] == ["run.B2.full"]

    index_path = tmp_path / "index-1.json"
    publish_immutable_fast_document(index_path, index)
    with pytest.raises(ValidationError, match="append-only"):
        build_fast_run_index(
            plan_path=plan_path,
            receipt_paths=[],
            workspace_root=tmp_path,
            previous_index_path=index_path,
            generated_at=NOW,
        )


def test_fast_index_rejects_duplicate_task_receipts(tmp_path: Path) -> None:
    plan_path, receipt_path = _publish_receipt(tmp_path)
    with pytest.raises(ValidationError, match="strict plan-ordinal|unique"):
        build_fast_run_index(
            plan_path=plan_path,
            receipt_paths=[receipt_path, receipt_path],
            workspace_root=tmp_path,
            generated_at=NOW,
        )


def test_fast_index_detects_normalized_run_physical_drift(tmp_path: Path) -> None:
    plan_path, receipt_path = _publish_receipt(tmp_path)
    run_path = tmp_path / "run.json"
    run_path.write_bytes(run_path.read_bytes() + b" \n")
    with pytest.raises(ValidationError, match="canonical or physical hash differs"):
        build_fast_run_index(
            plan_path=plan_path,
            receipt_paths=[receipt_path],
            workspace_root=tmp_path,
            generated_at=NOW,
        )
