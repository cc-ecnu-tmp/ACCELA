#!/usr/bin/env sh

set -eu

fail() {
  printf '%s\n' "$*" >&2
  exit 2
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command is unavailable: $1"
}

[ "$#" -le 1 ] || fail "usage: scripts/build-qemu-plugins.sh [OUTPUT_DIRECTORY]"
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
output=${1:-"$root/build/benchmark/qemu-plugins"}
mkdir -p "$output"

require_command cc
require_command pkg-config

plugin_include=${QEMU_PLUGIN_INCLUDE:-}
if [ -z "$plugin_include" ]; then
  for candidate in /usr/include /usr/local/include /opt/homebrew/include; do
    if [ -f "$candidate/qemu-plugin.h" ]; then
      plugin_include=$candidate
      break
    fi
  done
fi
[ -n "$plugin_include" ] && [ -f "$plugin_include/qemu-plugin.h" ] \
  || fail "qemu-plugin.h was not found; set QEMU_PLUGIN_INCLUDE"

case $(uname -s) in
  Linux)
    shared_flags='-shared -fPIC'
    suffix=so
    ;;
  Darwin)
    shared_flags='-dynamiclib -undefined dynamic_lookup'
    suffix=dylib
    ;;
  *)
    fail "QEMU plugin builds are unsupported on $(uname -s)"
    ;;
esac

for plugin in profile cache hotblocks; do
  temporary=$output/.$plugin.$suffix.$$
  trap 'rm -f "$temporary"' EXIT HUP INT TERM
  # shellcheck disable=SC2086
  cc -std=c11 -Wall -Wextra -Werror $shared_flags -fvisibility=hidden \
    $(pkg-config --cflags glib-2.0) -I"$plugin_include" \
    "$root/tools/qemu/$plugin.c" -o "$temporary" \
    $(pkg-config --libs glib-2.0)
  mv -f "$temporary" "$output/$plugin.$suffix"
  trap - EXIT HUP INT TERM
done

printf 'built QEMU plugins in %s\n' "$output"
