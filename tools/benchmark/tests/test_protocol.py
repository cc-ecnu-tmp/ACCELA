from __future__ import annotations

import errno
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.benchmark import protocol as protocol_module
from tools.benchmark.adapters import StageSpec
from tools.benchmark.cli import main
from tools.benchmark.protocol import (
    REQUIRED_ASSETS,
    capture_measurement_protocol,
    verify_measurement_protocol,
)


def _runner(adapter: str = "host") -> StageSpec:
    return StageSpec(
        "qemu",
        adapter,
        (
            "{qemu_binary}",
            "{runner_executable}",
            "{profile_plugin_binary}",
            "{cache_plugin_binary}",
            "{binary}",
            "{input}",
            "{metric_file}",
        ),
        {},
    )


def test_capture_and_verify_resolve_assets_from_explicit_workspace_without_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    assets: dict[str, Path] = {}
    for key in REQUIRED_ASSETS:
        path = asset_root / key
        path.write_bytes(key.encode("ascii"))
        assets[key] = Path("assets") / key
    assets["qemu_binary"] = Path(sys.executable)

    def unavailable_cwd(_cls: type[Path]) -> Path:
        raise FileNotFoundError(errno.ENOENT, "deleted working directory")

    monkeypatch.setattr(Path, "cwd", classmethod(unavailable_cwd))
    snapshot = capture_measurement_protocol(
        protocol_id="explicit-workspace-protocol",
        workspace_root=tmp_path,
        assets=assets,
        runner=_runner(),
        machine="virt",
        cpu_model="rv64",
        memory="128M",
    )
    verify_measurement_protocol(
        snapshot,
        workspace_root=tmp_path,
        assets=assets,
        runner=_runner(),
    )


def test_wsl_qemu_version_uses_explicit_workspace_for_wrapper_and_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path.resolve(strict=True)
    observations: dict[str, object] = {}

    class FakeMapper:
        def __init__(self, executable: str, distribution: str | None) -> None:
            observations["mapper_init"] = (executable, distribution)

        def to_wsl(self, path: Path) -> str:
            observations["binary"] = path
            return "/workspace/qemu"

        def wrap(self, command: list[str], *, cwd: Path) -> list[str]:
            observations["wrapped_command"] = command
            observations["wrapper_cwd"] = cwd
            return ["wsl.exe", "--exec", *command]

    def fake_run(command, **kwargs):
        observations["process_command"] = command
        observations["process_cwd"] = kwargs["cwd"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=b"QEMU emulator version 11.0.3\n",
            stderr=b"",
        )

    def unavailable_cwd(_cls: type[Path]) -> Path:
        raise FileNotFoundError(errno.ENOENT, "deleted working directory")

    monkeypatch.setattr(protocol_module, "WslPathMapper", FakeMapper)
    monkeypatch.setattr(protocol_module.subprocess, "run", fake_run)
    monkeypatch.setattr(Path, "cwd", classmethod(unavailable_cwd))

    version = protocol_module._qemu_version(
        Path(sys.executable),
        workspace_root=workspace_root,
        runner=_runner("wsl"),
        wsl_executable="wsl.exe",
        wsl_distribution="ACCELA-Test",
    )

    assert version == "QEMU emulator version 11.0.3"
    assert observations["mapper_init"] == ("wsl.exe", "ACCELA-Test")
    assert observations["wrapper_cwd"] == workspace_root
    assert observations["process_cwd"] == workspace_root
    assert observations["wrapped_command"] == ["/workspace/qemu", "--version"]


def test_protocol_cli_capture_and_verify_paths_are_workspace_anchored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    asset_arguments: list[str] = []
    for key in REQUIRED_ASSETS:
        if key == "qemu_binary":
            value = Path(sys.executable)
        else:
            relative = Path("assets") / key
            (tmp_path / relative).write_bytes(key.encode("ascii"))
            value = relative
        asset_arguments.extend(("--asset", f"{key}={value}"))
    runner_json = json.dumps(list(_runner().command), separators=(",", ":"))

    def unavailable_cwd(_cls: type[Path]) -> Path:
        raise FileNotFoundError(errno.ENOENT, "deleted working directory")

    monkeypatch.setattr(Path, "cwd", classmethod(unavailable_cwd))
    shared = [
        "--workspace-root",
        str(tmp_path),
        *asset_arguments,
        "--runner-command-json",
        runner_json,
    ]
    assert main(
        [
            "protocol",
            "capture",
            *shared,
            "--protocol-id",
            "cli-explicit-workspace",
            "--machine",
            "virt",
            "--cpu-model",
            "rv64",
            "--memory",
            "128M",
            "--output",
            "protocol.json",
        ]
    ) == 0
    assert (tmp_path / "protocol.json").is_file()
    capsys.readouterr()

    assert main(
        [
            "protocol",
            "verify",
            "protocol.json",
            *shared,
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["verified"] is True
