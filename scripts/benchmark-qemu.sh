#!/usr/bin/env sh

set -eu

fail() {
  printf '%s\n' "$*" >&2
  exit 2
}

[ "$#" -eq 2 ] || fail "usage: scripts/benchmark-qemu.sh PROGRAM_ELF METRIC_LOG"
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
binary=$1
metric_log=$2
[ -f "$binary" ] || fail "program ELF is not a regular file: $binary"
[ -d "$(dirname -- "$metric_log")" ] || fail "metric-log parent directory does not exist"

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
  -plugin "$profile" -plugin "$cache" -d plugin -D "$metric_log"
