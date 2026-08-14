#!/usr/bin/env python3
"""Run the ACCELA candidate evaluation without campaign contracts or ledgers."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import lru_cache
from multiprocessing import Manager
from pathlib import Path
from queue import Empty


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
CANDIDATE_ORDER = (
    "candidate.sysy-region-memory-forwarding",
    "candidate.function-specialization",
    "candidate.array-object-promotion",
    "candidate.nested-address-recurrence",
    "candidate.cost-model-loop-tiling",
    "candidate.rv64-word-pressure",
)
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


@dataclass
class GlobalProgress:
    total: int
    done: int = 0
    last_percent: int = -1

    def add_total(self, count: int) -> None:
        self.total += count

    def remove_total(self, count: int) -> None:
        self.total = max(self.done, self.total - count)

    def advance(self, count: int = 1) -> None:
        self.done = min(self.total, self.done + count)
        percent = self.done * 100 // self.total
        if percent == self.last_percent:
            return
        self.last_percent = percent
        width = 32
        filled = width * self.done // self.total
        line = f"[{'#' * filled}{'-' * (width - filled)}] {self.done}/{self.total} ({percent}%)"
        interactive = sys.stdout.isatty()
        print(line, end="\r" if interactive and self.done < self.total else "\n", flush=True)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._+-]+", "-", value).strip("-")


@lru_cache(maxsize=None)
def _enabled_candidates(profile_path: str) -> tuple[str, ...]:
    profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    enabled = profile.get("enable_candidates", [])
    if not isinstance(enabled, list):
        return ()
    return tuple(str(candidate) for candidate in enabled)


def _remark_summary(path: Path, profile_path: str) -> dict[str, object]:
    """Summarize candidate decisions without retaining source paths or payloads."""

    counts = Counter()
    reasons: Counter[str] = Counter()
    enabled = set(_enabled_candidates(profile_path))
    if not enabled or not path.is_file():
        return {"matched": 0, "applied": 0, "rejected": 0, "reasons": {}}
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                remark = json.loads(line)
            except json.JSONDecodeError:
                continue
            if remark.get("event_type") != "decision":
                continue
            if str(remark.get("pass")) not in enabled:
                continue
            decision = str(remark.get("decision"))
            reason = str(remark.get("reason", "unknown"))
            if decision == "candidate" and reason == "candidate_matched":
                counts["matched"] += 1
            elif decision == "applied":
                counts["applied"] += 1
            elif decision == "rejected":
                counts["rejected"] += 1
            reasons[reason] += 1
    return {
        "matched": counts["matched"],
        "applied": counts["applied"],
        "rejected": counts["rejected"],
        "reasons": dict(sorted(reasons.items())),
    }


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
        remarks_summary = _remark_summary(remarks, spec.profile_path)
        record = {
            "case_id": case_id,
            "status": "passed",
            "instructions": int(match.group(1)),
            "elapsed_seconds": time.monotonic() - started,
            "remark_summary": remarks_summary,
        }
    except Exception as exc:
        record["error"] = str(exc)
        record["elapsed_seconds"] = time.monotonic() - started
        _write_json(result_path, record)
        raise RuntimeError(f"{spec.stage}/{spec.profile_id}/{case_id}: {exc}") from exc
    _write_json(result_path, record)
    return record


def _enrich_cached_summary(spec: RunSpec, summary: dict[str, object]) -> bool:
    """Backfill remark coverage for summaries produced by older evaluator runs."""

    changed = False
    cases = summary.get("cases", [])
    if not isinstance(cases, list):
        return False
    profile_dir = Path(spec.output_root) / spec.stage / _slug(spec.profile_id)
    for row in cases:
        if not isinstance(row, dict) or row.get("status") != "passed":
            continue
        if "remark_summary" in row:
            continue
        case_id = str(row.get("case_id", "unknown"))
        matches = list(profile_dir.glob(f"*-{_slug(case_id)}"))
        case_dir = matches[0] if len(matches) == 1 else profile_dir / f"missing-{_slug(case_id)}"
        row["remark_summary"] = _remark_summary(case_dir / "remarks.jsonl", spec.profile_path)
        changed = True
    return changed


def _run_profile(spec: RunSpec, progress_queue: object) -> dict[str, object]:
    root = ROOT
    summary_path = Path(spec.output_root) / spec.stage / _slug(spec.profile_id) / "summary.json"
    if summary_path.is_file():
        previous = json.loads(summary_path.read_text(encoding="utf-8"))
        if previous.get("status") == "passed":
            if _enrich_cached_summary(spec, previous):
                _write_json(summary_path, previous)
            progress_queue.put(len(previous["cases"]))
            return previous
    manifest = json.loads(Path(spec.manifest_path).read_text(encoding="utf-8"))
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise RuntimeError(f"manifest contains no cases: {spec.manifest_path}")
    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=spec.jobs) as executor:
        futures = {
            executor.submit(_case_result, root, spec, case, index): str(case["id"])
            for index, case in enumerate(cases)
        }
        for future in as_completed(futures):
            case_id = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                # A case failure disqualifies only this profile.  The rest of
                # the campaign still runs so the report can distinguish a
                # candidate defect from a shared corpus or infrastructure
                # failure.
                results.append(
                    {
                        "case_id": case_id,
                        "status": "failed",
                        "error": str(exc),
                    }
                )
            progress_queue.put(1)
    results.sort(key=lambda item: str(item["case_id"]))
    failed = [row for row in results if row.get("status") != "passed"]
    summary = {
        "stage": spec.stage,
        "profile_id": spec.profile_id,
        "status": "passed" if not failed else "failed",
        "cases": results,
        "failed_case_count": len(failed),
    }
    _write_json(summary_path, summary)
    return summary


def _execute(
    specs: list[RunSpec],
    max_runs: int,
    progress_queue: object,
    progress: GlobalProgress,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    with ProcessPoolExecutor(max_workers=max_runs) as executor:
        futures = {executor.submit(_run_profile, spec, progress_queue): spec for spec in specs}
        pending = set(futures)
        try:
            while pending:
                try:
                    progress.advance(progress_queue.get(timeout=0.2))
                except Empty:
                    pass
                completed = {future for future in pending if future.done()}
                for future in completed:
                    results.append(future.result())
                pending -= completed
            while True:
                progress.advance(progress_queue.get_nowait())
        except Empty:
            pass
        except Exception:
            for future in futures:
                future.cancel()
            raise
    return results


def _case_count(specs: list[RunSpec]) -> int:
    return sum(
        len(json.loads(Path(spec.manifest_path).read_text(encoding="utf-8"))["cases"])
        for spec in specs
    )


def _geometric_mean(values: list[float]) -> float:
    if not values or any(value <= 0 for value in values):
        raise RuntimeError("geometric mean requires positive values")
    return math.exp(sum(math.log(value) for value in values) / len(values))


def _speedups(baseline: dict[str, object], candidate: dict[str, object]) -> list[float]:
    if not _summary_rankable(baseline) or not _summary_rankable(candidate):
        raise RuntimeError("baseline or candidate contains failed cases")
    baseline_cases = {str(row["case_id"]): int(row["instructions"]) for row in baseline["cases"]}
    candidate_cases = {str(row["case_id"]): int(row["instructions"]) for row in candidate["cases"]}
    if baseline_cases.keys() != candidate_cases.keys():
        raise RuntimeError("baseline and candidate case sets differ")
    return [baseline_cases[case_id] / candidate_cases[case_id] for case_id in baseline_cases]


def _summary_rankable(summary: dict[str, object]) -> bool:
    cases = summary.get("cases")
    return (
        summary.get("status") == "passed"
        and isinstance(cases, list)
        and bool(cases)
        and all(row.get("status") == "passed" for row in cases if isinstance(row, dict))
        and all(isinstance(row, dict) and "instructions" in row for row in cases)
    )


def _summary_failures(summary: dict[str, object]) -> list[str]:
    failures: list[str] = []
    cases = summary.get("cases", [])
    if isinstance(cases, list):
        for row in cases:
            if not isinstance(row, dict) or row.get("status") == "passed":
                continue
            case_id = str(row.get("case_id", "unknown"))
            error = str(row.get("error", "case failed"))
            failures.append(f"{case_id}: {error}")
    if not failures and summary.get("status") != "passed":
        failures.append("profile did not complete successfully")
    return failures[:20]


def _coverage(summary: dict[str, object]) -> dict[str, object]:
    counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    cases = summary.get("cases", [])
    if isinstance(cases, list):
        for row in cases:
            if not isinstance(row, dict):
                continue
            detail = row.get("remark_summary")
            if not isinstance(detail, dict):
                continue
            for key in ("matched", "applied", "rejected"):
                value = detail.get(key)
                if isinstance(value, int):
                    counts[key] += value
            detail_reasons = detail.get("reasons")
            if isinstance(detail_reasons, dict):
                for reason, value in detail_reasons.items():
                    if isinstance(value, int):
                        reasons[str(reason)] += value
    return {
        "matched": counts["matched"],
        "applied": counts["applied"],
        "rejected": counts["rejected"],
        "reasons": dict(sorted(reasons.items())),
    }


def _metric_row(
    output_root: Path,
    stages: list[str],
    profile_id: str,
    baseline_id: str = "baseline",
) -> dict[str, object]:
    stage_means: dict[str, float | None] = {}
    combined: list[float] = []
    failures: list[str] = []
    coverage: dict[str, dict[str, object]] = {}
    rankable = True
    case_count = 0
    failed_case_count = 0
    for stage in stages:
        baseline = _load_summary(output_root, stage, baseline_id)
        candidate = _load_summary(output_root, stage, profile_id)
        coverage[stage] = _coverage(candidate)
        failed_case_count += int(candidate.get("failed_case_count", 0))
        if not _summary_rankable(baseline):
            rankable = False
            failures.extend(f"{stage}/baseline: {reason}" for reason in _summary_failures(baseline))
        if not _summary_rankable(candidate):
            rankable = False
            failures.extend(f"{stage}/{profile_id}: {reason}" for reason in _summary_failures(candidate))
            stage_means[stage] = None
            continue
        values = _speedups(baseline, candidate)
        stage_means[stage] = _geometric_mean(values)
        case_count += len(values)
        if stage != "B2":
            combined.extend(values)
    return {
        "profile_id": profile_id,
        "rankable": rankable,
        "stage_geometric_means": stage_means,
        "combined_geometric_mean": _geometric_mean(combined) if combined else None,
        "case_count": case_count,
        "failed_case_count": failed_case_count,
        "failure_reasons": failures[:20],
        "coverage": coverage,
    }


def _gate_reasons(
    metrics: dict[str, object],
    stages: list[str],
    current: dict[str, object] | None = None,
    require_full: bool = False,
) -> list[str]:
    reasons = list(str(reason) for reason in metrics.get("failure_reasons", []))
    if not metrics.get("rankable", False):
        reasons.append("case-failure-not-rankable")
        return list(dict.fromkeys(reasons))
    required = [stage for stage in ("B3", "B4", "B5", "B6") if stage in stages]
    if require_full:
        missing = [stage for stage in ("B3", "B4", "B5", "B6") if stage not in stages]
        reasons.extend(f"missing-stage:{stage}" for stage in missing)
    b3 = metrics["stage_geometric_means"].get("B3")
    if b3 is None or b3 <= 1.0:
        reasons.append("B3-GM<=1.0")
    combined = metrics.get("combined_geometric_mean")
    if combined is None or combined <= 1.0:
        reasons.append("combined-B3-B6-GM<=1.0")
    for stage in required:
        value = metrics["stage_geometric_means"].get(stage)
        if value is None or value < 0.99:
            reasons.append(f"{stage}-GM<0.99")
    if current is not None:
        current_value = current.get("combined_geometric_mean")
        if (
            combined is None
            or current_value is None
            or combined <= float(current_value)
        ):
            reasons.append("does-not-improve-current-combination")
    if not require_full:
        reasons.append("incomplete-formal-stage-set")
    return list(dict.fromkeys(reasons))


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


def _canonical_candidates(candidates: list[str]) -> list[str]:
    order = {candidate: index for index, candidate in enumerate(CANDIDATE_ORDER)}
    return sorted(candidates, key=lambda candidate: order.get(candidate, len(order)))


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
            enabled = _canonical_candidates([left, right])
            profile_id = f"pair:{left}+{right}"
            path = directory / f"{_slug(profile_id)}.json"
            _write_json(
                path,
                {"schema_version": 2, "base": "FULL", "disable": [], "enable_candidates": enabled},
            )
            profiles[profile_id] = path
    return profiles


def _combination_profile(candidates: list[str], output_root: Path, prefix: str = "combo") -> tuple[str, Path]:
    profile_id = f"{prefix}:" + "+".join(candidates)
    directory = output_root / "profiles"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_slug(profile_id)}.json"
    _write_json(
        path,
        {
            "schema_version": 2,
            "base": "FULL",
            "disable": [],
            "enable_candidates": _canonical_candidates(candidates),
        },
    )
    return profile_id, path


def _format_metric(value: object) -> str:
    return "—" if value is None else f"{float(value):.6f}"


def _rank_key(row: dict[str, object]) -> tuple[int, float, str]:
    combined = row.get("combined_geometric_mean")
    return (
        0 if row.get("rankable") else 1,
        -float(combined) if combined is not None else math.inf,
        str(row.get("profile_id", "")),
    )


def _report(
    output_root: Path,
    stages: list[str],
    candidate_ids: list[str],
    pair_rows: list[dict[str, object]] | None = None,
    combination_rows: list[dict[str, object]] | None = None,
    final_row: dict[str, object] | None = None,
    final_decision: dict[str, object] | None = None,
) -> dict[str, object]:
    baseline = _metric_row(output_root, stages, "baseline")
    full_scope = set(("B2", "B3", "B4", "B5", "B6")).issubset(stages)
    rankings: list[dict[str, object]] = []
    for candidate_id in candidate_ids:
        metrics = _metric_row(output_root, stages, candidate_id)
        reasons = _gate_reasons(metrics, stages, baseline, require_full=full_scope)
        metrics.update(
            {
                "candidate_id": candidate_id,
                "eligible": not reasons,
                "rejection_reasons": reasons,
            }
        )
        rankings.append(metrics)
    rankings.sort(key=_rank_key)
    for rank, row in enumerate(rankings, 1):
        row["rank"] = rank

    result = {
        "status": "completed",
        "stages": stages,
        "formal_scope_complete": full_scope,
        "ranking": rankings,
        "single_candidate_ranking": rankings,
        "pair_diagnostics": pair_rows or [],
        "combinations": combination_rows or [],
        "final_verification": final_row,
        "final_decision": final_decision
        or {
            "decision": "not-decided",
            "reason": "full B2-B6 evaluation was not requested",
        },
    }
    _write_json(output_root / "summary.json", result)

    lines = [
        "# ACCELA candidate evaluation",
        "",
        f"Stages: {', '.join(stages)}. Formal B2–B6 scope: {'yes' if full_scope else 'no'}.",
        "",
        "## Single candidates",
        "",
        "| Rank | Candidate | Eligible | Combined GM | B2 | B3 | B4 | B5 | B6 | Cases |",
        "|---:|---|:---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rankings:
        means = row["stage_geometric_means"]
        lines.append(
            f"| {row['rank']} | `{row['candidate_id']}` | "
            f"{'yes' if row['eligible'] else 'no'} | {_format_metric(row['combined_geometric_mean'])} | "
            + " | ".join(_format_metric(means.get(stage)) for stage in STAGES)
            + f" | {row['case_count']} |"
        )
    if combination_rows:
        lines.extend(
            [
                "",
                "## Greedy combinations",
                "",
                "| Profile | Added | Accepted | Absolute GM | Incremental GM | B3 | B4 | B5 | B6 |",
                "|---|---|:---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for row in combination_rows:
            absolute = row.get("absolute", {})
            incremental = row.get("incremental", {})
            absolute_means = absolute.get("stage_geometric_means", {})
            lines.append(
                f"| `{row['profile_id']}` | `{row['added_candidate']}` | "
                f"{'yes' if row['accepted'] else 'no'} | "
                f"{_format_metric(absolute.get('combined_geometric_mean'))} | "
                f"{_format_metric(incremental.get('combined_geometric_mean'))} | "
                + " | ".join(_format_metric(absolute_means.get(stage)) for stage in ("B3", "B4", "B5", "B6"))
                + " |"
            )
    if pair_rows:
        lines.extend(
            [
                "",
                "## B3 pair diagnostics",
                "",
                "| Pair | Rankable | B3 GM | Cases |",
                "|---|:---:|---:|---:|",
            ]
        )
        for row in pair_rows:
            lines.append(
                f"| `{row['profile_id']}` | {'yes' if row['rankable'] else 'no'} | "
                f"{_format_metric(row['stage_geometric_means'].get('B3'))} | {row['case_count']} |"
            )
    if final_decision is not None:
        lines.extend(
            [
                "",
                "## Final integration decision",
                "",
                f"- Decision: **{final_decision.get('decision', 'not-decided')}**",
                f"- Selected candidates: `{', '.join(final_decision.get('selected_candidates', [])) or 'none'}`",
            ]
        )
        reasons = final_decision.get("reasons", [])
        if reasons:
            lines.append("- Reasons: " + "; ".join(str(reason) for reason in reasons))
    lines.extend(["", "## Rejection and failure reasons", ""])
    for row in rankings:
        reasons = row.get("rejection_reasons", [])
        if reasons:
            lines.append(f"- `{row['candidate_id']}`: " + "; ".join(str(reason) for reason in reasons))
    if not any(row.get("rejection_reasons") for row in rankings):
        lines.append("- none")
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
    diagnostics = not args.no_diagnostics and "B3" in args.stages
    b3_cases = len(
        json.loads((DATA / "manifests" / STAGES["B3"][0]).read_text(encoding="utf-8"))["cases"]
    )
    total = _case_count(specs)
    full_stages = ["B3", "B4", "B5", "B6"]
    full_scope = set(("B2", *full_stages)).issubset(args.stages)
    full_case_count = sum(
        len(json.loads((DATA / "manifests" / STAGES[stage][0]).read_text(encoding="utf-8"))["cases"])
        for stage in full_stages
    )
    all_case_count = sum(
        len(json.loads((DATA / "manifests" / STAGES[stage][0]).read_text(encoding="utf-8"))["cases"])
        for stage in STAGES
    )
    pair_rows: list[dict[str, object]] = []
    combination_rows: list[dict[str, object]] = []
    final_row: dict[str, object] | None = None
    final_decision: dict[str, object] = {
        "decision": "not-decided",
        "selected_candidates": [],
        "reasons": ["full B2-B6 evaluation was not requested"],
    }
    with Manager() as manager:
        progress_queue = manager.Queue()
        progress = GlobalProgress(total)
        _execute(specs, args.max_runs, progress_queue, progress)

        single_metrics = {
            candidate: _metric_row(output_root, args.stages, candidate)
            for candidate in candidates
        }
        ranked = sorted(
            candidates,
            key=lambda candidate: _rank_key(single_metrics[candidate] | {"profile_id": candidate}),
        )
        if diagnostics:
            top3 = [candidate for candidate in ranked if single_metrics[candidate]["rankable"]][:3]
            pair_profiles = _pair_profiles(top3, output_root)
            progress.add_total(len(pair_profiles) * b3_cases)
            if pair_profiles:
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
                    progress_queue,
                    progress,
                )
                pair_rows = []
                for profile_id in pair_profiles:
                    row = _metric_row(output_root, ["B3"], profile_id)
                    row["profile_id"] = profile_id
                    pair_rows.append(row)

        if full_scope:
            selected: list[str] = []
            current_profile = "baseline"
            current_metrics = _metric_row(output_root, full_stages, current_profile)
            for candidate in ranked:
                selected_for_profile = [*selected, candidate]
                profile_id, profile_path = _combination_profile(selected_for_profile, output_root)
                progress.add_total(full_case_count)
                _execute(
                    _specs(
                        full_stages,
                        {profile_id: profile_path},
                        output_root,
                        args.jobs,
                        args.compile_timeout,
                        args.run_timeout,
                    ),
                    args.max_runs,
                    progress_queue,
                    progress,
                )
                absolute = _metric_row(output_root, full_stages, profile_id)
                incremental = _metric_row(
                    output_root,
                    full_stages,
                    profile_id,
                    baseline_id=current_profile,
                )
                rejection_reasons = _gate_reasons(
                    absolute,
                    full_stages,
                    current=current_metrics,
                    require_full=True,
                )
                accepted = not rejection_reasons
                combination_rows.append(
                    {
                        "profile_id": profile_id,
                        "added_candidate": candidate,
                        "selected_candidates": selected_for_profile,
                        "accepted": accepted,
                        "rejection_reasons": rejection_reasons,
                        "absolute": absolute,
                        "incremental": incremental,
                    }
                )
                if accepted:
                    selected = selected_for_profile
                    current_profile = profile_id
                    current_metrics = absolute

            if selected:
                final_id, final_path = _combination_profile(selected, output_root, prefix="final")
                progress.add_total(all_case_count)
                _execute(
                    _specs(
                        list(STAGES),
                        {final_id: final_path},
                        output_root,
                        args.jobs,
                        args.compile_timeout,
                        args.run_timeout,
                    ),
                    args.max_runs,
                    progress_queue,
                    progress,
                )
                final_metrics = _metric_row(output_root, list(STAGES), final_id)
                final_reasons = _gate_reasons(
                    final_metrics,
                    list(STAGES),
                    current=_metric_row(output_root, list(STAGES), "baseline"),
                    require_full=True,
                )
                final_row = {
                    **final_metrics,
                    "profile_id": final_id,
                    "selected_candidates": selected,
                    "eligible": not final_reasons,
                    "rejection_reasons": final_reasons,
                }
                if final_reasons:
                    final_decision = {
                        "decision": "keep-current",
                        "selected_candidates": [],
                        "reasons": final_reasons,
                    }
                else:
                    final_decision = {
                        "decision": "integrate",
                        "selected_candidates": selected,
                        "reasons": [],
                    }
            else:
                final_row = {
                    **_metric_row(output_root, list(STAGES), "baseline"),
                    "profile_id": "baseline",
                    "selected_candidates": [],
                    "eligible": False,
                    "rejection_reasons": ["no candidate passed greedy combination gates"],
                }
                final_decision = {
                    "decision": "keep-current",
                    "selected_candidates": [],
                    "reasons": ["no candidate passed greedy combination gates"],
                }
    result = _report(
        output_root,
        args.stages,
        list(candidates),
        pair_rows=pair_rows,
        combination_rows=combination_rows,
        final_row=final_row,
        final_decision=final_decision,
    )
    winner = result["ranking"][0]["candidate_id"] if result["ranking"] else "none"
    print(f"winner={winner}", flush=True)
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
