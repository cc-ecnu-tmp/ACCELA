from __future__ import annotations

import subprocess
from pathlib import Path

from tools.benchmark.util import sha256_artifact


ROOT = Path(__file__).resolve().parents[3]


def test_hash_bound_repository_files_are_checked_out_with_lf() -> None:
    paths = (
        "tools/benchmark/schemas/run-record.v1.json",
        "docs/optimization/data/toolchain-snapshot.json",
        "docs/optimization/data/campaign/initial.plan.json",
        "scripts/reference-compile.sh",
        "tools/benchmark/reference_source.py",
        "tools/qemu/sysy-builtins.h",
        "tools/benchmark/reference-toolchain.Dockerfile",
    )
    result = subprocess.run(
        ["git", "check-attr", "eol", "--", *paths],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    observed = {
        line.split(": ", 2)[0]: line.split(": ", 2)[2]
        for line in result.stdout.splitlines()
    }
    assert observed == {path: "lf" for path in paths}
    for path in paths:
        assert b"\r\n" not in (ROOT / path).read_bytes()


def test_artifact_tree_hash_has_cross_platform_posix_ordering(tmp_path: Path) -> None:
    artifact = tmp_path / "compiler-classes"
    (artifact / "Beta").mkdir(parents=True)
    (artifact / "Beta" / "nested.bin").write_bytes(b"nested\x00payload")
    (artifact / "Zebra.txt").write_bytes(b"upper first")
    (artifact / "alpha.txt").write_bytes(b"lower first")
    (artifact / "éclair.txt").write_bytes("non-ascii\n".encode("utf-8"))

    assert sha256_artifact(artifact) == (
        "6b71ee08992d87e2510f93ad2ffc005712eff55619dd209ccf307aed99a4f6f6"
    )
