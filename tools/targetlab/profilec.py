from __future__ import annotations

import copy
import json
import statistics
from pathlib import Path

from .schema import INSTRUCTION_CLASSES, ValidationError, validate_profile


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as stream:
        return parse_json(stream.read(), str(path))


def parse_json(text, source="JSON input"):
    try:
        return json.loads(text, object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValidationError(f"non-finite JSON number is forbidden: {value}")))
    except json.JSONDecodeError as exception:
        raise ValidationError(f"invalid JSON in {source}: {exception}") from exception


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def canonical_json(document) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def measurement(values, source, clock_hz):
    if len(values) != 9:
        raise ValidationError("measurement must contain exactly 9 samples")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
        raise ValidationError("raw measurement values must be positive integers")
    numeric = [float(value) for value in values]
    if source == "rdcycle_x1000":
        numeric = [value / 1000.0 for value in numeric]
        source = "rdcycle"
    elif source == "clock_gettime_ns_x1000":
        numeric = [value / 1000.0 * clock_hz / 1_000_000_000.0 for value in numeric]
        source = "clock_gettime"
    median = statistics.median(numeric)
    deviations = [abs(value - median) for value in numeric]
    return {"median": float(median), "mad": float(statistics.median(deviations)),
        "sample_count": len(numeric), "source": source}


def profile_from_raw(template, raw):
    profile = copy.deepcopy(template)
    validate_profile(profile)
    if raw.get("schema_version") != 1 or set(raw) != {"schema_version", "environment", "samples"}:
        raise ValidationError("raw archive must contain schema_version=1, environment, and samples only")
    environment = raw["environment"]
    if not isinstance(environment, dict) or set(environment) != {"backend", "rdcycle", "rdinstret", "timer"}:
        raise ValidationError("raw environment is invalid")
    if environment["backend"] not in {"linux", "baremetal"}:
        raise ValidationError("raw environment backend is invalid")
    if not isinstance(environment["rdcycle"], bool) or not isinstance(environment["rdinstret"], bool):
        raise ValidationError("raw counter availability must be boolean")
    if environment["timer"] not in {"rdcycle", "clock_gettime"}:
        raise ValidationError("raw timer is invalid")
    profile["measurement_environment"] = copy.deepcopy(environment)
    expected = _expected_metrics(profile)
    seen = set()
    for index, sample in enumerate(raw["samples"]):
        if set(sample) != {"metric", "category", "source", "values"}:
            raise ValidationError(f"samples[{index}] has invalid keys")
        metric = sample["metric"]
        if metric in seen:
            raise ValidationError(f"duplicate raw metric {metric}")
        seen.add(metric)
        if metric not in expected:
            raise ValidationError(f"unsupported raw metric {metric}")
        if len(sample["values"]) != 9:
            raise ValidationError(f"raw metric {metric} must contain exactly 9 samples")
        expected_source = "rdcycle_x1000" if environment["timer"] == "rdcycle" \
            else "clock_gettime_ns_x1000"
        if sample["source"] != expected_source:
            raise ValidationError(f"raw metric {metric} source conflicts with measured environment")
        path = metric.split(".")
        if path[0] not in {"operations", "pairing", "branch", "spills", "diagnostics"}:
            raise ValidationError(f"unsupported raw metric {metric}")
        current = profile
        for part in path[:-1]:
            if part not in current or not isinstance(current[part], dict):
                raise ValidationError(f"raw metric does not exist in template: {metric}")
            current = current[part]
        leaf = path[-1]
        if leaf not in current or not isinstance(current[leaf], dict):
            raise ValidationError(f"raw metric does not name a measurement: {metric}")
        measured = measurement(sample["values"], sample["source"], profile["target"]["clock_hz"])
        current[leaf] = measured
        if path[0] == "pairing" and path[1] != path[2]:
            profile["pairing"][path[2]][path[1]] = copy.deepcopy(measured)
    missing = expected - seen
    if missing:
        raise ValidationError(f"raw archive misses {len(missing)} required metrics; first: {sorted(missing)[0]}")
    profile["profile"]["calibrated"] = True
    return validate_profile(profile)


def _expected_metrics(profile):
    result = set()
    for name in INSTRUCTION_CLASSES:
        result.update((f"operations.{name}.latency", f"operations.{name}.throughput"))
        for right in INSTRUCTION_CLASSES[INSTRUCTION_CLASSES.index(name):]:
            result.add(f"pairing.{name}.{right}")
    result.update(("branch.predictable", "branch.unpredictable", "spills.load", "spills.store"))
    result.update(("diagnostics.load_use", "diagnostics.pointer_chase"))
    for group, keys in (("working_set", ("4096", "32768", "262144")),
                        ("stride", ("8", "64", "512")),
                        ("frontend", ("64", "256", "1024")),
                        ("register_pressure", ("8", "16", "24", "32"))):
        result.update(f"diagnostics.{group}.{key}" for key in keys)
    return result


def generate_java(profile) -> str:
    validate_profile(profile)
    identity, target, scheduler = profile["profile"], profile["target"], profile["scheduler"]
    lines = ["package accela.cost;", "",
        "/** Generated by tools.targetlab.profilec. Do not edit manually. */",
        "public final class GeneratedTargetProfile {",
        "  private static final TargetProfile INSTANCE = create();",
        "  private GeneratedTargetProfile() {}",
        "  public static TargetProfile get() { return INSTANCE; }",
        "  private static TargetProfile create() {",
        "    TargetProfile.Builder builder = TargetProfile.builder()",
        f"        .identity({_java_string(identity['id'])}, {_java_string(target['isa'])}, {_java_string(target['abi'])}, {_java_string(target['code_model'])})",
        f"        .core({int(target['clock_hz'])}L, {target['fetch_width']}, {target['issue_width']}, {target['retire_width']})",
        f"        .capabilities({str(identity['calibrated']).lower()}, {str(profile['simd']['enabled']).lower()})",
        "        .scheduler(new SchedulerPolicy(" + ", ".join((str(scheduler["beam_width"]),
            str(scheduler["max_function_expansions"]), str(scheduler["max_module_expansions"]),
            _double(scheduler["uncertainty_weight"]), str(scheduler["enabled"]).lower())) + "));" ]
    for name in INSTRUCTION_CLASSES:
        operation = profile["operations"][name]
        lines.extend((f"    builder.operation(InstructionClass.{name.upper()}, new TargetProfile.OperationCost(",
            f"        {_java_measurement(operation['latency'])},",
            f"        {_java_measurement(operation['throughput'])},",
            f"        {_double(operation['resource_occupancy'])}, {operation['code_bytes']}, {_java_string(operation['resource'])}));"))
    for left_index, left in enumerate(INSTRUCTION_CLASSES):
        for right in INSTRUCTION_CLASSES[left_index:]:
            lines.append(f"    builder.pair(InstructionClass.{left.upper()}, InstructionClass.{right.upper()}, "
                f"{_java_measurement(profile['pairing'][left][right])});")
    lines.extend((f"    builder.branch({_java_measurement(profile['branch']['predictable'])}, {_java_measurement(profile['branch']['unpredictable'])});",
        f"    builder.spills({_java_measurement(profile['spills']['load'])}, {_java_measurement(profile['spills']['store'])});",
        "    builder.diagnostics(new TargetProfile.DiagnosticCosts(",
        f"        {_java_measurement(profile['diagnostics']['load_use'])},",
        f"        {_java_measurement(profile['diagnostics']['pointer_chase'])},",
        f"        {_java_curve(profile['diagnostics']['working_set'])},",
        f"        {_java_curve(profile['diagnostics']['stride'])},",
        f"        {_java_curve(profile['diagnostics']['frontend'])},",
        f"        {_java_curve(profile['diagnostics']['register_pressure'])}));",
        "    return builder.build();", "  }", "}", ""))
    return "\n".join(lines)


def _double(value):
    rendered = repr(float(value))
    return rendered if "." in rendered or "e" in rendered.lower() else rendered + ".0"


def _java_string(value):
    return json.dumps(value, ensure_ascii=False)


def _java_measurement(value):
    return "new Measurement(" + _double(value["median"]) + ", " + _double(value["mad"]) \
        + f", {value['sample_count']}, " + _java_string(value["source"]) + ")"


def _java_curve(value):
    entries = []
    for point, measurement_value in sorted(value.items(), key=lambda item: int(item[0])):
        entries.extend((str(int(point)), _java_measurement(measurement_value)))
    return "new java.util.TreeMap<>(java.util.Map.of(" + ", ".join(entries) + "))"


def embed(profile_path: Path, output_path: Path, verify=False):
    generated = generate_java(load_json(profile_path))
    if verify:
        if not output_path.exists() or output_path.read_text(encoding="utf-8") != generated:
            raise ValidationError("generated profile is stale; run the embed command")
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(generated, encoding="utf-8", newline="\n")
