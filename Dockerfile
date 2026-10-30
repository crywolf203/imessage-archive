ARG IMESSAGE_EXPORTER_VERSION=4.3.0
ARG NETMUXD_REV=3771657a20f3880c287551ea716762f1d52dded5

FROM rust:1-bookworm AS wifi-builder
ARG NETMUXD_REV
RUN apt-get update && apt-get install -y --no-install-recommends cmake pkg-config \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY patches/netmuxd-bsd-network-address.patch /build/netmuxd-bsd-network-address.patch
RUN git clone https://github.com/jkcoxson/netmuxd.git netmuxd \
    && cd netmuxd && git checkout --detach "${NETMUXD_REV}" \
    && git apply --check /build/netmuxd-bsd-network-address.patch \
    && git apply /build/netmuxd-bsd-network-address.patch \
    && cargo test --release --locked --no-default-features --lib devices::wire_address_tests \
    && cargo build --release --locked --no-default-features

FROM debian:bookworm-slim AS wifi-discovery
ARG NETMUXD_REV
LABEL org.opencontainers.image.title="iMessage Archive Wi-Fi discovery" \
      org.opencontainers.image.licenses="LGPL-2.1-only" \
      io.github.crywolf203.imessage-archive.netmuxd.revision="${NETMUXD_REV}" \
      io.github.crywolf203.imessage-archive.netmuxd.patch="bsd-network-address-v1"
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*
COPY --from=wifi-builder /build/netmuxd/target/release/netmuxd /usr/local/bin/netmuxd
COPY --from=wifi-builder /build/netmuxd/LICENSE /usr/share/doc/netmuxd/LICENSE
COPY wifi-entrypoint.sh /wifi-entrypoint.sh
RUN chmod +x /wifi-entrypoint.sh
ENTRYPOINT ["/usr/bin/tini", "--", "/wifi-entrypoint.sh"]

FROM rust:1-bookworm AS exporter-builder
ARG IMESSAGE_EXPORTER_VERSION
RUN cargo install imessage-exporter --version "${IMESSAGE_EXPORTER_VERSION}" --locked

FROM python:3.15-slim-bookworm@sha256:4e03260adeeebafccd084f3584e6b84823aba5651cfb53ef8a078ed441ddf9cd AS wifi-python-builder
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY app/wifi-requirements.txt /build/wifi-requirements.txt
RUN python -m venv /opt/wifi \
    && /opt/wifi/bin/pip install --no-cache-dir -r /build/wifi-requirements.txt

FROM python:3.15-slim-bookworm@sha256:4e03260adeeebafccd084f3584e6b84823aba5651cfb53ef8a078ed441ddf9cd

ARG APP_VERSION=4.0.1
ARG IMESSAGE_EXPORTER_VERSION
LABEL org.opencontainers.image.title="iMessage Archive" \
      org.opencontainers.image.description="Unraid-friendly iPhone backup, indexed message viewer, and export appliance" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      io.github.crywolf203.imessage-archive.exporter.version="${IMESSAGE_EXPORTER_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        ffmpeg \
        imagemagick \
        libheif1 \
        libimobiledevice-utils \
        procps \
        tini \
        usbmuxd \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/convert /usr/local/bin/magick

COPY --from=exporter-builder /usr/local/cargo/bin/imessage-exporter /usr/local/bin/imessage-exporter

WORKDIR /app
COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY app/wifi-requirements.txt /app/wifi-requirements.txt
COPY --from=wifi-python-builder /opt/wifi /opt/wifi

COPY app/ /app/
COPY VERSION /app/VERSION
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && mkdir -p /data/backups /data/exports /data/pdfs /var/lib/lockdown /var/run

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
