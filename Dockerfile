FROM ubuntu:24.04

RUN apt-get update && apt-get install -y \
    qemu-user-static \
    qemu-utils \
    parted \
    e2fsprogs \
    wget \
    xz-utils \
    dosfstools \
    kpartx \
    udev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY scripts/ /build/scripts/
COPY config/ /build/config/
COPY test-scripts/ /build/test-scripts/

ENTRYPOINT ["/build/scripts/build-image.sh"]
