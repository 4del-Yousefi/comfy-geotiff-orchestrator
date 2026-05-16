"""Cloudflare R2 client via S3-compatible API (boto3, run in a thread pool).

Tuned for multi-GB GeoTIFF transfers:
- Multipart upload kicks in at 16 MB threshold, with 8-way parallelism
- Per-request socket timeouts long enough to survive slow chunks
- Retries with backoff handle transient R2 hiccups
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

log = logging.getLogger(__name__)


# Tuned for multi-GB files. Multipart kicks in at the threshold; parts of
# `multipart_chunksize` are uploaded/downloaded with `max_concurrency` workers.
_TRANSFER = TransferConfig(
    multipart_threshold=16 * 1024 * 1024,    # 16 MB
    multipart_chunksize=64 * 1024 * 1024,    # 64 MB chunks
    max_concurrency=8,
    use_threads=True,
)


class StorageClient:
    def __init__(
        self,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "auto",
    ):
        self.bucket = bucket
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 5, "mode": "adaptive"},
                connect_timeout=30,
                read_timeout=600,
                max_pool_connections=32,
            ),
        )

    async def download(self, key: str, local_path: Path | str) -> None:
        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        log.info(f"downloading R2://{self.bucket}/{key} -> {local}")
        await asyncio.to_thread(
            self._s3.download_file, self.bucket, key, str(local), Config=_TRANSFER,
        )

    async def upload(self, local_path: Path | str, key: str, content_type: Optional[str] = None) -> None:
        local = Path(local_path)
        size_mb = round(local.stat().st_size / (1024 * 1024), 1)
        log.info(f"uploading {local} ({size_mb} MB) -> R2://{self.bucket}/{key}")
        extra = {"ContentType": content_type} if content_type else None
        await asyncio.to_thread(
            self._s3.upload_file, str(local), self.bucket, key,
            ExtraArgs=extra, Config=_TRANSFER,
        )

    def presigned_get_url(self, key: str, expires_sec: int = 3600) -> str:
        return self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_sec,
        )

    def presigned_put_url(self, key: str, expires_sec: int = 3600, content_type: str = "image/tiff") -> str:
        return self._s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=expires_sec,
        )

    async def head(self, key: str) -> dict:
        return await asyncio.to_thread(self._s3.head_object, Bucket=self.bucket, Key=key)
