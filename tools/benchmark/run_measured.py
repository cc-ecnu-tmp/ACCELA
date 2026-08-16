#!/usr/bin/env python3
"""Run one cold compiler process and record wall time plus wait4 peak RSS."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import time


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        raise SystemExit("usage: run_measured.py OUTPUT.json -- COMMAND [ARG ...]")
    output = Path(sys.argv[1])
    command = sys.argv[3:]
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            os.execvp(command[0], command)
        except BaseException as exception:
            print(f"failed to execute measured command: {exception}", file=sys.stderr)
            os._exit(127)
    def forward(signum, _frame):
        try:
            os.killpg(pid, signum)
        except ProcessLookupError:
            pass
    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, forward)
    _, status, usage = os.wait4(pid, 0)
    elapsed = time.perf_counter() - started
    peak = int(usage.ru_maxrss)
    if sys.platform != "darwin":
        peak *= 1024
    if os.WIFEXITED(status):
        exit_code = os.WEXITSTATUS(status)
    elif os.WIFSIGNALED(status):
        exit_code = 128 + os.WTERMSIG(status)
    else:
        exit_code = 1
    document = {
        "schema_version": 1,
        "elapsed_seconds": elapsed,
        "peak_bytes": peak,
        "exit_code": exit_code,
    }
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
