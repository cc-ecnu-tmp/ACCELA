#!/usr/bin/env sh

set -eu

if [ "$#" -ne 3 ]; then
  echo "usage: scripts/qemu-compare-classes.sh BASELINE_CLASSES CANDIDATE_CLASSES SOURCE.sy" >&2
  exit 2
fi
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
baseline_classes=$1
candidate_classes=$2
source=$3
runs=${QEMU_PAIRED_RUNS:-5}
riscv_size=${RISCV_SIZE:-riscv64-elf-size}
case "$runs" in
  ''|*[!0-9]*|0) echo "QEMU_PAIRED_RUNS must be a positive integer" >&2; exit 2 ;;
esac
for directory in "$baseline_classes" "$candidate_classes"; do
  if [ ! -d "$directory" ]; then
    echo "compiler classes directory does not exist: $directory" >&2
    exit 2
  fi
done
if [ ! -f "$source" ]; then
  echo "source does not exist: $source" >&2
  exit 2
fi

workspace=$(mktemp -d "${TMPDIR:-/tmp}/accela-qemu-paired.XXXXXX")
trap 'rm -rf "$workspace"' EXIT HUP INT TERM
name=$(basename "$source" .sy)
run=1
while [ "$run" -le "$runs" ]; do
  baseline_output=$(ACCELA_CLASSES="$baseline_classes" QEMU_PROFILE=1 \
    QEMU_WORK_ROOT="$workspace/baseline-$run" \
    QEMU_COMPILER_METADATA="$workspace/baseline-$run/compiler.json" \
    "$root/scripts/qemu-run.sh" "$source")
  candidate_output=$(ACCELA_CLASSES="$candidate_classes" QEMU_PROFILE=1 \
    QEMU_WORK_ROOT="$workspace/candidate-$run" \
    QEMU_COMPILER_METADATA="$workspace/candidate-$run/compiler.json" \
    "$root/scripts/qemu-run.sh" "$source")
  baseline=$(printf '%s\n' "$baseline_output" | awk -F'[ =]' \
    '/^instructions=/{print $2; found=1} END{if (!found) exit 1}')
  candidate=$(printf '%s\n' "$candidate_output" | awk -F'[ =]' \
    '/^instructions=/{print $2; found=1} END{if (!found) exit 1}')
  case "$baseline:$candidate" in
    *[!0-9:]*|0:*|*:0) echo "invalid instruction count for $name run $run" >&2; exit 1 ;;
  esac
  ratio=$(awk -v baseline="$baseline" -v candidate="$candidate" \
    'BEGIN { printf "%.9f", baseline / candidate }')
  baseline_metrics=$(python3 -c \
    'import json,sys; d=json.load(open(sys.argv[1])); print(d["elapsed_seconds"], d["peak_bytes"])' \
    "$workspace/baseline-$run/compiler.json")
  candidate_metrics=$(python3 -c \
    'import json,sys; d=json.load(open(sys.argv[1])); print(d["elapsed_seconds"], d["peak_bytes"])' \
    "$workspace/candidate-$run/compiler.json")
  baseline_seconds=${baseline_metrics% *}
  baseline_peak=${baseline_metrics#* }
  candidate_seconds=${candidate_metrics% *}
  candidate_peak=${candidate_metrics#* }
  baseline_code=$("$riscv_size" -A "$workspace/baseline-$run/accela/$name/program.o" |
    awk '$1 ~ /^\.text/ { total += $2 } END { if (total <= 0) exit 1; print total }')
  candidate_code=$("$riscv_size" -A "$workspace/candidate-$run/accela/$name/program.o" |
    awk '$1 ~ /^\.text/ { total += $2 } END { if (total <= 0) exit 1; print total }')
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$name" "$run" "$baseline" "$candidate" "$ratio" \
    "$baseline_seconds" "$candidate_seconds" "$baseline_peak" "$candidate_peak" \
    "$baseline_code" "$candidate_code"
  run=$((run + 1))
done
