from __future__ import annotations

import subprocess
import string
from pathlib import Path
from typing import Any, Mapping

from .adapters import StageSpec, WslPathMapper
from .errors import ConfigurationError, ExecutionError, ValidationError
from .schema import validate_document
from .util import sanitize_text, sha256_file, sha256_json


SOURCE_ASSETS = (
    "profile_plugin_source",
    "cache_plugin_source",
    "hotblocks_plugin_source",
    "runtime_filter_source",
    "runtime_source",
    "crt_source",
    "linker_script_source",
)
PLUGIN_ASSETS = (
    "profile_plugin_binary",
    "cache_plugin_binary",
    "hotblocks_plugin_binary",
)
REQUIRED_ASSETS = (*SOURCE_ASSETS, *PLUGIN_ASSETS, "qemu_binary", "runner_executable")

INPUT_TRANSPORT_SECTION = ".sysy_input_transport"
INPUT_TRANSPORT_SECTION_SIZE_BYTES = 4_112
INPUT_TRANSPORT = {
    "kind": "fw_cfg_dma",
    "item_name": "opt/accela/sysy-input",
    "exact_bytes": True,
    "eof": "size_delimited",
    "max_input_size_bytes": 4_294_967_295,
    "guest_buffer_size_bytes": 4_096,
    "guest_buffer_section": INPUT_TRANSPORT_SECTION,
    "transport_section_size_bytes": INPUT_TRANSPORT_SECTION_SIZE_BYTES,
}

_SOURCE_FIELDS = {
    "profile_plugin_source": "profile_plugin_sha256",
    "cache_plugin_source": "cache_plugin_sha256",
    "hotblocks_plugin_source": "hotblocks_plugin_sha256",
    "runtime_filter_source": "runtime_filter_sha256",
    "runtime_source": "runtime_sha256",
    "crt_source": "crt_sha256",
    "linker_script_source": "linker_script_sha256",
}
_PLUGIN_FIELDS = {
    "profile_plugin_binary": "profile_sha256",
    "cache_plugin_binary": "cache_sha256",
    "hotblocks_plugin_binary": "hotblocks_sha256",
}


def _normalized_assets(assets: Mapping[str, Path]) -> dict[str, Path]:
    if set(assets) != set(REQUIRED_ASSETS):
        missing = sorted(set(REQUIRED_ASSETS) - set(assets))
        extra = sorted(set(assets) - set(REQUIRED_ASSETS))
        raise ConfigurationError(
            "measurement protocol requires exact physical assets"
            f" (missing={','.join(missing) or '-'}; extra={','.join(extra) or '-'})"
        )
    normalized: dict[str, Path] = {}
    for key in REQUIRED_ASSETS:
        path = assets[key].resolve(strict=True)
        if not path.is_file():
            raise ConfigurationError(f"measurement protocol asset is not a regular file: {key}")
        normalized[key] = path
    return normalized


def _runner_command_sha256(runner: StageSpec, *, measurement_mode: str) -> str:
    if runner.kind != "qemu" or runner.command is None:
        raise ConfigurationError("measurement protocol requires a configured QEMU runner stage")
    if measurement_mode not in {"standard_proxy", "cache_hotblock"}:
        raise ConfigurationError("unknown measurement protocol mode")
    formatter = string.Formatter()
    command_placeholders: set[str] = set()
    all_placeholders: set[str] = set()
    for index, value in enumerate((*runner.command, *runner.environment.values())):
        try:
            parsed = {
                field_name
                for _, field_name, _, _ in formatter.parse(value)
                if field_name
            }
        except ValueError as exc:
            raise ConfigurationError(f"invalid QEMU runner command template: {exc}") from exc
        all_placeholders.update(parsed)
        if index < len(runner.command):
            command_placeholders.update(parsed)
    # The snapshot fixes a plugin log sink as well as the executable and exact
    # input payload.  All three must therefore be physical command arguments;
    # environment-only references cannot prove that the runner consumes them.
    for logical_name in ("binary", "metric_file", "input"):
        accepted = {logical_name, f"{logical_name}_host", f"{logical_name}_wsl"}
        if command_placeholders.isdisjoint(accepted):
            raise ConfigurationError(
                "QEMU runner command must reference the physical "
                f"{{{logical_name}}}, {{{logical_name}_host}}, or "
                f"{{{logical_name}_wsl}} artifact"
            )
    for logical_name in (
        "qemu_binary",
        "runner_executable",
        "profile_plugin_binary",
        "cache_plugin_binary",
        *(() if measurement_mode == "standard_proxy" else ("hotblocks_plugin_binary",)),
    ):
        accepted = {logical_name, f"{logical_name}_host", f"{logical_name}_wsl"}
        if all_placeholders.isdisjoint(accepted):
            raise ConfigurationError(
                f"QEMU runner must reference the physically verified {{{logical_name}}} asset"
            )
    return sha256_json(
        {"command": list(runner.command), "environment": dict(sorted(runner.environment.items()))}
    )


def _qemu_version(
    binary: Path,
    *,
    runner: StageSpec,
    wsl_executable: str,
    wsl_distribution: str | None,
) -> str:
    if runner.adapter == "host":
        command = [str(binary), "--version"]
    elif runner.adapter == "wsl":
        mapper = WslPathMapper(wsl_executable, wsl_distribution)
        command = mapper.wrap([mapper.to_wsl(binary), "--version"], cwd=Path.cwd())
    else:
        raise ConfigurationError("measurement protocol runner adapter must be host or wsl")
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExecutionError("cannot execute QEMU binary for protocol verification") from exc
    if result.returncode != 0:
        raise ExecutionError(
            "QEMU --version failed: "
            + sanitize_text(result.stderr.decode("utf-8", errors="replace")[-1024:])
        )
    lines = result.stdout.decode("utf-8", errors="strict").splitlines()
    if not lines or not lines[0].strip():
        raise ExecutionError("QEMU --version produced no version line")
    return lines[0].strip()


def capture_measurement_protocol(
    *,
    protocol_id: str,
    assets: Mapping[str, Path],
    runner: StageSpec,
    machine: str,
    cpu_model: str,
    memory: str,
    measurement_mode: str = "standard_proxy",
    wsl_executable: str = "wsl.exe",
    wsl_distribution: str | None = None,
) -> dict[str, Any]:
    normalized = _normalized_assets(assets)
    if not machine or not cpu_model or not memory:
        raise ConfigurationError("QEMU machine, cpu model, and memory must be non-empty")
    if measurement_mode not in {"standard_proxy", "cache_hotblock"}:
        raise ConfigurationError("unknown measurement protocol mode")
    runner_command_sha256 = _runner_command_sha256(
        runner, measurement_mode=measurement_mode
    )
    qemu_version = _qemu_version(
        normalized["qemu_binary"],
        runner=runner,
        wsl_executable=wsl_executable,
        wsl_distribution=wsl_distribution,
    )
    return validate_document(
        {
            "schema_version": "measurement-protocol.v1",
            "protocol_id": protocol_id,
            "measurement_mode": measurement_mode,
            "target": "rv64gc",
            "abi": "lp64d",
            "code_model": "medany",
            "input_transport": dict(INPUT_TRANSPORT),
            "sources": {
                field: sha256_file(normalized[key]) for key, field in _SOURCE_FIELDS.items()
            },
            "plugin_binaries": {
                field: sha256_file(normalized[key]) for key, field in _PLUGIN_FIELDS.items()
            },
            "qemu": {
                "binary_sha256": sha256_file(normalized["qemu_binary"]),
                "version": qemu_version,
                "machine": machine,
                "cpu_model": cpu_model,
                "accelerator": "tcg",
                "memory": memory,
                "plugin_log_flags": ["-d", "plugin", "-D", "{metric_file}"],
                "runner_command_sha256": runner_command_sha256,
                "runner_executable_sha256": sha256_file(normalized["runner_executable"]),
                "runner_adapter": runner.adapter,
                "wsl_distribution_sha256": (
                    sha256_json(wsl_distribution) if wsl_distribution is not None else None
                ),
            },
            "cache_model": {
                "size_bytes": 32768,
                "ways": 8,
                "line_bytes": 64,
                "replacement": "lru",
                "initial_state": "cold_per_timing_region",
            },
        }
    )


def verify_measurement_protocol(
    snapshot: Mapping[str, Any],
    *,
    assets: Mapping[str, Path],
    runner: StageSpec,
    wsl_executable: str = "wsl.exe",
    wsl_distribution: str | None = None,
) -> None:
    validated = validate_document(dict(snapshot))
    if validated["schema_version"] != "measurement-protocol.v1":
        raise ValidationError("measurement protocol must be measurement-protocol.v1")
    runner_command_sha256 = _runner_command_sha256(
        runner, measurement_mode=validated["measurement_mode"]
    )
    normalized = _normalized_assets(assets)
    for key, field in _SOURCE_FIELDS.items():
        if sha256_file(normalized[key]) != validated["sources"][field]:
            raise ValidationError(f"measurement protocol source hash drift: {key}")
    for key, field in _PLUGIN_FIELDS.items():
        if sha256_file(normalized[key]) != validated["plugin_binaries"][field]:
            raise ValidationError(f"measurement protocol plugin binary hash drift: {key}")
    if sha256_file(normalized["qemu_binary"]) != validated["qemu"]["binary_sha256"]:
        raise ValidationError("measurement protocol QEMU binary hash drift")
    if runner.adapter != validated["qemu"]["runner_adapter"]:
        raise ValidationError("measurement protocol QEMU runner adapter drift")
    observed_distribution = sha256_json(wsl_distribution) if wsl_distribution is not None else None
    if observed_distribution != validated["qemu"]["wsl_distribution_sha256"]:
        raise ValidationError("measurement protocol WSL distribution drift")
    if _qemu_version(
        normalized["qemu_binary"],
        runner=runner,
        wsl_executable=wsl_executable,
        wsl_distribution=wsl_distribution,
    ) != validated["qemu"]["version"]:
        raise ValidationError("measurement protocol QEMU version drift")
    if runner_command_sha256 != validated["qemu"]["runner_command_sha256"]:
        raise ValidationError("measurement protocol QEMU runner command/configuration drift")
    if sha256_file(normalized["runner_executable"]) != validated["qemu"]["runner_executable_sha256"]:
        raise ValidationError("measurement protocol runner executable/script hash drift")
