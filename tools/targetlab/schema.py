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


def _measurement(table, name, max_relative_mad):
    table = _table(table, name)
    _keys(table, ("median", "mad", "sample_count", "source"), name)
    median = _positive(table["median"], f"{name}.median")
    mad = table["mad"]
    if isinstance(mad, bool) or not isinstance(mad, (int, float)) or not math.isfinite(mad) or mad < 0:
        raise ValidationError(f"{name}.mad must be finite and non-negative")
    if not isinstance(table["sample_count"], int) or table["sample_count"] < 1:
        raise ValidationError(f"{name}.sample_count must be positive")
    if not isinstance(table["source"], str) or not table["source"].strip():
        raise ValidationError(f"{name}.source must be non-empty")
    if mad / median > max_relative_mad:
        raise ValidationError(
            f"{name} is unstable: MAD/median={mad / median:.6f} exceeds {max_relative_mad:.6f}"
        )


def validate_profile(profile):
    profile = _table(profile, "profile")
    _keys(profile, ("schema_version", "profile", "target", "scheduler", "operations", "pairing", "branch", "spills", "simd"), "profile")
    if profile["schema_version"] != 1:
        raise ValidationError("unsupported schema_version")

    identity = _table(profile["profile"], "profile.profile")
    _keys(identity, ("id", "calibrated"), "profile.profile")
    if not isinstance(identity["id"], str) or not identity["id"].strip():
        raise ValidationError("profile.profile.id must be non-empty")
    if not isinstance(identity["calibrated"], bool):
        raise ValidationError("profile.profile.calibrated must be boolean")

    target = _table(profile["target"], "target")
    _keys(target, ("isa", "abi", "code_model", "clock_hz", "fetch_width", "issue_width", "retire_width"), "target")
    for key in ("isa", "abi", "code_model"):
        if not isinstance(target[key], str) or not target[key].strip():
            raise ValidationError(f"target.{key} must be non-empty")
    _positive(target["clock_hz"], "target.clock_hz")
    for key in ("fetch_width", "issue_width", "retire_width"):
        if not isinstance(target[key], int) or not 1 <= target[key] <= 16:
            raise ValidationError(f"target.{key} must be an integer in 1..16")

    scheduler = _table(profile["scheduler"], "scheduler")
    _keys(scheduler, ("enabled", "beam_width", "max_function_expansions", "max_module_expansions", "uncertainty_weight"), "scheduler")
    if not isinstance(scheduler["enabled"], bool):
        raise ValidationError("scheduler.enabled must be boolean")
    beam = scheduler["beam_width"]
    if not isinstance(beam, int) or not 1 <= beam <= 64:
        raise ValidationError("scheduler.beam_width must be an integer in 1..64")
    if not isinstance(scheduler["max_function_expansions"], int) or scheduler["max_function_expansions"] < beam:
        raise ValidationError("scheduler.max_function_expansions must be at least beam_width")
    if not isinstance(scheduler["max_module_expansions"], int) or scheduler["max_module_expansions"] < scheduler["max_function_expansions"]:
        raise ValidationError("scheduler.max_module_expansions must be at least the function budget")
    if scheduler["uncertainty_weight"] < 0:
        raise ValidationError("scheduler.uncertainty_weight must be non-negative")

    operations = _table(profile["operations"], "operations")
    _keys(operations, INSTRUCTION_CLASSES, "operations")
    for name in INSTRUCTION_CLASSES:
        operation = _table(operations[name], f"operations.{name}")
        _keys(operation, ("latency", "throughput", "resource_occupancy", "code_bytes", "resource"), f"operations.{name}")
        _measurement(operation["latency"], f"operations.{name}.latency", 0.01)
        _measurement(operation["throughput"], f"operations.{name}.throughput", 0.01)
        _positive(operation["resource_occupancy"], f"operations.{name}.resource_occupancy")
        if not isinstance(operation["code_bytes"], int) or operation["code_bytes"] < 1:
            raise ValidationError(f"operations.{name}.code_bytes must be positive")
        if not isinstance(operation["resource"], str) or not operation["resource"].strip():
            raise ValidationError(f"operations.{name}.resource must be non-empty")

    pairing = _table(profile["pairing"], "pairing")
    _keys(pairing, INSTRUCTION_CLASSES, "pairing")
    for left in INSTRUCTION_CLASSES:
        row = _table(pairing[left], f"pairing.{left}")
        _keys(row, INSTRUCTION_CLASSES, f"pairing.{left}")
        for right in INSTRUCTION_CLASSES:
            _measurement(row[right], f"pairing.{left}.{right}", 0.01)
            if row[right]["median"] != pairing[right][left]["median"]:
                raise ValidationError(f"pairing matrix is asymmetric at {left}/{right}")

    branch = _table(profile["branch"], "branch")
    _keys(branch, ("predictable", "unpredictable"), "branch")
    _measurement(branch["predictable"], "branch.predictable", 0.05)
    _measurement(branch["unpredictable"], "branch.unpredictable", 0.05)
    spills = _table(profile["spills"], "spills")
    _keys(spills, ("load", "store"), "spills")
    _measurement(spills["load"], "spills.load", 0.05)
    _measurement(spills["store"], "spills.store", 0.05)
    simd = _table(profile["simd"], "simd")
    _keys(simd, ("enabled",), "simd")
    if not isinstance(simd["enabled"], bool):
        raise ValidationError("simd.enabled must be boolean")
    return profile
