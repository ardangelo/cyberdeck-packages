# Build with the linux/amd64 base digest and toolchain entries from source-lock.json.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG TOOLCHAIN_URL
ARG TOOLCHAIN_SHA256
ARG TOOLCHAIN_PREFIX
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl wget git make build-essential binutils patch diffutils \
       bzip2 gzip xz-utils zstd tar cpio unzip rsync file bc gawk \
       bison flex libssl-dev libncurses-dev python3 perl \
       debhelper dpkg-dev fakeroot kmod \
    && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    test -n "$TOOLCHAIN_URL"; test -n "$TOOLCHAIN_SHA256"; test -n "$TOOLCHAIN_PREFIX"; \
    curl --fail --location --proto '=https' --tlsv1.2 "$TOOLCHAIN_URL" -o /tmp/xuantie.tar.gz; \
    printf '%s  %s\n' "$TOOLCHAIN_SHA256" /tmp/xuantie.tar.gz | sha256sum --check --strict; \
    mkdir -p /opt/toolchain; \
    tar -xzf /tmp/xuantie.tar.gz -C /opt/toolchain; \
    test -x "/opt/toolchain/Xuantie-900-gcc-linux-6.6.0-glibc-x86_64-V3.0.2/bin/$TOOLCHAIN_PREFIX-gcc"; \
    rm /tmp/xuantie.tar.gz

WORKDIR /build
