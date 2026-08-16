#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
manifest="${LOOP_PERF_MANIFEST:-$root/tools/qemu/loop-perf-tests.txt}"
output="${1:-$root/build/qemu-loop-perf/results.tsv}"
mkdir -p "$(dirname "$output")"

printf 'test\tinstructions\tloads\tstores\tl1_accesses\tl1_hits\tl1_misses\tasm_bytes\n' > "$output"

while IFS= read -r relative || [[ -n "$relative" ]]; do
  [[ -z "$relative" || "$relative" == \#* ]] && continue
  source_file="$root/$relative"
  name="$(basename "$source_file" .sy)"

  instruction_report="$(QEMU_PROFILE=1 "$root/scripts/qemu-run.sh" "$source_file")"
  if [[ "$instruction_report" != *instructions=* ]]; then
    printf 'missing instruction profile for %s\n' "$relative" >&2
    exit 1
  fi
  instruction_line="${instruction_report#*instructions=}"
  instructions="${instruction_line%% *}"
  load_part="${instruction_report#*loads=}"
  loads="${load_part%% *}"
  store_part="${instruction_report#*stores=}"
  stores="${store_part%%$'\n'*}"
  stores="${stores%% *}"

  cache_report="$(QEMU_PROFILE=cache "$root/scripts/qemu-run.sh" "$source_file")"
  access_part="${cache_report#*accesses=}"
  accesses="${access_part%% *}"
  hit_part="${cache_report#*hits=}"
  hits="${hit_part%% *}"
  miss_part="${cache_report#*misses=}"
  misses="${miss_part%% *}"

  assembly="$root/build/qemu-run/accela/$name/program.s"
  asm_bytes="$(wc -c < "$assembly" | tr -d ' ')"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$relative" "$instructions" "$loads" "$stores" \
    "$accesses" "$hits" "$misses" "$asm_bytes" >> "$output"
  printf 'PROFILE %s instructions=%s misses=%s\n' "$relative" "$instructions" "$misses"
done < "$manifest"

markdown="${output%.tsv}.md"
{
  printf '| Test | Instructions | Loads | Stores | L1 accesses | L1 hits | L1 misses | ASM bytes |\n'
  printf '|---|---:|---:|---:|---:|---:|---:|---:|\n'
  while IFS=$'\t' read -r test instructions loads stores accesses hits misses asm_bytes; do
    [[ "$test" == test ]] && continue
    printf '| `%s` | %s | %s | %s | %s | %s | %s | %s |\n' \
      "$test" "$instructions" "$loads" "$stores" "$accesses" "$hits" "$misses" "$asm_bytes"
  done < "$output"
} > "$markdown"

printf 'WROTE %s\nWROTE %s\n' "$output" "$markdown"
