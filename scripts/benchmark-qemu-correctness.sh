#!/usr/bin/env sh

set -eu

fail() {
  printf '%s\n' "$*" >&2
  exit 2
}

[ "$#" -eq 2 ] \
  || fail "usage: scripts/benchmark-qemu-correctness.sh PROGRAM_ELF INPUT_FILE"
binary=$1
input=$2
[ -f "$binary" ] || fail "program ELF is not a regular file: $binary"
[ -f "$input" ] || fail "input is not a regular file: $input"
input_dir=$(CDPATH= cd -- "$(dirname -- "$input")" && pwd -P)
input=$input_dir/$(basename -- "$input")
safe_input=$(printf '%s' "$input" | LC_ALL=C tr -d ',[:cntrl:]')
[ "$safe_input" = "$input" ] \
  || fail "input path contains characters unsafe for QEMU -fw_cfg"
input_size=$(wc -c < "$input" | LC_ALL=C tr -d '[:space:]')
case "$input_size" in
  ''|*[!0-9]*) fail "unable to determine input size for QEMU -fw_cfg" ;;
esac
[ "$input_size" -le 4294967295 ] \
  || fail "input exceeds the fw_cfg 32-bit size limit"

qemu=${QEMU_SYSTEM_RISCV64:-qemu-system-riscv64}
command -v "$qemu" >/dev/null 2>&1 \
  || fail "required command is unavailable: $qemu"

exec "$qemu" \
  -machine virt -accel tcg,thread=single -smp 1 -m 512M \
  -bios none -kernel "$binary" -display none -monitor none -serial stdio \
  -fw_cfg "name=opt/accela/sysy-input,file=$input" \
  </dev/null
