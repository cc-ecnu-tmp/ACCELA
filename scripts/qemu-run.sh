#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
source_file="${1:?usage: scripts/qemu-run.sh SOURCE.sy}"
source_file="$(cd "$(dirname "$source_file")" && pwd)/$(basename "$source_file")"
name="$(basename "$source_file" .sy)"
compiler="${QEMU_COMPILER:-accela}"
work_root="${QEMU_WORK_ROOT:-$root/build/qemu-run}"
work="$work_root/$compiler/$name"
mkdir -p "$work"

if [[ -n "${ACCELA_JAVA:-}" ]]; then
  java="$ACCELA_JAVA"
elif [[ -n "${JAVA_HOME:-}" ]]; then
  java="$JAVA_HOME/bin/java"
else
  java=java
fi
classes="${ACCELA_CLASSES:-$root/build/classes/java/main}"
riscv_gcc="${RISCV_GCC:-riscv64-elf-gcc}"
llvm_clang="${LLVM_CLANG:-clang}"
if [[ "$compiler" == accela && ! -d "$classes" ]]; then
  bash "$root/gradlew" -p "$root" classes --no-daemon
fi

assembly="$work/program.s"
object="$work/program.o"
elf="$work/program.elf"
actual="$work/program.out"
case "$compiler" in
  accela)
    if [[ -n "${QEMU_COMPILER_METADATA:-}" ]]; then
      python3 "$root/tools/benchmark/run_measured.py" "$QEMU_COMPILER_METADATA" -- \
        "$java" -cp "$classes" Compiler "$source_file" -o "$assembly"
    else
      "$java" -cp "$classes" Compiler "$source_file" -o "$assembly"
    fi
    "$riscv_gcc" -march=rv64gc -mabi=lp64d -c "$assembly" -o "$object"
    program="$object"
    ;;
  llvm)
    "$llvm_clang" \
      --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -mcmodel=medany \
      -O3 -fwrapv -ffp-contract=off -ffreestanding -fno-builtin -x c \
      -include "$root/tools/qemu/sysy-builtins.h" \
      -S "$source_file" -o "$assembly"
    "$llvm_clang" \
      --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d \
      -c "$assembly" -o "$object"
    program="$object"
    ;;
  *)
    printf 'unknown QEMU_COMPILER: %s\n' "$compiler" >&2
    exit 2
    ;;
esac
"$riscv_gcc" \
  -march=rv64gc -mabi=lp64d -mcmodel=medany -O2 \
  -ffreestanding -fno-builtin -nostdlib -nostartfiles \
  -Wl,-T,"$root/tools/qemu/linker.ld" \
  "$root/tools/qemu/crt.S" "$root/tools/qemu/runtime.c" "$program" \
  -lgcc -o "$elf"

input="${source_file%.sy}.in"
expected="${source_file%.sy}.out"
stdin="$work/program.in"
if [[ -f "$input" ]]; then cp "$input" "$stdin"; else : > "$stdin"; fi
# The bare-metal serial runtime has no EOF indication.  Supply one whitespace transport
# terminator so token readers can finish when a corpus input does not end in whitespace.
printf '\n' >> "$stdin"
qemu=(qemu-system-riscv64 -machine virt -m 512M -bios none -kernel "$elf"
  -display none -monitor none -serial stdio)
profile_mode="${QEMU_PROFILE:-0}"
if [[ "$profile_mode" != 0 ]]; then
  if [[ "$profile_mode" != 1 && "$profile_mode" != hotblocks && "$profile_mode" != cache ]]; then
    printf 'unknown QEMU_PROFILE mode: %s\n' "$profile_mode" >&2; exit 2
  fi
  profile_source=profile.c
  if [[ "$profile_mode" != 1 ]]; then profile_source="$profile_mode.c"; fi
  case "$(uname -s)" in
    Darwin)
      profile="$work/profile.dylib"
      plugin_link=(-dynamiclib -undefined dynamic_lookup)
      if [[ -z "${QEMU_PLUGIN_INCLUDE:-}" ]]; then
        printf 'QEMU_PLUGIN_INCLUDE is required on Darwin\n' >&2
        exit 2
      fi
      ;;
    Linux)
      profile="$work/profile.so"
      plugin_link=(-shared -fPIC)
      ;;
    *)
      printf 'unsupported QEMU plugin host: %s\n' "$(uname -s)" >&2
      exit 2
      ;;
  esac
  profile_log="$work/profile.log"
  plugin_include_args=()
  if [[ -n "${QEMU_PLUGIN_INCLUDE:-}" ]]; then
    plugin_include_args=(-I"$QEMU_PLUGIN_INCLUDE")
  fi
  # shellcheck disable=SC2046
  cc "${plugin_link[@]}" -fvisibility=hidden \
    $(pkg-config --cflags glib-2.0) \
    "${plugin_include_args[@]}" \
    "$root/tools/qemu/$profile_source" -o "$profile"
  qemu+=(-plugin "$profile" -d plugin -D "$profile_log")
fi
timeout "${QEMU_TIMEOUT:-120}" "${qemu[@]}" \
  < "$stdin" > "$actual"

diff -u "$expected" "$actual"
if [[ "$profile_mode" != 0 ]]; then cat "$profile_log"; fi
printf 'PASS %s/%s\n' "$compiler" "$name"
