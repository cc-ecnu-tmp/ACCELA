from __future__ import annotations

import json
import os
import re
import shlex
import socket
import struct
import subprocess
import tempfile
import time
from pathlib import Path

MAILBOX_MAGIC = 0x414343454C414D42
MAILBOX_VERSION = 1


def run_checked(command, *, cwd=None, env=None, stdout=None, timeout=None, capture_output=False):
    try:
        completed = subprocess.run(command, cwd=cwd, env=env,
            stdout=subprocess.PIPE if capture_output else stdout,
            stderr=subprocess.PIPE, text=capture_output or stdout is None, timeout=timeout)
    except subprocess.TimeoutExpired as exception:
        raise RuntimeError(f"command timed out after {timeout} seconds: {shlex.join(command)}") from exception
    if completed.returncode != 0:
        detail = completed.stderr.strip() if completed.stderr else "no diagnostic"
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        raise RuntimeError(f"command failed ({completed.returncode}): {shlex.join(command)}\n{detail}")
    return completed


def build(config, root: Path):
    backend = config["backend"]
    environment = os.environ.copy()
    environment.update({
        "TARGETLAB_BACKEND": backend,
        "TARGETLAB_CC": config["cc"],
        "TARGETLAB_OBJCOPY": config["objcopy"],
        "TARGETLAB_BUILD_DIR": str(root / config["build_dir"]),
        "TARGETLAB_CLOCK_HZ": str(config["clock_hz"]),
        "TARGETLAB_MINIMUM_CYCLES": str(config["minimum_cycles"]),
    })
    if backend == "baremetal":
        environment["TARGETLAB_STARTUP"] = config["startup"]
        environment["TARGETLAB_LINKER"] = config["linker"]
    run_checked(["make", "-f", "tools/targetlab/target/Makefile", "all"], cwd=root,
        env=environment, timeout=config["timeout_seconds"])
    _verify_elf(config, root)


def _verify_elf(config, root):
    executable = root / config["build_dir"] / "targetlab.elf"
    completed = run_checked([config["nm"], "-S", str(executable)], cwd=root,
        timeout=config["timeout_seconds"], capture_output=True)
    symbols = {}
    for line in completed.stdout.splitlines():
        match = re.search(r"\b([0-9a-fA-F]+)\s+[A-Za-z]\s+(\S+)$", line.strip())
        if match:
            symbols[match.group(2)] = int(match.group(1), 16)
    for size in (64, 256, 1024):
        symbol = f"targetlab_frontend_{size}"
        if symbols.get(symbol) != size:
            raise RuntimeError(f"{symbol} has size {symbols.get(symbol)!r}, expected {size}")
    for symbol in ("targetlab_mailbox", "targetlab_done"):
        if symbol not in symbols:
            raise RuntimeError(f"TargetLab ELF misses required symbol {symbol}")


def run_linux(config, root: Path, output: Path):
    executable = root / config["build_dir"] / "targetlab.elf"
    command = shlex.split(config["execute"]) + [str(executable)]
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as stream:
        run_checked(command, cwd=root, stdout=stream, timeout=config["timeout_seconds"])


def run_baremetal(config, root: Path, output: Path):
    executable = (root / config["build_dir"] / "targetlab.elf").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="targetlab-") as temporary:
        temporary_path = Path(temporary)
        mailbox = output.with_suffix(output.suffix + ".mailbox.bin").resolve()
        script = temporary_path / "collect.gdb"
        script.write_text("\n".join((
            f"target extended-remote {config['gdb_remote']}",
            "monitor reset halt",
            f"file {_gdb_path(executable)}",
            "load",
            "break targetlab_done",
            "continue",
            f"dump binary memory {_gdb_path(mailbox)} &targetlab_mailbox (&targetlab_mailbox + 1)",
            "quit",
            "",
        )), encoding="utf-8")
        server = None
        log_stream = None
        try:
            if config["openocd_mode"] == "managed":
                log_path = output.with_suffix(output.suffix + ".openocd.log")
                log_stream = log_path.open("wb")
                server = subprocess.Popen([config["openocd"], "-f", config["openocd_config"]],
                    cwd=root, stdout=log_stream, stderr=subprocess.STDOUT)
                _wait_for_gdb_server(config["gdb_remote"], server,
                    min(30, config["timeout_seconds"]))
            run_checked([config["gdb"], "-batch", "-x", str(script)], cwd=root,
                timeout=config["timeout_seconds"])
            decode_mailbox(mailbox, output)
        finally:
            if server is not None:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
            if log_stream is not None:
                log_stream.close()


def _gdb_path(path: Path):
    return '"' + path.as_posix().replace('"', '\\"') + '"'


def _wait_for_gdb_server(remote, process, timeout):
    if ":" not in remote:
        raise RuntimeError("gdb_remote must use HOST:PORT")
    host, port_text = remote.rsplit(":", 1)
    host = host or "localhost"
    try:
        port = int(port_text)
    except ValueError as exception:
        raise RuntimeError("gdb_remote port is invalid") from exception
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"OpenOCD exited before GDB became ready ({process.returncode})")
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"OpenOCD GDB server did not become ready within {timeout} seconds")


def decode_mailbox(mailbox: Path, output: Path):
    data = mailbox.read_bytes()
    if len(data) < 40:
        raise RuntimeError("TargetLab mailbox is truncated")
    magic, version, status, total_length, count, flags, reserved = struct.unpack_from("<QIIQQII", data)
    if magic != MAILBOX_MAGIC or version != MAILBOX_VERSION:
        raise RuntimeError("TargetLab mailbox has an unsupported identity")
    if status != 1:
        raise RuntimeError(f"TargetLab target reported status {status}")
    if reserved != 0 or total_length != 40 + count * 168 or total_length > len(data):
        raise RuntimeError("TargetLab mailbox length or reserved fields are invalid")
    if flags & ~3:
        raise RuntimeError("TargetLab mailbox contains unknown counter capability flags")
    if not flags & 1:
        raise RuntimeError("TargetLab bare-metal run did not prove rdcycle availability")
    offset = 40
    timer = "rdcycle" if flags & 1 else "unavailable"
    lines = [json.dumps({"kind": "environment", "backend": "baremetal",
        "rdcycle": bool(flags & 1), "rdinstret": bool(flags & 2), "timer": timer},
        separators=(",", ":"))]
    for _ in range(count):
        if offset + 168 > len(data):
            raise RuntimeError("TargetLab mailbox entry is truncated")
        name, category, source, sample_count = struct.unpack_from("<48s16s24sQ", data, offset)
        offset += 96
        if sample_count != 9:
            raise RuntimeError("TargetLab mailbox sample count must be exactly 9")
        values = list(struct.unpack_from(f"<{sample_count}Q", data, offset))
        offset += 72
        lines.append(json.dumps({"kind": "sample",
            "metric": _mailbox_text(name, "metric"),
            "category": _mailbox_text(category, "category"),
            "source": _mailbox_text(source, "source"),
            "values": values,
        }, separators=(",", ":")))
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mailbox_text(raw, name):
    terminator = raw.find(b"\0")
    if terminator <= 0:
        raise RuntimeError(f"TargetLab mailbox {name} is empty or unterminated")
    try:
        return raw[:terminator].decode("ascii")
    except UnicodeDecodeError as exception:
        raise RuntimeError(f"TargetLab mailbox {name} is not ASCII") from exception
