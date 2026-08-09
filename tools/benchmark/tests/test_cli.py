from __future__ import annotations

import argparse
from pathlib import Path

from tools.benchmark.cli import build_parser, main
from tools.benchmark.metrics import ANALYZER_METRICS, rv64gc_qemu_v1
from tools.benchmark.schema import load_and_validate


def test_all_public_commands_are_registered() -> None:
    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    assert set(subparsers.choices) == {
        "inventory", "validate", "run", "ablate", "oracle", "report", "audit", "protocol"
    }
    ablate_subparsers = next(
        action
        for action in subparsers.choices["ablate"]._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    oracle_subparsers = next(
        action
        for action in subparsers.choices["oracle"]._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    validate_subparsers = next(
        action
        for action in subparsers.choices["validate"]._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert set(ablate_subparsers.choices) == {
        "profiles", "analyze", "campaign-plan", "campaign-finalize", "campaign-status",
        "campaign-next", "campaign-task"
    }
    assert set(oracle_subparsers.choices) == {"plan", "run"}
    assert set(validate_subparsers.choices) == {"schema", "suite"}


def test_inventory_and_validate_cli(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "case.sy").write_bytes(b"source")
    (suite / "case.in").write_bytes(b"input-without-final-newline")
    (suite / "case.out").write_bytes(b"input-without-final-newline")
    manifest = tmp_path / "manifest.json"
    assert main(
        [
            "inventory",
            str(suite),
            "--suite-id",
            "cli-suite",
            "--target",
            "rv64gc",
            "--data-role",
            "B1",
            "--origin-source",
            "cli-snapshot",
            "--origin-snapshot-sha256",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "--license-expression",
            "NOASSERTION",
            "--output",
            str(manifest),
        ]
    ) == 0
    assert main(
        ["validate", "schema", str(manifest), "--verify-files", "--suite-root", str(suite)]
    ) == 0
    document = load_and_validate(manifest, suite_root=suite, verify_files=True)
    assert document["cases"][0]["input"]["size_bytes"] == len(b"input-without-final-newline")


def test_formal_metric_profile_is_complete_and_versioned() -> None:
    profile = rv64gc_qemu_v1()
    assert profile["profile_id"] == "rv64gc-qemu-v1"
    additional = {item["metric_id"]: item for item in profile["additional"]}
    assert {"dynamic_load_count", "dynamic_store_count", "l1d_miss_count"}.issubset(additional)
    assert set(ANALYZER_METRICS).issubset(additional)
    assert {"compile_time_ns", "link_time_ns", "artifact_size_bytes", "binary_size_bytes"}.issubset(additional)
