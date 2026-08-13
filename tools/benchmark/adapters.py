from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .errors import ConfigurationError, ExecutionError
from .util import render_command, sanitize_text


@dataclass(frozen=True)
class StageSpec:
    kind: str
    adapter: str
    command: tuple[str, ...] | None
    environment: Mapping[str, str]


class WslPathMapper:
    """Translate host paths using the selected WSL distribution's own wslpath."""

    def __init__(self, executable: str = "wsl.exe", distribution: str | None = None) -> None:
        self.executable = executable
        self.distribution = distribution
        self._cache: dict[str, str] = {}
        self._lock = threading.Lock()

    def to_wsl(self, path: Path) -> str:
        resolved = str(path.resolve())
        with self._lock:
            cached = self._cache.get(resolved)
        if cached is not None:
            return cached
        if os.name != "nt":
            raise ConfigurationError("the WSL adapter must be invoked from a Windows host")
        command = [self.executable]
        if self.distribution:
            command.extend(["--distribution", self.distribution])
        command.extend(["--exec", "wslpath", "-a", "-u", resolved])
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutionError(f"cannot invoke WSL path adapter: {sanitize_text(str(exc))}") from exc
        if completed.returncode != 0:
            diagnostic = sanitize_text(completed.stderr.decode("utf-8", errors="replace"))
            raise ExecutionError(f"wslpath failed: {diagnostic}")
        translated = completed.stdout.decode("utf-8", errors="strict").strip()
        if not translated.startswith("/"):
            raise ExecutionError("wslpath returned a non-absolute path")
        with self._lock:
            self._cache[resolved] = translated
        return translated

    def wrap(self, command: Sequence[str], *, cwd: Path) -> list[str]:
        wrapped = [self.executable]
        if self.distribution:
            wrapped.extend(["--distribution", self.distribution])
        wrapped.extend(["--cd", self.to_wsl(cwd), "--exec"])
        wrapped.extend(command)
        return wrapped


class CommandRenderer:
    def __init__(self, mapper: WslPathMapper | None = None) -> None:
        self.mapper = mapper

    def render(
        self,
        stage: StageSpec,
        *,
        paths: Mapping[str, Path],
        scalars: Mapping[str, str],
        cwd: Path,
    ) -> tuple[list[str], dict[str, str] | None]:
        if stage.command is None:
            raise ConfigurationError("cannot render an empty stage command")
        values = dict(scalars)
        for key, path in paths.items():
            host_value = str(path.resolve())
            values[f"{key}_host"] = host_value
            if self.mapper is not None:
                wsl_value = self.mapper.to_wsl(path)
                values[f"{key}_wsl"] = wsl_value
            else:
                wsl_value = host_value if os.name != "nt" else ""
            values[key] = wsl_value if stage.adapter == "wsl" else host_value
        command = render_command(stage.command, values)
        rendered_environment = {key: value.format_map(values) for key, value in stage.environment.items()}

        if stage.adapter == "host":
            environment = os.environ.copy()
            environment.update(rendered_environment)
            return command, environment
        if stage.adapter != "wsl":
            raise ConfigurationError(f"unknown command adapter: {stage.adapter}")
        if self.mapper is None:
            raise ConfigurationError("WSL adapter selected without WSL configuration")
        if rendered_environment:
            command = ["env", *(f"{key}={value}" for key, value in rendered_environment.items()), *command]
        return self.mapper.wrap(command, cwd=cwd), None
