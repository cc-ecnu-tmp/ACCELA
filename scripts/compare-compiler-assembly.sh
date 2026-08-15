#!/usr/bin/env sh

set -eu

if [ "${1:-}" = "--case" ]; then
  if [ "$#" -ne 5 ]; then exit 2; fi
  baseline_classes=$2
  candidate_classes=$3
  workspace=$4
  source=$5
  name=$(basename "$source" .sy)
  "$ACCELA_COMPARE_JAVA" -cp "$baseline_classes" Compiler "$source" \
    -o "$workspace/baseline/$name.s"
  "$ACCELA_COMPARE_JAVA" -cp "$candidate_classes" Compiler "$source" \
    -o "$workspace/candidate/$name.s"
  result=same
  if ! cmp -s "$workspace/baseline/$name.s" "$workspace/candidate/$name.s"; then
    result=different
  fi
  printf '%s\n' "$result" > "$workspace/results/$name"
  exit 0
fi

if [ "$#" -ne 3 ]; then
  echo "usage: scripts/compare-compiler-assembly.sh BASELINE_CLASSES CANDIDATE_CLASSES CORPUS" >&2
  exit 2
fi

baseline_classes=$1
candidate_classes=$2
corpus=$3
if [ -n "${ACCELA_JAVA:-}" ]; then
  java=$ACCELA_JAVA
elif [ -n "${ACCELA_JAVA_HOME:-${JAVA_HOME:-}}" ]; then
  java=${ACCELA_JAVA_HOME:-$JAVA_HOME}/bin/java
elif command -v java >/dev/null 2>&1; then
  java=java
else
  echo "ACCELA_JAVA, ACCELA_JAVA_HOME or JAVA_HOME must select Java 21" >&2
  exit 2
fi
for directory in "$baseline_classes" "$candidate_classes" "$corpus"; do
  if [ ! -d "$directory" ]; then
    echo "required directory does not exist: $directory" >&2
    exit 2
  fi
done

workspace=${TMPDIR:-/tmp}/accela-assembly-compare.$$
trap 'rm -rf "$workspace"' EXIT HUP INT TERM
mkdir -p "$workspace/baseline" "$workspace/candidate" "$workspace/results"
jobs=${ACCELA_COMPARE_JOBS:-1}
case "$jobs" in
  ''|*[!0-9]*|0) echo "ACCELA_COMPARE_JOBS must be a positive integer" >&2; exit 2 ;;
esac
ACCELA_COMPARE_JAVA=$java
export ACCELA_COMPARE_JAVA
find "$corpus" -maxdepth 1 -type f -name '*.sy' -print | LC_ALL=C sort > "$workspace/sources"
running=0
while IFS= read -r source; do
  "$0" --case "$baseline_classes" "$candidate_classes" "$workspace" "$source" &
  running=$((running + 1))
  if [ "$running" -eq "$jobs" ]; then
    wait
    running=0
  fi
done < "$workspace/sources"
wait

total=$(find "$workspace/results" -type f | wc -l | tr -d ' ')
if [ "$total" -eq 0 ]; then
  echo "corpus contains no .sy source" >&2
  exit 2
fi
expected=$(wc -l < "$workspace/sources" | tr -d ' ')
if [ "$total" -ne "$expected" ]; then
  echo "assembly comparison incomplete: expected $expected results, found $total" >&2
  exit 1
fi
different=$(grep -l '^different$' "$workspace"/results/* 2>/dev/null | wc -l | tr -d ' ')
for result in "$workspace"/results/*; do
  if [ "$(cat "$result")" = different ]; then
    echo "DIFF $(basename "$result")"
  fi
done
echo "assembly comparison: $total cases, $different different"
