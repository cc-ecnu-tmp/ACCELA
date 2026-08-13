#!/bin/sh
set -eu

fail() {
  printf '%s\n' "candidate-workspace: $*" >&2
  exit 2
}

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$root"

[ "$(uname -s)" = Linux ] || fail "bootstrap requires Linux"
[ "$(uname -m)" = x86_64 ] || fail "bootstrap requires x86_64"
[ ! -L .venv ] || fail ".venv must not be a symbolic link"
[ ! -e .venv ] || [ -d .venv ] || fail ".venv must be a directory"
[ -d .tmp/wheelhouse ] || fail ".tmp/wheelhouse is missing"
[ -d .tmp/gradle-home ] || fail ".tmp/gradle-home is missing"

PYTHONPATH= PYTHONHOME= python3 -I -c \
  'import platform; assert platform.python_implementation() == "CPython" and platform.python_version() == "3.14.6" and platform.system() == "Linux" and platform.machine() == "x86_64"' \
  || fail "base Python identity differs"
PYTHONPATH= PYTHONHOME= python3 -I -m venv --clear .venv

PIP_CONFIG_FILE=/dev/null PIP_NO_INDEX=1 PYTHONPATH= PYTHONHOME= \
  .venv/bin/python -I -m pip --disable-pip-version-check install \
  --no-index --only-binary=:all: --require-hashes \
  --find-links .tmp/wheelhouse \
  --requirement tools/benchmark/requirements-linux-x86_64-py314.lock

PIP_CONFIG_FILE=/dev/null PIP_NO_INDEX=1 PYTHONPATH= PYTHONHOME= \
  .venv/bin/python -I -m pip --disable-pip-version-check install \
  --no-index --no-deps --no-build-isolation --editable .

PYTHONPATH= PYTHONHOME= .venv/bin/python -I -m tools.benchmark.candidate_workspace \
  --root "$root"

GRADLE_USER_HOME="$root/.tmp/gradle-home" \
  sh ./gradlew --offline --no-daemon --dependency-verification=strict \
  classes testClasses

sh scripts/build-qemu-plugins.sh
