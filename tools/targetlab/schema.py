from __future__ import annotations

import math
from collections.abc import Mapping

INSTRUCTION_CLASSES = (
    "integer_alu", "integer_mul", "integer_div", "float_alu", "float_mul",
    "float_div", "load", "store", "branch", "call_return", "address", "move",
)


class ValidationError(ValueError):
    pass


def _table(value, name):
    if not isinstance(value, Mapping):
        raise ValidationError(f"{name} must be a table")
    return value


def _keys(table, expected, name):
    unknown = set(table) - set(expected)
    missing = set(expected) - set(table)
    if unknown:
        raise ValidationError(f"{name} contains unknown keys: {', '.join(sorted(unknown))}")
    if missing:
        raise ValidationError(f"{name} misses keys: {', '.join(sorted(missing))}")


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValidationError(f"{name} must be finite and positive")
    return value


def _measurement(table, name, max_relative_mad, minimum_samples=1):
    table = _table(table, name)
    _keys(table, ("median", "mad", "sample_count", "source", "validity"), name)
    median = _positive(table["median"], f"{name}.median")
    mad = table["mad"]
    if isinstance(mad, bool) or not isinstance(mad, (int, float)) or not math.isfinite(mad) or mad < 0:
        raise ValidationError(f"{name}.mad must be finite and non-negative")
    if isinstance(table["sample_count"], bool) or not isinstance(table["sample_count"], int) \
            or table["sample_count"] < minimum_samples:
        raise ValidationError(f"{name}.sample_count must be at least {minimum_samples}")
    if not isinstance(table["source"], str) or not table["source"].strip():
        raise ValidationError(f"{name}.source must be non-empty")
    if table["validity"] != "accepted":
        raise ValidationError(f"{name}.validity must be accepted")
    if mad / median > max_relative_mad:
        raise ValidationError(
            f"{name} is unstable: MAD/median={mad / median:.6f} exceeds {max_relative_mad:.6f}"
        )


def validate_profile(profile):
    profile = _table(profile, "profile")
    _keys(profile, ("schema_version", "profile", "target", "measurement_environment",
        "scheduler", "operations", "pairing", "branch", "spills", "diagnostics", "simd"), "profile")
    if isinstance(profile["schema_version"], bool) or profile["schema_version"] != 1:
        raise ValidationError("unsupported schema_version")

    identity = _table(profile["profile"], "profile.profile")
    _keys(identity, ("id", "calibrated", "evidence_level"), "profile.profile")
    if not isinstance(identity["id"], str) or not identity["id"].strip():
        raise ValidationError("profile.profile.id must be non-empty")
    if not isinstance(identity["calibrated"], bool):
        raise ValidationError("profile.profile.calibrated must be boolean")
    if identity["evidence_level"] not in {"declared", "qemu_proxy", "target_hardware"}:
        raise ValidationError("profile.profile.evidence_level is invalid")
    if identity["calibrated"] != (identity["evidence_level"] != "declared"):
        raise ValidationError("calibrated and evidence_level are inconsistent")
    minimum_samples = 9 if identity["calibrated"] else 1

    target = _table(profile["target"], "target")
    _keys(target, ("isa", "abi", "code_model", "clock_hz", "fetch_width", "issue_width", "retire_width"), "target")
    for key in ("isa", "abi", "code_model"):
        if not isinstance(target[key], str) or not target[key].strip():
            raise ValidationError(f"target.{key} must be non-empty")
    if isinstance(target["clock_hz"], bool) or not isinstance(target["clock_hz"], int) \
            or target["clock_hz"] <= 0:
        raise ValidationError("target.clock_hz must be a positive integer")
    for key in ("fetch_width", "issue_width", "retire_width"):
        if isinstance(target[key], bool) or not isinstance(target[key], int) or not 1 <= target[key] <= 16:
            raise ValidationError(f"target.{key} must be an integer in 1..16")

    environment = _table(profile["measurement_environment"], "measurement_environment")
    _keys(environment, ("backend", "rdcycle", "rdinstret", "timer", "clock_hz",
        "minimum_cycles", "warmup_count", "sample_count", "measurement_mode"),
        "measurement_environment")
    if not isinstance(environment["backend"], str) \
            or environment["backend"] not in {"unmeasured", "linux", "baremetal"}:
        raise ValidationError("measurement_environment.backend is invalid")
    if not isinstance(environment["timer"], str) \
            or environment["timer"] not in {"unmeasured", "rdcycle", "clock_gettime"}:
        raise ValidationError("measurement_environment.timer is invalid")
    for key in ("rdcycle", "rdinstret"):
        if environment[key] is not None and not isinstance(environment[key], bool):
            raise ValidationError(f"measurement_environment.{key} must be boolean or null")
    if environment["clock_hz"] != target["clock_hz"]:
        raise ValidationError("measurement_environment.clock_hz must match target.clock_hz")
    if not isinstance(environment["minimum_cycles"], int) or environment["minimum_cycles"] < 1_000_000:
        raise ValidationError("measurement_environment.minimum_cycles must be at least 1000000")
    if isinstance(environment["warmup_count"], bool) or isinstance(environment["sample_count"], bool) \
            or environment["warmup_count"] != 2 or environment["sample_count"] != 9:
        raise ValidationError("measurement_environment requires 2 warmups and 9 samples")
    if environment["measurement_mode"] not in {"declared", "hardware", "qemu_proxy"}:
        raise ValidationError("measurement_environment.measurement_mode is invalid")
    if identity["calibrated"]:
        if environment["backend"] == "unmeasured" or environment["timer"] == "unmeasured":
            raise ValidationError("calibrated profiles require a measured environment")
        if environment["timer"] == "rdcycle" and environment["rdcycle"] is not True:
            raise ValidationError("rdcycle timer requires measured rdcycle availability")
        if environment["measurement_mode"] == "declared":
            raise ValidationError("calibrated profiles require hardware or qemu_proxy measurements")

    scheduler = _table(profile["scheduler"], "scheduler")
    _keys(scheduler, ("enabled", "beam_width", "max_function_expansions", "max_module_expansions", "uncertainty_weight"), "scheduler")
    if not isinstance(scheduler["enabled"], bool):
        raise ValidationError("scheduler.enabled must be boolean")
    beam = scheduler["beam_width"]
    if isinstance(beam, bool) or not isinstance(beam, int) or not 1 <= beam <= 64:
        raise ValidationError("scheduler.beam_width must be an integer in 1..64")
    if isinstance(scheduler["max_function_expansions"], bool) \
            or not isinstance(scheduler["max_function_expansions"], int) \
            or scheduler["max_function_expansions"] < beam:
        raise ValidationError("scheduler.max_function_expansions must be at least beam_width")
    if isinstance(scheduler["max_module_expansions"], bool) \
            or not isinstance(scheduler["max_module_expansions"], int) \
            or scheduler["max_module_expansions"] < scheduler["max_function_expansions"]:
        raise ValidationError("scheduler.max_module_expansions must be at least the function budget")
    if isinstance(scheduler["uncertainty_weight"], bool) \
            or not isinstance(scheduler["uncertainty_weight"], (int, float)) \
            or not math.isfinite(scheduler["uncertainty_weight"]) \
            or scheduler["uncertainty_weight"] < 0:
        raise ValidationError("scheduler.uncertainty_weight must be non-negative")

    operations = _table(profile["operations"], "operations")
    _keys(operations, INSTRUCTION_CLASSES, "operations")
    for name in INSTRUCTION_CLASSES:
        operation = _table(operations[name], f"operations.{name}")
        _keys(operation, ("latency", "throughput", "resource_occupancy", "code_bytes", "resource"), f"operations.{name}")
        _measurement(operation["latency"], f"operations.{name}.latency", 0.01, minimum_samples)
        _measurement(operation["throughput"], f"operations.{name}.throughput", 0.01, minimum_samples)
        _positive(operation["resource_occupancy"], f"operations.{name}.resource_occupancy")
        if isinstance(operation["code_bytes"], bool) or not isinstance(operation["code_bytes"], int) \
                or operation["code_bytes"] < 1:
            raise ValidationError(f"operations.{name}.code_bytes must be positive")
        if not isinstance(operation["resource"], str) or not operation["resource"].strip():
            raise ValidationError(f"operations.{name}.resource must be non-empty")

    pairing = _table(profile["pairing"], "pairing")
    _keys(pairing, INSTRUCTION_CLASSES, "pairing")
    for left in INSTRUCTION_CLASSES:
        row = _table(pairing[left], f"pairing.{left}")
        _keys(row, INSTRUCTION_CLASSES, f"pairing.{left}")
        for right in INSTRUCTION_CLASSES:
            _measurement(row[right], f"pairing.{left}.{right}", 0.01, minimum_samples)
            if row[right] != pairing[right][left]:
                raise ValidationError(f"pairing matrix is asymmetric at {left}/{right}")

    branch = _table(profile["branch"], "branch")
    _keys(branch, ("predictable", "unpredictable"), "branch")
    _measurement(branch["predictable"], "branch.predictable", 0.05, minimum_samples)
    _measurement(branch["unpredictable"], "branch.unpredictable", 0.05, minimum_samples)
    spills = _table(profile["spills"], "spills")
    _keys(spills, ("load", "store"), "spills")
    _measurement(spills["load"], "spills.load", 0.05, minimum_samples)
    _measurement(spills["store"], "spills.store", 0.05, minimum_samples)
    diagnostics = _table(profile["diagnostics"], "diagnostics")
    _keys(diagnostics, ("load_use", "pointer_chase", "working_set", "stride", "frontend",
        "register_pressure"), "diagnostics")
    _measurement(diagnostics["load_use"], "diagnostics.load_use", 0.05, minimum_samples)
    _measurement(diagnostics["pointer_chase"], "diagnostics.pointer_chase", 0.05, minimum_samples)
    for group, keys in (("working_set", ("4096", "32768", "262144")),
                        ("stride", ("8", "64", "512")),
                        ("frontend", ("64", "256", "1024")),
                        ("register_pressure", ("8", "16", "24", "32"))):
        table = _table(diagnostics[group], f"diagnostics.{group}")
        _keys(table, keys, f"diagnostics.{group}")
        for key in keys:
            _measurement(table[key], f"diagnostics.{group}.{key}", 0.05, minimum_samples)
    simd = _table(profile["simd"], "simd")
    _keys(simd, ("enabled", "isa_extension", "abi", "register_classes", "benchmark_classes"),
        "simd")
    if not isinstance(simd["enabled"], bool):
        raise ValidationError("simd.enabled must be boolean")
    for key in ("register_classes", "benchmark_classes"):
        if not isinstance(simd[key], list) or any(
                not isinstance(item, str) or not item.strip() for item in simd[key]):
            raise ValidationError(f"simd.{key} must be an array of non-empty strings")
        if len(simd[key]) != len(set(simd[key])):
            raise ValidationError(f"simd.{key} contains duplicates")
    if simd["enabled"]:
        for key in ("isa_extension", "abi"):
            if not isinstance(simd[key], str) or not simd[key].strip():
                raise ValidationError(f"enabled SIMD requires simd.{key}")
        if not simd["register_classes"] or not simd["benchmark_classes"]:
            raise ValidationError("enabled SIMD requires register and benchmark classes")
    elif simd["isa_extension"] is not None or simd["abi"] is not None \
            or simd["register_classes"] or simd["benchmark_classes"]:
        raise ValidationError("disabled SIMD must not declare fictional ABI or benchmark data")
    if identity["calibrated"]:
        expected_evidence = "qemu_proxy" if environment["measurement_mode"] == "qemu_proxy" \
            else "target_hardware"
        if identity["evidence_level"] != expected_evidence:
            raise ValidationError("profile evidence_level conflicts with measurement mode")
        expected_source = environment["timer"]
        for name, measurement in _measurement_nodes(profile):
            if measurement["source"] != expected_source:
                raise ValidationError(f"{name}.source conflicts with measurement_environment.timer")
    return profile


def _measurement_nodes(value, path="profile"):
    if not isinstance(value, Mapping):
        return
    if set(value) == {"median", "mad", "sample_count", "source", "validity"}:
        yield path, value
        return
    for key, child in value.items():
        yield from _measurement_nodes(child, f"{path}.{key}")
