FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# rasterio wheels bundle GDAL, so we don't need the OSGeo image.
# Just need a healthcheck-capable curl and ca-certificates.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/
RUN pip install -r requirements.txt

COPY src/ /app/src/
COPY workflows/ /app/workflows/

ENV WORKFLOW_PATH=/app/workflows/qwen-image-edit-sr.api.json \
    JOBS_DB_PATH=/data/jobs.db \
    TMP_DIR=/data/tmp

RUN mkdir -p /data/tmp

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

CMD ["uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
