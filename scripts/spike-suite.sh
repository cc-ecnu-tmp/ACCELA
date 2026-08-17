#!/usr/bin/env bash
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
result_dir="$root/build/spike-suite"
mkdir -p "$result_dir"
summary="$result_dir/summary.txt"
: > "$summary"

pass=0
fail=0
for suite in functional hidden_functional; do
  for source in "$root/testsuite/$suite"/*.sy; do
    label="$suite/$(basename "$source" .sy)"
    log="$result_dir/${suite}_$(basename "$source" .sy).log"
    if bash "$root/scripts/spike-run.sh" "$source" > "$log" 2>&1; then
      printf 'PASS %s\n' "$label" | tee -a "$summary"
      pass=$((pass + 1))
    else
      status=$?
      printf 'FAIL %s (%d)\n' "$label" "$status" | tee -a "$summary"
      fail=$((fail + 1))
    fi
  done
done

printf 'TOTAL 140 PASS %d FAIL %d\n' "$pass" "$fail" | tee -a "$summary"
exit "$fail"
