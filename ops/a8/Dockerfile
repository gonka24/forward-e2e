# ==============================================================================
# Dockerfile: A8 Acceptance Runner in Docker-in-Docker (DinD)
#
# Base: Debian 12 bookworm-slim, linux/amd64 manifest pinned by digest
# Architecture: linux/amd64 (default for reproducible cosmwasm & Gonka builds)
#
# Embedded Toolchain:
#   - Docker CE (dockerd, docker CLI, compose plugin, buildx plugin)
#   - Python 3.11+
#   - OpenJDK 21 Headless (for Kotlin / Gradle Testermint)
#   - Rust 1.81.0 (with wasm32-unknown-unknown, clippy, rustfmt)
#   - cosmwasm-check 2.2.2 (strictly validated by preflight guards)
#   - Node.js 22 LTS (for CosmWasm synthetic-host ExternalQuerier ABI probe)
#   - Go 1.26.8 (runner Go tooling; the boundary probe uses Gonka's pinned builder)
#   - Git, jq, curl, make, iptables, procps
# ==============================================================================

FROM debian:bookworm-20260918-slim@sha256:f3034a6ec3c1205360777c4aae76234998866ad18806ae62b63a3f84ccad782b

# Prevent interactive prompts during installation
ENV DEBIAN_FRONTEND=noninteractive

# Freeze Debian's package universe at the same date as the pinned base image.
# The snapshot's Release signatures are still verified by APT; only its expiry
# is disabled because a reproducible snapshot is intentionally historical.
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
       'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260918T000000Z bookworm main' \
       'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/20260918T000000Z bookworm-security main' \
       > /etc/apt/sources.list

# Install base dependencies, ca-certificates, curl, git, iptables
RUN apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
    apt-transport-https \
    bash \
    build-essential \
    ca-certificates \
    curl \
    gnupg \
    iptables \
    jq \
    make \
    pkg-config \
    procps \
    python3 \
    python3-pip \
    python3-venv \
    tar \
    unzip \
    wget \
    xz-utils \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install exact Temurin packages as immutable artifacts, not from a moving APT
# repository. Their hashes were cross-checked against Adoptium's signed
# Bookworm amd64 Packages index (2026-09-29); Debian dependencies come only
# from the pinned snapshot above.
ENV TEMURIN_VERSION=21.0.12.1.0+1-0
RUN set -eux; \
    curl -fsSL 'https://packages.adoptium.net/artifactory/deb/pool/main/t/temurin-21/temurin-21-jdk_21.0.12.1.0+1-0_amd64.deb' -o /tmp/temurin-21-jdk.deb; \
    echo '1c4fd4033deea86e69457366bc08a1a2d0056afad2745248917eafa84bd4e11d  /tmp/temurin-21-jdk.deb' | sha256sum -c -; \
    curl -fsSL 'https://packages.adoptium.net/artifactory/deb/pool/main/t/temurin-21/temurin-21-jre_21.0.12.1.0+1-0_amd64.deb' -o /tmp/temurin-21-jre.deb; \
    echo '6c36c5cae76391a558926db5b8df2114dc41ed2ba889edf962370083944588b3  /tmp/temurin-21-jre.deb' | sha256sum -c -; \
    curl -fsSL 'https://packages.adoptium.net/artifactory/deb/pool/main/a/adoptium-ca-certificates/adoptium-ca-certificates_1.0.6-1_all.deb' -o /tmp/adoptium-ca-certificates.deb; \
    echo 'bd146cae76f600fedcaf0ca1776cf099b48bcd01fdaa42479c3b1f3dc0d5f2f0  /tmp/adoptium-ca-certificates.deb' | sha256sum -c -; \
    apt-get -o Acquire::Check-Valid-Until=false update; \
    apt-get install -y --no-install-recommends /tmp/temurin-21-jdk.deb /tmp/temurin-21-jre.deb /tmp/adoptium-ca-certificates.deb; \
    rm -f /tmp/temurin-21-jdk.deb /tmp/temurin-21-jre.deb /tmp/adoptium-ca-certificates.deb; \
    rm -rf /var/lib/apt/lists/*

# Set Java Home
ENV JAVA_HOME=/usr/lib/jvm/temurin-21-jdk-amd64
ENV PATH="${JAVA_HOME}/bin:${PATH}"

# Install Docker packages by exact URL and SHA-256 from the official signed
# Bookworm amd64 Packages index (2026-09-25). No moving Docker APT repository
# or unreviewed signing-key download participates in this build.
RUN set -eux; \
    curl -fsSL 'https://download.docker.com/linux/debian/dists/bookworm/pool/stable/amd64/docker-ce_29.8.1-1~debian.12~bookworm_amd64.deb' -o /tmp/docker-ce.deb; \
    echo '3e38704938b78358563164cf419d00598e32a2119efc7cea80bdec2249efaf63  /tmp/docker-ce.deb' | sha256sum -c -; \
    curl -fsSL 'https://download.docker.com/linux/debian/dists/bookworm/pool/stable/amd64/docker-ce-cli_29.8.1-1~debian.12~bookworm_amd64.deb' -o /tmp/docker-ce-cli.deb; \
    echo 'ff812c5853c52ef120ec73132320805d179a376e42785085e2053ce7f2479860  /tmp/docker-ce-cli.deb' | sha256sum -c -; \
    curl -fsSL 'https://download.docker.com/linux/debian/dists/bookworm/pool/stable/amd64/containerd.io_2.3.6-1~debian.12~bookworm_amd64.deb' -o /tmp/containerd.io.deb; \
    echo '3dacccefaa69b13307813a3a2e1bdc65c8c69d5cf9da3a9e28b53e2998d195d7  /tmp/containerd.io.deb' | sha256sum -c -; \
    curl -fsSL 'https://download.docker.com/linux/debian/dists/bookworm/pool/stable/amd64/docker-buildx-plugin_0.37.1-1~debian.12~bookworm_amd64.deb' -o /tmp/docker-buildx-plugin.deb; \
    echo 'cf36da1a4c31287fef2c0cb5413aa660f56d4ec183b4d24dc47cd83ebef0a2c6  /tmp/docker-buildx-plugin.deb' | sha256sum -c -; \
    curl -fsSL 'https://download.docker.com/linux/debian/dists/bookworm/pool/stable/amd64/docker-compose-plugin_5.5.1-1~debian.12~bookworm_amd64.deb' -o /tmp/docker-compose-plugin.deb; \
    echo '0455e6a54bcc07b1bcb5d92d9e6e77dedc485433c87c1b75572e6d837a96e459  /tmp/docker-compose-plugin.deb' | sha256sum -c -; \
    apt-get -o Acquire::Check-Valid-Until=false update; \
    apt-get install -y --no-install-recommends /tmp/docker-ce.deb /tmp/docker-ce-cli.deb /tmp/containerd.io.deb /tmp/docker-buildx-plugin.deb /tmp/docker-compose-plugin.deb; \
    rm -f /tmp/docker-ce.deb /tmp/docker-ce-cli.deb /tmp/containerd.io.deb /tmp/docker-buildx-plugin.deb /tmp/docker-compose-plugin.deb; \
    rm -rf /var/lib/apt/lists/*

RUN test "$(dpkg-query -W temurin-21-jdk | cut -f2)" = "${TEMURIN_VERSION}" \
    && test "$(dpkg-query -W temurin-21-jre | cut -f2)" = "${TEMURIN_VERSION}" \
    && test "$(dpkg-query -W adoptium-ca-certificates | cut -f2)" = "1.0.6-1" \
    && docker --version \
    && dockerd --version \
    && docker compose version \
    && docker buildx version

# Install a checksum-verified supported Node.js LTS archive, not a live setup script.
ENV NODE_VERSION=22.23.3
ENV NODE_SHA256=df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de
RUN curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" -o node.tar.xz \
    && echo "${NODE_SHA256}  node.tar.xz" | sha256sum -c - \
    && mkdir -p /opt/node \
    && tar -C /opt/node --strip-components=1 -xJf node.tar.xz \
    && rm node.tar.xz
ENV PATH="/opt/node/bin:${PATH}"

# Install a supported Go release; verify the official linux/amd64 archive hash.
ENV GO_VERSION=1.26.8
ENV GO_SHA256=d0f743b33e8d8945e6b1f432edd15785c70507121d6e2a723b21285eddf8b57b
RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" -o go.tar.gz \
    && echo "${GO_SHA256}  go.tar.gz" | sha256sum -c - \
    && tar -C /usr/local -xzf go.tar.gz \
    && rm go.tar.gz
ENV PATH="/usr/local/go/bin:${PATH}"

# Install Rust 1.81.0 with wasm32 target
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/root/.cargo \
    PATH="/root/.cargo/bin:${PATH}"
ENV RUSTUP_VERSION=1.28.2
ENV RUSTUP_SHA256=20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c
RUN curl -fsSL "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/x86_64-unknown-linux-gnu/rustup-init" -o rustup-init \
    && echo "${RUSTUP_SHA256}  rustup-init" | sha256sum -c - \
    && chmod +x rustup-init \
    && ./rustup-init -y --no-modify-path \
    --default-toolchain 1.81.0 \
    --profile minimal \
    --component clippy \
    --component rustfmt \
    --target wasm32-unknown-unknown \
    && rm rustup-init \
    && chmod -R a+w /usr/local/rustup /root/.cargo

# Install cosmwasm-check 2.2.2 (strictly pinned)
RUN cargo install cosmwasm-check --version 2.2.2 --locked

# Prepare directories
RUN mkdir -p /workspace /out /input/marketplace /input/gonka /input/bundles /var/lib/docker

# Copy runner codebase
WORKDIR /app
COPY . /app

# Check all binding fixtures after Docker applies its build-context exclusions.
# Reuse the offline packaging test so builds and checkouts share one inventory.
RUN python3 -B -m unittest ops.a8.tests.test_e2e_host_wrappers.RunnerBuildContextTests

# Configure Git safe.directory for specific input and workspace paths (never global *)
RUN git config --system --add safe.directory /input/marketplace \
    && git config --system --add safe.directory /input/gonka \
    && git config --system --add safe.directory '/workspace/*' \
    && git config --system --add safe.directory /app

# Install the single E2E entrypoint
COPY ops/a8/e2e-entrypoint.sh /usr/local/bin/e2e-entrypoint
RUN chmod +x /usr/local/bin/e2e-entrypoint

# Default runtime environment variables
ENV A8_WORKSPACE_DIR=/workspace \
    A8_OUTPUT_DIR=/out \
    DOCKER_HOST=unix:///var/run/docker.sock \
    GONKA_SOURCE=/input/gonka \
    MARKETPLACE_SOURCE=/input/marketplace \
    PYTHONPATH=/app

ENTRYPOINT ["/usr/local/bin/e2e-entrypoint"]
CMD ["list"]
