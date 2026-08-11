FROM accela/candidate-toolchain:2026-r2

ARG ACCELA_ROOTFS_LAYER_SHA256
ARG ACCELA_DOCKER_CLI_SHA256
ARG SOURCE_DATE_EPOCH=0

COPY --chmod=0755 docker /usr/local/bin/docker

ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

LABEL org.accela.toolchain.rootfs-layer-sha256="${ACCELA_ROOTFS_LAYER_SHA256}" \
      org.accela.toolchain.docker-cli-sha256="${ACCELA_DOCKER_CLI_SHA256}"
