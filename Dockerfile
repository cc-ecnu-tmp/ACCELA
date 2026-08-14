FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      binutils-riscv64-unknown-elf build-essential ca-certificates curl \
      gcc-riscv64-unknown-elf libglib2.0-dev openjdk-21-jdk-headless \
      pkg-config python3 qemu-system-riscv \
    && curl -fsSL \
      https://gitlab.com/qemu-project/qemu/-/raw/v10.0.11/include/qemu/qemu-plugin.h \
      -o /usr/local/include/qemu-plugin.h \
    && ln -s /usr/bin/riscv64-unknown-elf-gcc /usr/local/bin/riscv64-elf-gcc \
    && ln -s /usr/bin/riscv64-unknown-elf-readelf /usr/local/bin/riscv64-elf-readelf \
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
WORKDIR /workspace
COPY . .
RUN --mount=type=cache,target=/root/.gradle \
    sh ./gradlew classes --no-daemon --dependency-verification=off \
    && sh scripts/build-qemu-plugins.sh

ENTRYPOINT ["python3", "scripts/evaluate_candidates.py"]
CMD ["--max-runs", "6", "--jobs", "4", "--skip-build", "--output-root", "/results"]
