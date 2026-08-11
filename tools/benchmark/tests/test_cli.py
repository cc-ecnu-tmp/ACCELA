from __future__ import annotations

import argparse
import errno
import os
from pathlib import Path

import pytest

from tools.benchmark.cli import _resolve_workspace_root, build_parser, main
from tools.benchmark.errors import ConfigurationError
from tools.benchmark.metrics import ANALYZER_METRICS, rv64gc_qemu_v1
from tools.benchmark.schema import load_and_validate


def test_all_public_commands_are_registered() -> None:
    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    assert set(subparsers.choices) == {
        "inventory", "validate", "run", "oracle", "candidates", "report", "audit", "protocol"
    }
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
    protocol_subparsers = next(
        action
        for action in subparsers.choices["protocol"]._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    run_parser = subparsers.choices["run"]
    oracle_run_parser = oracle_subparsers.choices["run"]
    validate_suite_parser = validate_subparsers.choices["suite"]
    for formal_parser in (
        run_parser,
        oracle_run_parser,
        validate_suite_parser,
        protocol_subparsers.choices["capture"],
        protocol_subparsers.choices["verify"],
    ):
        workspace_action = next(
            action
            for action in formal_parser._actions
            if action.dest == "workspace_root"
        )
        assert workspace_action.required
    for run_parser_with_provenance in (run_parser, validate_suite_parser):
        environment_action = next(
            action
            for action in run_parser_with_provenance._actions
            if action.dest == "execution_environment_sha256"
        )
        assert not environment_action.required
    assert not any(
        action.dest == "execution_environment_sha256"
        for action in oracle_run_parser._actions
    )
    assert not any(
        action.dest.startswith("candidate_campaign_")
        or action.dest in {"candidate_status_ledger", "candidate_task_id"}
        for action in oracle_run_parser._actions
    )
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


def test_parser_and_explicit_workspace_do_not_require_a_valid_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_root = tmp_path.resolve(strict=True)

    def unavailable_cwd(_cls: type[Path]) -> Path:
        raise FileNotFoundError(errno.ENOENT, "deleted working directory")

    monkeypatch.setattr(Path, "cwd", classmethod(unavailable_cwd))
    parser = build_parser()
    args = parser.parse_args(
        [
            "candidates",
            "profiles",
            "--registry",
            str(tmp_path / "catalog.json"),
            "--pass-registry",
            str(tmp_path / "pass-registry.json"),
            "--workspace-root",
            str(expected_root),
            "--matrix-id",
            "cwd-independent-parser",
            "--output-dir",
            str(tmp_path / "profiles"),
        ]
    )
    assert _resolve_workspace_root(args.workspace_root) == expected_root

    with pytest.raises(ConfigurationError, match="--workspace-root is required"):
        _resolve_workspace_root(None)
    with pytest.raises(ConfigurationError, match="must be an absolute path"):
        _resolve_workspace_root(Path("relative-workspace"))


def test_cli_error_rendering_retains_original_error_when_cwd_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "missing.json"

    def unavailable_cwd(_cls: type[Path]) -> Path:
        raise FileNotFoundError(errno.ENOENT, "deleted working directory")

    monkeypatch.setattr(Path, "cwd", classmethod(unavailable_cwd))
    assert main(["validate", "schema", str(missing)]) == 2

    diagnostic = capsys.readouterr().err
    assert "ValidationError: cannot read valid UTF-8 JSON" in diagnostic
    assert "current_working_directory=unavailable" in diagnostic
    assert "class=FileNotFoundError" in diagnostic
    assert "errno_name=ENOENT" in diagnostic
    assert f"errno_code={errno.ENOENT}" in diagnostic
    assert str(tmp_path) not in diagnostic


@pytest.mark.skipif(os.name == "nt", reason="Windows does not allow removing the process CWD")
def test_cli_error_rendering_survives_a_physically_deleted_cwd(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "missing.json"
    deleted = tmp_path / "deleted-cwd"
    deleted.mkdir()
    original_directory = os.open(".", os.O_RDONLY)
    os.chdir(deleted)
    os.rmdir(deleted)
    try:
        assert main(["validate", "schema", str(missing)]) == 2
    finally:
        os.fchdir(original_directory)
        os.close(original_directory)

    diagnostic = capsys.readouterr().err
    assert "ValidationError: cannot read valid UTF-8 JSON" in diagnostic
    assert "current_working_directory=unavailable" in diagnostic
    assert "errno_name=ENOENT" in diagnostic
