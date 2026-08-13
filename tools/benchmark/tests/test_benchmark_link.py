from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
POSIX_SH = shutil.which("sh") if os.name == "posix" else None

pytestmark = pytest.mark.skipif(POSIX_SH is None, reason="requires a POSIX shell")


def _write_executable(path: Path, payload: str) -> None:
    path.write_text(payload, encoding="utf-8", newline="\n")
    path.chmod(0o755)


@pytest.fixture
def link_workspace(tmp_path: Path) -> dict[str, Path]:
    workspace = tmp_path / "workspace"
    for relative in (
        "scripts/benchmark-link.sh",
        "tools/qemu/crt.S",
        "tools/qemu/runtime.c",
        "tools/qemu/linker.ld",
    ):
        source = ROOT / relative
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    input_path = workspace / "input.s"
    input_path.write_text(".text\n", encoding="ascii", newline="\n")
    output_dir = workspace / "output"
    output_dir.mkdir()
    fake_bin = workspace / "fake-bin"
    fake_bin.mkdir()

    _write_executable(
        fake_bin / "riscv64-elf-gcc",
        """#!/bin/sh
set -eu
: "${FAKE_LINK_LOG:?}"
printf '%s\\n' "$@" > "$FAKE_LINK_LOG"
output=
while [ "$#" -gt 0 ]; do
  if [ "$1" = -o ]; then
    shift
    [ "$#" -gt 0 ] || exit 64
    output=$1
  fi
  shift
done
[ -n "$output" ] || exit 64
printf 'fake ELF\\n' > "$output"
""",
    )
    _write_executable(
        fake_bin / "riscv64-elf-readelf",
        """#!/bin/sh
set -eu
: "${FAKE_READELF_LOG:?}"
printf '%s\\n' "$*" >> "$FAKE_READELF_LOG"
case " $* " in
  *" --file-header "*)
    printf 'ELF Header:\\n  Type:                              %s (fixture)\\n' "${FAKE_ELF_TYPE:-EXEC}"
    ;;
  *" --program-headers "*)
    printf 'Program Headers:\\n  Type Offset VirtAddr\\n'
    case "${FAKE_FORBIDDEN_SEGMENT:-none}" in
      none) printf '  LOAD 0x0 0x80000000\\n' ;;
      INTERP|DYNAMIC) printf '  %s 0x0 0x80000000\\n' "$FAKE_FORBIDDEN_SEGMENT" ;;
      *) exit 65 ;;
    esac
    ;;
  *" --relocs "*)
    if [ "${FAKE_RELOCATIONS:-none}" = present ]; then
      printf "Relocation section '.rela.dyn' at offset 0x100 contains 1 entry:\\n"
      printf '0000000080000000  0000000000000003 R_RISCV_RELATIVE 0\\n'
    else
      printf 'There are no relocations in this file.\\n'
    fi
    ;;
  *) exit 64 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "ambient-riscv-gcc",
        "#!/bin/sh\nprintf 'ambient compiler must not run\\n' >&2\nexit 99\n",
    )

    return {
        "workspace": workspace,
        "script": workspace / "scripts/benchmark-link.sh",
        "input": input_path,
        "output": output_dir / "program.elf",
        "fake_bin": fake_bin,
        "link_log": workspace / "link.log",
        "readelf_log": workspace / "readelf.log",
    }


def _run_link(
    harness: dict[str, Path],
    *,
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("RISCV_GCC", None)
    environment["PATH"] = (
        str(harness["fake_bin"]) + os.pathsep + environment.get("PATH", "")
    )
    environment["FAKE_LINK_LOG"] = str(harness["link_log"])
    environment["FAKE_READELF_LOG"] = str(harness["readelf_log"])
    if extra_environment:
        environment.update(extra_environment)
    return subprocess.run(
        [
            str(POSIX_SH),
            str(harness["script"]),
            str(harness["input"]),
            str(harness["output"]),
        ],
        cwd=harness["workspace"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_formal_link_is_static_non_pie_and_validates_the_elf(
    link_workspace: dict[str, Path],
) -> None:
    result = _run_link(link_workspace)

    assert result.returncode == 0, result.stderr
    assert link_workspace["output"].is_file()
    arguments = link_workspace["link_log"].read_text(encoding="utf-8").splitlines()
    assert "-fno-pie" in arguments
    assert "-no-pie" in arguments
    assert "-static" in arguments
    assert arguments.count("-o") == 1
    assert arguments[-1] == str(link_workspace["output"])
    assert link_workspace["readelf_log"].read_text(encoding="utf-8").splitlines() == [
        f"--wide --file-header {link_workspace['output']}",
        f"--wide --program-headers {link_workspace['output']}",
        f"--wide --relocs {link_workspace['output']}",
    ]


def test_formal_link_rejects_ambient_compiler_override(
    link_workspace: dict[str, Path],
) -> None:
    result = _run_link(
        link_workspace,
        extra_environment={"RISCV_GCC": "ambient-riscv-gcc"},
    )

    assert result.returncode == 2
    assert "RISCV_GCC is not supported by the formal linker" in result.stderr
    assert not link_workspace["link_log"].exists()
    assert not link_workspace["output"].exists()


@pytest.mark.parametrize(
    ("environment", "diagnostic"),
    (
        ({"FAKE_ELF_TYPE": "DYN"}, "not ET_EXEC"),
        ({"FAKE_FORBIDDEN_SEGMENT": "INTERP"}, "forbidden PT_INTERP"),
        ({"FAKE_FORBIDDEN_SEGMENT": "DYNAMIC"}, "forbidden PT_DYNAMIC"),
        ({"FAKE_RELOCATIONS": "present"}, "unresolved relocation"),
    ),
)
def test_formal_link_rejects_non_bare_metal_elf_contracts_and_removes_output(
    link_workspace: dict[str, Path],
    environment: dict[str, str],
    diagnostic: str,
) -> None:
    result = _run_link(link_workspace, extra_environment=environment)

    assert result.returncode == 2
    assert diagnostic in result.stderr
    assert not link_workspace["output"].exists()
