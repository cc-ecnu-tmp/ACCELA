from __future__ import annotations

from typing import Any, Mapping

from .candidate_workspace import VENV_PATH
from .errors import ValidationError
from .util import executable_label, sha256_json


FORMAL_ANALYZER_CONTRACT_VERSION = "candidate-binary-analyzer.v1"
FORMAL_ANALYZER_LAUNCHER = f"{VENV_PATH}/bin/python"
FORMAL_ANALYZER_MODULE = "tools.benchmark.binary_analyzer"
FORMAL_ANALYZER_TOOLCHAINS = ("accela", "gcc", "clang")


def _formal_analyzer_argv(toolchain: str) -> tuple[str, ...]:
    if toolchain not in FORMAL_ANALYZER_TOOLCHAINS:
        raise ValidationError(f"unknown formal analyzer toolchain: {toolchain}")
    argv = [
        FORMAL_ANALYZER_LAUNCHER,
        "-I",
        "-m",
        FORMAL_ANALYZER_MODULE,
        "{binary}",
        "--toolchain",
        toolchain,
        "--readelf-command",
        "riscv64-elf-readelf",
        "--objdump-command",
        "riscv64-elf-objdump",
    ]
    if toolchain == "accela":
        argv.extend(("--remarks", "{remarks_file}"))
    argv.extend(("--timeout", "60", "--output", "{analysis_file}"))
    return tuple(argv)


def candidate_analyzer_contract() -> dict[str, Any]:
    commands: dict[str, dict[str, Any]] = {}
    for toolchain in FORMAL_ANALYZER_TOOLCHAINS:
        argv = _formal_analyzer_argv(toolchain)
        commands[toolchain] = {
            "argv": list(argv),
            "command_sha256": sha256_json(
                {"command": list(argv), "environment": {}}
            ),
            "executable": executable_label(argv),
        }
    return {
        "contract_version": FORMAL_ANALYZER_CONTRACT_VERSION,
        "adapter": "host",
        "environment_keys": [],
        "commands": commands,
    }


def candidate_analyzer_stage(
    contract: Mapping[str, Any], *, toolchain: str
) -> dict[str, Any]:
    expected = candidate_analyzer_contract()
    if contract != expected:
        raise ValidationError("candidate binary-analyzer contract differs")
    command = expected["commands"].get(toolchain)
    if command is None:
        raise ValidationError(f"unknown formal analyzer toolchain: {toolchain}")
    return {
        "kind": "analyzer",
        "adapter": expected["adapter"],
        "command_sha256": command["command_sha256"],
        "executable": command["executable"],
        "environment_keys": expected["environment_keys"],
    }
