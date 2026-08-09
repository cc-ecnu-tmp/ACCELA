from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "tools" / "qemu" / "runtime.c"
CRT = ROOT / "tools" / "qemu" / "crt.S"
LINKER = ROOT / "tools" / "qemu" / "linker.ld"
CORRECTNESS_RUNNER = ROOT / "scripts" / "benchmark-qemu-correctness.sh"


def _required_command(name: str) -> str:
    command = shutil.which(name)
    if command is None:
        pytest.skip(f"QEMU runtime integration requires {name}")
    return command


def _compile_program(tmp_path: Path, source_text: str) -> Path:
    compiler = _required_command(os.environ.get("RISCV_GCC", "riscv64-elf-gcc"))
    source = tmp_path / "program.c"
    binary = tmp_path / "program.elf"
    source.write_text(source_text, encoding="utf-8", newline="\n")
    result = subprocess.run(
        [
            compiler,
            "-march=rv64gc",
            "-mabi=lp64d",
            "-mcmodel=medany",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-ffreestanding",
            "-fno-builtin",
            "-nostdlib",
            "-nostartfiles",
            f"-Wl,-T,{LINKER}",
            str(CRT),
            str(RUNTIME),
            str(source),
            "-lgcc",
            "-o",
            str(binary),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return binary


def _invoke_program(
    binary: Path, input_path: Path, *, timeout: float = 30
) -> subprocess.CompletedProcess[bytes]:
    _required_command("qemu-system-riscv64")
    return subprocess.run(
        ["sh", str(CORRECTNESS_RUNNER), str(binary), str(input_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _run_program(binary: Path, input_path: Path, *, timeout: float = 30) -> bytes:
    result = _invoke_program(binary, input_path, timeout=timeout)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout


def _write_input(tmp_path: Path, data: bytes, name: str = "program.in") -> Path:
    input_path = tmp_path / name
    input_path.write_bytes(data)
    return input_path


def _build_qemu_plugins(tmp_path: Path) -> Path:
    _required_command("qemu-system-riscv64")
    _required_command("cc")
    _required_command("pkg-config")
    if not any((candidate / "qemu-plugin.h").is_file() for candidate in (
        Path("/usr/include"), Path("/usr/local/include"), Path("/opt/homebrew/include")
    )):
        pytest.skip("QEMU plugin integration requires qemu-plugin.h")
    plugin_dir = tmp_path / "plugins"
    build = subprocess.run(
        ["sh", str(ROOT / "scripts" / "build-qemu-plugins.sh"), str(plugin_dir)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    assert build.returncode == 0, build.stderr.decode("utf-8", errors="replace")
    return plugin_dir


def _run_metrics(
    binary: Path, input_path: Path, plugin_dir: Path, metric_path: Path
) -> str:
    environment = dict(os.environ)
    environment["QEMU_PLUGIN_DIR"] = str(plugin_dir)
    result = subprocess.run(
        ["sh", str(ROOT / "scripts" / "benchmark-qemu.sh"),
         str(binary), str(metric_path), str(input_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return metric_path.read_text(encoding="utf-8")


def _profile_counts(metrics: str) -> tuple[int, int, int]:
    profile = re.search(
        r"(?m)^instructions=(\d+) loads=(\d+) stores=(\d+)$", metrics
    )
    assert profile is not None, metrics
    return tuple(map(int, profile.groups()))


INTEGER_PROBE = r"""
int getint(void);
int getch(void);
void putint(int value);
void putch(int value);

int main(void) {
  int value = getint();
  int delimiter = getch();
  int eof = getch();
  putint(value);
  putch(' ');
  putint(delimiter);
  putch(' ');
  putint(eof);
  putch('\n');
  return 0;
}
"""


def test_integer_at_eof_and_one_byte_pushback(tmp_path: Path) -> None:
    binary = _compile_program(tmp_path, INTEGER_PROBE)
    assert _run_program(binary, _write_input(tmp_path, b"42X")) == b"42 88 -1\n0\n"


def test_integer_without_trailing_delimiter_terminates_at_eof(tmp_path: Path) -> None:
    binary = _compile_program(tmp_path, INTEGER_PROBE)
    assert _run_program(binary, _write_input(tmp_path, b"99")) == b"99 -1 -1\n0\n"


def test_integer_accepts_leading_plus(tmp_path: Path) -> None:
    binary = _compile_program(tmp_path, INTEGER_PROBE)
    assert _run_program(binary, _write_input(tmp_path, b"+17!")) == b"17 33 -1\n0\n"


def test_empty_input_reports_eof(tmp_path: Path) -> None:
    binary = _compile_program(
        tmp_path,
        r"""
int getch(void);
void putint(int value);
void putch(int value);
int main(void) { putint(getch()); putch('\n'); return 0; }
""",
    )
    assert _run_program(binary, _write_input(tmp_path, b"")) == b"-1\n0\n"


def test_float_without_trailing_delimiter_terminates_at_eof(tmp_path: Path) -> None:
    binary = _compile_program(
        tmp_path,
        r"""
float getfloat(void);
int getch(void);
void putfloat(float value);
void putch(int value);
void putint(int value);
int main(void) {
  putfloat(getfloat());
  putch(' ');
  putint(getch());
  putch('\n');
  return 0;
}
""",
    )
    input_path = _write_input(tmp_path, b"-0x1.8p+1")
    assert _run_program(binary, input_path) == b"-0x1.8p+1 -1\n0\n"


def test_multi_chunk_binary_input_is_exact(tmp_path: Path) -> None:
    binary = _compile_program(
        tmp_path,
        r"""
int getch(void);
void putint(int value);
void putch(int value);
int main(void) {
  unsigned count = 0;
  unsigned hash = 2166136261U;
  int ch;
  while ((ch = getch()) >= 0) {
    hash = (hash ^ (unsigned) ch) * 16777619U;
    count++;
  }
  putint((int) count);
  putch(' ');
  putint((int) hash);
  putch('\n');
  return 0;
}
""",
    )
    data = bytes(range(256)) * (16384 + 1) + b"exact-tail"
    expected_hash = 2166136261
    for value in data:
        expected_hash = ((expected_hash ^ value) * 16777619) & 0xFFFFFFFF
    signed_hash = expected_hash if expected_hash < 0x80000000 else expected_hash - 0x100000000

    start = time.monotonic()
    output = _run_program(binary, _write_input(tmp_path, data), timeout=120)
    elapsed = time.monotonic() - start
    assert output == f"{len(data)} {signed_hash}\n0\n".encode()
    assert elapsed < 120


def test_transport_section_is_fixed_nobits_aw_and_helpers_are_named(tmp_path: Path) -> None:
    binary = _compile_program(tmp_path, INTEGER_PROBE)
    readelf = _required_command(os.environ.get("RISCV_READELF", "riscv64-elf-readelf"))
    sections = subprocess.run(
        [readelf, "--wide", "--sections", str(binary)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=True,
        text=True,
    ).stdout
    match = re.search(
        r"(?m)^\s*\[\s*\d+\]\s+\.sysy_input_transport\s+NOBITS\s+"
        r"[0-9a-f]+\s+[0-9a-f]+\s+001010\s+[0-9a-f]+\s+WA\s+",
        sections,
        re.IGNORECASE,
    )
    assert match is not None, sections

    symbols = subprocess.run(
        [readelf, "--wide", "--symbols", str(binary)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=True,
        text=True,
    ).stdout
    helpers = (
        "__accela_input_to_be32",
        "__accela_input_to_be64",
        "__accela_input_fail",
        "__accela_input_dma_read",
        "__accela_input_initialize",
        "__accela_input_getc",
    )
    input_function_symbols = {
        line.split()[-1]
        for line in symbols.splitlines()
        if " FUNC " in line and "__accela_input_" in line
    }
    assert input_function_symbols == set(helpers)
    for helper in helpers:
        assert re.search(
            rf"(?m)^\s*\d+:\s+[0-9a-f]+\s+\d+\s+FUNC\s+GLOBAL\s+HIDDEN\s+\d+\s+{helper}$",
            symbols,
            re.IGNORECASE,
        )


def test_runtime_filter_excludes_all_input_helper_symbols(tmp_path: Path) -> None:
    compiler = _required_command("cc")
    source = tmp_path / "filter-test.c"
    executable = tmp_path / "filter-test"
    source.write_text(
        r"""
#include <stdbool.h>
#include <stddef.h>
#include <string.h>

struct qemu_plugin_insn { const char *symbol; };
static const char *qemu_plugin_insn_symbol(const struct qemu_plugin_insn *insn) {
  return insn->symbol;
}
static int g_strcmp0(const char *left, const char *right) {
  if (left == NULL) return right == NULL ? 0 : -1;
  if (right == NULL) return 1;
  return strcmp(left, right);
}
static bool g_str_has_prefix(const char *text, const char *prefix) {
  return strncmp(text, prefix, strlen(prefix)) == 0;
}
#include "tools/qemu/runtime-filter.h"

int main(void) {
  const struct qemu_plugin_insn exact = {"__accela_input_dma_read"};
  const struct qemu_plugin_insn optimized = {"__accela_input_initialize.constprop.0"};
  const struct qemu_plugin_insn public_api = {"getint"};
  const struct qemu_plugin_insn near_miss = {"__accela_inputs_not_runtime"};
  const struct qemu_plugin_insn helper_suffix_miss = {"__accela_input_getc_user"};
  const struct qemu_plugin_insn user_prefix = {"__accela_input_user_work"};
  const struct qemu_plugin_insn user = {"main"};
  return !(is_io_runtime(&exact)
           && is_io_runtime(&optimized)
           && is_io_runtime(&public_api)
           && !is_io_runtime(&near_miss)
           && !is_io_runtime(&helper_suffix_miss)
           && !is_io_runtime(&user_prefix)
           && !is_io_runtime(&user));
}
""",
        encoding="utf-8",
        newline="\n",
    )
    subprocess.run(
        [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", f"-I{ROOT}",
         str(source), "-o", str(executable)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=True,
    )
    subprocess.run([str(executable)], timeout=30, check=True)


def test_plugins_exclude_fw_cfg_runtime_from_explicit_region(tmp_path: Path) -> None:
    binary = _compile_program(
        tmp_path,
        r"""
int getint(void);
void putint(int value);
void putch(int value);
void _sysy_starttime(int line);
void _sysy_stoptime(int line);
int main(void) {
  _sysy_starttime(1);
  int value = getint();
  _sysy_stoptime(1);
  putint(value);
  putch('\n');
  return 0;
}
""",
    )
    plugin_dir = _build_qemu_plugins(tmp_path)

    metrics: list[str] = []
    for index, contents in enumerate((b"1", b"7" * 4000)):
        input_path = _write_input(tmp_path, contents, name=f"profile-{index}.in")
        metric_path = tmp_path / f"profile-{index}.log"
        metrics.append(_run_metrics(binary, input_path, plugin_dir, metric_path))
    assert metrics[0] == metrics[1]
    assert _profile_counts(metrics[0]) == (4, 0, 0)
    assert re.search(r"(?m)^l1d=32KiB/8-way/64B accesses=\d+", metrics[0])


def test_plugins_count_user_function_with_reserved_prefix(tmp_path: Path) -> None:
    binary = _compile_program(
        tmp_path,
        r"""
void putint(int value);
void putch(int value);
void _sysy_starttime(int line);
void _sysy_stoptime(int line);
__attribute__((noinline)) int __accela_input_user_work(int limit) {
  volatile int total = 0;
  for (int index = 0; index < limit; index++) total += index ^ 0x55;
  return total;
}
int main(void) {
  _sysy_starttime(1);
  int value = __accela_input_user_work(1000);
  _sysy_stoptime(1);
  putint(value);
  putch('\n');
  return 0;
}
""",
    )
    plugin_dir = _build_qemu_plugins(tmp_path)
    metrics = _run_metrics(
        binary,
        _write_input(tmp_path, b""),
        plugin_dir,
        tmp_path / "user-prefix.log",
    )
    instructions, loads, stores = _profile_counts(metrics)
    assert instructions > 1000
    assert loads > 100
    assert stores > 100


def test_plugins_exclude_float_input_and_output_helpers(tmp_path: Path) -> None:
    binary = _compile_program(
        tmp_path,
        r"""
float getfloat(void);
void putfloat(float value);
void _sysy_starttime(int line);
void _sysy_stoptime(int line);
int main(void) {
  _sysy_starttime(1);
  putfloat(getfloat());
  _sysy_stoptime(1);
  return 0;
}
""",
    )
    plugin_dir = _build_qemu_plugins(tmp_path)
    metrics = [
        _run_metrics(
            binary,
            _write_input(tmp_path, contents, name=f"float-{index}.in"),
            plugin_dir,
            tmp_path / f"float-{index}.log",
        )
        for index, contents in enumerate((b"1", b"-0x1.fffffep+100"))
    ]
    assert metrics[0] == metrics[1]
    instructions, loads, stores = _profile_counts(metrics[0])
    assert instructions <= 16
    assert loads == 0
    assert stores == 0


def test_missing_fw_cfg_input_is_observable(tmp_path: Path) -> None:
    qemu = _required_command("qemu-system-riscv64")
    binary = _compile_program(tmp_path, INTEGER_PROBE)
    result = subprocess.run(
        [
            qemu,
            "-machine", "virt",
            "-accel", "tcg,thread=single",
            "-smp", "1",
            "-m", "512M",
            "-bios", "none",
            "-kernel", str(binary),
            "-display", "none",
            "-monitor", "none",
            "-serial", "stdio",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    assert result.returncode == 125
    assert result.stdout == b"ACCELA_INPUT_ERROR missing_input_file\n"


@pytest.mark.parametrize(
    ("contents", "diagnostic"),
    [
        (b"", b"ACCELA_INPUT_ERROR unexpected_eof_getint\n"),
        (b"x", b"ACCELA_INPUT_ERROR invalid_integer\n"),
    ],
)
def test_invalid_integer_fails_fast(
    tmp_path: Path, contents: bytes, diagnostic: bytes
) -> None:
    binary = _compile_program(tmp_path, INTEGER_PROBE)
    result = _invoke_program(binary, _write_input(tmp_path, contents))
    assert result.returncode == 125
    assert result.stdout == diagnostic


def test_runner_rejects_fw_cfg_unsafe_input_path(tmp_path: Path) -> None:
    _required_command("sh")
    binary = tmp_path / "program.elf"
    binary.write_bytes(b"placeholder")
    input_path = _write_input(tmp_path, b"", name="unsafe,input")
    result = subprocess.run(
        ["sh", str(CORRECTNESS_RUNNER), str(binary), str(input_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        text=True,
    )
    assert result.returncode == 2
    assert "unsafe for QEMU -fw_cfg" in result.stderr


def test_runner_rejects_input_larger_than_fw_cfg_size_field(tmp_path: Path) -> None:
    _required_command("sh")
    binary = tmp_path / "program.elf"
    binary.write_bytes(b"placeholder")
    input_path = tmp_path / "too-large.in"
    try:
        with input_path.open("wb") as stream:
            stream.truncate(0x100000000)
    except OSError as exc:
        pytest.skip(f"filesystem cannot create a sparse 4 GiB boundary fixture: {exc}")
    result = subprocess.run(
        ["sh", str(CORRECTNESS_RUNNER), str(binary), str(input_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        text=True,
    )
    assert result.returncode == 2
    assert "fw_cfg 32-bit size limit" in result.stderr
