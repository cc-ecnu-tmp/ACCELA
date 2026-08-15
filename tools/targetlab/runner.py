from __future__ import annotations

import json
import os
import shlex
import struct
import subprocess
import tempfile
from pathlib import Path

MAILBOX_MAGIC = 0x414343454C414D42
MAILBOX_VERSION = 1


def run_checked(command, *, cwd=None, env=None, stdout=None):
    completed = subprocess.run(command, cwd=cwd, env=env, stdout=stdout, stderr=subprocess.PIPE, text=stdout is None)
    if completed.returncode != 0:
        detail = completed.stderr.strip() if completed.stderr else "no diagnostic"
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
    })
    if backend == "baremetal":
        environment["TARGETLAB_STARTUP"] = config["startup"]
        environment["TARGETLAB_LINKER"] = config["linker"]
    run_checked(["make", "-f", "tools/targetlab/target/Makefile", "all"], cwd=root, env=environment)


def run_linux(config, root: Path, output: Path):
    executable = root / config["build_dir"] / "targetlab.elf"
    command = shlex.split(config["execute"]) + [str(executable)]
    with output.open("wb") as stream:
        run_checked(command, cwd=root, stdout=stream)


def run_baremetal(config, root: Path, output: Path):
    executable = (root / config["build_dir"] / "targetlab.elf").resolve()
    with tempfile.TemporaryDirectory(prefix="targetlab-") as temporary:
        temporary_path = Path(temporary)
        mailbox = temporary_path / "mailbox.bin"
        script = temporary_path / "collect.gdb"
        script.write_text("\n".join((
            f"target extended-remote {config['gdb_remote']}",
            "monitor reset halt",
            f"load {executable.as_posix()}",
            "break targetlab_done",
            "continue",
            f"dump binary memory {mailbox.as_posix()} &targetlab_mailbox (&targetlab_mailbox + 1)",
            "quit",
            "",
        )), encoding="utf-8")
        run_checked([config["gdb"], "-batch", "-x", str(script)], cwd=root)
        decode_mailbox(mailbox, output)


def decode_mailbox(mailbox: Path, output: Path):
    data = mailbox.read_bytes()
    if len(data) < 24:
        raise RuntimeError("TargetLab mailbox is truncated")
    magic, version, status, count = struct.unpack_from("<QIIQ", data)
    if magic != MAILBOX_MAGIC or version != MAILBOX_VERSION:
        raise RuntimeError("TargetLab mailbox has an unsupported identity")
    if status != 1:
        raise RuntimeError(f"TargetLab target reported status {status}")
    offset = 24
    lines = []
    for _ in range(count):
        if offset + 168 > len(data):
            raise RuntimeError("TargetLab mailbox entry is truncated")
        name, category, source, sample_count = struct.unpack_from("<48s16s24sQ", data, offset)
        offset += 96
        if sample_count > 9:
            raise RuntimeError("TargetLab mailbox sample count is invalid")
        values = list(struct.unpack_from(f"<{sample_count}Q", data, offset))
        offset += 72
        lines.append(json.dumps({
            "metric": name.split(b"\0", 1)[0].decode(),
            "category": category.split(b"\0", 1)[0].decode(),
            "source": source.split(b"\0", 1)[0].decode(),
            "values": values,
        }, separators=(",", ":")))
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
