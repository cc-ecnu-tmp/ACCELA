#!/usr/bin/env python3
"""Reproducible correctness and static RISC-V code-quality baselines for ACCELA."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent.parent
TESTSUITE = ROOT / "testsuite"
BENCH_ROOT = ROOT / "build" / "bench"
RESULTS_ROOT = ROOT / "bench-results"
REFERENCE_ROOT = ROOT / "thirdparty" / "sysy-competition"
REFERENCE_URL = "https://github.com/AdUhTkJm/sysy-competition.git"
OFFICIAL_SUITES = ("functional", "h_functional")
DEFAULT_COMPILERS = ("accela", "reference", "llvm-o3")
MEMORY_OPCODES = {
    "lb", "lbu", "lh", "lhu", "lw", "lwu", "ld", "flw", "fld",
    "sb", "sh", "sw", "sd", "fsw", "fsd",
}


class BenchError(RuntimeError):
    pass


def run(
    command: list[str],
    *,
    cwd: Path = ROOT,
    timeout: int | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def require_tool(name: str, candidates: Iterable[str]) -> str:
    for candidate in candidates:
        path = shutil.which(candidate) if "/" not in candidate else candidate
        if path and Path(path).is_file() and os.access(path, os.X_OK):
            return str(Path(path).resolve())
    raise BenchError(f"required tool not found: {name}")


def find_java_home() -> Path:
    candidates = [
        os.environ.get("ACCELA_JAVA_HOME", ""),
        "/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home",
        os.environ.get("JAVA_HOME", ""),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        java = Path(candidate) / "bin" / "java"
        if not java.is_file():
            continue
        proc = run([str(java), "-version"])
        match = re.search(r'version "(\d+)', proc.stderr + proc.stdout)
        if match and 17 <= int(match.group(1)) <= 24:
            return Path(candidate)
    raise BenchError("JDK 17-24 not found; install openjdk@21 or set ACCELA_JAVA_HOME")


def toolchain() -> dict[str, str]:
    java_home = find_java_home()
    llvm_root = Path("/opt/homebrew/opt/llvm/bin")
    return {
        "java": str(java_home / "bin" / "java"),
        "java_home": str(java_home),
        "clang": require_tool("clang", [str(llvm_root / "clang"), "clang"]),
        "clang++": require_tool("clang++", [str(llvm_root / "clang++"), "clang++"]),
        "llvm-mc": require_tool("llvm-mc", [str(llvm_root / "llvm-mc"), "llvm-mc"]),
        "llvm-objdump": require_tool(
            "llvm-objdump", [str(llvm_root / "llvm-objdump"), "llvm-objdump"]
        ),
        "llvm-size": require_tool("llvm-size", [str(llvm_root / "llvm-size"), "llvm-size"]),
    }


def build_accela(tools: dict[str, str], skip_build: bool) -> None:
    classes = ROOT / "build" / "classes" / "java" / "main"
    if skip_build and classes.is_dir():
        return
    env = os.environ.copy()
    env["ACCELA_JAVA_HOME"] = tools["java_home"]
    proc = subprocess.run(["bash", str(ROOT / "scripts" / "build.sh")], cwd=ROOT, env=env)
    if proc.returncode != 0:
        raise BenchError("ACCELA build or unit tests failed")


def reference_sources() -> list[Path]:
    return sorted((REFERENCE_ROOT / "src").rglob("*.cpp"))


def build_reference(tools: dict[str, str], skip_build: bool) -> Path:
    if not (REFERENCE_ROOT / ".git").is_dir():
        REFERENCE_ROOT.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "clone", REFERENCE_URL, str(REFERENCE_ROOT)], cwd=ROOT
        )
        if proc.returncode != 0:
            raise BenchError("failed to clone comparison compiler")

    output = REFERENCE_ROOT / "build" / "sysyc"
    sources = reference_sources()
    if not sources:
        raise BenchError("comparison compiler has no C++ sources")
    stale = not output.is_file() or any(source.stat().st_mtime > output.stat().st_mtime for source in sources)
    if skip_build and output.is_file():
        stale = False
    if stale:
        output.parent.mkdir(parents=True, exist_ok=True)
        command = [
            tools["clang++"],
            "-std=c++20",
            "-O3",
            "-I",
            str(REFERENCE_ROOT / "src"),
            *(str(source) for source in sources),
            "-o",
            str(output),
        ]
        proc = subprocess.run(command, cwd=REFERENCE_ROOT)
        if proc.returncode != 0:
            raise BenchError("comparison compiler build failed")
    return output


def collect_tests(suite: str, pattern: str | None, limit: int | None) -> list[Path]:
    if suite == "official":
        directories = OFFICIAL_SUITES
    else:
        directories = (suite,)
    tests = sorted(path for directory in directories for path in (TESTSUITE / directory).glob("*.sy"))
    if pattern:
        tests = [path for path in tests if pattern in str(path.relative_to(TESTSUITE))]
    if limit is not None:
        tests = tests[:limit]
    if not tests:
        raise BenchError(f"no tests selected for suite {suite!r}")
    if suite == "official" and not pattern and limit is None and len(tests) != 140:
        raise BenchError(f"official suite must contain 140 tests, found {len(tests)}")
    missing = [path for path in tests if not path.with_suffix(".out").is_file()]
    if missing:
        raise BenchError(f"missing expected output for {missing[0]}")
    return tests


def normalized(text: str) -> str:
    return " ".join(text.split())


def short_error(proc: subprocess.CompletedProcess[str], limit: int = 1200) -> str:
    text = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
    return text[-limit:]


def correctness_case(
    source: Path,
    tools: dict[str, str],
    sylib_object: Path,
    timeout: int,
) -> tuple[str, dict[str, Any]]:
    name = str(source.relative_to(TESTSUITE))
    started = time.monotonic()
    temp_root = BENCH_ROOT / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="correctness-", dir=temp_root) as directory:
        temporary = Path(directory)
        ir = temporary / "program.ll"
        executable = temporary / "program"
        command = [
            tools["java"],
            "-cp",
            str(ROOT / "build" / "classes" / "java" / "main"),
            "accela.Main",
            "--ir",
            str(source),
        ]
        try:
            emitted = run(command, timeout=timeout)
        except subprocess.TimeoutExpired:
            return name, {"status": "fail", "stage": "ir", "error": "timeout"}
        if emitted.returncode != 0:
            return name, {"status": "fail", "stage": "ir", "error": short_error(emitted)}
        ir.write_text(emitted.stdout)

        linked = run(
            [tools["clang"], str(ir), str(sylib_object), "-o", str(executable), "-lm"],
            timeout=timeout,
        )
        if linked.returncode != 0:
            return name, {"status": "fail", "stage": "link", "error": short_error(linked)}

        input_file = source.with_suffix(".in")
        input_text = input_file.read_text() if input_file.is_file() else ""
        try:
            executed = run([str(executable)], timeout=timeout, input_text=input_text)
        except subprocess.TimeoutExpired:
            return name, {"status": "fail", "stage": "run", "error": "timeout"}

        exit_code = executed.returncode % 256
        actual = normalized(f"{executed.stdout}\n{exit_code}\n")
        expected = normalized(source.with_suffix(".out").read_text())
        elapsed = round(time.monotonic() - started, 3)
        if actual != expected:
            return name, {
                "status": "fail",
                "stage": "compare",
                "error": f"expected {expected[:400]!r}, got {actual[:400]!r}",
                "seconds": elapsed,
            }
        return name, {"status": "pass", "seconds": elapsed}


def compile_native_sylib(tools: dict[str, str]) -> Path:
    output = BENCH_ROOT / "sylib-native.o"
    output.parent.mkdir(parents=True, exist_ok=True)
    source = TESTSUITE / "libsysy" / "sylib.c"
    proc = run([tools["clang"], "-O2", "-c", str(source), "-o", str(output)])
    if proc.returncode != 0:
        raise BenchError(f"failed to build native SysY runtime: {short_error(proc)}")
    return output


def run_correctness(
    tests: list[Path], tools: dict[str, str], jobs: int, timeout: int
) -> dict[str, Any]:
    sylib_object = compile_native_sylib(tools)
    cases: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = [
            executor.submit(correctness_case, source, tools, sylib_object, timeout)
            for source in tests
        ]
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            name, result = future.result()
            cases[name] = result
            state = "PASS" if result["status"] == "pass" else f"FAIL/{result['stage']}"
            print(f"[correctness {index:3d}/{len(tests)}] {state:12s} {name}", flush=True)
    passed = sum(case["status"] == "pass" for case in cases.values())
    return {
        "method": "ACCELA optimized LLVM-like IR linked and executed natively",
        "scope_note": "This validates frontend/midend semantics, not RISC-V execution semantics.",
        "passed": passed,
        "failed": len(cases) - passed,
        "cases": dict(sorted(cases.items())),
    }


def assembly_command(
    compiler: str,
    source: Path,
    assembly: Path,
    tools: dict[str, str],
    reference: Path | None,
) -> list[str]:
    if compiler == "accela":
        return [
            tools["java"],
            "-cp",
            str(ROOT / "build" / "classes" / "java" / "main"),
            "Compiler",
            str(source),
            "-o",
            str(assembly),
        ]
    if compiler == "reference":
        if reference is None:
            raise BenchError("comparison compiler was not built")
        return [str(reference), str(source), "--rv", "-S", "-o", str(assembly)]
    if compiler == "llvm-o3":
        return [
            tools["clang"],
            "-target",
            "riscv64-unknown-linux-gnu",
            "-march=rv64gc",
            "-mabi=lp64d",
            "-O3",
            "-S",
            "-x",
            "c",
            "-include",
            str(ROOT / "scripts" / "sysy-runtime.h"),
            str(source),
            "-o",
            str(assembly),
        ]
    raise BenchError(f"unknown compiler: {compiler}")


def object_metrics(object_file: Path, tools: dict[str, str], timeout: int) -> tuple[int, int, int]:
    dumped = run(
        [tools["llvm-objdump"], "-d", "--no-show-raw-insn", str(object_file)],
        timeout=timeout,
    )
    if dumped.returncode != 0:
        raise BenchError(short_error(dumped))
    opcodes = [
        match.group(1)
        for line in dumped.stdout.splitlines()
        if (match := re.match(r"^\s*[0-9a-f]+:\s+([a-z0-9.]+)", line))
    ]
    instructions = len(opcodes)
    memory_operations = sum(opcode in MEMORY_OPCODES for opcode in opcodes)

    sized = run([tools["llvm-size"], "-A", str(object_file)], timeout=timeout)
    if sized.returncode != 0:
        raise BenchError(short_error(sized))
    text_bytes = 0
    for line in sized.stdout.splitlines():
        match = re.match(r"^\.text(?:\.\S+)?\s+(\d+)\s+", line.strip())
        if match:
            text_bytes += int(match.group(1))
    return instructions, memory_operations, text_bytes


def assembly_case(
    compiler: str,
    source: Path,
    tools: dict[str, str],
    reference: Path | None,
    timeout: int,
) -> tuple[str, str, dict[str, Any]]:
    name = str(source.relative_to(TESTSUITE))
    relative = source.relative_to(TESTSUITE).with_suffix(".s")
    assembly = BENCH_ROOT / "assembly" / compiler / relative
    object_file = assembly.with_suffix(".o")
    assembly.parent.mkdir(parents=True, exist_ok=True)
    command = assembly_command(compiler, source, assembly, tools, reference)
    started = time.monotonic()
    try:
        compiled = run(command, timeout=timeout)
    except subprocess.TimeoutExpired:
        return compiler, name, {"status": "fail", "stage": "compile", "error": "timeout"}
    if compiled.returncode != 0 or not assembly.is_file():
        return compiler, name, {
            "status": "fail",
            "stage": "compile",
            "error": short_error(compiled),
        }

    assembled = run(
        [
            tools["llvm-mc"],
            "-triple=riscv64",
            "-mattr=+m,+a,+f,+d,+c",
            "-filetype=obj",
            str(assembly),
            "-o",
            str(object_file),
        ],
        timeout=timeout,
    )
    if assembled.returncode != 0:
        return compiler, name, {
            "status": "fail",
            "stage": "assemble",
            "error": short_error(assembled),
        }
    try:
        instructions, memory_operations, text_bytes = object_metrics(object_file, tools, timeout)
    except BenchError as error:
        return compiler, name, {"status": "fail", "stage": "measure", "error": str(error)}
    return compiler, name, {
        "status": "pass",
        "instructions": instructions,
        "memory_operations": memory_operations,
        "text_bytes": text_bytes,
        "seconds": round(time.monotonic() - started, 3),
    }


def summarize_compiler(cases: dict[str, dict[str, Any]]) -> dict[str, Any]:
    successful = [case for case in cases.values() if case["status"] == "pass"]
    return {
        "compiled": len(successful),
        "failed": len(cases) - len(successful),
        "total_instructions": sum(case["instructions"] for case in successful),
        "total_memory_operations": sum(case["memory_operations"] for case in successful),
        "total_text_bytes": sum(case["text_bytes"] for case in successful),
        "cases": dict(sorted(cases.items())),
    }


def ratio(
    numerator: dict[str, Any], denominator: dict[str, Any], total: int,
    metric: str = "total_instructions",
) -> float | None:
    if numerator["compiled"] != total or denominator["compiled"] != total:
        return None
    value = denominator[metric]
    return round(numerator[metric] / value, 6) if value else None


def run_assembly_benchmark(
    tests: list[Path],
    compilers: tuple[str, ...],
    tools: dict[str, str],
    jobs: int,
    timeout: int,
    skip_build: bool,
) -> dict[str, Any]:
    reference = build_reference(tools, skip_build) if "reference" in compilers else None
    cases: dict[str, dict[str, dict[str, Any]]] = {compiler: {} for compiler in compilers}
    work = [(compiler, source) for compiler in compilers for source in tests]
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = [
            executor.submit(assembly_case, compiler, source, tools, reference, timeout)
            for compiler, source in work
        ]
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            compiler, name, result = future.result()
            cases[compiler][name] = result
            state = "OK" if result["status"] == "pass" else f"FAIL/{result['stage']}"
            print(
                f"[assembly {index:3d}/{len(work)}] {state:13s} {compiler:10s} {name}",
                flush=True,
            )

    summaries = {compiler: summarize_compiler(cases[compiler]) for compiler in compilers}
    comparisons: dict[str, Any] = {}
    if "accela" in summaries and "reference" in summaries:
        comparisons["reference_over_accela_instruction_ratio"] = ratio(
            summaries["reference"], summaries["accela"], len(tests)
        )
        comparisons["reference_over_accela_memory_ratio"] = ratio(
            summaries["reference"], summaries["accela"], len(tests),
            "total_memory_operations",
        )
    if "accela" in summaries and "llvm-o3" in summaries:
        comparisons["llvm_o3_over_accela_instruction_ratio"] = ratio(
            summaries["llvm-o3"], summaries["accela"], len(tests)
        )
        comparisons["llvm_o3_over_accela_memory_ratio"] = ratio(
            summaries["llvm-o3"], summaries["accela"], len(tests),
            "total_memory_operations",
        )
    return {
        "method": "RISC-V objects assembled with LLVM MC; static machine instructions counted by llvm-objdump",
        "scope_note": "Counts generated program text once; it is a code-quality proxy, not dynamic executed cycles.",
        "compilers": summaries,
        "comparisons": comparisons,
    }


def command_version(command: list[str]) -> str:
    try:
        proc = run(command, timeout=10)
        text = (proc.stdout or proc.stderr).strip()
        return text.splitlines()[0] if text else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def git_value(arguments: list[str], cwd: Path = ROOT) -> str:
    proc = run(["git", *arguments], cwd=cwd, timeout=10)
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"


def metadata(args: argparse.Namespace, tests: list[Path], tools: dict[str, str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "suite": args.suite,
        "test_count": len(tests),
        "filter": args.filter,
        "accela_commit": git_value(["rev-parse", "HEAD"]),
        "accela_dirty": bool(git_value(["status", "--porcelain"])),
        "reference_commit": (
            git_value(["rev-parse", "HEAD"], REFERENCE_ROOT)
            if (REFERENCE_ROOT / ".git").is_dir()
            else None
        ),
        "host": {"platform": platform.platform(), "machine": platform.machine()},
        "tools": {
            "java": command_version([tools["java"], "-version"]),
            "clang": command_version([tools["clang"], "--version"]),
            "llvm-mc": command_version([tools["llvm-mc"], "--version"]),
        },
    }


def print_summary(result: dict[str, Any]) -> None:
    print("\n=== BASELINE SUMMARY ===")
    correctness = result.get("correctness")
    if correctness:
        print(f"correctness: {correctness['passed']}/{correctness['passed'] + correctness['failed']} passed")
        failures = [name for name, case in correctness["cases"].items() if case["status"] != "pass"]
        if failures:
            print("first failures: " + ", ".join(failures[:10]))
    assembly = result.get("assembly")
    if assembly:
        for name, summary in assembly["compilers"].items():
            print(
                f"{name:10s}: {summary['compiled']:3d} compiled, "
                f"{summary['failed']:3d} failed, {summary['total_instructions']:8d} instructions, "
                f"{summary['total_memory_operations']:8d} memory ops, "
                f"{summary['total_text_bytes']:8d} text bytes"
            )
        for name, value in assembly["comparisons"].items():
            print(f"{name}: {value if value is not None else 'n/a'}")


def write_result(result: dict[str, Any], requested: str | None, update_latest: bool) -> Path:
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    if requested:
        output = Path(requested)
        if not output.is_absolute():
            output = ROOT / output
    else:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        output = RESULTS_ROOT / f"baseline-{stamp}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    output.write_text(serialized)
    if update_latest:
        (RESULTS_ROOT / "latest.json").write_text(serialized)
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        choices=("baseline", "correctness", "benchmark"),
        default="baseline",
    )
    parser.add_argument(
        "--suite",
        choices=("official", "functional", "h_functional", "hidden_functional", "llm_gen"),
        default="official",
    )
    parser.add_argument("--filter", help="substring filter for a test path")
    parser.add_argument("--limit", type=int, help="run only the first N selected tests")
    parser.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument("--timeout", type=int, default=45, help="per-stage timeout in seconds")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument(
        "--compilers",
        nargs="+",
        choices=DEFAULT_COMPILERS,
        default=list(DEFAULT_COMPILERS),
    )
    parser.add_argument("--output", help="JSON result path (default: bench-results/timestamp.json)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.jobs < 1:
        raise BenchError("--jobs must be positive")
    tools = toolchain()
    tests = collect_tests(args.suite, args.filter, args.limit)
    build_accela(tools, args.skip_build)
    result = metadata(args, tests, tools)

    if args.command in ("baseline", "correctness"):
        result["correctness"] = run_correctness(tests, tools, args.jobs, args.timeout)
    if args.command in ("baseline", "benchmark"):
        result["assembly"] = run_assembly_benchmark(
            tests,
            tuple(args.compilers),
            tools,
            args.jobs,
            args.timeout,
            args.skip_build,
        )

    update_latest = (
        args.command == "baseline" and args.suite == "official" and not args.filter and args.limit is None
    )
    output = write_result(result, args.output, update_latest)
    print_summary(result)
    print(f"result: {output}")

    correctness_failed = result.get("correctness", {}).get("failed", 0)
    assembly_failed = sum(
        summary["failed"] for summary in result.get("assembly", {}).get("compilers", {}).values()
    )
    return 1 if correctness_failed or assembly_failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BenchError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
