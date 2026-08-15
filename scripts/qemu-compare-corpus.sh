#!/usr/bin/env sh

set -eu

if [ "${1:-}" = "--case" ]; then
  if [ "$#" -ne 7 ]; then exit 2; fi
  case_id=$2
  baseline_classes=$3
  candidate_classes=$4
  corpus=$5
  output=$6
  runner=$7
  "$runner" "$baseline_classes" "$candidate_classes" "$corpus/$case_id.sy" \
    > "$output/$case_id.runs"
  exit 0
fi

if [ "$#" -ne 5 ]; then
  echo "usage: scripts/qemu-compare-corpus.sh BASELINE_CLASSES CANDIDATE_CLASSES CORPUS CASE_LIST OUTPUT" >&2
  exit 2
fi
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
baseline_classes=$1
candidate_classes=$2
corpus=$3
case_list=$4
output=$5
jobs=${QEMU_PAIRED_JOBS:-1}
case "$jobs" in
  ''|*[!0-9]*|0) echo "QEMU_PAIRED_JOBS must be a positive integer" >&2; exit 2 ;;
esac
for path in "$baseline_classes" "$candidate_classes" "$corpus"; do
  if [ ! -d "$path" ]; then echo "required directory does not exist: $path" >&2; exit 2; fi
done
if [ ! -f "$case_list" ]; then echo "case list does not exist: $case_list" >&2; exit 2; fi
if [ -e "$output" ]; then echo "output already exists: $output" >&2; exit 2; fi
mkdir -p "$output"

count=0
running=0
while IFS= read -r case_id; do
  case_id=${case_id%"$(printf '\r')"}
  if [ -z "$case_id" ]; then continue; fi
  case "$case_id" in
    *[!A-Za-z0-9_.-]*) echo "invalid case id: $case_id" >&2; exit 2 ;;
  esac
  if [ ! -f "$corpus/$case_id.sy" ]; then
    echo "case source does not exist: $case_id" >&2
    exit 2
  fi
  "$0" --case "$case_id" "$baseline_classes" "$candidate_classes" \
    "$corpus" "$output" "$root/scripts/qemu-compare-classes.sh" &
  count=$((count + 1))
  running=$((running + 1))
  if [ "$running" -eq "$jobs" ]; then wait; running=0; fi
done < "$case_list"
wait

completed=$(find "$output" -maxdepth 1 -type f -name '*.runs' | wc -l | tr -d ' ')
if [ "$completed" -ne "$count" ]; then
  echo "paired comparison incomplete: expected $count results, found $completed" >&2
  exit 1
fi
expected_runs=${QEMU_PAIRED_RUNS:-5}
find "$output" -maxdepth 1 -type f -name '*.runs' -print | LC_ALL=C sort |
while IFS= read -r result; do
  actual_runs=$(wc -l < "$result" | tr -d ' ')
  if [ "$actual_runs" -ne "$expected_runs" ]; then
    echo "paired comparison incomplete: $result has $actual_runs/$expected_runs runs" >&2
    exit 1
  fi
done
find "$output" -maxdepth 1 -type f -name '*.runs' -print | LC_ALL=C sort |
while IFS= read -r result; do cat "$result"; done > "$output/results.tsv"
echo "paired QEMU comparison: $completed cases x $expected_runs runs"
