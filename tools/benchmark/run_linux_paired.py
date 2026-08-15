#!/usr/bin/env python3
"""Cold-compile and run paired ACCELA configurations on an RV64 Linux target."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time


CASE_ID = re.compile(r"[A-Za-z0-9_.-]+")
_ACTIVE_PROCESS = None
_INTERRUPTED_SIGNAL = None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-classes", type=Path, required=True)
    parser.add_argument("--candidate-classes", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--case-list", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--java", default="java")
    parser.add_argument("--gcc", default="gcc")
    parser.add_argument("--size", default="size")
    args = parser.parse_args(argv)
    _validate(args)
    cases = _case_ids(args.case_list)
    partial = args.output.with_name(args.output.name + ".partial")
    if args.output.exists() or partial.exists():
        raise RuntimeError(f"output already exists: {args.output} or {partial}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with partial.open("w", encoding="utf-8", newline="\n") as stream:
        for case_id in cases:
            _run_case(args, case_id, stream)
            stream.flush()
    partial.replace(args.output)
    print(f"paired RV64 Linux comparison: {len(cases)} cases x {args.runs} runs")
    return 0


def _validate(args) -> None:
    for path, name in ((args.baseline_classes, "baseline classes"),
            (args.candidate_classes, "candidate classes"), (args.corpus, "corpus")):
        if not path.is_dir():
            raise RuntimeError(f"{name} directory does not exist: {path}")
    for path, name in ((args.case_list, "case list"), (args.runtime, "SysY runtime")):
        if not path.is_file():
            raise RuntimeError(f"{name} does not exist: {path}")
    if args.runs < 5:
        raise RuntimeError("paired Linux measurement requires at least five runs")
    if args.warmups < 1:
        raise RuntimeError("paired Linux measurement requires at least one warmup")
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        raise RuntimeError("timeout-seconds must be finite and positive")


def _case_ids(path: Path) -> list[str]:
    result = []
    seen = set()
    for line_number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        case_id = raw.strip()
        if not case_id:
            continue
        if not CASE_ID.fullmatch(case_id):
            raise RuntimeError(f"invalid case id at line {line_number}: {case_id!r}")
        if case_id in seen:
            raise RuntimeError(f"duplicate case id: {case_id}")
        seen.add(case_id)
        result.append(case_id)
    if not result:
        raise RuntimeError("case list is empty")
    return result


def _run_case(args, case_id: str, stream) -> None:
    source = args.corpus / f"{case_id}.sy"
    expected_path = args.corpus / f"{case_id}.out"
    input_path = args.corpus / f"{case_id}.in"
    if not source.is_file() or not expected_path.is_file():
        raise RuntimeError(f"case {case_id} is missing source or expected output")
    expected = expected_path.read_bytes()
    input_data = input_path.read_bytes() if input_path.is_file() else b""
    reference_assembly = {}
    for run_index in range(1, args.runs + 1):
        with tempfile.TemporaryDirectory(prefix=f"accela-linux-{case_id}-") as temporary:
            root = Path(temporary)
            artifacts = {}
            for side, classes in (("baseline", args.baseline_classes),
                    ("candidate", args.candidate_classes)):
                assembly = root / f"{side}.s"
                executable = root / side
                metadata = root / f"{side}.compiler.json"
                _checked([sys.executable, str(Path(__file__).with_name("run_measured.py")),
                    str(metadata), "--", args.java, "-cp", str(classes), "Compiler",
                    str(source), "-o", str(assembly)], args.timeout_seconds)
                assembly_bytes = assembly.read_bytes()
                if side in reference_assembly and reference_assembly[side] != assembly_bytes:
                    raise RuntimeError(f"compiler output is nondeterministic for {case_id}/{side}")
                reference_assembly.setdefault(side, assembly_bytes)
                _checked([args.gcc, "-O2", "-march=rv64gc", "-mabi=lp64d",
                    "-mcmodel=medany", str(assembly), str(args.runtime), "-o", str(executable)],
                    args.timeout_seconds)
                artifacts[side] = (executable, _load_metadata(metadata),
                    _text_size(args.size, executable))
            order = ("baseline", "candidate") if run_index % 2 else ("candidate", "baseline")
            for _ in range(args.warmups):
                for side in reversed(order):
                    completed = _execute(artifacts[side][0], input_data, args.timeout_seconds)
                    _verify_output(case_id, side, run_index, expected, completed)
            measured = {}
            for side in order:
                executable = artifacts[side][0]
                started = time.perf_counter_ns()
                completed = _execute(executable, input_data, args.timeout_seconds)
                elapsed = (time.perf_counter_ns() - started) / 1_000_000_000.0
                if elapsed <= 0.0 or not math.isfinite(elapsed):
                    raise RuntimeError(f"invalid runtime for {case_id}/{side}/{run_index}")
                _verify_output(case_id, side, run_index, expected, completed)
                measured[side] = (elapsed, completed.returncode)
            if measured["baseline"][1] != measured["candidate"][1]:
                raise RuntimeError(f"exit status mismatch for {case_id}/{run_index}: "
                    f"{measured['baseline'][1]} != {measured['candidate'][1]}")
            baseline_seconds = measured["baseline"][0]
            candidate_seconds = measured["candidate"][0]
            baseline_metadata = artifacts["baseline"][1]
            candidate_metadata = artifacts["candidate"][1]
            values = (case_id, run_index, baseline_seconds, candidate_seconds,
                baseline_seconds / candidate_seconds,
                baseline_metadata["elapsed_seconds"], candidate_metadata["elapsed_seconds"],
                baseline_metadata["peak_bytes"], candidate_metadata["peak_bytes"],
                artifacts["baseline"][2], artifacts["candidate"][2])
            stream.write("\t".join(_render(value) for value in values) + "\n")


def _checked(command: list[str], timeout: float) -> None:
    completed = _run_process(command, timeout=timeout, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"command failed ({completed.returncode}): {command!r}\n"
            f"{completed.stderr[-2000:]}")


def _execute(executable: Path, input_data: bytes, timeout: float):
    return _run_process([str(executable)], input_data=input_data, timeout=timeout)


def _verify_output(case_id: str, side: str, run_index: int,
        expected: bytes, completed) -> None:
    actual = _judge_output(completed.stdout, completed.returncode)
    if actual != expected:
        raise RuntimeError(f"output mismatch for {case_id}/{side}/{run_index}: "
            f"expected {expected[:200]!r}, got {actual[:200]!r}")


def _load_metadata(path: Path) -> dict:
    document = json.loads(path.read_text(encoding="utf-8"))
    if set(document) != {"schema_version", "elapsed_seconds", "peak_bytes", "exit_code"} \
            or document["schema_version"] != 1 or document["exit_code"] != 0 \
            or not isinstance(document["peak_bytes"], int) or document["peak_bytes"] <= 0 \
            or not isinstance(document["elapsed_seconds"], (int, float)) \
            or document["elapsed_seconds"] <= 0:
        raise RuntimeError(f"invalid compiler metadata: {path}")
    return document


def _text_size(size_command: str, executable: Path) -> int:
    completed = _run_process([size_command, "-A", str(executable)], timeout=30.0, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"size failed for {executable}: {completed.stderr[-2000:]}")
    total = 0
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].startswith(".text"):
            total += int(fields[1])
    if total <= 0:
        raise RuntimeError(f"size reported no .text for {executable}")
    return total


def _render(value) -> str:
    if isinstance(value, float):
        return format(value, ".17g")
    return str(value)


def _judge_output(stdout: bytes, exit_code: int) -> bytes:
    actual = stdout.replace(b"\r\n", b"\n")
    if actual and not actual.endswith(b"\n"):
        actual += b"\n"
    return actual + str(exit_code).encode("ascii") + b"\n"


def _run_process(command: list[str], *, timeout: float, input_data=None, text=False):
    global _ACTIVE_PROCESS, _INTERRUPTED_SIGNAL
    process = subprocess.Popen(command, stdin=subprocess.PIPE if input_data is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text, start_new_session=True)
    _ACTIVE_PROCESS = process
    try:
        try:
            stdout, stderr = process.communicate(input=input_data, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
        if _INTERRUPTED_SIGNAL is not None:
            interrupted = _INTERRUPTED_SIGNAL
            _INTERRUPTED_SIGNAL = None
            raise SystemExit(128 + interrupted)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
        _ACTIVE_PROCESS = None


def _forward_signal(signum, _frame):
    global _INTERRUPTED_SIGNAL
    _INTERRUPTED_SIGNAL = signum
    process = _ACTIVE_PROCESS
    if process is not None and process.poll() is None:
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass
    if process is None or process.poll() is not None:
        raise SystemExit(128 + signum)


if __name__ == "__main__":
    for caught_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(caught_signal, _forward_signal)
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError,
            subprocess.TimeoutExpired) as exception:
        print(f"linux paired benchmark: error: {exception}", file=sys.stderr)
        raise SystemExit(2) from exception
