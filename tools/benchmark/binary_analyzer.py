from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from .errors import BenchmarkError, ExecutionError, ValidationError
from .metrics import ANALYZER_METRICS
from .schema import load_and_validate_jsonl, validate_document
from .util import atomic_write_json, sanitize_text

_SECTION = re.compile(
    r"^\s*\[\s*\d+\]\s+(?P<name>\S+)\s+(?P<type>\S+)\s+"
    r"[0-9A-Fa-f]+\s+[0-9A-Fa-f]+\s+(?P<size>[0-9A-Fa-f]+)\s+"
    r"[0-9A-Fa-f]+\s+(?P<flags>\S*)\s+\d+\s+\d+\s+\d+\s*$"
)
_INSTRUCTION = re.compile(
    r"^\s*[0-9A-Fa-f]+:\s+(?:[0-9A-Fa-f]{2,8}\s+)+(?P<mnemonic>[A-Za-z0-9_.]+)(?:\s+(?P<operands>.*))?$"
)
_STACK_ADJUST_THREE_OPERAND = re.compile(r"^\s*sp\s*,\s*sp\s*,\s*-(?P<size>\d+)\b")
_STACK_ADJUST_COMPRESSED = re.compile(r"^\s*sp\s*,\s*-(?P<size>\d+)\b")
_RISCV_ARCH = re.compile(r"Tag_RISCV_arch:\s*[\"']?(?P<arch>rv64[a-z0-9_]+)", re.IGNORECASE)

# Binutils 2.45 canonicalizes ``-march=rv64gc`` by spelling out the ratified
# subextensions implied by M, A, and C.  These names do not widen the target
# contract: Zmmul is part of M, Zaamo/Zalrsc are the split A extension, and
# Zca/Zcd are the RV64 compressed subsets.  Keep this closure explicit so a
# genuinely wider extension (for example V, Zba, or a vendor X extension) is
# still rejected instead of being accepted by a prefix rule.
_RV64GC_CANONICAL_CLOSURE = {
    "i",
    "m",
    "a",
    "f",
    "d",
    "c",
    "zicsr",
    "zifencei",
    "zmmul",
    "zaamo",
    "zalrsc",
    "zca",
    "zcd",
}


def _validate_elf_contract(text: str) -> None:
    if not re.search(r"(?m)^\s*Class:\s*ELF64\s*$", text):
        raise ValidationError("ELF header is not 64-bit")
    if not re.search(r"(?m)^\s*Machine:\s*RISC-V\s*$", text):
        raise ValidationError("ELF header machine is not RISC-V")
    if not re.search(r"(?mi)^\s*Flags:.*\bdouble-float ABI\b", text):
        raise ValidationError("ELF header does not declare the LP64D double-float ABI")
    match = _RISCV_ARCH.search(text)
    if match is None:
        raise ValidationError("ELF attributes do not declare Tag_RISCV_arch")
    tokens = match.group("arch").lower().removeprefix("rv64").split("_")
    extension_names: set[str] = set()
    for token in tokens:
        name_match = re.match(r"(?P<name>[a-z]+?)(?:\d+(?:p\d+)*)?$", token)
        if name_match is None:
            raise ValidationError("ELF RISC-V architecture attribute is malformed")
        name = name_match.group("name")
        if name == "g":
            extension_names.update({"i", "m", "a", "f", "d"})
        else:
            extension_names.add(name)
    required = {"i", "m", "a", "f", "d", "c"}
    if not required.issubset(extension_names):
        raise ValidationError("ELF architecture does not provide the complete RV64GC contract")
    unsupported = sorted(extension_names - _RV64GC_CANONICAL_CLOSURE)
    if unsupported:
        raise ValidationError("ELF architecture exceeds RV64GC: " + ", ".join(unsupported))


def _run_tool(command: Sequence[str], *, label: str, timeout_seconds: float) -> str:
    try:
        result = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExecutionError(f"{label} failed to execute") from exc
    if result.returncode != 0:
        diagnostic = result.stderr.decode("utf-8", errors="replace")[-2048:]
        raise ExecutionError(f"{label} exited with code {result.returncode}: {sanitize_text(diagnostic)}")
    return result.stdout.decode("utf-8", errors="replace")


def _sections(text: str) -> dict[str, tuple[str, int, frozenset[str]]]:
    sections: dict[str, tuple[str, int, frozenset[str]]] = {}
    for line in text.splitlines():
        match = _SECTION.match(line)
        if match:
            name = match.group("name")
            if name in sections:
                raise ValidationError(f"readelf output repeats section {name}")
            sections[name] = (
                match.group("type"),
                int(match.group("size"), 16),
                frozenset(match.group("flags")),
            )
    if not any("A" in flags and "X" in flags for _, _, flags in sections.values()):
        raise ValidationError("readelf output does not contain an allocated executable section")
    return sections


def _instruction_counts(text: str) -> dict[str, int]:
    counts = {
        "static_total_instructions": 0,
        "static_integer_instructions": 0,
        "static_floating_point_instructions": 0,
        "static_vector_instructions": 0,
        "static_branch_instructions": 0,
        "static_load_instructions": 0,
        "static_store_instructions": 0,
        "stack_frame_bytes": 0,
    }
    load_mnemonics = {
        "lb", "lbu", "lh", "lhu", "lw", "lwu", "ld", "flw", "fld", "flq",
        "lwsp", "ldsp", "flwsp", "fldsp",
    }
    store_mnemonics = {
        "sb", "sh", "sw", "sd", "fsw", "fsd", "fsq", "swsp", "sdsp", "fswsp", "fsdsp",
    }
    branch_mnemonics = {
        "beq", "bne", "blt", "bge", "bltu", "bgeu", "beqz", "bnez", "blez", "bgez",
        "bltz", "bgtz", "j", "jr", "jal", "jalr", "call", "tail", "ret",
    }
    for line in text.splitlines():
        match = _INSTRUCTION.match(line)
        if match is None:
            continue
        mnemonic = match.group("mnemonic").lower()
        compressed = mnemonic.startswith("c.")
        base = mnemonic[2:] if compressed else mnemonic
        operands = match.group("operands") or ""
        counts["static_total_instructions"] += 1
        if base.startswith("v"):
            counts["static_vector_instructions"] += 1
        elif base.startswith("f") and base not in {"fence", "fence.i"}:
            counts["static_floating_point_instructions"] += 1
        else:
            counts["static_integer_instructions"] += 1
        if base in load_mnemonics:
            counts["static_load_instructions"] += 1
        if base in store_mnemonics:
            counts["static_store_instructions"] += 1
        if base in branch_mnemonics or base.startswith("b"):
            counts["static_branch_instructions"] += 1
        if compressed and base in {"addi", "addi16sp"}:
            adjust = _STACK_ADJUST_COMPRESSED.search(operands)
            if adjust:
                counts["stack_frame_bytes"] = max(counts["stack_frame_bytes"], int(adjust.group("size")))
        elif base in {"addi", "addiw"}:
            adjust = _STACK_ADJUST_THREE_OPERAND.search(operands)
            if adjust:
                counts["stack_frame_bytes"] = max(counts["stack_frame_bytes"], int(adjust.group("size")))
    if counts["static_total_instructions"] == 0:
        raise ValidationError("objdump output contains no decodable instructions")
    if counts["static_vector_instructions"]:
        raise ValidationError("linked binary contains vector/RVV instructions outside RV64GC")
    return counts


def _remarks_metrics(path: Path | None, *, toolchain: str) -> dict[str, tuple[int | None, str | None]]:
    if path is None:
        reason = "remarks_not_available" if toolchain == "accela" else "not_supported_by_toolchain"
        return {"spill_count": (None, reason), "reload_count": (None, reason)}
    if toolchain != "accela":
        raise ValidationError("optimization remarks are only valid for toolchain=accela")
    events = load_and_validate_jsonl(path)
    source_keys = {"spill_count": "spill_stores", "reload_count": "spill_reloads"}
    latest_module: dict[str, tuple[int, int]] = {}
    latest_function: dict[tuple[str, str], tuple[int, int]] = {}
    for event in events:
        if event["event_type"] != "pass_summary":
            continue
        for metric_id, source_key in source_keys.items():
            if source_key in event["after"]:
                value = event["after"][source_key]
                if value < 0:
                    raise ValidationError(f"remark machine counter {source_key} must not be negative")
                observation = (event["sequence"], value)
                if event["target_kind"] == "module":
                    if metric_id not in latest_module or observation[0] > latest_module[metric_id][0]:
                        latest_module[metric_id] = observation
                elif event["target_kind"] == "function":
                    key = (metric_id, event["target_name"])
                    if key not in latest_function or observation[0] > latest_function[key][0]:
                        latest_function[key] = observation
    result: dict[str, tuple[int | None, str | None]] = {}
    for metric_id in source_keys:
        module = latest_module.get(metric_id)
        if module is not None:
            result[metric_id] = (module[1], None)
            continue
        observations = [
            value for (key, _), (_, value) in latest_function.items() if key == metric_id
        ]
        result[metric_id] = (sum(observations), None) if observations else (None, "not_emitted_by_compiler")
    return result


def analyze_binary(
    *,
    binary: Path,
    toolchain: str,
    readelf_command: str = "readelf",
    objdump_command: str = "objdump",
    remarks_path: Path | None = None,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    resolved = binary.resolve(strict=True)
    if not resolved.is_file():
        raise ValidationError("binary must be a regular file")
    if toolchain not in {"accela", "gcc", "clang"}:
        raise ValidationError("toolchain must be accela, gcc, or clang")
    readelf = _run_tool(
        (readelf_command, "--wide", "--file-header", "--arch-specific", "--sections", str(resolved)),
        label="readelf", timeout_seconds=timeout_seconds,
    )
    _validate_elf_contract(readelf)
    section_table = _sections(readelf)
    instruction_table = _instruction_counts(
        _run_tool((objdump_command, "--disassemble", str(resolved)), label="objdump", timeout_seconds=timeout_seconds)
    )
    def section_bytes(*, writable: bool, executable: bool, nobits: bool) -> int:
        total = 0
        for section_type, size, flags in section_table.values():
            if "A" not in flags:
                continue
            if ("W" in flags) != writable or ("X" in flags) != executable:
                continue
            if (section_type == "NOBITS") != nobits:
                continue
            total += size
        return total

    values: dict[str, tuple[int | None, str | None]] = {
        # Classify loadable ELF sections by type/flags so small-data sections
        # (.sdata/.sbss/.srodata) and compiler-specific split sections are not lost.
        "elf_text_bytes": (section_bytes(writable=False, executable=True, nobits=False), None),
        "elf_rodata_bytes": (section_bytes(writable=False, executable=False, nobits=False), None),
        "elf_data_bytes": (section_bytes(writable=True, executable=False, nobits=False), None),
        "elf_bss_bytes": (section_bytes(writable=True, executable=False, nobits=True), None),
        **{key: (value, None) for key, value in instruction_table.items()},
        **_remarks_metrics(remarks_path, toolchain=toolchain),
    }
    if set(values) != set(ANALYZER_METRICS):
        raise AssertionError("binary analyzer metric catalog is internally inconsistent")
    measurements = [
        {
            "metric_id": metric_id,
            "value": value,
            "unit": ANALYZER_METRICS[metric_id],
            "availability": "measured" if value is not None else "unavailable",
            "reason": reason,
        }
        for metric_id, (value, reason) in sorted(values.items())
    ]
    return validate_document({"schema_version": "binary-analysis.v1", "measurements": measurements})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="accela-binary-analyzer")
    parser.add_argument("binary", type=Path)
    parser.add_argument("--toolchain", choices=("accela", "gcc", "clang"), required=True)
    parser.add_argument("--readelf-command", default="readelf")
    parser.add_argument("--objdump-command", default="objdump")
    parser.add_argument("--remarks", type=Path)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.timeout <= 0:
            raise ValidationError("timeout must be greater than zero")
        result = analyze_binary(
            binary=args.binary,
            toolchain=args.toolchain,
            readelf_command=args.readelf_command,
            objdump_command=args.objdump_command,
            remarks_path=args.remarks,
            timeout_seconds=args.timeout,
        )
        atomic_write_json(args.output, result)
        return 0
    except (BenchmarkError, OSError) as exc:
        print(f"error: {sanitize_text(str(exc), (Path.cwd(),))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
