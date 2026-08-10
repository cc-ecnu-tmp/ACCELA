from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
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


def test_posix_publish_anchors_link_names_to_one_directory_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "event.json"
    if journal_module.os.name == "nt":
        pytest.skip("linkat publication is the POSIX durability implementation")
    real_link = journal_module.os.link
    observed_directory_fds: list[tuple[int | None, int | None]] = []

    def observe_linkat(
        source: object,
        target: object,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        observed_directory_fds.append((src_dir_fd, dst_dir_fd))
        real_link(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(journal_module.os, "link", observe_linkat)
    durable_create_json(destination, {"value": "direct-exclusive-create"})

    assert len(observed_directory_fds) == 1
    source_fd, target_fd = observed_directory_fds[0]
    assert source_fd is not None and source_fd == target_fd
    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "value": "direct-exclusive-create"
    }
    assert not list(tmp_path.glob(".*.partial"))


def test_durable_create_records_path_free_symbolic_errno(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "event.json"

    real_open = journal_module.os.open

    def reject_create(path, flags, mode=0o777, *, dir_fd=None):
        if journal_module.os.name == "nt" or dir_fd is not None:
            raise OSError(errno.EXDEV, "cross-device link", str(destination))
        return real_open(path, flags, mode)

    monkeypatch.setattr(journal_module.os, "open", reject_create)
    with pytest.raises(ExecutionError) as raised:
        durable_create_json(destination, {"value": "unpublished"})

    diagnostic = str(raised.value)
    assert "operation=temporary_create" in diagnostic
    assert "class=OSError" in diagnostic
    assert "errno_name=EXDEV" in diagnostic
    assert f"errno_code={errno.EXDEV}" in diagnostic
    assert str(tmp_path) not in diagnostic
    assert not destination.exists()


@pytest.mark.skipif(os.name == "nt", reason="EXDEV is a POSIX linkat failure")
def test_caught_linkat_exdev_cleans_partial_and_preserves_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "event.json"

    def reject_publish(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EXDEV, "cross-device link", str(destination))

    monkeypatch.setattr(journal_module.os, "link", reject_publish)
    with pytest.raises(ExecutionError) as raised:
        durable_create_json(destination, {"value": "complete-before-publish"})

    diagnostic = str(raised.value)
    assert "operation=publish_linkat" in diagnostic
    assert "class=OSError" in diagnostic
    assert "errno_name=EXDEV" in diagnostic
    assert f"errno_code={errno.EXDEV}" in diagnostic
    assert str(tmp_path) not in diagnostic
    assert not destination.exists()
    assert not list(tmp_path.glob(".event.json.*.partial"))


def test_durable_json_create_never_replaces_existing_evidence(tmp_path: Path) -> None:
    destination = tmp_path / "identity.json"
    destination.write_bytes(b"winner")

    with pytest.raises(ExecutionError, match="already exists"):
        durable_create_json(destination, {"value": "loser"})

    assert destination.read_bytes() == b"winner"
    assert not list(tmp_path.glob(".identity.json.*.partial"))


@pytest.mark.skipif(os.name != "nt", reason="MoveFileExW is the Windows publisher")
def test_windows_publish_uses_absolute_no_replace_write_through_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes

    destination = tmp_path / "event.json"
    observations: dict[str, object] = {}

    class FakeMoveFileEx:
        argtypes = None
        restype = None

        def __call__(self, source: str, target: str, flags: int) -> int:
            observations.update(source=source, target=target, flags=flags)
            os.replace(source, target)
            return 1

    class FakeKernel32:
        MoveFileExW = FakeMoveFileEx()

    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: FakeKernel32())
    durable_create_json(destination, {"value": "windows-publish-contract"})

    source = Path(str(observations["source"]))
    target = Path(str(observations["target"]))
    assert source.is_absolute()
    assert target == destination.resolve(strict=True)
    assert observations["flags"] == 0x00000008
    assert destination.is_file()


def test_concurrent_durable_create_has_exactly_one_winner(tmp_path: Path) -> None:
    destination = tmp_path / "identity.json"
    barrier = threading.Barrier(2)

    def contender(value: str) -> tuple[str, str | None]:
        barrier.wait(timeout=10)
        try:
            durable_create_json(destination, {"value": value})
        except ExecutionError as exc:
            return value, str(exc)
        return value, None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(contender, ("first", "second")))

    winners = [value for value, error in results if error is None]
    losers = [error for _, error in results if error is not None]
    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0] is not None
    assert losers[0].startswith("durable evidence destination already exists")
    if os.name == "nt":
        assert "operation=publish_movefileex" in losers[0]
        assert "winerror_code=" in losers[0]
    else:
        assert "operation=publish_linkat" in losers[0]
        assert "errno_name=EEXIST" in losers[0]
    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "value": winners[0]
    }
    assert not list(tmp_path.glob(".identity.json.*.partial"))


def test_caught_failed_write_cleans_partial_and_preserves_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "event.json"
    real_write = journal_module.os.write
    writes = 0

    def short_then_fail(descriptor: int, payload: object) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            return real_write(descriptor, memoryview(payload)[:5])
        raise OSError(errno.EIO, "injected write failure", str(destination))

    monkeypatch.setattr(journal_module.os, "write", short_then_fail)
    with pytest.raises(ExecutionError) as raised:
        durable_create_json(destination, {"value": "crash-visible"})

    diagnostic = str(raised.value)
    assert "operation=write" in diagnostic
    assert "errno_name=EIO" in diagnostic
    assert f"errno_code={errno.EIO}" in diagnostic
    assert str(tmp_path) not in diagnostic
    assert not destination.exists()
    assert not list(tmp_path.glob(".event.json.*.partial"))


def test_cleanup_failure_is_reported_without_masking_original_io_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "event.json"

    def reject_write(*_args: object, **_kwargs: object) -> int:
        raise OSError(errno.EIO, "injected write failure", str(destination))

    def reject_cleanup(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(errno.EACCES, "injected cleanup failure", str(destination))

    monkeypatch.setattr(journal_module.os, "write", reject_write)
    monkeypatch.setattr(journal_module.os, "unlink", reject_cleanup)
    with pytest.raises(ExecutionError) as raised:
        durable_create_json(destination, {"value": "cleanup-observable"})

    diagnostic = str(raised.value)
    assert "operation=write" in diagnostic
    assert "errno_name=EIO" in diagnostic
    assert "cleanup_failures=" in diagnostic
    assert "operation=failure_partial_unlink" in diagnostic
    assert "errno_name=EACCES" in diagnostic
    assert str(tmp_path) not in diagnostic
    assert not destination.exists()
    assert len(list(tmp_path.glob(".event.json.*.partial"))) == 1


@pytest.mark.skipif(os.name == "nt", reason="directory fsync is the POSIX contract")
def test_post_publish_fsync_error_never_deletes_final_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "event.json"
    expected = b'{"value":"published-final"}\n'
    real_fsync = journal_module.os.fsync
    armed = True

    def fail_first_directory_fsync(descriptor: int) -> None:
        nonlocal armed
        if armed and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            armed = False
            raise OSError(errno.EIO, "injected directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(journal_module.os, "fsync", fail_first_directory_fsync)
    with pytest.raises(ExecutionError) as raised:
        durable_create_json(destination, {"value": "published-final"})

    diagnostic = str(raised.value)
    assert "operation=publish_directory_fsync" in diagnostic
    assert "errno_name=EIO" in diagnostic
    assert destination.read_bytes() == expected
    assert not list(tmp_path.glob(".event.json.*.partial"))


def test_process_crash_leaves_partial_evidence_without_publishing_final(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "event.json"
    expected = b'{"value":"crash-visible"}\n'
    crash_script = textwrap.dedent(
        """
        import os
        import sys
        from pathlib import Path

        from tools.benchmark import journal
        from tools.benchmark.journal import durable_create_json

        real_write = os.write

        def write_then_crash(descriptor, payload):
            real_write(descriptor, memoryview(payload)[:5])
            os._exit(91)

        journal.os.write = write_then_crash
        durable_create_json(Path(sys.argv[1]), {"value": "crash-visible"})
        """
    )
    result = subprocess.run(
        (sys.executable, "-c", crash_script, str(destination)),
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 91, result.stderr.decode("utf-8", errors="replace")
    assert not destination.exists()
    partials = list(tmp_path.glob(".event.json.*.partial"))
    assert len(partials) == 1
    assert partials[0].read_bytes() == expected[:5]


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
