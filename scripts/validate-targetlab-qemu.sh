#!/usr/bin/env sh

set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"

python_bin=${PYTHON:-python3}
linux_prefix=${TARGETLAB_LINUX_PREFIX:-riscv64-linux-gnu}
elf_prefix=${TARGETLAB_ELF_PREFIX:-riscv64-elf}
qemu_user=${TARGETLAB_QEMU_USER:-qemu-riscv64}
qemu_system=${TARGETLAB_QEMU_SYSTEM:-qemu-system-riscv64}
gdb_bin=${TARGETLAB_GDB:-gdb}
sysroot=${TARGETLAB_LINUX_SYSROOT:-/usr/riscv64-linux-gnu}
clock_hz=${TARGETLAB_QEMU_CLOCK_HZ:-50000000}
minimum_cycles=${TARGETLAB_QEMU_MINIMUM_CYCLES:-1000000}
result_dir=${TARGETLAB_QEMU_RESULT_DIR:-build/targetlab-qemu-validation}

mkdir -p "$result_dir"

bare_config="$result_dir/baremetal-config.json"
bare_raw="$result_dir/baremetal.raw.jsonl"
bare_collected="$result_dir/baremetal.collected.json"
bare_profile="$result_dir/baremetal.profile.json"

"$python_bin" -m tools.targetlab configure \
  --backend baremetal \
  --cc "${elf_prefix}-gcc" \
  --objcopy "${elf_prefix}-objcopy" \
  --nm "${elf_prefix}-nm" \
  --clock-hz "$clock_hz" \
  --minimum-cycles "$minimum_cycles" \
  --measurement-mode qemu_proxy \
  --gdb "$gdb_bin" \
  --gdb-remote localhost:3333 \
  --debug-server-kind qemu \
  --debug-server-mode managed \
  --debug-server-executable "$qemu_system" \
  --startup tools/targetlab/tests/fixtures/rv64-start.S \
  --linker tools/targetlab/tests/fixtures/rv64-virt.ld \
  --build-dir "$result_dir/baremetal-build" \
  --output "$bare_config"
"$python_bin" -m tools.targetlab doctor "$bare_config"
"$python_bin" -m tools.targetlab build "$bare_config"
"$python_bin" -m tools.targetlab run "$bare_config" --output "$bare_raw"
"$python_bin" -m tools.targetlab collect "$bare_raw" "$bare_collected"
"$python_bin" -m tools.targetlab profile "$bare_collected" \
  config/target/boomv3-development.json "$bare_profile" \
  --profile-id qemu-rv64gc-baremetal-smoke
"$python_bin" -m tools.targetlab validate "$bare_profile"

linux_config="$result_dir/linux-config.json"
linux_raw="$result_dir/linux.raw.jsonl"
linux_collected="$result_dir/linux.collected.json"
linux_profile="$result_dir/linux.profile.json"

"$python_bin" -m tools.targetlab configure \
  --backend linux \
  --cc "${linux_prefix}-gcc" \
  --objcopy "${linux_prefix}-objcopy" \
  --nm "${linux_prefix}-nm" \
  --clock-hz "$clock_hz" \
  --minimum-cycles "$minimum_cycles" \
  --measurement-mode qemu_proxy \
  --execute "$qemu_user -L $sysroot" \
  --build-dir "$result_dir/linux-build" \
  --output "$linux_config"
"$python_bin" -m tools.targetlab doctor "$linux_config"
"$python_bin" -m tools.targetlab build "$linux_config"
linux_run_log="$result_dir/linux-run.log"
if ! "$python_bin" -m tools.targetlab run "$linux_config" --output "$linux_raw" \
    2>"$linux_run_log"; then
  cat "$linux_run_log" >&2
  if grep -Eq 'TargetLab metric .* failed at sample .*: (baseline subtraction is non-positive|counter moved backwards)' \
      "$linux_run_log"; then
    printf '%s\n' 'TargetLab QEMU validation: bare-metal passed; Linux target correctly rejected invalid timing evidence'
    exit 0
  fi
  exit 2
fi
"$python_bin" -m tools.targetlab collect "$linux_raw" "$linux_collected"
if "$python_bin" -m tools.targetlab profile "$linux_collected" \
    config/target/boomv3-development.json "$linux_profile" \
    --profile-id qemu-rv64gc-linux-smoke; then
  "$python_bin" -m tools.targetlab validate "$linux_profile"
  printf '%s\n' 'TargetLab QEMU validation: bare-metal and Linux profiles passed quality gates'
else
  printf '%s\n' 'TargetLab QEMU validation: bare-metal passed; Linux transport passed but quality gate rejected noisy QEMU timing'
fi
