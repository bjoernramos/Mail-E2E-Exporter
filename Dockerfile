# Mail E2E Exporter
# Minimal, production-ready image
FROM python:3.12-slim

# Build args for version metadata (can be overridden at build time)
ARG VERSION="0.2.1"
ARG BUILD_DATE="2025-11-07"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UVICORN_WORKERS=1 \
    APP_VERSION=${VERSION} \
    BUILD_DATE=${BUILD_DATE}

# OCI labels for GitHub/Docker Hub
LABEL org.opencontainers.image.title="Mail E2E Exporter" \
      org.opencontainers.image.description="Prometheus exporter that verifies email end-to-end delivery via SMTP/IMAP and exposes metrics." \
      org.opencontainers.image.url="https://github.com/bjoernramos/Mail-E2E-Exporter" \
      org.opencontainers.image.source="https://github.com/bjoernramos/Mail-E2E-Exporter" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="CC-BY-NC-4.0"

WORKDIR /app

# System deps (IMAP/SSL, timezone, locales minimal)
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    tzdata ca-certificates \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip>=25.3

COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy application
COPY app /app

EXPOSE 9782

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9782"]
