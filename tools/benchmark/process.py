from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, Sequence

import psutil

from .errors import ExecutionError
from .util import sanitize_text, sha256_file


ProcessStatus = Literal["ok", "error", "timeout", "cancelled"]


@dataclass(frozen=True)
class StreamDigest:
    sha256: str
    size_bytes: int

    def as_record(self) -> dict[str, object]:
        return {"sha256": self.sha256, "size_bytes": self.size_bytes}


@dataclass(frozen=True)
class ProcessResult:
    status: ProcessStatus
    duration_ns: int
    exit_code: int | None
    stdout: StreamDigest
    stderr: StreamDigest
    diagnostic: str | None

    def as_phase_record(self) -> dict[str, object]:
        return {
            "status": self.status,
            "duration_ns": self.duration_ns,
            "exit_code": self.exit_code,
            "stdout": self.stdout.as_record(),
            "stderr": self.stderr.as_record(),
            "diagnostic": self.diagnostic,
        }


def _digest(path: Path) -> StreamDigest:
    return StreamDigest(sha256_file(path), path.stat().st_size)


def _read_tail(path: Path, limit: int = 8192) -> str:
    with path.open("rb") as stream:
        size = path.stat().st_size
        if size > limit:
            stream.seek(size - limit)
        return stream.read(limit).decode("utf-8", errors="replace")


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except psutil.Error:
                pass
        try:
            parent.terminate()
        except psutil.Error:
            pass
        _, alive = psutil.wait_procs([*children, parent], timeout=1.5)
        for item in alive:
            try:
                item.kill()
            except psutil.Error:
                pass
    except psutil.Error:
        process.kill()


def run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: dict[str, str] | None,
    stdin_path: Path | None,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: float,
    privacy_roots: Sequence[Path],
    allow_nonzero_exit: bool = False,
    cancellation_event: threading.Event | None = None,
) -> ProcessResult:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    stdin_stream: BinaryIO | None = None
    started = time.monotonic_ns()
    status: ProcessStatus = "error"
    exit_code: int | None = None
    diagnostic: str | None = None
    try:
        if stdin_path is not None:
            stdin_stream = stdin_path.open("rb")
        with stdout_path.open("wb") as stdout_stream, stderr_path.open("wb") as stderr_stream:
            process: subprocess.Popen[bytes] | None = None
            if cancellation_event is not None and cancellation_event.is_set():
                status = "cancelled"
                diagnostic = "process cancelled by benchmark scheduler"
            else:
                try:
                    process = subprocess.Popen(
                        list(command),
                        cwd=cwd,
                        env=environment,
                        stdin=stdin_stream if stdin_stream is not None else subprocess.DEVNULL,
                        stdout=stdout_stream,
                        stderr=stderr_stream,
                        shell=False,
                    )
                except OSError as exc:
                    diagnostic = sanitize_text(f"process start failed: {exc}", privacy_roots)
                    process = None
            if process is not None:
                deadline = time.monotonic() + timeout_seconds
                while True:
                    if cancellation_event is not None and cancellation_event.is_set():
                        _terminate_process_tree(process)
                        process.wait(timeout=5)
                        status = "cancelled"
                        diagnostic = "process cancelled by benchmark scheduler"
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        _terminate_process_tree(process)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        status = "timeout"
                        diagnostic = f"process exceeded {timeout_seconds:g} seconds"
                        break
                    try:
                        exit_code = process.wait(timeout=min(0.1, remaining))
                    except subprocess.TimeoutExpired:
                        continue
                    status = "ok" if exit_code == 0 or allow_nonzero_exit else "error"
                    break
    finally:
        if stdin_stream is not None:
            stdin_stream.close()
    duration = max(0, time.monotonic_ns() - started)
    stdout_path.touch(exist_ok=True)
    stderr_path.touch(exist_ok=True)
    if status == "error" and diagnostic is None:
        tail = _read_tail(stderr_path)
        diagnostic = sanitize_text(
            f"process exited with code {exit_code}" + (f": {tail}" if tail else ""),
            privacy_roots,
        )
    elif diagnostic is not None:
        diagnostic = sanitize_text(diagnostic, privacy_roots)
    return ProcessResult(status, duration, exit_code, _digest(stdout_path), _digest(stderr_path), diagnostic)


def first_mismatch_offset(expected: Path, actual: Path, chunk_size: int = 1024 * 1024) -> int | None:
    offset = 0
    with expected.open("rb") as expected_stream, actual.open("rb") as actual_stream:
        while True:
            left = expected_stream.read(chunk_size)
            right = actual_stream.read(chunk_size)
            if left == right:
                if not left:
                    return None
                offset += len(left)
                continue
            common = min(len(left), len(right))
            for index in range(common):
                if left[index] != right[index]:
                    return offset + index
            return offset + common


def extract_metric(path: Path, pattern: re.Pattern[str], *, allow_zero: bool = False) -> float:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ExecutionError("configured metric file/output is missing or unreadable") from exc
    matches = list(pattern.finditer(text))
    if not matches:
        raise ExecutionError("configured metric pattern did not match process output")
    match = matches[-1]
    if "value" in pattern.groupindex:
        raw = match.group("value")
    elif match.lastindex:
        raw = match.group(1)
    else:
        raw = match.group(0)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ExecutionError("configured metric pattern did not capture a number") from exc
    minimum_ok = value >= 0 if allow_zero else value > 0
    if not minimum_ok or not (value < float("inf")):
        qualifier = "non-negative" if allow_zero else "greater than zero"
        raise ExecutionError(f"captured metric must be finite and {qualifier}")
    return value
