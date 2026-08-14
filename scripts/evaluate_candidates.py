#!/usr/bin/env python3
"""Run the ACCELA candidate evaluation without campaign contracts or ledgers."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs/optimization/data"
PROFILE_DIR = DATA / "candidates/profiles"
STAGES = {
    "B2": ("b2-family-smoke.manifest.json", ".tmp/official/2026-riscv-performance/performance"),
    "B3": ("b3-official-performance-2026.manifest.json", ".tmp/official/2026-riscv-performance/performance"),
    "B4": ("b4-official-performance-2025-preliminary.manifest.json", ".tmp/official/2025-riscv-prelim"),
    "B5": ("b5-structural-variants.manifest.json", "benchmarks"),
    "B6": ("b6-mature-benchmarks.manifest.json", "benchmarks"),
}
INSTRUCTION_RE = re.compile(r"(?:^|\s)instructions=(\d+)(?:\s|$)")


@dataclass(frozen=True)
class RunSpec:
    stage: str
    profile_id: str
    profile_path: str
    manifest_path: str
    suite_root: str
    output_root: str
    jobs: int
    compile_timeout: int
    run_timeout: int


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._+-]+", "-", value).strip("-")


def _run_command(command: list[str], directory: Path, prefix: Path, timeout: int) -> bytes:
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        prefix.with_suffix(".stdout").write_bytes(exc.stdout or b"")
        prefix.with_suffix(".stderr").write_bytes(exc.stderr or b"")
        raise RuntimeError(f"timeout after {timeout}s: {' '.join(command[:2])}") from exc
    prefix.with_suffix(".stdout").write_bytes(result.stdout)
    prefix.with_suffix(".stderr").write_bytes(result.stderr)
    if result.returncode != 0:
        diagnostic = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        detail = diagnostic[-1] if diagnostic else f"exit {result.returncode}"
        raise RuntimeError(f"command failed ({detail}): {' '.join(command[:2])}")
    prefix.with_suffix(".seconds").write_text(
        f"{time.monotonic() - started:.6f}\n", encoding="ascii"
    )
    return result.stdout


def _case_result(
    root: Path,
    spec: RunSpec,
    case: dict[str, object],
    index: int,
) -> dict[str, object]:
    case_id = str(case["id"])
    case_dir = Path(spec.output_root) / spec.stage / _slug(spec.profile_id) / f"{index:04d}-{_slug(case_id)}"
    result_path = case_dir / "result.json"
    if result_path.is_file():
        previous = json.loads(result_path.read_text(encoding="utf-8"))
        if previous.get("status") == "passed":
            return previous
    case_dir.mkdir(parents=True, exist_ok=True)
    suite = Path(spec.suite_root)
    source = suite / str(case["source"]["path"])
    expected = suite / str(case["expected_output"]["path"])
    input_record = case.get("input")
    if input_record is None:
        input_path = case_dir / "empty.in"
        input_path.write_bytes(b"")
    else:
        input_path = suite / str(input_record["path"])
    for label, path in (("source", source), ("expected output", expected), ("input", input_path)):
        if not path.is_file():
            raise RuntimeError(f"{label} is missing for {case_id}: {path}")

    assembly = case_dir / "program.s"
    remarks = case_dir / "remarks.jsonl"
    binary = case_dir / "program.elf"
    metrics = case_dir / "metrics.log"
    started = time.monotonic()
    record: dict[str, object] = {"case_id": case_id, "status": "failed"}
    try:
        _run_command(
            ["sh", "scripts/benchmark-compile.sh", spec.profile_path, str(source), str(assembly), str(remarks)],
            root,
            case_dir / "compile",
            spec.compile_timeout,
        )
        _run_command(
            ["sh", "scripts/benchmark-link.sh", str(assembly), str(binary)],
            root,
            case_dir / "link",
            spec.compile_timeout,
        )
        actual = _run_command(
            ["sh", "scripts/benchmark-qemu.sh", str(binary), str(metrics), str(input_path)],
            root,
            case_dir / "run",
            spec.run_timeout,
        )
        wanted = expected.read_bytes()
        if actual != wanted:
            offset = next(
                (position for position, pair in enumerate(zip(actual, wanted)) if pair[0] != pair[1]),
                min(len(actual), len(wanted)),
            )
            raise RuntimeError(f"wrong output for {case_id} at byte {offset}")
        metric_text = metrics.read_text(encoding="utf-8")
        match = INSTRUCTION_RE.search(metric_text)
        if match is None:
            raise RuntimeError(f"instruction metric is missing for {case_id}")
        record = {
            "case_id": case_id,
            "status": "passed",
            "instructions": int(match.group(1)),
            "elapsed_seconds": time.monotonic() - started,
        }
    except Exception as exc:
        record["error"] = str(exc)
        record["elapsed_seconds"] = time.monotonic() - started
        _write_json(result_path, record)
        raise
    _write_json(result_path, record)
    return record


def _run_profile(spec: RunSpec) -> dict[str, object]:
    root = ROOT
    summary_path = Path(spec.output_root) / spec.stage / _slug(spec.profile_id) / "summary.json"
    if summary_path.is_file():
        previous = json.loads(summary_path.read_text(encoding="utf-8"))
        if previous.get("status") == "passed":
            return previous
    manifest = json.loads(Path(spec.manifest_path).read_text(encoding="utf-8"))
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise RuntimeError(f"manifest contains no cases: {spec.manifest_path}")
    results: list[dict[str, object]] = []
    total = len(cases)
    step = max(1, math.ceil(total / 20))
    with ThreadPoolExecutor(max_workers=spec.jobs) as executor:
        futures = {
            executor.submit(_case_result, root, spec, case, index): str(case["id"])
            for index, case in enumerate(cases)
        }
        try:
            for future in as_completed(futures):
                results.append(future.result())
                done = len(results)
                if done == 1 or done == total or done % step == 0:
                    width = 24
                    filled = width * done // total
                    bar = "#" * filled + "-" * (width - filled)
                    print(
                        f"[{bar}] {done:>3}/{total:<3} {spec.stage}/{spec.profile_id}",
                        flush=True,
                    )
        except Exception:
            for future in futures:
                future.cancel()
            raise
    results.sort(key=lambda item: str(item["case_id"]))
    summary = {
        "stage": spec.stage,
        "profile_id": spec.profile_id,
        "status": "passed",
        "cases": results,
    }
    _write_json(summary_path, summary)
    return summary


def _execute(specs: list[RunSpec], max_runs: int) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    with ProcessPoolExecutor(max_workers=max_runs) as executor:
        futures = {executor.submit(_run_profile, spec): spec for spec in specs}
        try:
            for future in as_completed(futures):
                spec = futures[future]
                result = future.result()
                print(f"completed {spec.stage}/{spec.profile_id}", flush=True)
                results.append(result)
        except Exception:
            for future in futures:
                future.cancel()
            raise
    return results


def _geometric_mean(values: list[float]) -> float:
    if not values or any(value <= 0 for value in values):
        raise RuntimeError("geometric mean requires positive values")
    return math.exp(sum(math.log(value) for value in values) / len(values))


def _speedups(baseline: dict[str, object], candidate: dict[str, object]) -> list[float]:
    baseline_cases = {str(row["case_id"]): int(row["instructions"]) for row in baseline["cases"]}
    candidate_cases = {str(row["case_id"]): int(row["instructions"]) for row in candidate["cases"]}
    if baseline_cases.keys() != candidate_cases.keys():
        raise RuntimeError("baseline and candidate case sets differ")
    return [baseline_cases[case_id] / candidate_cases[case_id] for case_id in baseline_cases]


def _profiles() -> tuple[Path, dict[str, Path]]:
    baseline = PROFILE_DIR / "baseline.json"
    candidate_files = list(PROFILE_DIR.glob("candidate.*.json"))
    if not baseline.is_file() or len(candidate_files) != 6:
        raise RuntimeError("expected baseline.json and six candidate profiles")
    candidates: dict[str, Path] = {}
    for path in candidate_files:
        profile = json.loads(path.read_text(encoding="utf-8"))
        enabled = profile.get("enable_candidates")
        if not isinstance(enabled, list) or len(enabled) != 1:
            raise RuntimeError(f"candidate profile must enable exactly one candidate: {path.name}")
        candidates[str(enabled[0])] = path
    return baseline, dict(sorted(candidates.items()))


def _specs(
    stages: list[str],
    profiles: dict[str, Path],
    output_root: Path,
    jobs: int,
    compile_timeout: int,
    run_timeout: int,
) -> list[RunSpec]:
    specs: list[RunSpec] = []
    for stage in stages:
        manifest_name, suite_name = STAGES[stage]
        manifest = DATA / "manifests" / manifest_name
        suite = ROOT / suite_name
        if not manifest.is_file() or not suite.is_dir():
            raise RuntimeError(f"{stage} manifest or suite is missing")
        for profile_id, profile in profiles.items():
            specs.append(
                RunSpec(
                    stage,
                    profile_id,
                    str(profile),
                    str(manifest),
                    str(suite),
                    str(output_root),
                    jobs,
                    compile_timeout,
                    run_timeout,
                )
            )
    return specs


def _load_summary(output_root: Path, stage: str, profile_id: str) -> dict[str, object]:
    path = output_root / stage / _slug(profile_id) / "summary.json"
    if not path.is_file():
        raise RuntimeError(f"summary is missing: {stage}/{profile_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def _pair_profiles(top3: list[str], output_root: Path) -> dict[str, Path]:
    profiles: dict[str, Path] = {}
    directory = output_root / "profiles"
    directory.mkdir(parents=True, exist_ok=True)
    for left_index, left in enumerate(top3):
        for right in top3[left_index + 1 :]:
            profile_id = f"pair:{left}+{right}"
            path = directory / f"{_slug(profile_id)}.json"
            _write_json(
                path,
                {"schema_version": 2, "base": "FULL", "disable": [], "enable_candidates": [left, right]},
            )
            profiles[profile_id] = path
    return profiles


def _report(output_root: Path, stages: list[str], candidate_ids: list[str]) -> dict[str, object]:
    rankings: list[dict[str, object]] = []
    for candidate_id in candidate_ids:
        stage_means: dict[str, float] = {}
        combined: list[float] = []
        for stage in stages:
            baseline = _load_summary(output_root, stage, "baseline")
            candidate = _load_summary(output_root, stage, candidate_id)
            values = _speedups(baseline, candidate)
            stage_means[stage] = _geometric_mean(values)
            if stage != "B2":
                combined.extend(values)
        rankings.append(
            {
                "candidate_id": candidate_id,
                "eligible": True,
                "stage_geometric_means": stage_means,
                "combined_geometric_mean": _geometric_mean(combined),
                "case_count": len(combined),
            }
        )
    rankings.sort(key=lambda row: (-float(row["combined_geometric_mean"]), str(row["candidate_id"])))
    result = {"status": "completed", "stages": stages, "ranking": rankings}
    _write_json(output_root / "summary.json", result)
    lines = ["# ACCELA candidate evaluation", "", "| Rank | Candidate | Combined GM | B2 | B3 | B4 | B5 | B6 |", "|---:|---|---:|---:|---:|---:|---:|---:|"]
    for rank, row in enumerate(rankings, 1):
        means = row["stage_geometric_means"]
        lines.append(
            f"| {rank} | `{row['candidate_id']}` | {row['combined_geometric_mean']:.6f} | "
            + " | ".join(f"{means.get(stage, float('nan')):.6f}" for stage in STAGES)
            + " |"
        )
    (output_root / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


def _preflight(skip_build: bool) -> None:
    for command in ("sh", "java", "riscv64-elf-gcc", "riscv64-elf-readelf", "qemu-system-riscv64"):
        if shutil.which(command) is None:
            raise RuntimeError(f"required command is missing: {command}")
    if not skip_build:
        subprocess.run(["sh", "./gradlew", "classes", "--no-daemon"], cwd=ROOT, check=True)
        subprocess.run(["sh", "scripts/build-qemu-plugins.sh"], cwd=ROOT, check=True)
    if not (ROOT / "build/classes/java/main").is_dir():
        raise RuntimeError("compiler classes are missing")
    for name in ("profile.so", "cache.so"):
        if not (ROOT / "build/benchmark/qemu-plugins" / name).is_file():
            raise RuntimeError(f"QEMU plugin is missing: {name}")


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-runs", type=_positive, default=6)
    parser.add_argument("--jobs", type=_positive, default=4)
    parser.add_argument("--stages", nargs="+", choices=tuple(STAGES), default=list(STAGES))
    parser.add_argument("--output-root", type=Path, default=Path(".tmp/simple-evaluation"))
    parser.add_argument("--compile-timeout", type=_positive, default=180)
    parser.add_argument("--run-timeout", type=_positive, default=1800)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--no-diagnostics", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not set(args.stages).intersection({"B3", "B4", "B5", "B6"}):
        parser.error("ranking requires at least one of B3, B4, B5, or B6")
    output_root = (ROOT / args.output_root).resolve() if not args.output_root.is_absolute() else args.output_root.resolve()
    _preflight(args.skip_build)
    baseline, candidates = _profiles()
    profiles = {"baseline": baseline, **candidates}
    specs = _specs(
        args.stages,
        profiles,
        output_root,
        args.jobs,
        args.compile_timeout,
        args.run_timeout,
    )
    print(f"running {len(specs)} profiles with max_runs={args.max_runs}, jobs={args.jobs}", flush=True)
    _execute(specs, args.max_runs)
    if not args.no_diagnostics and "B3" in args.stages:
        ranked = sorted(
            candidates,
            key=lambda candidate: (
                -_geometric_mean(
                    _speedups(
                        _load_summary(output_root, "B3", "baseline"),
                        _load_summary(output_root, "B3", candidate),
                    )
                ),
                candidate,
            ),
        )
        pair_profiles = _pair_profiles(ranked[:3], output_root)
        _execute(
            _specs(
                ["B3"],
                pair_profiles,
                output_root,
                args.jobs,
                args.compile_timeout,
                args.run_timeout,
            ),
            args.max_runs,
        )
    result = _report(output_root, args.stages, list(candidates))
    print(f"winner={result['ranking'][0]['candidate_id']}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("evaluation interrupted", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"evaluation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
