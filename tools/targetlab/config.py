from __future__ import annotations

from collections.abc import Mapping

from .schema import ValidationError


def validate_config(value):
    if not isinstance(value, Mapping):
        raise ValidationError("TargetLab configuration must be a JSON object")
    backend = value.get("backend")
    common = {"backend", "cc", "objcopy", "nm", "build_dir", "clock_hz", "minimum_cycles", "timeout_seconds"}
    expected = common | ({"execute"} if backend == "linux" else
                         {"gdb", "gdb_remote", "startup", "linker", "openocd",
                          "openocd_config", "openocd_mode"} if backend == "baremetal" else set())
    unknown = set(value) - expected
    missing = expected - set(value)
    if backend not in {"linux", "baremetal"}:
        raise ValidationError("backend must be linux or baremetal")
    if unknown:
        raise ValidationError(f"configuration contains unknown keys: {', '.join(sorted(unknown))}")
    if missing:
        raise ValidationError(f"configuration misses keys: {', '.join(sorted(missing))}")
    for key in expected - {"clock_hz", "minimum_cycles", "timeout_seconds"}:
        if key != "backend" and (not isinstance(value[key], str) or not value[key].strip()):
            raise ValidationError(f"configuration.{key} must be non-empty")
        if key != "backend" and any(character in value[key] for character in "\r\n\0"):
            raise ValidationError(f"configuration.{key} contains a forbidden control character")
    for key in ("clock_hz", "minimum_cycles", "timeout_seconds"):
        if isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] <= 0:
            raise ValidationError(f"configuration.{key} must be a positive integer")
    if backend == "baremetal" and value["openocd_mode"] not in {"managed", "external"}:
        raise ValidationError("configuration.openocd_mode must be managed or external")
    return value
