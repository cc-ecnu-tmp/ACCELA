from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.benchmark import binary_analyzer
from tools.benchmark.binary_analyzer import analyze_binary
from tools.benchmark.errors import ValidationError
from tools.benchmark.schema import validate_document
from tools.benchmark.util import atomic_write_text


READELF = """
  Class:                             ELF64
  Machine:                           RISC-V
  Flags:                             0x5, RVC, double-float ABI
  Tag_RISCV_arch: "rv64i2p1_m2p0_a2p1_f2p2_d2p2_c2p0_zicsr2p0_zifencei2p0_zmmul1p0_zaamo1p0_zalrsc1p0_zca1p0_zcd1p0"
  [ 1] .text             PROGBITS        0000000000010000 001000 000040 00  AX  0   0  4
  [ 2] .rodata           PROGBITS        0000000000010040 001040 000010 00   A  0   0  8
  [ 3] .data             PROGBITS        0000000000010050 001050 000008 00  WA  0   0  8
  [ 4] .bss              NOBITS          0000000000010058 001058 000020 00  WA  0   0  8
  [ 5] .srodata          PROGBITS        0000000000010078 001078 000004 00   A  0   0  4
  [ 6] .sdata            PROGBITS        000000000001007c 00107c 000004 00  WA  0   0  4
  [ 7] .sbss             NOBITS          0000000000010080 001080 000008 00  WA  0   0  8
"""

OBJDUMP = """
0000000000010000 <main>:
   10000:  1101                 addi sp,sp,-32
   10002:  e406                 sd ra,8(sp)
   10004:  4502                 lw a0,0(sp)
   10006:  00b50533             add a0,a0,a1
   1000a:  00058563             beqz a1,10014
   1000e:  02b57553             fadd.s fa0,fa0,fa1
   10012:  02b50533             mul a0,a0,a1
   10016:  8082                 ret
   10018:  0000100f             fence.i
   1001c:  4482                 lwsp s1,0(sp)
   1001e:  c026                 swsp s1,0(sp)
   10020:  7139                 c.addi16sp sp,-64
   10022:  117d                 c.addi sp,-1
"""


def test_binary_analyzer_emits_sections_instruction_classes_and_normalized_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    binary = tmp_path / "program.elf"
    binary.write_bytes(b"ELF")

    def fake_run(command, *, label, timeout_seconds):
        assert timeout_seconds == 5
        return READELF if label == "readelf" else OBJDUMP

    monkeypatch.setattr(binary_analyzer, "_run_tool", fake_run)
    result = analyze_binary(binary=binary, toolchain="gcc", timeout_seconds=5)
    validate_document(result)
    by_id = {item["metric_id"]: item for item in result["measurements"]}
    assert by_id["elf_text_bytes"]["value"] == 64
    assert by_id["elf_rodata_bytes"]["value"] == 20
    assert by_id["elf_data_bytes"]["value"] == 12
    assert by_id["elf_bss_bytes"]["value"] == 40
    assert by_id["static_total_instructions"]["value"] == 13
    assert by_id["static_load_instructions"]["value"] == 2
    assert by_id["static_store_instructions"]["value"] == 2
    assert by_id["static_floating_point_instructions"]["value"] == 1
    assert by_id["static_vector_instructions"]["value"] == 0
    assert by_id["stack_frame_bytes"]["value"] == 64
    assert by_id["spill_count"] == {
        "metric_id": "spill_count",
        "value": None,
        "unit": "instructions",
        "availability": "unavailable",
        "reason": "not_supported_by_toolchain",
    }


def test_accela_analyzer_aggregates_remark_spills_and_allows_same_count_changed(
    tmp_path: Path, monkeypatch
) -> None:
    binary = tmp_path / "program.elf"
    binary.write_bytes(b"ELF")
    events = [
        {
            "schema_version": "optimization-remark.v1",
            "sequence": 1,
            "event_type": "pass_summary",
            "pass": "backend.regalloc",
            "occurrence": 1,
            "stage": "backend_function",
            "target_kind": "function",
            "target_name": "main",
            "elapsed_ns": 20,
            "changed": True,
            "before": {"instructions": 8, "spill_stores": 1, "spill_reloads": 1},
            "after": {"instructions": 8, "spill_stores": 3, "spill_reloads": 2},
            "delta": {"spill_stores": 2, "spill_reloads": 1},
            "details": {},
            "decision_observability": "available",
        },
        {
            "schema_version": "optimization-remark.v1",
            "sequence": 2,
            "event_type": "pass_summary",
            "pass": "backend.regalloc",
            "occurrence": 1,
            "stage": "backend_function",
            "target_kind": "function",
            "target_name": "helper",
            "elapsed_ns": 10,
            "changed": True,
            "before": {"spill_stores": 0, "spill_reloads": 0},
            "after": {"spill_stores": 4, "spill_reloads": 4},
            "delta": {"spill_stores": 4, "spill_reloads": 4},
            "details": {},
            "decision_observability": "available",
        },
        {
            "schema_version": "optimization-remark.v1",
            "sequence": 3,
            "event_type": "pass_summary",
            "pass": "machine.emit",
            "occurrence": 1,
            "stage": "backend_module",
            "target_kind": "module",
            "target_name": "<module>",
            "elapsed_ns": 10,
            "changed": False,
            "before": {"spill_stores": 7, "spill_reloads": 6},
            "after": {"spill_stores": 7, "spill_reloads": 6},
            "delta": {},
            "details": {},
            "decision_observability": "available",
        },
    ]
    remarks = tmp_path / "remarks.jsonl"
    atomic_write_text(remarks, "".join(json.dumps(event) + "\n" for event in events))
    monkeypatch.setattr(
        binary_analyzer,
        "_run_tool",
        lambda command, *, label, timeout_seconds: READELF if label == "readelf" else OBJDUMP,
    )
    result = analyze_binary(binary=binary, toolchain="accela", remarks_path=remarks)
    by_id = {item["metric_id"]: item for item in result["measurements"]}
    # The final module snapshot is already an aggregate; function snapshots
    # must not be added again.
    assert by_id["spill_count"]["value"] == 7
    assert by_id["reload_count"]["value"] == 6


def test_binary_analyzer_rejects_non_rv64gc_attributes_and_rvv_instructions(
    tmp_path: Path, monkeypatch
) -> None:
    binary = tmp_path / "program.elf"
    binary.write_bytes(b"ELF")
    vector_objdump = OBJDUMP.replace("mul a0,a0,a1", "vadd.vv v0,v0,v0")
    monkeypatch.setattr(
        binary_analyzer,
        "_run_tool",
        lambda command, *, label, timeout_seconds: READELF if label == "readelf" else vector_objdump,
    )
    with pytest.raises(ValidationError, match="vector/RVV"):
        analyze_binary(binary=binary, toolchain="gcc")
    non_contract = READELF.replace(
        "rv64i2p1_m2p0_a2p1_f2p2_d2p2_c2p0",
        "rv64i2p1_m2p0_a2p1_f2p2_d2p2_c2p0_v1p0",
    )
    monkeypatch.setattr(
        binary_analyzer,
        "_run_tool",
        lambda command, *, label, timeout_seconds: non_contract if label == "readelf" else OBJDUMP,
    )
    with pytest.raises(ValidationError, match="exceeds RV64GC"):
        analyze_binary(binary=binary, toolchain="gcc")


def test_binary_analyzer_accepts_real_toolchain_rv64gc_closure(tmp_path: Path) -> None:
    required_tools = {
        name: shutil.which(name)
        for name in ("riscv64-elf-gcc", "riscv64-elf-readelf", "riscv64-elf-objdump")
    }
    if any(path is None for path in required_tools.values()):
        pytest.skip("RV64GC bare-metal toolchain is not installed")

    source = tmp_path / "minimal.S"
    binary = tmp_path / "minimal.elf"
    source.write_text(
        '.attribute arch, "rv64gc"\n'
        ".text\n"
        ".globl _start\n"
        "_start:\n"
        "  li a0, 0\n"
        "  ret\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            required_tools["riscv64-elf-gcc"],
            "-march=rv64gc",
            "-mabi=lp64d",
            "-mcmodel=medany",
            "-nostdlib",
            "-Wl,-e,_start",
            "-Wl,--no-relax",
            str(source),
            "-o",
            str(binary),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    result = analyze_binary(
        binary=binary,
        toolchain="gcc",
        readelf_command=required_tools["riscv64-elf-readelf"],
        objdump_command=required_tools["riscv64-elf-objdump"],
    )
    by_id = {item["metric_id"]: item for item in result["measurements"]}
    assert by_id["static_total_instructions"]["value"] > 0
    assert by_id["static_vector_instructions"]["value"] == 0
