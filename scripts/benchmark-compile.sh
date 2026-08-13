#!/usr/bin/env sh

set -eu

fail() {
  printf '%s\n' "$*" >&2
  exit 2
}

[ "$#" -eq 4 ] || fail "usage: scripts/benchmark-compile.sh PROFILE.json SOURCE.sy OUTPUT.s REMARKS.jsonl"
profile=$1
source_file=$2
output=$3
remarks=$4
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
classes=$root/build/classes/java/main

[ -f "$profile" ] || fail "pipeline profile is not a regular file: $profile"
[ -f "$source_file" ] || fail "source is not a regular file: $source_file"
[ -d "$classes" ] || fail "compiler classes are missing; run ./gradlew classes --no-daemon"
[ -d "$(dirname -- "$output")" ] || fail "output parent directory does not exist"
[ -d "$(dirname -- "$remarks")" ] || fail "remarks parent directory does not exist"

if [ -n "${ACCELA_JAVA_HOME:-}" ]; then
  java=$ACCELA_JAVA_HOME/bin/java
elif [ -n "${JAVA_HOME:-}" ]; then
  java=$JAVA_HOME/bin/java
else
  java=java
fi
command -v "$java" >/dev/null 2>&1 || [ -x "$java" ] \
  || fail "required Java executable is unavailable: $java"

exec "$java" -cp "$classes" BenchmarkCompiler "$source_file" -o "$output" \
  --profile "$profile" --remarks "$remarks"
