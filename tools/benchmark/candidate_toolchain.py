from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigurationError, ValidationError
from .util import resolve_without_symlinks, sha256_file, validate_relative_path


_SNAPSHOT_SCHEMA = "accela-toolchain-snapshot.v1"
_DOCKERFILE_PATH = "tools/benchmark/candidate-toolchain.Dockerfile"
_DOCKER_INSTALL_PATH = "/usr/local/bin/docker"
_CONTAINER_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_IMAGE_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")


def _is_nonzero_sha256(value: object, *, image_id: bool = False) -> bool:
    if not isinstance(value, str):
        return False
    payload = value[7:] if image_id and value.startswith("sha256:") else value
    pattern = _IMAGE_ID if image_id else _SHA256
    return pattern.fullmatch(value) is not None and payload != "0" * 64


def _read_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot read {label} as UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must contain an object")
    return value


def load_candidate_toolchain_contract(
    *, root: Path, snapshot_path: Path
) -> dict[str, Any]:
    workspace = resolve_without_symlinks(root, label="candidate toolchain workspace")
    if not workspace.is_dir():
        raise ConfigurationError("candidate toolchain workspace must be a directory")
    snapshot = _read_object(
        resolve_without_symlinks(snapshot_path, label="candidate toolchain snapshot"),
        label="candidate toolchain snapshot",
    )
    if snapshot.get("schema") != _SNAPSHOT_SCHEMA:
        raise ValidationError("candidate toolchain snapshot schema differs")
    proxy = snapshot.get("proxy_execution")
    contract = proxy.get("candidate_toolchain") if isinstance(proxy, dict) else None
    if not isinstance(contract, dict) or set(contract) != {
        "base_image_tag",
        "base_image_id",
        "dockerfile_path",
        "dockerfile_sha256",
        "docker_cli",
        "image_tag",
        "image_id",
        "rootfs_layers",
    }:
        raise ValidationError("candidate toolchain contract fields differ")
    base_tag = contract.get("base_image_tag")
    base_id = contract.get("base_image_id")
    image_tag = contract.get("image_tag")
    image_id = contract.get("image_id")
    dockerfile_path = contract.get("dockerfile_path")
    dockerfile_sha256 = contract.get("dockerfile_sha256")
    layers = contract.get("rootfs_layers")
    docker_cli = contract.get("docker_cli")
    if (
        not isinstance(base_tag, str)
        or _IMAGE_TAG.fullmatch(base_tag) is None
        or not _is_nonzero_sha256(base_id, image_id=True)
        or not isinstance(image_tag, str)
        or _IMAGE_TAG.fullmatch(image_tag) is None
        or image_tag == base_tag
        or not _is_nonzero_sha256(image_id, image_id=True)
        or dockerfile_path != _DOCKERFILE_PATH
        or not isinstance(dockerfile_sha256, str)
        or not _is_nonzero_sha256(dockerfile_sha256)
        or not isinstance(layers, list)
        or len(layers) != 2
        or any(not _is_nonzero_sha256(item, image_id=True) for item in layers)
        or not isinstance(docker_cli, dict)
        or set(docker_cli) != {"install_path", "sha256", "version", "version_output"}
    ):
        raise ValidationError("candidate toolchain identity is invalid")
    if (
        docker_cli.get("install_path") != _DOCKER_INSTALL_PATH
        or not isinstance(docker_cli.get("sha256"), str)
        or not _is_nonzero_sha256(docker_cli["sha256"])
        or not isinstance(docker_cli.get("version"), str)
        or re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", docker_cli["version"]) is None
        or not isinstance(docker_cli.get("version_output"), str)
        or docker_cli["version_output"]
        != f"Docker version {docker_cli['version']}, build dfc4efb"
    ):
        raise ValidationError("candidate toolchain Docker CLI identity is invalid")
    validate_relative_path(dockerfile_path, label="candidate toolchain Dockerfile path")
    dockerfile = resolve_without_symlinks(
        workspace / dockerfile_path, label="candidate toolchain Dockerfile"
    )
    try:
        dockerfile.relative_to(workspace)
    except ValueError as exc:
        raise ValidationError("candidate toolchain Dockerfile escapes the workspace") from exc
    if not dockerfile.is_file() or sha256_file(dockerfile) != dockerfile_sha256:
        raise ValidationError("candidate toolchain Dockerfile physical hash differs")
    return json.loads(json.dumps(contract))


def verify_candidate_toolchain_image(
    contract: Mapping[str, Any], inspect_document: Mapping[str, Any]
) -> None:
    rootfs = inspect_document.get("RootFS")
    config = inspect_document.get("Config")
    repo_tags = inspect_document.get("RepoTags")
    if (
        inspect_document.get("Id") != contract["image_id"]
        or not isinstance(repo_tags, list)
        or contract["image_tag"] not in repo_tags
        or not isinstance(rootfs, dict)
        or rootfs.get("Type") != "layers"
        or rootfs.get("Layers") != contract["rootfs_layers"]
        or not isinstance(config, dict)
    ):
        raise ValidationError("candidate toolchain image identity differs")
    labels = config.get("Labels")
    environment = config.get("Env")
    if not isinstance(labels, dict) or not isinstance(environment, list):
        raise ValidationError("candidate toolchain image configuration is invalid")
    expected_labels = {
        "org.accela.toolchain.rootfs-layer-sha256": contract["rootfs_layers"][0][7:],
        "org.accela.toolchain.docker-cli-sha256": contract["docker_cli"]["sha256"],
    }
    if any(labels.get(key) != value for key, value in expected_labels.items()):
        raise ValidationError("candidate toolchain image labels differ")
    environment_by_key: dict[str, str] = {}
    for item in environment:
        if not isinstance(item, str) or "=" not in item:
            raise ValidationError("candidate toolchain image environment is invalid")
        key, value = item.split("=", 1)
        if key in environment_by_key:
            raise ValidationError("candidate toolchain image environment repeats a key")
        environment_by_key[key] = value
    if (
        environment_by_key.get("PATH") != _CONTAINER_PATH
        or environment_by_key.get("LANG") != "C.UTF-8"
        or environment_by_key.get("JAVA_HOME") != "/usr/lib/jvm/default"
    ):
        raise ValidationError("candidate toolchain image environment differs")


def _load_inspect(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise ValidationError("candidate toolchain image inspect must contain one object")
    return value[0]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build/verify the frozen ACCELA Docker toolchain.")
    commands = parser.add_subparsers(dest="command", required=True)
    emit = commands.add_parser("emit-build-contract")
    emit.add_argument("--root", type=Path, required=True)
    emit.add_argument("--snapshot", type=Path, required=True)
    verify = commands.add_parser("verify-image-inspect")
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--snapshot", type=Path, required=True)
    verify.add_argument("--inspect", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        contract = load_candidate_toolchain_contract(
            root=args.root, snapshot_path=args.snapshot
        )
        if args.command == "verify-image-inspect":
            verify_candidate_toolchain_image(contract, _load_inspect(args.inspect))
            return 0
    except (OSError, UnicodeError, json.JSONDecodeError, ConfigurationError, ValidationError) as exc:
        print(f"candidate-toolchain: {exc}", file=sys.stderr)
        return 2
    docker_cli = contract["docker_cli"]
    for value in (
        contract["base_image_tag"],
        contract["base_image_id"],
        contract["rootfs_layers"][0],
        contract["dockerfile_path"],
        contract["dockerfile_sha256"],
        docker_cli["sha256"],
        docker_cli["version_output"],
        contract["image_tag"],
        contract["image_id"],
    ):
        print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
