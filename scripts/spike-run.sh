#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
source_file="${1:?usage: scripts/spike-run.sh SOURCE.sy}"
source_file="$(cd "$(dirname "$source_file")" && pwd)/$(basename "$source_file")"
name="$(basename "$source_file" .sy)"
work="$root/build/spike-run/$name"
mkdir -p "$work"

java_home="${ACCELA_JAVA_HOME:-/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home}"
java="$java_home/bin/java"
classes="$root/build/classes/java/main"
vlen="${RISCV_VLEN:-128}"
spike="${SPIKE:-spike}"
if [[ "$spike" == spike && -x /tmp/accela-spike-install/bin/spike ]]; then
  spike=/tmp/accela-spike-install/bin/spike
fi
timeout_command="${TIMEOUT:-timeout}"
if ! command -v "$timeout_command" >/dev/null 2>&1 && command -v gtimeout >/dev/null 2>&1; then
  timeout_command=gtimeout
fi

if [[ ! -d "$classes" ]]; then
  JAVA_HOME="$java_home" bash "$root/gradlew" -p "$root" classes --no-daemon
fi

assembly="$work/program.s"
elf="$work/program.elf"
actual="$work/program.out"
stdin="$work/program.in"
"$java" -cp "$classes" Compiler "$source_file" -o "$assembly" \
  --simd rvv --riscv-vlen "$vlen"

/opt/homebrew/opt/riscv64-elf-gcc/bin/riscv64-elf-gcc \
  -march="rv64gcv_zvl${vlen}b" -mabi=lp64d -mcmodel=medany -O2 \
  -ffreestanding -fno-builtin -nostdlib -nostartfiles -DACCELA_SPIKE \
  -Wl,-T,"$root/tools/qemu/linker.ld" \
  "$root/tools/qemu/crt.S" "$root/tools/qemu/runtime.c" "$assembly" \
  -lgcc -o "$elf"

input="${source_file%.sy}.in"
expected="${source_file%.sy}.out"
if [[ -f "$input" ]]; then cp "$input" "$stdin"; else : > "$stdin"; fi
printf '\n' >> "$stdin"
"$timeout_command" "${SPIKE_TIMEOUT:-120}s" \
  "$spike" --isa="rv64gcv_zvl${vlen}b" "$elf" < "$stdin" > "$actual"
diff -u "$expected" "$actual"
printf 'PASS %s\n' "$source_file"
