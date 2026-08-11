from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping

from .candidate_workspace import candidate_python_contract
from .errors import ConfigurationError, ValidationError
from .util import resolve_without_symlinks, sha256_file, validate_relative_path


_SNAPSHOT_SCHEMA = "accela-toolchain-snapshot.v1"
_DOCKERFILE_PATH = "tools/benchmark/candidate-toolchain.Dockerfile"
_DOCKER_INSTALL_PATH = "/usr/local/bin/docker"
_CONTAINER_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_IMAGE_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_BOOTSTRAP_SCRIPT_PATH = "scripts/bootstrap-candidate-workspace.sh"
_QEMU_PLUGIN_BUILDER_PATH = "scripts/build-qemu-plugins.sh"
_GRADLE_VERSION = "8.14"
_GRADLE_DISTRIBUTION_URL = (
    "https://services.gradle.org/distributions/gradle-8.14-bin.zip"
)
_GRADLE_DISTRIBUTION_SHA256 = (
    "61ad310d3c7d3e5da131b76bbf22b5a4c0786e9d892dae8c1658d4b484de3caa"
)
_GRADLE_WRAPPER_JAR_SHA256 = (
    "91a239400bb638f36a1795d8fdf7939d532cdc7d794d1119b7261aac158b1e60"
)
_GRADLE_PATHS = {
    "build_file": "build.gradle.kts",
    "dependency_lock": "gradle.lockfile",
    "verification_metadata": "gradle/verification-metadata.xml",
    "wrapper_jar": "gradle/wrapper/gradle-wrapper.jar",
    "wrapper_properties": "gradle/wrapper/gradle-wrapper.properties",
}
_GRADLE_COMPONENT = re.compile(r"[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+")


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


def _physical_ref(workspace: Path, relative_path: str, *, label: str) -> dict[str, str]:
    validate_relative_path(relative_path, label=f"{label} path")
    path = resolve_without_symlinks(workspace / relative_path, label=label)
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise ValidationError(f"{label} escapes the workspace") from exc
    if not path.is_file():
        raise ValidationError(f"{label} must be a regular file")
    return {"path": relative_path, "physical_sha256": sha256_file(path)}


def _read_gradle_wrapper_properties(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError("cannot read Gradle wrapper properties") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ValidationError("Gradle wrapper properties contain a malformed entry")
        key, value = stripped.split("=", 1)
        if key in values:
            raise ValidationError("Gradle wrapper properties repeat a key")
        values[key] = value
    expected = {
        "distributionBase": "GRADLE_USER_HOME",
        "distributionPath": "wrapper/dists",
        "distributionSha256Sum": _GRADLE_DISTRIBUTION_SHA256,
        "distributionUrl": _GRADLE_DISTRIBUTION_URL.replace(":", "\\:", 1),
        "zipStoreBase": "GRADLE_USER_HOME",
        "zipStorePath": "wrapper/dists",
    }
    if values != expected:
        raise ValidationError("Gradle wrapper distribution contract differs")
    return values


def _validate_gradle_verification_metadata(path: Path) -> None:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ConfigurationError("cannot read Gradle verification metadata") from exc
    namespace = "{https://schema.gradle.org/dependency-verification}"
    if root.tag != f"{namespace}verification-metadata":
        raise ValidationError("Gradle verification metadata root differs")
    configuration = root.find(f"{namespace}configuration")
    components = root.find(f"{namespace}components")
    if (
        configuration is None
        or configuration.findtext(f"{namespace}verify-metadata") != "true"
        or configuration.findtext(f"{namespace}verify-signatures") != "false"
        or components is None
    ):
        raise ValidationError("Gradle verification metadata configuration differs")
    hashes = [
        element.get("value")
        for element in components.iter(f"{namespace}sha256")
    ]
    if not hashes or any(not _is_nonzero_sha256(value) for value in hashes):
        raise ValidationError("Gradle verification metadata SHA-256 entries are invalid")
    artifacts = list(components.iter(f"{namespace}artifact"))
    if not artifacts or len(hashes) != len(artifacts):
        raise ValidationError("Gradle verification metadata must hash every artifact exactly once")


def _validate_gradle_lock(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError("cannot read Gradle dependency lock") from exc
    entries = [line for line in lines if line and not line.startswith("#")]
    if not entries or entries[-1].split("=", 1)[0] != "empty":
        raise ValidationError("Gradle dependency lock is incomplete")
    modules: set[str] = set()
    for entry in entries:
        if "=" not in entry:
            raise ValidationError("Gradle dependency lock contains a malformed entry")
        module, configurations = entry.split("=", 1)
        if module != "empty" and _GRADLE_COMPONENT.fullmatch(module) is None:
            raise ValidationError("Gradle dependency lock module identity is invalid")
        if module in modules or not configurations:
            raise ValidationError("Gradle dependency lock repeats or omits an entry")
        modules.add(module)
    if len(modules) < 2:
        raise ValidationError("Gradle dependency lock contains no resolved dependency")


def build_workspace_bootstrap_contract(*, root: Path) -> dict[str, Any]:
    workspace = resolve_without_symlinks(root, label="candidate workspace")
    wrapper_properties_path = workspace / _GRADLE_PATHS["wrapper_properties"]
    _read_gradle_wrapper_properties(wrapper_properties_path)
    wrapper_jar_path = workspace / _GRADLE_PATHS["wrapper_jar"]
    if sha256_file(wrapper_jar_path) != _GRADLE_WRAPPER_JAR_SHA256:
        raise ValidationError("Gradle wrapper JAR physical hash differs")
    _validate_gradle_verification_metadata(
        workspace / _GRADLE_PATHS["verification_metadata"]
    )
    _validate_gradle_lock(workspace / _GRADLE_PATHS["dependency_lock"])
    try:
        build_file = (workspace / _GRADLE_PATHS["build_file"]).read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError("cannot read Gradle build file") from exc
    if "dependencyLocking {\n    lockAllConfigurations()\n}" not in build_file:
        raise ValidationError("Gradle dependency locking is not enabled globally")
    return {
        "bootstrap_script": _physical_ref(
            workspace, _BOOTSTRAP_SCRIPT_PATH, label="candidate bootstrap script"
        ),
        "python": candidate_python_contract(root=workspace),
        "gradle": {
            "version": _GRADLE_VERSION,
            "distribution_url": _GRADLE_DISTRIBUTION_URL,
            "distribution_sha256": _GRADLE_DISTRIBUTION_SHA256,
            **{
                key: _physical_ref(workspace, path, label=f"Gradle {key}")
                for key, path in _GRADLE_PATHS.items()
            },
        },
        "qemu_plugin_builder": _physical_ref(
            workspace, _QEMU_PLUGIN_BUILDER_PATH, label="QEMU plugin builder"
        ),
    }


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
        "workspace_bootstrap",
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
    expected_bootstrap = build_workspace_bootstrap_contract(root=workspace)
    if contract.get("workspace_bootstrap") != expected_bootstrap:
        raise ValidationError("candidate workspace bootstrap contract differs")
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
