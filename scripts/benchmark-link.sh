#!/usr/bin/env sh

set -eu

fail() {
  printf '%s\n' "$*" >&2
  exit 2
}

[ "$#" -eq 2 ] || fail "usage: scripts/benchmark-link.sh INPUT_ASSEMBLY_OR_OBJECT OUTPUT_ELF"
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
input=$1
output=$2
[ -f "$input" ] || fail "input artifact is not a regular file: $input"
[ -d "$(dirname -- "$output")" ] || fail "output parent directory does not exist"

compiler=${RISCV_GCC:-riscv64-elf-gcc}
command -v "$compiler" >/dev/null 2>&1 || fail "required command is unavailable: $compiler"

"$compiler" \
  -march=rv64gc -mabi=lp64d -mcmodel=medany -O2 \
  -ffreestanding -fno-builtin -nostdlib -nostartfiles \
  -Wl,-T,"$root/tools/qemu/linker.ld" \
  "$root/tools/qemu/crt.S" "$root/tools/qemu/runtime.c" "$input" \
  -lgcc -o "$output"
