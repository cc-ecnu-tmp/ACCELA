#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate corpus structure and optionally compile/run generated SysY."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
CLASSPATH = REPO / "build" / "classes" / "java" / "main"


def java_executable() -> str:
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidate = Path(java_home) / "bin" / ("java.exe" if os.name == "nt" else "java")
        if candidate.is_file():
            return str(candidate)
    return "java"


def split_interpreter_transcript(
    transcript: bytes, line_separator: bytes | None = None
) -> tuple[bytes, int]:
    """Separate program stdout from accela.Main's diagnostic exit-code trailer.

    The development interpreter serializes ``stdout + platform newline + exit
    code + platform newline``.  The first newline is transport framing, not
    program output.  Removing that framing avoids accepting whitespace changes
    while leaving the program's own final newline untouched.
    """
    separator = os.linesep.encode("ascii") if line_separator is None else line_separator
    if not separator:
        raise RuntimeError("interpreter line separator must not be empty")
    if not transcript.endswith(separator):
        raise RuntimeError("interpreter transcript is missing its final line separator")
    framed = transcript[: -len(separator)]
    stdout, marker, exit_text = framed.rpartition(separator)
    if not marker:
        raise RuntimeError("interpreter transcript is missing its exit-code frame")
    try:
        exit_code = int(exit_text.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as error:
        raise RuntimeError("interpreter exit-code frame is not a decimal integer") from error
    if not 0 <= exit_code <= 255:
        raise RuntimeError(f"interpreter exit code is outside uint8: {exit_code}")
    return stdout, exit_code


def split_expected_output(expected: bytes) -> tuple[bytes, int]:
    """Decode the corpus's ``exact stdout`` plus LF-terminated uint8 trailer."""
    if not expected.endswith(b"\n"):
        raise RuntimeError("expected output is missing its final LF")
    body = expected[:-1]
    split_at = body.rfind(b"\n")
    if split_at < 0:
        stdout = b""
        exit_text = body
    else:
        stdout = body[: split_at + 1]
        exit_text = body[split_at + 1 :]
    try:
        exit_code = int(exit_text.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as error:
        raise RuntimeError("expected exit-code trailer is not a decimal integer") from error
    if not 0 <= exit_code <= 255:
        raise RuntimeError(f"expected exit code is outside uint8: {exit_code}")
    return stdout, exit_code


def byte_summary(data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    return f"length={len(data)}, sha256={digest}"


def load_manifest() -> dict[str, object]:
    return json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))


def all_sources(manifest: dict[str, object]) -> list[Path]:
    paths = [ROOT / item["source"] for item in manifest["benchmarks"]]
    for family in manifest["oracle_families"]:
        for variant in family["variants"]:
            paths.append(ROOT / variant["baseline"])
            paths.append(ROOT / variant["optimized"])
    for item in manifest.get("structural_variants", []):
        paths.append(ROOT / item["source"])
    return paths


def static_validate(manifest: dict[str, object]) -> None:
    benchmarks = manifest["benchmarks"]
    if len(benchmarks) != 22:
        raise RuntimeError(f"expected 22 primary benchmarks, found {len(benchmarks)}")
    groups = {}
    for item in benchmarks:
        groups[item["group"]] = groups.get(item["group"], 0) + 1
        if len(item["datasets"]) != 4:
            raise RuntimeError(f"{item['id']} does not have four datasets")
        tier_roles = {dataset["tier"]: dataset["role"] for dataset in item["datasets"]}
        if tier_roles != {
            "correctness": "correctness",
            "small": "performance",
            "medium": "performance",
            "large": "performance",
        }:
            raise RuntimeError(f"invalid dataset tiers/roles for {item['id']}: {tier_roles}")
        for dataset in item["datasets"]:
            for key in ("input", "output"):
                if not (ROOT / dataset[key]).is_file():
                    raise RuntimeError(f"missing {dataset[key]}")
    if groups != {"polybench-style": 14, "embench-style": 8}:
        raise RuntimeError(f"unexpected primary groups: {groups}")
    families = manifest["oracle_families"]
    if len(families) != 11:
        raise RuntimeError(f"expected 11 oracle families, found {len(families)}")
    for family in families:
        if len(family["variants"]) != 3:
            raise RuntimeError(f"{family['family']} does not have three variants")
        for variant in family["variants"]:
            if len(variant["datasets"]) != 3:
                raise RuntimeError(
                    f"{family['family']}/{variant['variant']} does not have three datasets"
                )
            for dataset in variant["datasets"]:
                for key in ("input", "output"):
                    if not (ROOT / dataset[key]).is_file():
                        raise RuntimeError(f"missing {dataset[key]}")
    structural = manifest.get("structural_variants", [])
    if structural and len(structural) != 60:
        raise RuntimeError(f"expected 60 structural variants, found {len(structural)}")
    for item in structural:
        if item["role"] != "structural_variant":
            raise RuntimeError(f"invalid structural role for {item['source']}")
        for key in ("source", "input", "output"):
            if not (ROOT / item[key]).is_file():
                raise RuntimeError(f"missing {item[key]}")
    for source in all_sources(manifest):
        text = source.read_text(encoding="utf-8")
        if "SPDX-License-Identifier: MIT" not in text:
            raise RuntimeError(f"missing SPDX marker: {source.relative_to(ROOT)}")
        if "ACCELA clean-room original" not in text:
            raise RuntimeError(f"missing provenance marker: {source.relative_to(ROOT)}")
        if chr(58) + chr(92) in text or "/home/" in text or "/Users/" in text:
            raise RuntimeError(f"machine-local path in {source.relative_to(ROOT)}")


def compiler_command(mode: str, source: Path) -> list[str]:
    if not CLASSPATH.is_dir():
        raise RuntimeError("compiler classes are missing; build them before compiler validation")
    return [java_executable(), "-cp", str(CLASSPATH), "accela.Main", mode, str(source)]


def parse_all(manifest: dict[str, object]) -> None:
    sources = all_sources(manifest)
    for index, source in enumerate(sources, 1):
        proc = subprocess.run(
            compiler_command("--ir", source), cwd=REPO, text=True,
            capture_output=True, timeout=60, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"parse/IR failure in {source.relative_to(ROOT)}:\n{proc.stderr}")
        print(f"parse {index:03d}/{len(sources)} {source.relative_to(ROOT)}")


def run_case(source: Path, input_path: Path, output_path: Path) -> None:
    proc = subprocess.run(
        compiler_command("--interpret", source), cwd=REPO,
        input=input_path.read_bytes(),
        capture_output=True, timeout=120, check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"interpreter failure in {source.relative_to(ROOT)}:\n{stderr}")
    actual_stdout, actual_exit = split_interpreter_transcript(proc.stdout)
    expected_stdout, expected_exit = split_expected_output(output_path.read_bytes())
    if actual_stdout != expected_stdout or actual_exit != expected_exit:
        raise RuntimeError(
            f"output mismatch in {source.relative_to(ROOT)}\n"
            f"expected stdout: {byte_summary(expected_stdout)}\n"
            f"actual stdout:   {byte_summary(actual_stdout)}\n"
            f"expected return: {expected_exit}\n"
            f"actual return:   {actual_exit}"
        )


def interpret_correctness(manifest: dict[str, object]) -> None:
    count = 0
    for item in manifest["benchmarks"]:
        dataset = next(entry for entry in item["datasets"] if entry["tier"] == "correctness")
        run_case(ROOT / item["source"], ROOT / dataset["input"], ROOT / dataset["output"])
        count += 1
        print(f"run {count:03d} {item['id']}/correctness")
    for family in manifest["oracle_families"]:
        for variant in family["variants"]:
            dataset = next(entry for entry in variant["datasets"] if entry["tier"] == "small")
            for role in ("baseline", "optimized"):
                run_case(ROOT / variant[role], ROOT / dataset["input"], ROOT / dataset["output"])
                count += 1
                print(f"run {count:03d} {family['family']}/{variant['variant']}/{role}")
    for item in manifest.get("structural_variants", []):
        run_case(ROOT / item["source"], ROOT / item["input"], ROOT / item["output"])
        count += 1
        print(f"run {count:03d} structural/{item['family_taxonomy']}/{item['variant_kind']}")


def interpret_primary_all(manifest: dict[str, object]) -> None:
    count = 0
    for item in manifest["benchmarks"]:
        for dataset in item["datasets"]:
            run_case(ROOT / item["source"], ROOT / dataset["input"], ROOT / dataset["output"])
            count += 1
            print(f"primary {count:03d} {item['id']}/{dataset['tier']}")


def interpret_oracles_all(manifest: dict[str, object]) -> None:
    count = 0
    for family in manifest["oracle_families"]:
        for variant in family["variants"]:
            for dataset in variant["datasets"]:
                for role in ("baseline", "optimized"):
                    run_case(ROOT / variant[role], ROOT / dataset["input"], ROOT / dataset["output"])
                    count += 1
                    print(
                        f"oracle {count:03d} {family['family']}/"
                        f"{variant['variant']}/{dataset['tier']}/{role}"
                    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parse", action="store_true")
    parser.add_argument("--interpret-correctness", action="store_true")
    parser.add_argument("--interpret-primary-all", action="store_true")
    parser.add_argument("--interpret-oracles-all", action="store_true")
    args = parser.parse_args()
    manifest = load_manifest()
    static_validate(manifest)
    print("static corpus validation passed")
    if args.parse:
        parse_all(manifest)
    if args.interpret_correctness:
        interpret_correctness(manifest)
    if args.interpret_primary_all:
        interpret_primary_all(manifest)
    if args.interpret_oracles_all:
        interpret_oracles_all(manifest)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
