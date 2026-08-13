#!/usr/bin/env sh

set -eu

fail() {
  printf '%s\n' "$*" >&2
  exit 2
}

[ "$#" -eq 3 ] || fail "usage: scripts/benchmark-qemu.sh PROGRAM_ELF METRIC_LOG INPUT_FILE"
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
binary=$1
metric_log=$2
input=$3
[ -f "$binary" ] || fail "program ELF is not a regular file: $binary"
[ -d "$(dirname -- "$metric_log")" ] || fail "metric-log parent directory does not exist"
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
command -v "$qemu" >/dev/null 2>&1 || fail "required command is unavailable: $qemu"
plugin_dir=${QEMU_PLUGIN_DIR:-"$root/build/benchmark/qemu-plugins"}
case "$plugin_dir" in
  /*) ;;
  *) plugin_dir=$root/$plugin_dir ;;
esac
case $(uname -s) in
  Linux) suffix=so ;;
  Darwin) suffix=dylib ;;
  *) fail "QEMU plugin execution is unsupported on $(uname -s)" ;;
esac
profile=${QEMU_PROFILE_PLUGIN:-$plugin_dir/profile.$suffix}
cache=${QEMU_CACHE_PLUGIN:-$plugin_dir/cache.$suffix}
[ -f "$profile" ] || fail "profile plugin is missing; run scripts/build-qemu-plugins.sh"
[ -f "$cache" ] || fail "cache plugin is missing; run scripts/build-qemu-plugins.sh"

exec "$qemu" \
  -machine virt -accel tcg,thread=single -smp 1 -m 512M \
  -bios none -kernel "$binary" -display none -monitor none -serial stdio \
  -fw_cfg "name=opt/accela/sysy-input,file=$input" \
  -plugin "$profile" -plugin "$cache" -d plugin -D "$metric_log" \
  </dev/null
