#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
java_home="${ACCELA_JAVA_HOME:-/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home}"
java="${java_home}/bin/java"
spike="${SPIKE:-spike}"
assembler="${RISCV_AS:-/opt/homebrew/opt/riscv64-elf-binutils/bin/riscv64-elf-as}"
linker="${RISCV_LD:-/opt/homebrew/opt/riscv64-elf-binutils/bin/riscv64-elf-ld}"
source_file="${1:-${root}/testsuite/vector/rvv_runtime.sy}"
vlen="${RISCV_VLEN:-128}"
expected="${EXPECTED:-37}"

work="$(mktemp -d "${TMPDIR:-/tmp}/accela-rvv-spike.XXXXXX")"
if [[ "${KEEP_WORK:-0}" == "1" ]]; then
  echo "RVV Spike artifacts: $work"
else
  trap 'rm -rf "$work"' EXIT
fi

JAVA_HOME="$java_home" bash "$root/gradlew" -p "$root" classes --no-daemon >/dev/null

{
  "$java" -cp "$root/build/classes/java/main" \
    accela.Main --asm --simd rvv --riscv-vlen "$vlen" "$source_file"
  cat <<EOF
.section .text
.globl _start
_start:
  li sp, 0x80020000
  li t0, 0x6600
  csrs mstatus, t0
  call main
  li a0, 0
  j __accela_exit

.globl getint
getint:
  li a0, 5
  ret

.globl putint
putint:
  li t0, $expected
  bne a0, t0, __accela_fail
  ret

.globl putch
putch:
  li t0, 10
  bne a0, t0, __accela_fail
  ret

__accela_fail:
  li a0, 1
__accela_exit:
  slli a0, a0, 1
  ori a0, a0, 1
  la t0, tohost
  sd a0, 0(t0)
1:
  j 1b

.section .tohost,"aw",@progbits
.p2align 3
.globl tohost
tohost:
  .dword 0
.globl fromhost
fromhost:
  .dword 0
EOF
} | "$assembler" -march=rv64gcv -o "$work/test.o"

"$linker" -Ttext=0x80001000 --section-start=.tohost=0x80010000 \
  -e _start -o "$work/test.elf" "$work/test.o"
if [[ -n "${SPIKE_ARGS:-}" ]]; then
  read -r -a spike_args <<<"$SPIKE_ARGS"
  "$spike" "${spike_args[@]}" --isa="rv64gcv_zvl${vlen}b" "$work/test.elf"
else
  "$spike" --isa="rv64gcv_zvl${vlen}b" "$work/test.elf"
fi
