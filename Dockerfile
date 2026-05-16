FROM ghcr.io/osgeo/gdal:ubuntu-small-3.9.2

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pip python3-venv \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --break-system-packages -r requirements.txt

COPY src/ /app/src/
COPY workflows/ /app/workflows/

ENV WORKFLOW_PATH=/app/workflows/qwen-image-edit-sr.api.json \
    JOBS_DB_PATH=/data/jobs.db \
    TMP_DIR=/data/tmp

RUN mkdir -p /data/tmp

EXPOSE 8080

CMD ["uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
