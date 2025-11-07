# Mail E2E Exporter
# Minimal, production-ready image
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UVICORN_WORKERS=1

WORKDIR /app

# System deps (IMAP/SSL, timezone, locales minimal)
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy application
COPY app /app

EXPOSE 9782

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9782"]
