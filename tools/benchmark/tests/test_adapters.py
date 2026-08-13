from __future__ import annotations

from pathlib import Path

from tools.benchmark.adapters import CommandRenderer, StageSpec


class FakeMapper:
    def to_wsl(self, path: Path) -> str:
        return "/mapped/" + path.name

    def wrap(self, command, *, cwd: Path):
        return ["wsl.exe", "--cd", self.to_wsl(cwd), "--exec", *command]


def test_host_compile_and_wsl_run_render_distinct_paths(tmp_path: Path) -> None:
    source = tmp_path / "case.sy"
    artifact = tmp_path / "case.s"
    paths = {"source": source, "artifact": artifact}
    renderer = CommandRenderer(FakeMapper())

    host = StageSpec("benchmark-compiler", "host", ("java", "Compiler", "{source_host}", "{artifact_host}"), {})
    host_command, host_env = renderer.render(host, paths=paths, scalars={}, cwd=tmp_path)
    assert host_command[-2:] == [str(source.resolve()), str(artifact.resolve())]
    assert host_env is not None

    wsl = StageSpec("external", "wsl", ("riscv64-linux-gnu-gcc", "{artifact_wsl}"), {"LANG": "C"})
    wsl_command, wsl_env = renderer.render(wsl, paths=paths, scalars={}, cwd=tmp_path)
    assert wsl_command[:4] == ["wsl.exe", "--cd", "/mapped/" + tmp_path.name, "--exec"]
    assert "riscv64-linux-gnu-gcc" in wsl_command
    assert "/mapped/case.s" in wsl_command
    assert wsl_env is None
