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
[ -f "$source_file" ] || fail "source is not a regular file: $source_file"
[ -d "$(dirname -- "$output_file")" ] || fail "output parent directory does not exist"
if command -v docker >/dev/null 2>&1; then
  docker_cli=docker
  container_mount_path() {
    printf '%s\n' "$1"
  }
elif command -v docker.exe >/dev/null 2>&1 && command -v wslpath >/dev/null 2>&1; then
  docker_cli=docker.exe
  container_mount_path() {
    wslpath -w "$1"
  }
else
  fail "required Docker CLI is unavailable"
fi

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
source_dir=$(CDPATH= cd -- "$(dirname -- "$source_file")" && pwd -P)
output_dir=$(CDPATH= cd -- "$(dirname -- "$output_file")" && pwd -P)
source_name=$(basename -- "$source_file")
output_name=$(basename -- "$output_file")
snapshot_image=
snapshot_image_id=
if [ -z "${ACCELA_REFERENCE_IMAGE:-}" ] || [ -z "${ACCELA_REFERENCE_IMAGE_ID:-}" ]; then
  snapshot=${ACCELA_TOOLCHAIN_SNAPSHOT:-$root/docs/optimization/data/toolchain-snapshot.json}
  [ -f "$snapshot" ] || fail "toolchain snapshot is not a regular file"
  python=${PYTHON:-python3}
  command -v "$python" >/dev/null 2>&1 || fail "required Python executable is unavailable: $python"
  if ! snapshot_values=$("$python" -c \
    'import json, sys; data = json.load(open(sys.argv[1], encoding="utf-8")); ref = data["reference_frontends"]; print(ref["local_image_tag"] + "|" + ref["local_image_id"])' \
    "$snapshot" 2>/dev/null); then
    fail "cannot read reference image identity from toolchain snapshot"
  fi
  case "$snapshot_values" in
    *'|'*) ;;
    *) fail "toolchain snapshot has an invalid reference image identity" ;;
  esac
  snapshot_image=${snapshot_values%%|*}
  snapshot_image_id=${snapshot_values#*|}
fi
image=${ACCELA_REFERENCE_IMAGE:-$snapshot_image}
expected_image_id=${ACCELA_REFERENCE_IMAGE_ID:-$snapshot_image_id}
[ -n "$image" ] || fail "reference image tag is empty"
image_digest=${expected_image_id#sha256:}
if [ "$image_digest" = "$expected_image_id" ] || [ "${#image_digest}" -ne 64 ]; then
  fail "ACCELA_REFERENCE_IMAGE_ID must be a sha256 image ID"
fi
case "$image_digest" in
  *[!0-9a-f]*) fail "ACCELA_REFERENCE_IMAGE_ID must be a sha256 image ID" ;;
esac
if ! actual_image_id=$("$docker_cli" image inspect --format '{{.Id}}' "$image" 2>/dev/null); then
  fail "reference image is unavailable: $image"
fi
actual_image_id=$(printf '%s' "$actual_image_id" | tr -d '\r')
[ "$actual_image_id" = "$expected_image_id" ] \
  || fail "reference image ID mismatch: expected $expected_image_id, got $actual_image_id"
source_mount=$(container_mount_path "$source_dir")
output_mount=$(container_mount_path "$output_dir")
support_mount=$(container_mount_path "$root/tools/qemu")

run_container() {
  "$docker_cli" run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --tmpfs /tmp:rw,nosuid,nodev,size=64m \
    --user "$(id -u):$(id -g)" \
    --mount "type=bind,src=$source_mount,dst=/input,readonly" \
    --mount "type=bind,src=$output_mount,dst=/output" \
    --mount "type=bind,src=$support_mount,dst=/support,readonly" \
    "$expected_image_id" "$@"
}

case "$frontend" in
  gcc)
    run_container /usr/bin/riscv64-linux-gnu-gcc-13 \
      -march=rv64gc -mabi=lp64d -mcmodel=medany -O2 \
      -fwrapv -fno-fast-math -ffp-contract=off -ffreestanding -fno-builtin \
      -x c -include /support/sysy-builtins.h -S \
      "/input/$source_name" -o "/output/$output_name"
    ;;
  clang)
    run_container /usr/bin/clang-18 --target=riscv64-unknown-elf \
      -march=rv64gc -mabi=lp64d -mcmodel=medany -O3 \
      -fwrapv -fno-fast-math -ffp-contract=off -ffreestanding -fno-builtin \
      -fno-addrsig -x c -include /support/sysy-builtins.h -S \
      "/input/$source_name" -o "/output/$output_name"
    ;;
  *)
    fail "unknown reference frontend: $frontend"
    ;;
esac
