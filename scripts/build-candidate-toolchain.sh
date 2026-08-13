#!/usr/bin/env sh

set -eu

fail() {
  printf '%s\n' "$*" >&2
  exit 2
}

[ "$#" -eq 1 ] \
  || fail "usage: scripts/build-candidate-toolchain.sh DOCKER_CLI_BINARY"
docker_cli_source=$1
[ -f "$docker_cli_source" ] && [ ! -L "$docker_cli_source" ] \
  || fail "Docker CLI source must be a regular non-symlink file"
[ -z "${DOCKER_HOST+x}" ] && [ -z "${DOCKER_CONTEXT+x}" ] \
  || fail "candidate toolchain build rejects ambient Docker endpoint overrides"

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
snapshot=$root/docs/optimization/data/toolchain-snapshot.json
benchmark_python=$root/.venv/bin/python
[ -x "$benchmark_python" ] \
  || fail "candidate toolchain build requires the repository .venv"
command -v docker >/dev/null 2>&1 || fail "required Docker CLI is unavailable"

if ! contract=$("$benchmark_python" -I -m tools.benchmark.candidate_toolchain \
    emit-build-contract --root "$root" --snapshot "$snapshot"); then
  fail "cannot load the candidate toolchain build contract"
fi
[ "$(printf '%s\n' "$contract" | awk 'END { print NR }')" -eq 9 ] \
  || fail "candidate toolchain build contract emitted malformed metadata"
base_image_tag=$(printf '%s\n' "$contract" | sed -n '1p')
base_image_id=$(printf '%s\n' "$contract" | sed -n '2p')
rootfs_layer=$(printf '%s\n' "$contract" | sed -n '3p')
dockerfile_path=$(printf '%s\n' "$contract" | sed -n '4p')
dockerfile_sha256=$(printf '%s\n' "$contract" | sed -n '5p')
docker_cli_sha256=$(printf '%s\n' "$contract" | sed -n '6p')
docker_cli_version_output=$(printf '%s\n' "$contract" | sed -n '7p')
image_tag=$(printf '%s\n' "$contract" | sed -n '8p')
image_id=$(printf '%s\n' "$contract" | sed -n '9p')
dockerfile=$root/$dockerfile_path

observed_cli_sha256=$(sha256sum "$docker_cli_source" | awk '{ print $1 }')
[ "$observed_cli_sha256" = "$docker_cli_sha256" ] \
  || fail "Docker CLI source SHA-256 differs from the frozen contract"
observed_cli_version=$("$docker_cli_source" --version)
[ "$observed_cli_version" = "$docker_cli_version_output" ] \
  || fail "Docker CLI source version differs from the frozen contract"
observed_dockerfile_sha256=$(sha256sum "$dockerfile" | awk '{ print $1 }')
[ "$observed_dockerfile_sha256" = "$dockerfile_sha256" ] \
  || fail "candidate toolchain Dockerfile SHA-256 differs from the frozen contract"

docker version --format '{{.Server.Version}}' >/dev/null \
  || fail "Docker daemon is unreachable"
observed_base_id=$(docker image inspect --format '{{.Id}}' "$base_image_tag") \
  || fail "candidate toolchain base image is unavailable"
[ "$observed_base_id" = "$base_image_id" ] \
  || fail "candidate toolchain base image ID differs from the frozen contract"

context=$(mktemp -d)
inspect_file=$context/image-inspect.json
cleanup() {
  rm -rf -- "$context"
}
trap cleanup EXIT HUP INT TERM

verify_image() {
  "$benchmark_python" -I -m tools.benchmark.candidate_toolchain \
    verify-image-inspect --root "$root" --snapshot "$snapshot" \
    --inspect "$inspect_file"

  observed_image_id=$(docker image inspect --format '{{.Id}}' "$image_tag")
  [ "$observed_image_id" = "$image_id" ] \
    || fail "candidate toolchain image ID differs after verification"
  observed_image_cli_sha256=$(
    docker run --rm --network none --read-only --cap-drop ALL \
      --security-opt no-new-privileges "$image_id" \
      sha256sum /usr/local/bin/docker | awk '{ print $1 }'
  )
  [ "$observed_image_cli_sha256" = "$docker_cli_sha256" ] \
    || fail "candidate toolchain image Docker CLI SHA-256 differs"
  observed_image_cli_version=$(
    docker run --rm --network none --read-only --cap-drop ALL \
      --security-opt no-new-privileges "$image_id" docker --version
  )
  [ "$observed_image_cli_version" = "$docker_cli_version_output" ] \
    || fail "candidate toolchain image Docker CLI version differs"
}

if docker image inspect "$image_tag" >"$inspect_file" 2>/dev/null; then
  verify_image
  printf 'verified existing candidate toolchain image %s (%s)\n' \
    "$image_tag" "$image_id"
  exit 0
fi

cp -- "$docker_cli_source" "$context/docker"
chmod 0755 "$context/docker"
LC_ALL=C TZ=UTC0 touch -t 197001010000.00 "$context/docker"

docker build --pull=false --no-cache \
  --build-arg SOURCE_DATE_EPOCH=0 \
  --build-arg "ACCELA_ROOTFS_LAYER_SHA256=${rootfs_layer#sha256:}" \
  --build-arg "ACCELA_DOCKER_CLI_SHA256=$docker_cli_sha256" \
  --file "$dockerfile" --tag "$image_tag" "$context"
docker image inspect "$image_tag" >"$inspect_file"
verify_image

printf 'built and verified candidate toolchain image %s (%s)\n' \
  "$image_tag" "$image_id"
