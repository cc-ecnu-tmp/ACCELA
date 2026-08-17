#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
output="$root/vector-results"
java_home="${ACCELA_JAVA_HOME:-/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home}"
java="$java_home/bin/java"
classes="$root/build/classes/java/main"
vlen="${RISCV_VLEN:-128}"

JAVA_HOME="$java_home" bash "$root/gradlew" -p "$root" classes --no-daemon >/dev/null
mkdir -p "$output"

count=0
for suite in functional hidden_functional vector; do
  for source in "$root/testsuite/$suite"/*.sy; do
    name="$(basename "$source" .sy)"
    destination="$output/${suite}__${name}"
    mkdir -p "$destination"

    "$java" -cp "$classes" Compiler "$source" \
      -o "$destination/scalar.s" --simd none --riscv-vlen "$vlen"
    "$java" -cp "$classes" Compiler "$source" \
      -o "$destination/vector.s" --simd rvv --riscv-vlen "$vlen"

    status=0
    diff -u "$destination/scalar.s" "$destination/vector.s" \
      > "$destination/diff" || status=$?
    if [[ "$status" -gt 1 ]]; then
      printf 'diff failed for %s/%s\n' "$suite" "$name" >&2
      exit "$status"
    fi
    count=$((count + 1))
  done
done

if [[ "$count" -ne 155 ]]; then
  printf 'expected 155 result directories, generated %d\n' "$count" >&2
  exit 1
fi
printf 'generated %d scalar/vector assembly comparisons in %s\n' "$count" "$output"
