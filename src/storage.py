"""Cloudflare R2 client via S3-compatible API (boto3, run in a thread pool)."""

import asyncio
import logging
from pathlib import Path
from typing import Optional

import boto3
from botocore.config import Config

log = logging.getLogger(__name__)


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
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=15,
                read_timeout=120,
            ),
        )

    async def download(self, key: str, local_path: Path | str) -> None:
        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        log.info(f"downloading R2://{self.bucket}/{key} -> {local}")
        await asyncio.to_thread(self._s3.download_file, self.bucket, key, str(local))

    async def upload(self, local_path: Path | str, key: str, content_type: Optional[str] = None) -> None:
        local = Path(local_path)
        log.info(f"uploading {local} -> R2://{self.bucket}/{key}")
        extra = {"ContentType": content_type} if content_type else None
        await asyncio.to_thread(
            self._s3.upload_file, str(local), self.bucket, key, ExtraArgs=extra
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
