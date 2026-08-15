from __future__ import annotations

import json
import math
import random
import statistics
from pathlib import Path


class BenchmarkError(ValueError):
    pass


def load_json(path: Path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BenchmarkError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(
                BenchmarkError(f"non-finite JSON number: {value}")))
    except json.JSONDecodeError as exception:
        raise BenchmarkError(f"invalid JSON in {path}: {exception}") from exception


def validate_manifest(path: Path):
    manifest = load_json(path)
    _keys(manifest, {"schema_version", "target", "abi", "runtime", "max_static_bytes", "cases"},
        "manifest")
    if manifest["schema_version"] != 1:
        raise BenchmarkError("manifest schema_version must be 1")
    for key in ("target", "abi", "runtime"):
        _text(manifest[key], f"manifest.{key}")
    max_static = _positive_int(manifest["max_static_bytes"], "manifest.max_static_bytes")
    if not isinstance(manifest["cases"], list) or not manifest["cases"]:
        raise BenchmarkError("manifest.cases must be a non-empty array")
    seen = set()
    root = path.resolve().parent
    for index, case in enumerate(manifest["cases"]):
        name = f"manifest.cases[{index}]"
        _keys(case, {"id", "source", "input", "input_required", "expected", "timeout_seconds",
            "expected_static_bytes", "excluded_reason"}, name)
        case_id = _text(case["id"], f"{name}.id")
        if case_id in seen:
            raise BenchmarkError(f"duplicate case id: {case_id}")
        seen.add(case_id)
        if not isinstance(case["input_required"], bool):
            raise BenchmarkError(f"{name}.input_required must be boolean")
        _positive(case["timeout_seconds"], f"{name}.timeout_seconds")
        static_bytes = _positive_int(case["expected_static_bytes"], f"{name}.expected_static_bytes",
            allow_zero=True)
        if static_bytes > max_static:
            raise BenchmarkError(f"{case_id} expected static storage exceeds manifest limit")
        if case["excluded_reason"] is not None:
            _text(case["excluded_reason"], f"{name}.excluded_reason")
        source = _contained_file(root, case["source"], f"{name}.source")
        if source.suffix != ".sy":
            raise BenchmarkError(f"{name}.source must be a .sy file")
        _contained_file(root, case["expected"], f"{name}.expected")
        if case["input"] is None:
            if case["input_required"]:
                raise BenchmarkError(f"{case_id} requires an input file")
        else:
            input_path = _contained_file(root, case["input"], f"{name}.input")
            if case["input_required"] and input_path.stat().st_size == 0:
                raise BenchmarkError(f"{case_id} requires non-empty input")
    return manifest


def analyze(document, bootstrap_samples=10_000):
    _keys(document, {"schema_version", "comparison", "evidence_level", "target", "abi",
        "runtime", "cases"}, "results")
    if document["schema_version"] != 1:
        raise BenchmarkError("results schema_version must be 1")
    if document["comparison"] not in {"r1_full", "r2_llvm"}:
        raise BenchmarkError("comparison must be r1_full or r2_llvm")
    if document["evidence_level"] not in {"qemu_proxy", "boom_hardware"}:
        raise BenchmarkError("evidence_level must be qemu_proxy or boom_hardware")
    for key in ("target", "abi", "runtime"):
        _text(document[key], f"results.{key}")
    if not isinstance(document["cases"], list) or not document["cases"]:
        raise BenchmarkError("results.cases must be a non-empty array")
    ratios = []
    included = []
    excluded = []
    seen = set()
    compile_times = []
    peaks = []
    code_sizes = []
    for index, case in enumerate(document["cases"]):
        name = f"results.cases[{index}]"
        _keys(case, {"id", "excluded_reason", "runs"}, name)
        case_id = _text(case["id"], f"{name}.id")
        if case_id in seen:
            raise BenchmarkError(f"duplicate result case id: {case_id}")
        seen.add(case_id)
        if case["excluded_reason"] is not None:
            excluded.append((case_id, _text(case["excluded_reason"], f"{name}.excluded_reason")))
            if case["runs"] != []:
                raise BenchmarkError(f"excluded case {case_id} must not contain runs")
            continue
        if not isinstance(case["runs"], list) or len(case["runs"]) < 5:
            raise BenchmarkError(f"included case {case_id} requires at least five paired runs")
        run_ratios = []
        for run_index, run in enumerate(case["runs"]):
            run_name = f"{name}.runs[{run_index}]"
            _keys(run, {"baseline_seconds", "candidate_seconds", "baseline_compile_seconds",
                "candidate_compile_seconds", "baseline_peak_bytes", "candidate_peak_bytes",
                "baseline_code_bytes", "candidate_code_bytes", "cold_start", "cache_reused"}, run_name)
            if run["cold_start"] is not True or run["cache_reused"] is not False:
                raise BenchmarkError(f"{run_name} must be a cold start without cache reuse")
            baseline = _positive(run["baseline_seconds"], f"{run_name}.baseline_seconds")
            candidate = _positive(run["candidate_seconds"], f"{run_name}.candidate_seconds")
            run_ratios.append(baseline / candidate)
            compile_times.append((_positive(run["baseline_compile_seconds"],
                f"{run_name}.baseline_compile_seconds"), _positive(run["candidate_compile_seconds"],
                f"{run_name}.candidate_compile_seconds")))
            peaks.append((_positive_int(run["baseline_peak_bytes"], f"{run_name}.baseline_peak_bytes"),
                _positive_int(run["candidate_peak_bytes"], f"{run_name}.candidate_peak_bytes")))
            code_sizes.append((_positive_int(run["baseline_code_bytes"], f"{run_name}.baseline_code_bytes"),
                _positive_int(run["candidate_code_bytes"], f"{run_name}.candidate_code_bytes")))
        ratio = _geomean(run_ratios)
        ratios.append(ratio)
        included.append((case_id, ratio))
    if not ratios:
        raise BenchmarkError("results contain no included cases")
    gm = _geomean(ratios)
    rng = random.Random(0)
    boot = sorted(_geomean([rng.choice(ratios) for _ in ratios])
        for _ in range(_positive_int(bootstrap_samples, "bootstrap_samples")))
    lower = _percentile(boot, 0.025)
    upper = _percentile(boot, 0.975)
    worst_id, worst = min(included, key=lambda item: item[1])
    formal = document["evidence_level"] == "boom_hardware"
    threshold = lower >= 1.0 if document["comparison"] == "r1_full" else lower > 1.0
    passed = formal and threshold and worst >= 0.90
    return {"gm": gm, "ci_lower": lower, "ci_upper": upper, "worst_case": worst_id,
        "worst_ratio": worst, "included": len(included), "total": len(document["cases"]),
        "excluded": excluded, "formal_evidence": formal, "gate_passed": passed,
        "compile_seconds_median": tuple(statistics.median(values) for values in zip(*compile_times)),
        "peak_bytes_max": tuple(max(values) for values in zip(*peaks)),
        "code_bytes_median": tuple(statistics.median(values) for values in zip(*code_sizes))}


def render(document, analysis):
    return "\n".join((f"# ACCELA {document['comparison']} paired report", "",
        f"- Evidence: `{document['evidence_level']}`; formal=`{str(analysis['formal_evidence']).lower()}`",
        f"- Coverage: {analysis['included']}/{analysis['total']}",
        f"- Paired GM: {analysis['gm']:.6f}",
        f"- 95% case-bootstrap CI: [{analysis['ci_lower']:.6f}, {analysis['ci_upper']:.6f}]",
        f"- Worst case: `{analysis['worst_case']}` = {analysis['worst_ratio']:.6f}",
        f"- Gate passed: `{str(analysis['gate_passed']).lower()}`", "",
        "Compile time and memory are reported only; they are not release limits.", ""))


def _keys(value, expected, name):
    if not isinstance(value, dict) or set(value) != expected:
        raise BenchmarkError(f"{name} fields are invalid")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkError(f"{name} must be non-empty text")
    return value


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value <= 0:
        raise BenchmarkError(f"{name} must be finite and positive")
    return float(value)


def _positive_int(value, name, allow_zero=False):
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BenchmarkError(f"{name} must be an integer >= {minimum}")
    return value


def _contained_file(root, value, name):
    text = _text(value, name)
    path = Path(text)
    if path.is_absolute():
        raise BenchmarkError(f"{name} must be relative")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exception:
        raise BenchmarkError(f"{name} escapes the manifest directory") from exception
    if not resolved.is_file():
        raise BenchmarkError(f"{name} does not exist: {text}")
    return resolved


def _geomean(values):
    return math.exp(sum(math.log(value) for value in values) / len(values))


def _percentile(values, fraction):
    position = fraction * (len(values) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1.0 - weight) + values[upper] * weight
