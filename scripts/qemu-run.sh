#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
source_file="${1:?usage: scripts/qemu-run.sh SOURCE.sy}"
source_file="$(cd "$(dirname "$source_file")" && pwd)/$(basename "$source_file")"
name="$(basename "$source_file" .sy)"
compiler="${QEMU_COMPILER:-accela}"
work="$root/build/qemu-run/$compiler/$name"
mkdir -p "$work"

java_home="${ACCELA_JAVA_HOME:-/opt/homebrew/opt/openjdk@21}"
java="$java_home/bin/java"
classes="$root/build/classes/java/main"
if [[ "$compiler" == accela && ! -d "$classes" ]]; then
  JAVA_HOME="$java_home" bash "$root/gradlew" -p "$root" classes --no-daemon
fi

assembly="$work/program.s"
object="$work/program.o"
elf="$work/program.elf"
actual="$work/program.out"
case "$compiler" in
  accela)
    "$java" -cp "$classes" Compiler "$source_file" -o "$assembly"
    program="$assembly"
    ;;
  llvm)
    "${LLVM_CLANG:-/opt/homebrew/opt/llvm/bin/clang}" \
      --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -mcmodel=medany \
      -O3 -fwrapv -ffp-contract=off -ffreestanding -fno-builtin -x c \
      -include "$root/tools/qemu/sysy-builtins.h" \
      -S "$source_file" -o "$assembly"
    "${LLVM_CLANG:-/opt/homebrew/opt/llvm/bin/clang}" \
      --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d \
      -c "$assembly" -o "$object"
    program="$object"
    ;;
  *)
    printf 'unknown QEMU_COMPILER: %s\n' "$compiler" >&2
    exit 2
    ;;
esac
riscv64-elf-gcc \
  -march=rv64gc -mabi=lp64d -mcmodel=medany -O2 \
  -ffreestanding -fno-builtin -nostdlib -nostartfiles \
  -Wl,-T,"$root/tools/qemu/linker.ld" \
  "$root/tools/qemu/crt.S" "$root/tools/qemu/runtime.c" "$program" \
  -lgcc -o "$elf"

input="${source_file%.sy}.in"
expected="${source_file%.sy}.out"
stdin="$work/program.in"
if [[ -f "$input" ]]; then cp "$input" "$stdin"; else : > "$stdin"; fi
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
  profile="$work/profile.dylib"
  profile_log="$work/profile.log"
  # shellcheck disable=SC2046
  cc -dynamiclib -undefined dynamic_lookup -fvisibility=hidden \
    $(pkg-config --cflags glib-2.0) \
    -I"${QEMU_PLUGIN_INCLUDE:-/opt/homebrew/include}" \
    "$root/tools/qemu/$profile_source" -o "$profile"
  qemu+=(-plugin "$profile" -d plugin -D "$profile_log")
fi
timeout "${QEMU_TIMEOUT:-120}" "${qemu[@]}" \
  < "$stdin" > "$actual"

diff -u "$expected" "$actual"
if [[ "$profile_mode" != 0 ]]; then cat "$profile_log"; fi
printf 'PASS %s/%s\n' "$compiler" "$name"
