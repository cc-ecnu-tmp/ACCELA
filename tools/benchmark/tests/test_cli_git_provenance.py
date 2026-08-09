from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tools.benchmark.cli import _verify_git_provenance
from tools.benchmark.errors import ConfigurationError


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def test_git_provenance_fails_fast_on_head_or_dirty_mismatch(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("Git is not installed")
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.name", "ACCELA benchmark test")
    _git(tmp_path, "config", "user.email", "benchmark-test@invalid.example")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("frozen\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "fixture")
    head = _git(tmp_path, "rev-parse", "HEAD")

    _verify_git_provenance(tmp_path, declared_commit=head, declared_dirty=False)
    with pytest.raises(ConfigurationError, match="commit differs"):
        _verify_git_provenance(tmp_path, declared_commit="0" * 40, declared_dirty=False)

    tracked.write_text("modified\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="dirty workspace"):
        _verify_git_provenance(tmp_path, declared_commit=head, declared_dirty=False)
    _verify_git_provenance(tmp_path, declared_commit=head, declared_dirty=True)
    _git(tmp_path, "restore", "tracked.txt")

    staged = tmp_path / "staged.txt"
    staged.write_text("staged\n", encoding="utf-8")
    _git(tmp_path, "add", "staged.txt")
    with pytest.raises(ConfigurationError, match="dirty workspace"):
        _verify_git_provenance(tmp_path, declared_commit=head, declared_dirty=False)
    _verify_git_provenance(tmp_path, declared_commit=head, declared_dirty=True)
    _git(tmp_path, "reset", "--quiet", "HEAD", "--", "staged.txt")
    staged.unlink()

    (tmp_path / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="dirty workspace"):
        _verify_git_provenance(tmp_path, declared_commit=head, declared_dirty=False)
    _verify_git_provenance(tmp_path, declared_commit=head, declared_dirty=True)
