from __future__ import annotations

import json
import os
import re
import shlex
import shutil
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
        "TARGETLAB_MEASUREMENT_MODE": config["measurement_mode"],
    })
    if backend == "baremetal":
        environment["TARGETLAB_STARTUP"] = config["startup"]
        environment["TARGETLAB_LINKER"] = config["linker"]
    run_checked(["make", "-f", "tools/targetlab/target/Makefile", "clean", "all"], cwd=root,
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
        mailbox = temporary_path / "mailbox.bin"
        preserved_mailbox = output.with_suffix(output.suffix + ".mailbox.bin").resolve()
        script = temporary_path / "collect.gdb"
        server_config = config["debug_server"]
        commands = [f"file {_gdb_path(executable)}",
            f"target extended-remote {config['gdb_remote']}"]
        if server_config["kind"] == "openocd":
            commands.extend(("monitor reset halt", "load"))
        commands.extend(("break targetlab_done", "continue",
            f"dump binary memory {_gdb_path(mailbox)} &targetlab_mailbox (&targetlab_mailbox + 1)",
            "quit", ""))
        script.write_text("\n".join(commands), encoding="utf-8")
        server = None
        log_stream = None
        try:
            if server_config["mode"] == "managed":
                log_path = output.with_suffix(output.suffix + f".{server_config['kind']}.log")
                log_stream = log_path.open("wb")
                server = subprocess.Popen(_debug_server_command(server_config, executable,
                    config["gdb_remote"]),
                    cwd=root, stdout=log_stream, stderr=subprocess.STDOUT)
                _wait_for_gdb_server(config["gdb_remote"], server,
                    min(30, config["timeout_seconds"]))
            try:
                run_checked([config["gdb"], "-batch", "-x", str(script)], cwd=root,
                    timeout=config["timeout_seconds"])
            finally:
                if mailbox.exists():
                    shutil.copyfile(mailbox, preserved_mailbox)
            decode_mailbox(preserved_mailbox, output)
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
    rendered = path.as_posix()
    if any(character in rendered for character in "\r\n\0"):
        raise RuntimeError("GDB path contains a forbidden control character")
    return rendered.replace("\\", "\\\\").replace(" ", "\\ ").replace("\t", "\\\t")


def _debug_server_command(server, executable, remote):
    if server["kind"] == "openocd":
        return [server["executable"], "-f", server["config"]]
    host, port = _split_remote(remote)
    return [server["executable"], "-machine", server["machine"], "-m", server["memory"],
        "-bios", "none", "-kernel", str(executable), "-S", "-gdb", f"tcp:{host}:{port}",
        "-icount", "shift=0,align=off,sleep=off",
        "-display", "none", "-monitor", "none", "-serial", "none"]


def _wait_for_gdb_server(remote, process, timeout):
    host, port = _split_remote(remote)
    connect_host = "localhost" if host in {"", "0.0.0.0", "::"} else host
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"debug server exited before GDB became ready ({process.returncode})")
        try:
            with socket.create_connection((connect_host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"debug server GDB endpoint did not become ready within {timeout} seconds")


def _split_remote(remote):
    if not isinstance(remote, str) or ":" not in remote:
        raise RuntimeError("gdb_remote must use HOST:PORT")
    host, port_text = remote.rsplit(":", 1)
    try:
        port = int(port_text)
    except ValueError as exception:
        raise RuntimeError("gdb_remote port is invalid") from exception
    if not 1 <= port <= 65535:
        raise RuntimeError("gdb_remote port is out of range")
    return host or "localhost", port


def decode_mailbox(mailbox: Path, output: Path):
    data = mailbox.read_bytes()
    if len(data) < 96:
        raise RuntimeError("TargetLab mailbox is truncated")
    (magic, version, status, total_length, count, flags, reserved, clock_hz,
        minimum_cycles, warmup_count, configured_samples, measurement_mode, reserved2, failure_sample,
        failure_baseline, failure_measured) = struct.unpack_from("<QIIQQIIQQIIIIQQQ", data)
    if magic != MAILBOX_MAGIC or version != MAILBOX_VERSION:
        raise RuntimeError("TargetLab mailbox has an unsupported identity")
    if status != 1:
        raise RuntimeError(f"TargetLab target reported status {status} after {count} metrics; "
            f"sample={failure_sample} baseline={failure_baseline} measured={failure_measured}")
    if reserved != 0 or reserved2 != 0 or total_length != 96 + count * 344 or total_length > len(data):
        raise RuntimeError("TargetLab mailbox length or reserved fields are invalid")
    if flags & ~3:
        raise RuntimeError("TargetLab mailbox contains unknown counter capability flags")
    if not flags & 1:
        raise RuntimeError("TargetLab bare-metal run did not prove rdcycle availability")
    if clock_hz <= 0 or minimum_cycles <= 0 or warmup_count != 2 or configured_samples != 9:
        raise RuntimeError("TargetLab mailbox measurement configuration is invalid")
    if failure_sample != (1 << 64) - 1 or failure_baseline != 0 or failure_measured != 0:
        raise RuntimeError("successful TargetLab mailbox retains failure diagnostics")
    if measurement_mode not in {0, 1}:
        raise RuntimeError("TargetLab mailbox measurement mode is invalid")
    offset = 96
    timer = "rdcycle" if flags & 1 else "unavailable"
    lines = [json.dumps({"kind": "environment", "backend": "baremetal",
        "rdcycle": bool(flags & 1), "rdinstret": bool(flags & 2), "timer": timer,
        "clock_hz": clock_hz, "minimum_cycles": minimum_cycles,
        "warmup_count": warmup_count, "sample_count": configured_samples,
        "measurement_mode": "qemu_proxy" if measurement_mode == 1 else "hardware"},
        separators=(",", ":"))]
    for _ in range(count):
        if offset + 344 > len(data):
            raise RuntimeError("TargetLab mailbox entry is truncated")
        name, category, source, sample_count, iterations, normalization = struct.unpack_from(
            "<48s32s24sQQQ", data, offset)
        offset += 128
        if sample_count != 9:
            raise RuntimeError("TargetLab mailbox sample count must be exactly 9")
        if iterations <= 0 or normalization <= 0:
            raise RuntimeError("TargetLab mailbox iteration metadata is invalid")
        baseline_values = list(struct.unpack_from("<9Q", data, offset))
        measured_values = list(struct.unpack_from("<9Q", data, offset + 72))
        values = list(struct.unpack_from("<9Q", data, offset + 144))
        offset += 216
        lines.append(json.dumps({"kind": "sample",
            "metric": _mailbox_text(name, "metric"),
            "category": _mailbox_text(category, "category"),
            "source": _mailbox_text(source, "source"),
            "iterations": iterations,
            "normalization": normalization,
            "baseline_values": baseline_values,
            "measured_values": measured_values,
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
