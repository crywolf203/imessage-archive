ARG IMESSAGE_EXPORTER_VERSION=4.3.0

FROM rust:1-bookworm@sha256:48b7f435b8fc6378ce43b44f15e3a47fd8919b2825f835c39bd4bd3da50af5b8 AS exporter-builder
ARG IMESSAGE_EXPORTER_VERSION
RUN cargo install imessage-exporter --version "${IMESSAGE_EXPORTER_VERSION}" --locked

FROM python:3.13-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530

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

COPY app/ /app/
COPY VERSION /app/VERSION
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && mkdir -p /data/backups /data/exports /data/pdfs /var/lib/lockdown /var/run

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
