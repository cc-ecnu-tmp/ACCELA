#!/usr/bin/env sh

set -eu

fail() {
  printf '%s\n' "$*" >&2
  exit 2
}

[ "$#" -eq 3 ] || fail "usage: scripts/reference-compile.sh gcc|clang SOURCE.sy OUTPUT.s"
frontend=$1
source_file=$2
output_file=$3
case "$frontend" in
  gcc|clang) ;;
  *) fail "unknown reference frontend: $frontend" ;;
esac
[ -f "$source_file" ] || fail "source is not a regular file: $source_file"
[ -d "$(dirname -- "$output_file")" ] || fail "output parent directory does not exist"
[ -z "${ACCELA_REFERENCE_IMAGE+x}" ] \
  || fail "ACCELA_REFERENCE_IMAGE is unsupported; use the frozen toolchain snapshot"
[ -z "${ACCELA_REFERENCE_IMAGE_ID+x}" ] \
  || fail "ACCELA_REFERENCE_IMAGE_ID is unsupported; use the frozen toolchain snapshot"
[ -z "${ACCELA_TOOLCHAIN_SNAPSHOT+x}" ] \
  || fail "ACCELA_TOOLCHAIN_SNAPSHOT is unsupported; use the repository snapshot"
[ -z "${PYTHON+x}" ] \
  || fail "PYTHON is unsupported; the reference launcher is fixed to python3"

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
output_dir=$(CDPATH= cd -- "$(dirname -- "$output_file")" && pwd -P)
output_name=$(basename -- "$output_file")
translated_name=.accela-sysy-reference.cpp
argv_name=.accela-reference-argv.txt
[ "$output_name" != "$translated_name" ] && [ "$output_name" != "$argv_name" ] \
  || fail "output name is reserved by the reference adapter"
snapshot=$root/docs/optimization/data/toolchain-snapshot.json
[ -f "$snapshot" ] || fail "toolchain snapshot is not a regular file"
python=python3
command -v "$python" >/dev/null 2>&1 || fail "required Python executable is unavailable: $python"
if ! python_version=$("$python" -I -c '
import json
import platform
import re
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        snapshot = json.load(stream)
    expected = snapshot["proxy_execution"]["python"]
except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
    print("reference Python contract is invalid", file=sys.stderr)
    raise SystemExit(2)
if not isinstance(expected, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", expected) is None:
    print("reference Python contract has an invalid version", file=sys.stderr)
    raise SystemExit(2)
actual = platform.python_version()
if actual != expected:
    print(
        f"reference Python version mismatch: expected {expected}, got {actual}",
        file=sys.stderr,
    )
    raise SystemExit(2)
print(actual)
' "$snapshot"); then
  fail "cannot validate the isolated reference Python launcher"
fi
case "$python_version" in
  ''|*[!0-9.]*) fail "isolated reference Python emitted malformed version metadata" ;;
esac
printf 'ACCELA_REFERENCE_PYTHON python_mode=isolated version=%s\n' \
  "$python_version" >&2

translated_file=$output_dir/$translated_name
argv_file=$output_dir/$argv_name
docker_stderr_file=$output_dir/.accela-reference-docker.stderr
[ ! -e "$translated_file" ] && [ ! -e "$argv_file" ] \
  && [ ! -e "$docker_stderr_file" ] \
  || fail "reference adapter temporary output already exists"
cleanup() {
  rm -f -- "$translated_file" "$argv_file" "$docker_stderr_file"
}
trap cleanup EXIT HUP INT TERM

if ! snapshot_values=$("$python" -I "$root/tools/benchmark/reference_source.py" contract \
    --root "$root" --snapshot "$snapshot" --frontend "$frontend" \
    --artifact-name "$output_name" --argv-output "$argv_file"); then
  fail "cannot validate reference frontend contract from toolchain snapshot"
fi
metadata_count=$(printf '%s\n' "$snapshot_values" | awk 'END { print NR }')
[ "$metadata_count" -eq 5 ] \
  || fail "reference frontend contract emitted malformed metadata"
image=$(printf '%s\n' "$snapshot_values" | sed -n '1p')
expected_image_id=$(printf '%s\n' "$snapshot_values" | sed -n '2p')
adapter_sha256=$(printf '%s\n' "$snapshot_values" | sed -n '3p')
builtin_header_sha256=$(printf '%s\n' "$snapshot_values" | sed -n '4p')
compiler_argv_sha256=$(printf '%s\n' "$snapshot_values" | sed -n '5p')

docker_transport_unreachable() {
  LC_ALL=C grep -Eiq \
    'cannot connect to (the )?docker daemon|error during connect|is the docker daemon running|failed to connect to the docker api|^failed to initialize: protocol not available$|permission denied while trying to connect to the docker daemon|unable to resolve docker endpoint|dial (unix|tcp)|connect: connection refused|docker\.sock.*(no such file|connection refused)|context deadline exceeded|client\.timeout exceeded while awaiting headers|open .*docker.*(cannot find|does not exist|system cannot find)' \
    "$docker_stderr_file"
}

select_docker_candidate() {
  candidate_cli=$1
  candidate_label=$2
  : > "$docker_stderr_file"
  if candidate_version=$("$candidate_cli" version --format '{{.Server.Version}}' \
      2>"$docker_stderr_file"); then
    candidate_version=$(printf '%s' "$candidate_version" | tr -d '\r')
    case "$candidate_version" in
      ''|*[!0-9A-Za-z.+_-]*)
        fail "Docker readiness returned malformed server version for chosen_cli=$candidate_label"
        ;;
      [0-9]*) ;;
      *) fail "Docker readiness returned malformed server version for chosen_cli=$candidate_label" ;;
    esac
    docker_cli=$candidate_cli
    chosen_cli=$candidate_label
    docker_server_version=$candidate_version
    return 0
  fi
  if docker_transport_unreachable; then
    printf 'ACCELA_REFERENCE_DOCKER_CANDIDATE candidate_cli=%s readiness=transport_unreachable\n' \
      "$candidate_label" >&2
    return 1
  fi
  fail "Docker readiness failed with a non-transport error for chosen_cli=$candidate_label"
}

docker_cli=
chosen_cli=
docker_server_version=
if command -v docker >/dev/null 2>&1; then
  if select_docker_candidate docker native; then
    :
  fi
fi
if [ -z "$docker_cli" ] && command -v docker.exe >/dev/null 2>&1; then
  command -v wslpath >/dev/null 2>&1 \
    || fail "windows Docker CLI requires wslpath for bind mounts"
  if select_docker_candidate docker.exe windows; then
    :
  fi
fi
[ -n "$docker_cli" ] || fail "no reachable Docker daemon is available"

printf 'ACCELA_REFERENCE_DOCKER chosen_cli=%s server_version=%s readiness=reachable\n' \
  "$chosen_cli" "$docker_server_version" >&2
case "$chosen_cli" in
  native)
    container_mount_path() {
      printf '%s\n' "$1"
    }
    ;;
  windows)
    container_mount_path() {
      wslpath -w "$1"
    }
    ;;
  *) fail "internal Docker candidate state is invalid" ;;
esac

if ! actual_image_id=$("$docker_cli" image inspect --format '{{.Id}}' "$image" \
    2>"$docker_stderr_file"); then
  if LC_ALL=C grep -Eiq 'no such image|image .* not found' "$docker_stderr_file"; then
    inspect_reason=image_missing
  else
    inspect_reason=inspect_failed
  fi
  fail "reference image inspect failed for chosen_cli=$chosen_cli reason=$inspect_reason"
fi
actual_image_id=$(printf '%s' "$actual_image_id" | tr -d '\r')
actual_image_digest=${actual_image_id#sha256:}
if [ "$actual_image_digest" = "$actual_image_id" ] \
    || [ "${#actual_image_digest}" -ne 64 ]; then
  fail "reference image inspect returned malformed image ID for chosen_cli=$chosen_cli"
fi
case "$actual_image_digest" in
  *[!0-9a-f]*)
    fail "reference image inspect returned malformed image ID for chosen_cli=$chosen_cli"
    ;;
esac
[ "$actual_image_id" = "$expected_image_id" ] \
  || fail "reference image ID mismatch: expected $expected_image_id, got $actual_image_id"

output_mount=$(container_mount_path "$output_dir")
support_mount=$(container_mount_path "$root/tools/qemu")

"$python" -I "$root/tools/benchmark/reference_source.py" adapt \
  "$source_file" "$translated_file"
[ -f "$translated_file" ] || fail "reference adapter did not create translated source"
[ -f "$argv_file" ] || fail "reference contract did not create compiler argv"

run_container() {
  "$docker_cli" run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --tmpfs /tmp:rw,nosuid,nodev,size=64m \
    --user "$(id -u):$(id -g)" \
    --mount "type=bind,src=$output_mount,dst=/output" \
    --mount "type=bind,src=$support_mount,dst=/support,readonly" \
    "$expected_image_id" "$@"
}

set --
while IFS= read -r argument; do
  [ -n "$argument" ] || fail "reference compiler argv contains an empty argument"
  set -- "$@" "$argument"
done < "$argv_file"
[ "$#" -gt 0 ] || fail "reference compiler argv is empty"
printf 'ACCELA_REFERENCE_COMMAND frontend=%s argv_sha256=%s adapter_sha256=%s header_sha256=%s\n' \
  "$frontend" "$compiler_argv_sha256" "$adapter_sha256" "$builtin_header_sha256" >&2
run_container "$@"
