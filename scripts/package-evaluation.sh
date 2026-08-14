#!/usr/bin/env sh

set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
output=${1:-"$root/accela-evaluation.tar.zst"}
official=$root/.tmp/official

command -v git >/dev/null 2>&1 || { echo "git is required" >&2; exit 2; }
command -v tar >/dev/null 2>&1 || { echo "tar is required" >&2; exit 2; }
command -v zstd >/dev/null 2>&1 || { echo "zstd is required" >&2; exit 2; }
for suite in \
  2026-riscv-performance/performance \
  2025-riscv-prelim; do
  [ -d "$official/$suite" ] || { echo "missing suite: .tmp/official/$suite" >&2; exit 2; }
done

stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT HUP INT TERM
git -C "$root" archive HEAD | tar -xf - -C "$stage"
mkdir -p "$stage/.tmp"
cp -a "$official" "$stage/.tmp/"
tar --zstd -cf "$output" -C "$stage" .
printf 'created %s\n' "$output"
