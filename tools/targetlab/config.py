from __future__ import annotations

from collections.abc import Mapping

from .schema import ValidationError


def validate_config(value):
    if not isinstance(value, Mapping):
        raise ValidationError("TargetLab configuration must be a JSON object")
    backend = value.get("backend")
    common = {"backend", "cc", "objcopy", "nm", "build_dir", "clock_hz", "minimum_cycles",
        "timeout_seconds", "measurement_mode"}
    expected = common | ({"execute"} if backend == "linux" else
                         {"gdb", "gdb_remote", "startup", "linker", "debug_server"}
                         if backend == "baremetal" else set())
    unknown = set(value) - expected
    missing = expected - set(value)
    if backend not in {"linux", "baremetal"}:
        raise ValidationError("backend must be linux or baremetal")
    if unknown:
        raise ValidationError(f"configuration contains unknown keys: {', '.join(sorted(unknown))}")
    if missing:
        raise ValidationError(f"configuration misses keys: {', '.join(sorted(missing))}")
    for key in expected - {"clock_hz", "minimum_cycles", "timeout_seconds", "debug_server"}:
        if key != "backend" and (not isinstance(value[key], str) or not value[key].strip()):
            raise ValidationError(f"configuration.{key} must be non-empty")
        if key != "backend" and any(character in value[key] for character in "\r\n\0"):
            raise ValidationError(f"configuration.{key} contains a forbidden control character")
    for key in ("clock_hz", "minimum_cycles", "timeout_seconds"):
        if isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] <= 0:
            raise ValidationError(f"configuration.{key} must be a positive integer")
    if value["measurement_mode"] not in {"hardware", "qemu_proxy"}:
        raise ValidationError("configuration.measurement_mode must be hardware or qemu_proxy")
    if backend == "baremetal":
        _validate_debug_server(value["debug_server"])
    return value


def _validate_debug_server(server):
    if not isinstance(server, Mapping):
        raise ValidationError("configuration.debug_server must be an object")
    kind = server.get("kind")
    expected = {"kind", "mode", "executable", "config"} if kind == "openocd" else \
        {"kind", "mode", "executable", "machine", "memory"} if kind == "qemu" else set()
    if not expected:
        raise ValidationError("configuration.debug_server.kind must be openocd or qemu")
    unknown = set(server) - expected
    missing = expected - set(server)
    if unknown or missing:
        raise ValidationError("configuration.debug_server fields do not match its kind")
    if server["mode"] not in {"managed", "external"}:
        raise ValidationError("configuration.debug_server.mode must be managed or external")
    for key in expected - {"kind", "mode"}:
        if not isinstance(server[key], str) or not server[key].strip() \
                or any(character in server[key] for character in "\r\n\0"):
            raise ValidationError(f"configuration.debug_server.{key} must be non-empty safe text")
