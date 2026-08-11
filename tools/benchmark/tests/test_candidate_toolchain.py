from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tools.benchmark.candidate_toolchain import (
    load_candidate_toolchain_contract,
    verify_candidate_toolchain_image,
)
from tools.benchmark.errors import ValidationError
from tools.benchmark.util import sha256_file


ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT = ROOT / "docs" / "optimization" / "data" / "toolchain-snapshot.json"


def _inspect(contract: dict[str, object]) -> dict[str, object]:
    docker_cli = contract["docker_cli"]
    assert isinstance(docker_cli, dict)
    layers = contract["rootfs_layers"]
    assert isinstance(layers, list)
    return {
        "Id": contract["image_id"],
        "RepoTags": [contract["image_tag"]],
        "RootFS": {"Type": "layers", "Layers": layers},
        "Config": {
            "Env": [
                "LANG=C.UTF-8",
                "JAVA_HOME=/usr/lib/jvm/default",
                "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            ],
            "Labels": {
                "org.accela.toolchain.rootfs-layer-sha256": layers[0][7:],
                "org.accela.toolchain.docker-cli-sha256": docker_cli["sha256"],
            },
        },
    }


def test_repository_snapshot_binds_candidate_image_dockerfile_and_cli() -> None:
    contract = load_candidate_toolchain_contract(root=ROOT, snapshot_path=SNAPSHOT)

    assert contract["dockerfile_path"] == (
        "tools/benchmark/candidate-toolchain.Dockerfile"
    )
    assert contract["dockerfile_sha256"] == sha256_file(
        ROOT / contract["dockerfile_path"]
    )
    assert contract["docker_cli"]["install_path"] == "/usr/local/bin/docker"
    assert contract["rootfs_layers"][0] == (
        "sha256:f0c5cb375e83b3dd661d3b6effd57c3eb3e1000c51fe4897896318d7b57b3055"
    )


def test_candidate_image_inspect_accepts_only_the_frozen_identity() -> None:
    contract = load_candidate_toolchain_contract(root=ROOT, snapshot_path=SNAPSHOT)
    inspect = _inspect(contract)

    verify_candidate_toolchain_image(contract, inspect)

    drifted = json.loads(json.dumps(inspect))
    drifted["RootFS"]["Layers"][1] = "sha256:" + "f" * 64
    with pytest.raises(ValidationError, match="image identity differs"):
        verify_candidate_toolchain_image(contract, drifted)

    drifted = json.loads(json.dumps(inspect))
    drifted["Config"]["Labels"][
        "org.accela.toolchain.docker-cli-sha256"
    ] = "0" * 64
    with pytest.raises(ValidationError, match="image labels differ"):
        verify_candidate_toolchain_image(contract, drifted)


def test_candidate_contract_rejects_dockerfile_physical_drift(tmp_path: Path) -> None:
    dockerfile = tmp_path / "tools" / "benchmark" / "candidate-toolchain.Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "tools/benchmark/candidate-toolchain.Dockerfile", dockerfile)
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    dockerfile.write_text("FROM scratch\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="Dockerfile physical hash differs"):
        load_candidate_toolchain_contract(root=tmp_path, snapshot_path=snapshot_path)
