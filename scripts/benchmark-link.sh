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

if [ "${RISCV_GCC+x}" = x ]; then
  fail "RISCV_GCC is not supported by the formal linker; unset it"
fi

compiler=riscv64-elf-gcc
readelf=riscv64-elf-readelf
command -v "$compiler" >/dev/null 2>&1 || fail "required command is unavailable: $compiler"
command -v "$readelf" >/dev/null 2>&1 || fail "required command is unavailable: $readelf"

validated=0
cleanup_invalid_output() {
  if [ "$validated" -ne 1 ]; then
    rm -f -- "$output"
  fi
}
trap cleanup_invalid_output 0

"$compiler" \
  -march=rv64gc -mabi=lp64d -mcmodel=medany -O2 \
  -fno-pie -no-pie -static \
  -ffreestanding -fno-builtin -nostdlib -nostartfiles \
  -Wl,-T,"$root/tools/qemu/linker.ld" \
  "$root/tools/qemu/crt.S" "$root/tools/qemu/runtime.c" "$input" \
  -lgcc -o "$output"

[ -f "$output" ] || fail "linker did not produce a regular output file: $output"

elf_header=$(LC_ALL=C "$readelf" --wide --file-header "$output") \
  || fail "failed to inspect linked ELF header: $output"
elf_type=$(
  printf '%s\n' "$elf_header" |
    LC_ALL=C awk '
      $1 == "Type:" { count += 1; type = $2 }
      END {
        if (count != 1 || type == "") exit 1
        print type
      }
    '
) || fail "readelf did not report exactly one ELF type: $output"
[ "$elf_type" = EXEC ] \
  || fail "linked artifact is not ET_EXEC: observed $elf_type"

program_headers=$(LC_ALL=C "$readelf" --wide --program-headers "$output") \
  || fail "failed to inspect linked ELF program headers: $output"
forbidden_segment=$(
  printf '%s\n' "$program_headers" |
    LC_ALL=C awk '
      $1 == "INTERP" || $1 == "DYNAMIC" { print $1; exit }
    '
)
[ -z "$forbidden_segment" ] \
  || fail "linked artifact contains forbidden PT_$forbidden_segment segment"

relocations=$(LC_ALL=C "$readelf" --wide --relocs "$output") \
  || fail "failed to inspect linked ELF relocations: $output"
relocation_section_count=$(
  printf '%s\n' "$relocations" |
    LC_ALL=C awk '
      $1 == "Relocation" && $2 == "section" { count += 1 }
      END { print count + 0 }
    '
)
[ "$relocation_section_count" -eq 0 ] \
  || fail "linked artifact contains unresolved relocation sections"

validated=1
trap - 0
