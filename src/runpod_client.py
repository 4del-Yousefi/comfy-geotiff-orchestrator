"""Async RunPod client for ComfyUI serverless endpoint.

Submits a workflow + input image, polls until completion, returns the
largest output image (assumed to be the final SR result).
"""

import asyncio
import base64
import io
import logging
from typing import Any

import httpx
from PIL import Image

log = logging.getLogger(__name__)


TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}


class RunPodError(RuntimeError):
    pass


class RunPodClient:
    def __init__(
        self,
        endpoint_id: str,
        api_key: str,
        base_url: str,
        poll_interval_sec: float,
        tile_timeout_sec: int,
    ):
        self.endpoint_id = endpoint_id
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.poll_interval_sec = poll_interval_sec
        self.tile_timeout_sec = tile_timeout_sec
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=15.0, read=60.0),
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def aclose(self):
        await self._client.aclose()

    async def _post(self, path: str, json_body: dict) -> dict:
        url = f"{self.base_url}/{self.endpoint_id}{path}"
        r = await self._client.post(url, json=json_body)
        if r.status_code >= 400:
            raise RunPodError(f"POST {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    async def _get(self, path: str) -> dict:
        url = f"{self.base_url}/{self.endpoint_id}{path}"
        r = await self._client.get(url)
        if r.status_code >= 400:
            raise RunPodError(f"GET {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    async def submit_and_wait(
        self,
        workflow: dict,
        image_name: str,
        image_b64: str,
    ) -> bytes:
        """Submit one job, poll until terminal, return the largest output image bytes."""
        payload = {
            "input": {
                "workflow": workflow,
                "images": [{"name": image_name, "image": image_b64}],
            }
        }
        sub = await self._post("/run", payload)
        job_id = sub.get("id")
        if not job_id:
            raise RunPodError(f"submit returned no id: {sub}")

        deadline = asyncio.get_event_loop().time() + self.tile_timeout_sec
        while asyncio.get_event_loop().time() < deadline:
            try:
                status = await self._get(f"/status/{job_id}")
            except RunPodError as e:
                # transient: retry after the interval
                log.warning(f"status poll error for {job_id}: {e}")
                await asyncio.sleep(self.poll_interval_sec)
                continue

            st = status.get("status")
            if st in TERMINAL:
                if st != "COMPLETED":
                    raise RunPodError(
                        f"job {job_id} {st}: {status.get('error') or status.get('output')}"
                    )
                out = status.get("output") or {}
                images = out.get("images") or []
                if not images:
                    err = out.get("error")
                    if err:
                        raise RunPodError(f"job {job_id} returned error: {err}")
                    raise RunPodError(f"job {job_id} returned no images: {out}")
                return _pick_largest_image_bytes(images)
            await asyncio.sleep(self.poll_interval_sec)

        # Best-effort cancel
        try:
            await self._post(f"/cancel/{job_id}", {})
        except RunPodError:
            pass
        raise RunPodError(f"job {job_id} timed out after {self.tile_timeout_sec}s")


def _pick_largest_image_bytes(images: list[dict]) -> bytes:
    """Many ComfyUI workflows emit several outputs (intermediate + final).
    Take the largest by pixel count — typically the upscaled SR result."""
    best: bytes | None = None
    best_px = -1
    for item in images:
        data_b64 = item.get("data")
        if not data_b64:
            continue
        try:
            raw = base64.b64decode(data_b64)
            with Image.open(io.BytesIO(raw)) as im:
                px = im.width * im.height
            if px > best_px:
                best_px = px
                best = raw
        except Exception as e:
            log.warning(f"could not decode candidate image: {e}")
    if best is None:
        raise RunPodError("no decodable images in output")
    return best
