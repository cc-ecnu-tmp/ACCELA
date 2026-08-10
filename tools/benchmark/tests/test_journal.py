from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from tools.benchmark import journal as journal_module
from tools.benchmark.errors import ExecutionError
from tools.benchmark.journal import AttemptJournal, durable_create_json


_IDENTITY_SHA256 = "a" * 64
_ARTIFACT_SHA256 = "b" * 64


def _journal(tmp_path: Path) -> AttemptJournal:
    tmp_path.mkdir(parents=True, exist_ok=True)
    attempt_directory = tmp_path / "attempt-0000"
    attempt_directory.mkdir()
    journal = AttemptJournal(attempt_directory, identity_sha256=_IDENTITY_SHA256)
    journal.initialize()
    return journal


def _compile_phase() -> dict[str, object]:
    return {
        "status": "ok",
        "duration_ns": 1,
        "exit_code": 0,
        "stdout": {"sha256": "c" * 64, "size_bytes": 0},
        "stderr": {"sha256": "c" * 64, "size_bytes": 0},
        "diagnostic": None,
    }


def _case_prefix(*, samples: list[dict[str, object]], status: str) -> dict[str, object]:
    return {
        "compile": _compile_phase(),
        "compile_samples": [],
        "compile_statistics": {
            "sample_count": 1,
            "median_duration_ns": 1,
            "mad_duration_ns": 0,
        },
        "cache_hit": False,
        "artifact_sha256": _ARTIFACT_SHA256,
        "remarks_sha256": None,
        "remarks_event_count": None,
        "link": None,
        "analyze": None,
        "analysis_sha256": None,
        "samples": samples,
        "status": status,
        "cancellation_reason": None,
    }


def _sample() -> dict[str, object]:
    return {"index": 0, "status": "passed"}


def _compile_result() -> dict[str, object]:
    return {
        **{
            key: value
            for key, value in _case_prefix(samples=[], status="pending").items()
            if key
            in {
                "compile",
                "compile_samples",
                "compile_statistics",
                "cache_hit",
                "artifact_sha256",
                "remarks_sha256",
                "remarks_event_count",
            }
        },
        "case_prefix": _case_prefix(samples=[], status="pending"),
    }


def _run_result() -> dict[str, object]:
    return {
        "sample": _sample(),
        "case_prefix": _case_prefix(samples=[_sample()], status="pending"),
    }


def _passed_case() -> dict[str, object]:
    return _case_prefix(samples=[_sample()], status="passed")


def test_posix_publish_is_atomic_no_replace_when_destination_wins_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary = tmp_path / "event.tmp"
    destination = tmp_path / "event.json"
    temporary.write_bytes(b"loser")
    real_link = journal_module.os.link

    def publish_racing_winner(source: Path, target: Path) -> None:
        target.write_bytes(b"winner")
        real_link(source, target)

    monkeypatch.setattr(journal_module.os, "link", publish_racing_winner)
    with pytest.raises(FileExistsError):
        journal_module._publish_posix_no_replace(temporary, destination)

    assert destination.read_bytes() == b"winner"
    assert temporary.read_bytes() == b"loser"


def test_durable_json_create_never_replaces_existing_evidence(tmp_path: Path) -> None:
    destination = tmp_path / "identity.json"
    destination.write_bytes(b"winner")

    with pytest.raises(ExecutionError, match="already exists"):
        durable_create_json(destination, {"value": "loser"})

    assert destination.read_bytes() == b"winner"


def test_journal_rejects_out_of_order_and_duplicate_stages_before_publish(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    with pytest.raises(ExecutionError, match="run phase is duplicated or out of order"):
        journal.append_phase_started("run-0000")
    assert journal.load().events == ()

    journal.append_phase_started("compile")
    (journal.attempt_directory / "compile.log").write_bytes(b"compile")
    journal.append_phase_result("compile", _compile_result())
    event_count = len(journal.load().events)

    with pytest.raises(ExecutionError, match="compile phase is duplicated or out of order"):
        journal.append_phase_started("compile")
    assert len(journal.load().events) == event_count


def test_passed_terminal_requires_complete_phase_result_and_raw_evidence(
    tmp_path: Path,
) -> None:
    no_phases = _journal(tmp_path / "no-phases")
    (no_phases.attempt_directory / "unexpected.log").write_bytes(b"raw")
    with pytest.raises(ExecutionError, match="phase sequence differs"):
        no_phases.append_terminal(_passed_case())
    assert no_phases.load().terminal_case_result is None

    no_phase_raw = _journal(tmp_path / "no-phase-raw")
    no_phase_raw.append_phase_started("compile")
    no_phase_raw.append_phase_result("compile", _compile_result())
    no_phase_raw.append_phase_started("run-0000")
    no_phase_raw.append_phase_result("run-0000", _run_result())
    (no_phase_raw.attempt_directory / "late.log").write_bytes(b"late")
    with pytest.raises(ExecutionError, match="empty phase result"):
        no_phase_raw.append_terminal(_passed_case())
    assert no_phase_raw.load().terminal_case_result is None


def test_journal_binds_phase_results_and_detects_event_tamper(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.append_phase_started("compile")
    (journal.attempt_directory / "compile.log").write_bytes(b"compile")
    journal.append_phase_result("compile", _compile_result())
    journal.append_phase_started("run-0000")
    (journal.attempt_directory / "run.log").write_bytes(b"run")
    journal.append_phase_result("run-0000", _run_result())

    altered = _passed_case()
    altered["samples"] = [{"index": 0, "status": "passed", "extra": True}]
    with pytest.raises(ExecutionError, match="terminal prefix"):
        journal.append_terminal(altered)

    snapshot = journal.append_terminal(_passed_case())
    journal.verify_terminal_raw_files(snapshot)
    terminal_path = sorted(journal.directory.glob("event-*.json"))[-1]
    terminal_path.write_bytes(terminal_path.read_bytes() + b" ")
    with pytest.raises(ExecutionError, match="content hash differs"):
        journal.load()


def test_infrastructure_terminal_binds_only_the_latest_successful_prefix(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path / "accepted")
    journal.append_phase_started("compile")
    (journal.attempt_directory / "compile.log").write_bytes(b"compile")
    journal.append_phase_result("compile", _compile_result())
    infrastructure = _case_prefix(samples=[], status="cancelled")
    infrastructure["cancellation_reason"] = "infrastructure_failure"

    snapshot = journal.append_terminal(infrastructure)
    assert snapshot.terminal_case_result == infrastructure
    journal.verify_terminal_raw_files(snapshot)

    real_failure = _journal(tmp_path / "real-failure")
    real_failure.append_phase_started("compile")
    (real_failure.attempt_directory / "compile.log").write_bytes(b"compile")
    failed_prefix = _case_prefix(samples=[], status="compile_error")
    failed_prefix["compile"] = {**_compile_phase(), "status": "error"}
    real_failure.append_phase_result(
        "compile",
        {
            "compile": failed_prefix["compile"],
            "compile_samples": failed_prefix["compile_samples"],
            "compile_statistics": failed_prefix["compile_statistics"],
            "cache_hit": failed_prefix["cache_hit"],
            "case_prefix": failed_prefix,
        },
    )
    failed = {**failed_prefix, "status": "cancelled"}
    failed["cancellation_reason"] = "infrastructure_failure"
    with pytest.raises(ExecutionError, match="committed real stage failure"):
        real_failure.append_terminal(failed)
    assert real_failure.load().terminal_case_result is None


def test_journal_independently_rejects_multiple_cancelled_terminal_phases(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    compile_prefix = _case_prefix(samples=[], status="cancelled")
    compile_prefix["cancellation_reason"] = "scheduler_cancelled"
    compile_prefix["compile"] = {**_compile_phase(), "status": "cancelled"}
    compile_prefix["compile_samples"] = [{"status": "cancelled"}]

    journal.append_phase_started("compile")
    journal.append_phase_result(
        "compile",
        {
            "compile": compile_prefix["compile"],
            "compile_samples": compile_prefix["compile_samples"],
            "compile_statistics": compile_prefix["compile_statistics"],
            "cache_hit": compile_prefix["cache_hit"],
            "case_prefix": deepcopy(compile_prefix),
        },
    )
    link_prefix = deepcopy(compile_prefix)
    link_prefix["link"] = {**_compile_phase(), "status": "cancelled"}
    journal.append_phase_started("link")
    journal.append_phase_result(
        "link",
        {"link": link_prefix["link"], "case_prefix": deepcopy(link_prefix)},
    )

    with pytest.raises(ExecutionError, match="predecessor compile stage"):
        journal.append_terminal(link_prefix)
    assert journal.load().terminal_case_result is None
