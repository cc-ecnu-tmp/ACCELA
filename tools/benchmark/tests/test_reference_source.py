from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.benchmark.reference_source import (
    REFERENCE_COMMON_SEMANTICS,
    REFERENCE_FRONTEND_ARGV,
    REFERENCE_LAUNCHER_CONTRACT,
    ReferenceSourceError,
    adapt_source,
    load_reference_frontend_contract,
    select_reference_volume_mount,
)
from tools.benchmark.util import sha256_json


ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT = ROOT / "docs" / "optimization" / "data" / "toolchain-snapshot.json"
WRAPPER = ROOT / "scripts" / "reference-compile.sh"
POSIX_SH = shutil.which("sh") if os.name == "posix" else None


def _write_executable(path: Path, payload: str) -> None:
    path.write_text(payload, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _probe_fixed_reference_python_version() -> str:
    result = subprocess.run(
        [
            *REFERENCE_LAUNCHER_CONTRACT["python_argv_prefix"],
            "-c",
            "import platform; print(platform.python_version())",
        ],
        env=os.environ.copy(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    version = result.stdout.strip()
    assert version and version.count(".") == 2
    assert all(component.isdecimal() for component in version.split("."))
    return version


@pytest.fixture
def reference_wrapper_workspace(tmp_path: Path) -> dict[str, Path | str]:
    workspace = tmp_path / "workspace"
    support_paths = (
        "scripts/reference-compile.sh",
        "tools/benchmark/reference_source.py",
        "tools/benchmark/reference-toolchain.Dockerfile",
        "tools/qemu/sysy-builtins.h",
    )
    for relative in support_paths:
        source = ROOT / relative
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    frontends = snapshot["reference_frontends"]
    frontends["compile_driver_sha256"] = hashlib.sha256(
        (workspace / "scripts/reference-compile.sh").read_bytes()
    ).hexdigest()
    frontends["source_adapter_sha256"] = hashlib.sha256(
        (workspace / "tools/benchmark/reference_source.py").read_bytes()
    ).hexdigest()
    frontends["dockerfile_sha256"] = hashlib.sha256(
        (workspace / "tools/benchmark/reference-toolchain.Dockerfile").read_bytes()
    ).hexdigest()
    frontends["builtin_header_sha256"] = hashlib.sha256(
        (workspace / "tools/qemu/sysy-builtins.h").read_bytes()
    ).hexdigest()
    frontends["common_semantics"] = list(REFERENCE_COMMON_SEMANTICS)
    image_id = "sha256:" + "a" * 64
    frontends["local_image_tag"] = "accela/reference-test:frozen"
    frontends["local_image_id"] = image_id
    reference_python_version = _probe_fixed_reference_python_version()
    snapshot["proxy_execution"]["python"] = reference_python_version
    snapshot_path = workspace / "docs/optimization/data/toolchain-snapshot.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    source_path = workspace / "case.sy"
    source_path.write_text("int main() { return 0; }\n", encoding="ascii", newline="\n")
    output_dir = workspace / "output"
    output_dir.mkdir()
    fake_bin = workspace / "fake-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "wslpath",
        "#!/bin/sh\n[ \"$1\" = -w ] || exit 64\nprintf '%s\\n' \"$2\"\n",
    )
    return {
        "root": workspace,
        "wrapper": workspace / "scripts/reference-compile.sh",
        "snapshot": snapshot_path,
        "source": source_path,
        "output": output_dir / "artifact.s",
        "fake_bin": fake_bin,
        "image_id": image_id,
        "python_version": reference_python_version,
    }


def _run_reference_wrapper(
    harness: dict[str, Path | str], *, extra_environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PATH"] = (
        str(harness["fake_bin"]) + os.pathsep + environment.get("PATH", "")
    )
    environment["ACCELA_TEST_IMAGE_ID"] = str(harness["image_id"])
    if extra_environment:
        environment.update(extra_environment)
    return subprocess.run(
        [
            str(POSIX_SH),
            str(harness["wrapper"]),
            "gcc",
            str(harness["source"]),
            str(harness["output"]),
        ],
        cwd=harness["root"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_reserved_identifiers_are_renamed_without_touching_sysy_keywords() -> None:
    adapted = adapt_source(
        b"int delete(int or) { int class = or; while (class) return delete(class); }\n"
    )
    assert adapted.identifier_prefix == "__accela_sysy_cxx_0_"
    assert adapted.renamed_identifiers == ("class", "delete", "or")
    assert adapted.payload == (
        b"int __accela_sysy_cxx_0_delete(int __accela_sysy_cxx_0_or) "
        b"{ int __accela_sysy_cxx_0_class = __accela_sysy_cxx_0_or; "
        b"while (__accela_sysy_cxx_0_class) return "
        b"__accela_sysy_cxx_0_delete(__accela_sysy_cxx_0_class); }\n"
    )


def test_generated_identifier_prefix_is_collision_free_and_deterministic() -> None:
    payload = b"int __accela_sysy_cxx_0_delete; int delete;\n"
    first = adapt_source(payload)
    second = adapt_source(payload)
    assert first == second
    assert first.identifier_prefix == "__accela_sysy_cxx_1_"
    assert b"__accela_sysy_cxx_1_delete" in first.payload
    assert b"__accela_sysy_cxx_0_delete" in first.payload


def test_comments_are_byte_exact_and_line_count_is_preserved() -> None:
    payload = (
        b"// delete or 1.5 \' \"\n"
        b"int delete = 1; /* class\n0xffffffff \' \" */\n"
    )
    adapted = adapt_source(payload)
    assert b"// delete or 1.5 \' \"\n" in adapted.payload
    assert b"/* class\n0xffffffff \' \" */" in adapted.payload
    assert adapted.payload.count(b"\n") == payload.count(b"\n")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (b"0", b"((int)0)"),
        (b"077", b"((int)63)"),
        (b"0x7fffffff", b"((int)2147483647)"),
        (b"0x80000000", b"((int)(-2147483647 - 1))"),
        (b"0xffffffff", b"((int)-1)"),
        (b"-2147483648", b"-((int)(-2147483647 - 1))"),
    ],
)
def test_integer_constants_have_explicit_signed_int32_values(
    source: bytes, expected: bytes
) -> None:
    adapted = adapt_source(source)
    assert adapted.payload == expected
    assert adapted.integer_literal_count == 1


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (b"1.0", b"1.0f"),
        (b".5", b".5f"),
        (b"1e-45", b"1e-45f"),
        (b"0x1p-149", b"0x1p-149f"),
        (b"0x1.fffffep+127", b"0x1.fffffep+127f"),
        (b"1F", b"1.0f"),
    ],
)
def test_float_constants_are_explicit_binary32(source: bytes, expected: bytes) -> None:
    adapted = adapt_source(source)
    assert adapted.payload == expected
    assert adapted.float_literal_count == 1


def test_constants_remain_valid_in_dimensions_and_folding_expressions() -> None:
    adapted = adapt_source(
        b"const int N = 0x4; int a[N][02]; int x = 0xffffffff + 2;\n"
    )
    assert adapted.payload == (
        b"const int N = ((int)4); int a[N][((int)2)]; "
        b"int x = ((int)-1) + ((int)2);\n"
    )


@pytest.mark.parametrize(
    "source",
    [
        b"4294967296",
        b"09",
        b"0x",
        b"0x1.0",
        b"1e+",
        b"/* unterminated",
        b'"unterminated',
        b"int \x00x;",
    ],
)
def test_malformed_or_out_of_contract_tokens_fail_fast(source: bytes) -> None:
    with pytest.raises(ReferenceSourceError):
        adapt_source(source)


@pytest.mark.parametrize(
    "lexeme",
    [
        b'"text"',
        b"'c'",
        b"#include",
        b"x ? y : z",
        b"x & y",
        b"x | y",
        b"x ^ y",
        b"~x",
        b"x++",
        b"--x",
        b"x += y",
        b"x <<= y",
        b"x -> y",
    ],
)
def test_non_sysy_lexemes_fail_before_the_cpp_frontend(lexeme: bytes) -> None:
    with pytest.raises(ReferenceSourceError, match="SysY lexical contract"):
        adapt_source(lexeme)


def test_all_sysy_operators_and_delimiters_remain_accepted() -> None:
    adapted = adapt_source(b"+ - * / % < > <= >= == != && || ! = ; , ( ) [ ] { }")
    assert adapted.payload == b"+ - * / % < > <= >= == != && || ! = ; , ( ) [ ] { }"


@pytest.mark.parametrize("frontend", ["gcc", "clang"])
def test_repository_snapshot_executes_the_exact_ssot_argv(frontend: str) -> None:
    contract = load_reference_frontend_contract(
        root=ROOT,
        snapshot_path=SNAPSHOT,
        frontend=frontend,
        artifact_name="artifact.s",
    )
    expected = tuple(
        argument.replace("{artifact_name}", "artifact.s")
        for argument in REFERENCE_FRONTEND_ARGV[frontend]
    )
    assert contract.compiler_argv == expected
    assert contract.compiler_argv_sha256 == sha256_json(
        {"argv": list(REFERENCE_FRONTEND_ARGV[frontend])}
    )


def test_repository_snapshot_freezes_the_reference_launcher_policy() -> None:
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert (
        snapshot["reference_frontends"]["launcher_contract"]
        == REFERENCE_LAUNCHER_CONTRACT
    )


@pytest.mark.skipif(os.name != "posix", reason="Docker mount paths are POSIX paths")
def test_named_volume_selector_binds_outer_image_labels_and_safe_subpaths(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "volume" / "ACCELA"
    output = workspace / "output"
    support = workspace / "tools" / "qemu"
    output.mkdir(parents=True)
    support.mkdir(parents=True)
    container_id = "a" * 64
    hostname = container_id[:12]
    image_id = "sha256:" + "b" * 64
    volume_name = "accela_candidate_evaluation_2026_r2"
    container_inspect = tmp_path / "container.json"
    volume_inspect = tmp_path / "volume.json"
    container_inspect.write_text(
        json.dumps(
            [
                {
                    "Id": container_id,
                    "Image": image_id,
                    "Config": {"Hostname": hostname},
                    "Mounts": [
                        {
                            "Type": "volume",
                            "Name": volume_name,
                            "Destination": str(tmp_path / "volume"),
                            "RW": True,
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    volume_inspect.write_text(
        json.dumps(
            [
                {
                    "Name": volume_name,
                    "Driver": "local",
                    "Scope": "local",
                    "Labels": {
                        "org.accela.campaign": "accela-candidate-evaluation-2026-r2",
                        "org.accela.purpose": "formal-workspace",
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    selected = select_reference_volume_mount(
        root=workspace,
        output_dir=output,
        support_dir=support,
        container_inspect_path=container_inspect,
        volume_inspect_path=volume_inspect,
        expected_volume_name=volume_name,
        expected_campaign="accela-candidate-evaluation-2026-r2",
        expected_purpose="formal-workspace",
        expected_candidate_image_id=image_id,
        observed_hostname=hostname,
    )

    assert selected.volume_name == volume_name
    assert selected.output_subpath == "ACCELA/output"
    assert selected.support_subpath == "ACCELA/tools/qemu"
    assert selected.volume_name_sha256 == hashlib.sha256(
        volume_name.encode("utf-8")
    ).hexdigest()
    assert selected.container_id_sha256 == hashlib.sha256(
        container_id.encode("ascii")
    ).hexdigest()


@pytest.mark.skipif(os.name != "posix", reason="Docker mount paths are POSIX paths")
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("wrong_image", "outer-container identity"),
        ("nested_output_bind", "share one effective Docker mount"),
        ("wrong_volume_label", "identity or labels differ"),
        ("unsafe_subpath", "unsafe volume subpath"),
    ],
)
def test_named_volume_selector_rejects_docker_identity_and_path_drift(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    workspace = tmp_path / "volume" / "ACCELA"
    output_name = "output,bad" if mutation == "unsafe_subpath" else "output"
    output = workspace / output_name
    support = workspace / "tools" / "qemu"
    output.mkdir(parents=True)
    support.mkdir(parents=True)
    container_id = "a" * 64
    hostname = container_id[:12]
    image_id = "sha256:" + "b" * 64
    mounts = [
        {
            "Type": "volume",
            "Name": "accela_candidate_evaluation_2026_r2",
            "Destination": str(tmp_path / "volume"),
            "RW": True,
        }
    ]
    if mutation == "nested_output_bind":
        mounts.append(
            {
                "Type": "bind",
                "Source": "/host/output",
                "Destination": str(output),
                "RW": True,
            }
        )
    container = {
        "Id": container_id,
        "Image": "sha256:" + "c" * 64 if mutation == "wrong_image" else image_id,
        "Config": {"Hostname": hostname},
        "Mounts": mounts,
    }
    labels = {
        "org.accela.campaign": "wrong"
        if mutation == "wrong_volume_label"
        else "accela-candidate-evaluation-2026-r2",
        "org.accela.purpose": "formal-workspace",
    }
    container_inspect = tmp_path / "container.json"
    volume_inspect = tmp_path / "volume.json"
    container_inspect.write_text(json.dumps([container]), encoding="utf-8")
    volume_inspect.write_text(
        json.dumps(
            [
                {
                    "Name": "accela_candidate_evaluation_2026_r2",
                    "Driver": "local",
                    "Scope": "local",
                    "Labels": labels,
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ReferenceSourceError, match=message):
        select_reference_volume_mount(
            root=workspace,
            output_dir=output,
            support_dir=support,
            container_inspect_path=container_inspect,
            volume_inspect_path=volume_inspect,
            expected_volume_name="accela_candidate_evaluation_2026_r2",
            expected_campaign="accela-candidate-evaluation-2026-r2",
            expected_purpose="formal-workspace",
            expected_candidate_image_id=image_id,
            observed_hostname=hostname,
        )


def test_reference_source_rejects_launcher_contract_drift(
    reference_wrapper_workspace: dict[str, Path | str],
) -> None:
    harness = reference_wrapper_workspace
    snapshot_path = Path(harness["snapshot"])
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["reference_frontends"]["launcher_contract"][
        "docker_fallback_policy"
    ] = "any_failure"
    snapshot_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(ReferenceSourceError, match="launcher contract has drifted"):
        load_reference_frontend_contract(
            root=Path(harness["root"]),
            snapshot_path=snapshot_path,
            frontend="gcc",
            artifact_name="artifact.s",
        )


def test_snapshot_argv_hash_drift_fails_before_execution(tmp_path: Path) -> None:
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    snapshot["reference_frontends"]["gcc"]["compiler_argv_sha256"] = "0" * 64
    drifted = tmp_path / "snapshot.json"
    drifted.write_text(json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(ReferenceSourceError, match="argv hash mismatch"):
        load_reference_frontend_contract(
            root=ROOT,
            snapshot_path=drifted,
            frontend="gcc",
            artifact_name="artifact.s",
        )


def test_shell_wrapper_has_no_parallel_hardcoded_frontend_or_image_override() -> None:
    wrapper = WRAPPER.read_text(encoding="utf-8")
    assert "riscv64-linux-gnu-g++-13" not in wrapper
    assert "--target=riscv64-unknown-elf" not in wrapper
    assert "run_container \"$@\"" in wrapper
    assert "${ACCELA_REFERENCE_IMAGE:-" not in wrapper
    assert "${ACCELA_REFERENCE_IMAGE_ID:-" not in wrapper
    assert '[ -z "${ACCELA_TOOLCHAIN_SNAPSHOT+x}" ]' in wrapper
    assert '[ -z "${PYTHON+x}" ]' in wrapper
    assert "${ACCELA_TOOLCHAIN_SNAPSHOT:-" not in wrapper
    assert "${PYTHON:-" not in wrapper
    assert "snapshot=$root/docs/optimization/data/toolchain-snapshot.json" in wrapper
    assert "python=python3" in wrapper
    assert '"$python" -I' in wrapper
    assert "python_mode=isolated" in wrapper
    assert "type=volume,src=$volume_name" in wrapper
    assert "volume-subpath=$output_subpath" in wrapper
    assert "volume-subpath=$support_subpath" in wrapper
    assert "Docker CLI SHA-256 differs" in wrapper
    assert "--expected-candidate-image-id" in wrapper


@pytest.mark.skipif(POSIX_SH is None, reason="reference wrapper requires a POSIX shell")
@pytest.mark.parametrize(
    "native_diagnostic",
    [
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
        "Is the docker daemon running?",
        "Failed to initialize: protocol not available",
    ],
)
def test_native_transport_failure_falls_through_to_reachable_windows_daemon(
    reference_wrapper_workspace: dict[str, Path | str],
    native_diagnostic: str,
) -> None:
    harness = reference_wrapper_workspace
    fake_bin = Path(harness["fake_bin"])
    native_log = Path(harness["root"]) / "native.log"
    windows_log = Path(harness["root"]) / "windows.log"
    _write_executable(
        fake_bin / "docker",
        """#!/bin/sh
printf '%s\n' "$*" >> "$ACCELA_TEST_NATIVE_LOG"
if [ "$1" = version ]; then
  printf '%s\n' "$ACCELA_TEST_NATIVE_DIAGNOSTIC" >&2
  exit 1
fi
exit 65
""",
    )
    _write_executable(
        fake_bin / "docker.exe",
        """#!/bin/sh
printf '%s\n' "$*" >> "$ACCELA_TEST_WINDOWS_LOG"
case "$1:$2" in
  version:--format) printf '%s\n' '28.3.1-desktop.1' ;;
  image:inspect) printf '%s\n' "$ACCELA_TEST_IMAGE_ID" ;;
  run:*) exit 0 ;;
  *) exit 64 ;;
esac
""",
    )
    result = _run_reference_wrapper(
        harness,
        extra_environment={
            "ACCELA_TEST_NATIVE_LOG": str(native_log),
            "ACCELA_TEST_WINDOWS_LOG": str(windows_log),
            "ACCELA_TEST_NATIVE_DIAGNOSTIC": native_diagnostic,
        },
    )
    assert result.returncode == 0, result.stderr
    assert "candidate_cli=native readiness=transport_unreachable" in result.stderr
    assert (
        "ACCELA_REFERENCE_DOCKER chosen_cli=windows "
        "server_version=28.3.1-desktop.1 readiness=reachable"
    ) in result.stderr
    assert native_log.read_text(encoding="utf-8").splitlines() == [
        "version --format {{.Server.Version}}"
    ]
    assert [line.split()[0] for line in windows_log.read_text(encoding="utf-8").splitlines()] == [
        "version",
        "image",
        "run",
    ]


@pytest.mark.skipif(POSIX_SH is None, reason="reference wrapper requires a POSIX shell")
def test_reachable_native_daemon_with_wrong_image_id_never_tries_windows(
    reference_wrapper_workspace: dict[str, Path | str],
) -> None:
    harness = reference_wrapper_workspace
    fake_bin = Path(harness["fake_bin"])
    native_log = Path(harness["root"]) / "native.log"
    windows_log = Path(harness["root"]) / "windows.log"
    _write_executable(
        fake_bin / "docker",
        """#!/bin/sh
printf '%s\n' "$*" >> "$ACCELA_TEST_NATIVE_LOG"
case "$1:$2" in
  version:--format) printf '%s\n' '27.5.0' ;;
  image:inspect) printf '%s\n' 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' ;;
  run:*) exit 70 ;;
  *) exit 64 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "docker.exe",
        """#!/bin/sh
printf '%s\n' "$*" >> "$ACCELA_TEST_WINDOWS_LOG"
exit 71
""",
    )
    result = _run_reference_wrapper(
        harness,
        extra_environment={
            "ACCELA_TEST_NATIVE_LOG": str(native_log),
            "ACCELA_TEST_WINDOWS_LOG": str(windows_log),
        },
    )
    assert result.returncode == 2
    assert "ACCELA_REFERENCE_DOCKER chosen_cli=native server_version=27.5.0" in result.stderr
    assert "reference image ID mismatch" in result.stderr
    assert not windows_log.exists()
    assert [line.split()[0] for line in native_log.read_text(encoding="utf-8").splitlines()] == [
        "version",
        "image",
    ]


@pytest.mark.skipif(POSIX_SH is None, reason="reference wrapper requires a POSIX shell")
@pytest.mark.parametrize(
    "native_diagnostic",
    [
        "server rejected the requested API version",
        "Failed to initialize: protocol not available after authorization denial",
    ],
)
def test_non_transport_native_readiness_failure_never_tries_windows(
    reference_wrapper_workspace: dict[str, Path | str],
    native_diagnostic: str,
) -> None:
    harness = reference_wrapper_workspace
    fake_bin = Path(harness["fake_bin"])
    windows_log = Path(harness["root"]) / "windows.log"
    _write_executable(
        fake_bin / "docker",
        """#!/bin/sh
printf '%s\n' "$ACCELA_TEST_NATIVE_DIAGNOSTIC" >&2
exit 1
""",
    )
    _write_executable(
        fake_bin / "docker.exe",
        """#!/bin/sh
printf '%s\n' "$*" >> "$ACCELA_TEST_WINDOWS_LOG"
exit 0
""",
    )
    result = _run_reference_wrapper(
        harness,
        extra_environment={
            "ACCELA_TEST_NATIVE_DIAGNOSTIC": native_diagnostic,
            "ACCELA_TEST_WINDOWS_LOG": str(windows_log),
        },
    )
    assert result.returncode == 2
    assert "non-transport error for chosen_cli=native" in result.stderr
    assert not windows_log.exists()


@pytest.mark.skipif(POSIX_SH is None, reason="reference wrapper requires a POSIX shell")
def test_isolated_python_ignores_malicious_pythonpath(
    reference_wrapper_workspace: dict[str, Path | str],
) -> None:
    harness = reference_wrapper_workspace
    fake_bin = Path(harness["fake_bin"])
    malicious = Path(harness["root"]) / "malicious-pythonpath"
    malicious.mkdir()
    (malicious / "platform.py").write_text(
        "raise RuntimeError('PYTHONPATH module was imported')\n",
        encoding="utf-8",
        newline="\n",
    )
    _write_executable(
        fake_bin / "docker",
        """#!/bin/sh
case "$1:$2" in
  version:--format) printf '%s\n' '27.5.0' ;;
  image:inspect) printf '%s\n' "$ACCELA_TEST_IMAGE_ID" ;;
  run:*) exit 0 ;;
  *) exit 64 ;;
esac
""",
    )
    result = _run_reference_wrapper(
        harness,
        extra_environment={
            "PYTHONPATH": str(malicious),
            "PYTHONHOME": str(malicious),
        },
    )
    assert result.returncode == 0, result.stderr
    assert (
        "ACCELA_REFERENCE_PYTHON python_mode=isolated "
        f"version={harness['python_version']}"
        in result.stderr
    )
    assert "PYTHONPATH module was imported" not in result.stderr


@pytest.mark.skipif(POSIX_SH is None, reason="reference wrapper requires a POSIX shell")
def test_python_version_drift_fails_before_docker_readiness(
    reference_wrapper_workspace: dict[str, Path | str],
) -> None:
    harness = reference_wrapper_workspace
    snapshot_path = Path(harness["snapshot"])
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["proxy_execution"]["python"] = "0.0.0"
    snapshot_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    docker_log = Path(harness["root"]) / "docker.log"
    _write_executable(
        Path(harness["fake_bin"]) / "docker",
        """#!/bin/sh
printf '%s\n' "$*" >> "$ACCELA_TEST_DOCKER_LOG"
exit 0
""",
    )
    result = _run_reference_wrapper(
        harness,
        extra_environment={"ACCELA_TEST_DOCKER_LOG": str(docker_log)},
    )
    assert result.returncode == 2
    assert (
        "reference Python version mismatch: expected 0.0.0, "
        f"got {harness['python_version']}"
        in result.stderr
    )
    assert "cannot validate the isolated reference Python launcher" in result.stderr
    assert not docker_log.exists()
