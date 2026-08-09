FROM ubuntu@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
      clang-18=1:18.1.3-1ubuntu1 \
      gcc-13-riscv64-linux-gnu=13.3.0-6ubuntu2~24.04.1cross1 \
    && rm -rf /var/lib/apt/lists/*

ENV LC_ALL=C.UTF-8

ENTRYPOINT ["/usr/bin/env"]
