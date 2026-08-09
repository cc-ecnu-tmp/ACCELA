#!/usr/bin/env sh

set -eu

fail() {
  printf '%s\n' "$*" >&2
  exit 2
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command is unavailable: $1"
}

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
[ "$#" -eq 1 ] || fail "usage: scripts/qemu-run.sh SOURCE.sy"
[ -f "$1" ] || fail "source is not a regular file: $1"
source_dir=$(CDPATH= cd -- "$(dirname -- "$1")" && pwd -P)
source_file=$source_dir/$(basename -- "$1")
name=$(basename -- "$source_file" .sy)
compiler=${QEMU_COMPILER:-accela}
work=$root/build/qemu-run/$compiler/$name
mkdir -p "$work"

if [ -n "${ACCELA_JAVA_HOME:-}" ]; then
  java=$ACCELA_JAVA_HOME/bin/java
elif [ -n "${JAVA_HOME:-}" ]; then
  java=$JAVA_HOME/bin/java
else
  require_command java
  java=$(command -v java)
fi
[ -x "$java" ] || fail "Java executable is unavailable: $java"

classes=$root/build/classes/java/main
if { [ "$compiler" = accela ] || [ "$compiler" = benchmark ]; } && [ ! -d "$classes" ]; then
  if [ -n "${ACCELA_JAVA_HOME:-}" ]; then
    JAVA_HOME=$ACCELA_JAVA_HOME "$root/gradlew" -p "$root" classes --no-daemon
  elif [ -n "${JAVA_HOME:-}" ]; then
    "$root/gradlew" -p "$root" classes --no-daemon
  else
    "$root/gradlew" -p "$root" classes --no-daemon
  fi
fi

assembly=$work/program.s
elf=$work/program.elf
actual=$work/program.out
remarks=$work/remarks.jsonl
case "$compiler" in
  accela)
    "$java" -cp "$classes" Compiler "$source_file" -S -o "$assembly" -O1
    program=$assembly
    ;;
  benchmark)
    profile=${ACCELA_PIPELINE_PROFILE:?ACCELA_PIPELINE_PROFILE is required for QEMU_COMPILER=benchmark}
    [ -f "$profile" ] || fail "pipeline profile is not a regular file: $profile"
    "$java" -cp "$classes" BenchmarkCompiler "$source_file" -o "$assembly" \
      --profile "$profile" --remarks "$remarks"
    program=$assembly
    ;;
  gcc)
    "$root/scripts/reference-compile.sh" gcc "$source_file" "$assembly"
    program=$assembly
    ;;
  clang)
    "$root/scripts/reference-compile.sh" clang "$source_file" "$assembly"
    program=$assembly
    ;;
  *)
    fail "unknown QEMU_COMPILER: $compiler"
    ;;
esac

riscv_gcc=${RISCV_GCC:-riscv64-elf-gcc}
require_command "$riscv_gcc"
"$riscv_gcc" \
  -march=rv64gc -mabi=lp64d -mcmodel=medany -O2 \
  -ffreestanding -fno-builtin -nostdlib -nostartfiles \
  -Wl,-T,"$root/tools/qemu/linker.ld" \
  "$root/tools/qemu/crt.S" "$root/tools/qemu/runtime.c" "$program" \
  -lgcc -o "$elf"

input=${source_file%.sy}.in
expected=${source_file%.sy}.out
[ -f "$expected" ] || fail "expected output is missing: $expected"
stdin=$work/program.in
if [ -f "$input" ]; then
  cp "$input" "$stdin"
else
  : > "$stdin"
fi

qemu=${QEMU_SYSTEM_RISCV64:-qemu-system-riscv64}
require_command "$qemu"
set -- "$qemu" -machine virt -accel tcg,thread=single -smp 1 -m 512M \
  -bios none -kernel "$elf" -display none -monitor none -serial stdio

profile_mode=${QEMU_PROFILE:-0}
profile_log=$work/profile.log
case "$profile_mode" in
  0)
    ;;
  1|instructions|hotblocks|cache|metrics)
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
      Darwin)
        shared_flags='-dynamiclib -undefined dynamic_lookup'
        plugin_suffix=dylib
        ;;
      Linux)
        shared_flags='-shared -fPIC'
        plugin_suffix=so
        ;;
      *)
        fail "QEMU plugin builds are unsupported on $(uname -s)"
        ;;
    esac
    plugin_dir=$work/plugins
    mkdir -p "$plugin_dir"
    build_plugin() {
      plugin_name=$1
      # shellcheck disable=SC2086
      cc -std=c11 -Wall -Wextra -Werror $shared_flags -fvisibility=hidden \
        $(pkg-config --cflags glib-2.0) -I"$plugin_include" \
        "$root/tools/qemu/$plugin_name.c" -o "$plugin_dir/$plugin_name.$plugin_suffix" \
        $(pkg-config --libs glib-2.0)
    }
    if [ "$profile_mode" = metrics ]; then
      build_plugin profile
      build_plugin cache
      set -- "$@" \
        -plugin "$plugin_dir/profile.$plugin_suffix" \
        -plugin "$plugin_dir/cache.$plugin_suffix"
    else
      plugin_name=$profile_mode
      [ "$plugin_name" = 1 ] && plugin_name=profile
      [ "$plugin_name" = instructions ] && plugin_name=profile
      build_plugin "$plugin_name"
      set -- "$@" -plugin "$plugin_dir/$plugin_name.$plugin_suffix"
    fi
    set -- "$@" -d plugin -D "$profile_log"
    ;;
  *)
    fail "unknown QEMU_PROFILE mode: $profile_mode"
    ;;
esac

if [ -n "${TIMEOUT_COMMAND:-}" ]; then
  timeout_command=$TIMEOUT_COMMAND
elif command -v timeout >/dev/null 2>&1; then
  timeout_command=timeout
elif command -v gtimeout >/dev/null 2>&1; then
  timeout_command=gtimeout
else
  fail "required command is unavailable: timeout (or GNU gtimeout)"
fi
"$timeout_command" "${QEMU_TIMEOUT:-120}" "$@" < "$stdin" > "$actual"
cmp "$expected" "$actual" || {
  diff -u "$expected" "$actual" >&2 || true
  fail "program output does not match expected output"
}
if [ "$profile_mode" != 0 ]; then
  [ -f "$profile_log" ] || fail "QEMU did not produce a plugin log"
  if grep -Eq '^(profile|cache|hotblocks)_error=' "$profile_log"; then
    grep -E '^(profile|cache|hotblocks)_error=' "$profile_log" >&2
    fail "QEMU plugin rejected malformed measurement regions"
  fi
  cat "$profile_log"
fi
printf 'PASS %s/%s\n' "$compiler" "$name"
