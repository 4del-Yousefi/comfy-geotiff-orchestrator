"""Job pipeline: download → tile → fan-out to RunPod → stitch → upload GeoTIFF.

Key constraints honored:
- Output GeoTIFF preserves CRS and updates the affine transform for the
  upscale factor (pixel size shrinks by 1/upscale_factor).
- Tiles are read with windowed I/O, so peak memory is dominated by the
  output canvas, not the input.
- Bounded async concurrency to RunPod (semaphore).
"""

import asyncio
import base64
import io
import json
import logging
import os
import time
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.windows import Window

from .config import Settings
from .db import JobStore
from .runpod_client import RunPodClient, RunPodError
from .stitching import DiskCanvas, feather_mask_2d
from .storage import StorageClient
from .tiling import Tile, compute_tiles

log = logging.getLogger(__name__)


def _load_workflow(path: Path) -> dict:
    with path.open("rb") as f:
        return json.load(f)


def _encode_tile_png(tile_arr: np.ndarray) -> str:
    """tile_arr: (H, W, 3) uint8 -> base64 PNG string."""
    buf = io.BytesIO()
    Image.fromarray(tile_arr, mode="RGB").save(buf, format="PNG", optimize=False)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _read_tile_rgb(src, tile: Tile) -> np.ndarray:
    """Read a tile from the source GeoTIFF and return it as HxWx3 uint8 RGB."""
    win = Window(tile.x0, tile.y0, tile.size, tile.size)
    arr = src.read(window=win)  # (count, H, W)

    if arr.size == 0:
        raise ValueError(f"empty tile read at ({tile.x0}, {tile.y0})")

    if src.count == 1:
        band = arr[0]
        if band.dtype != np.uint8:
            band = _normalize_to_uint8(band)
        rgb = np.stack([band, band, band], axis=-1)
    else:
        # Use first 3 bands; common GeoTIFFs are RGB or RGBN
        bands = arr[: min(3, src.count)]
        if bands.shape[0] < 3:
            # Pad missing bands by repeating last band
            pad = [bands[-1]] * (3 - bands.shape[0])
            bands = np.stack(list(bands) + pad, axis=0)
        if bands.dtype != np.uint8:
            bands = np.stack([_normalize_to_uint8(b) for b in bands], axis=0)
        rgb = np.transpose(bands, (1, 2, 0))  # (H, W, 3)
    return np.ascontiguousarray(rgb)


def _normalize_to_uint8(band: np.ndarray) -> np.ndarray:
    """Stretch a non-uint8 band to 0..255 using its min/max."""
    b = band.astype(np.float32)
    lo, hi = float(np.nanmin(b)), float(np.nanmax(b))
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return np.zeros_like(b, dtype=np.uint8)
    out = (b - lo) * (255.0 / (hi - lo))
    return np.clip(out, 0, 255).astype(np.uint8)


def _decode_png_to_rgb(png_bytes: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(png_bytes)) as im:
        im = im.convert("RGB")
        return np.array(im)


async def run_job(
    job_id: str,
    settings: Settings,
    db: JobStore,
    storage: StorageClient,
    runpod: RunPodClient,
) -> None:
    """Top-level pipeline. Updates DB at each stage; never raises out."""
    t0 = time.time()
    local_in: Path | None = None
    local_out: Path | None = None
    canvas: DiskCanvas | None = None
    job_scratch: Path | None = None
    try:
        job = await db.get(job_id)
        if job is None:
            log.error(f"job {job_id} vanished before run")
            return

        tile_size = int(job["tile_size"])
        overlap_ratio = float(job["overlap_ratio"])
        upscale = int(job["upscale_factor"])

        # ---- 1. Download input ----
        await db.update(job_id, status="DOWNLOADING", progress_msg="fetching input")
        local_in = settings.tmp_dir / f"{job_id}_in.tif"
        local_in.parent.mkdir(parents=True, exist_ok=True)
        await storage.download(job["input_key"], local_in)

        # ---- 2. Plan tiles ----
        await db.update(job_id, status="TILING", progress_msg="reading geotiff")
        workflow = _load_workflow(settings.workflow_path)

        # NOTE: don't wrap the whole pipeline in `rasterio.Env(...)` — its state
        # is thread-local, and our async fan-out + the disk-backed canvas's
        # finalize step run in worker threads that don't inherit the parent
        # env, producing "No GDAL environment exists" errors mid-job.
        # rasterio.open() establishes its own env per-call, which is enough.
        if True:
            with rasterio.open(local_in) as src:
                tiles = compute_tiles(src.width, src.height, tile_size, overlap_ratio)
                if len(tiles) > settings.max_tile_count:
                    raise ValueError(
                        f"job would produce {len(tiles)} tiles > max {settings.max_tile_count}"
                    )

                await db.update(
                    job_id,
                    status="PROCESSING",
                    tiles_total=len(tiles),
                    tiles_done=0,
                    progress_msg=f"{len(tiles)} tiles",
                )

                out_w = src.width * upscale
                out_h = src.height * upscale

                # Per-job scratch dir for the disk-backed canvas
                job_scratch = settings.tmp_dir / f"{job_id}_canvas"
                canvas = DiskCanvas(
                    height=out_h, width=out_w, channels=3,
                    scratch_dir=job_scratch,
                )

                out_tile_size = tile_size * upscale
                # Feathered ramp covers half the overlap region on each side
                ramp_px = max(int(round(out_tile_size * overlap_ratio * 0.5)), 1)
                mask = feather_mask_2d(out_tile_size, ramp_px)

                # ---- 3. Fan out to RunPod ----
                semaphore = asyncio.Semaphore(settings.runpod_max_concurrent)
                done_counter = {"n": 0}
                progress_lock = asyncio.Lock()

                async def process_one(tile: Tile) -> None:
                    async with semaphore:
                        tile_rgb_in = _read_tile_rgb(src, tile)
                        b64 = _encode_tile_png(tile_rgb_in)
                        try:
                            png_bytes = await runpod.submit_and_wait(
                                workflow=workflow,
                                image_name=settings.input_image_name,
                                image_b64=b64,
                            )
                        except RunPodError as e:
                            raise RunPodError(f"tile {tile.idx} (x={tile.x0},y={tile.y0}): {e}")
                        tile_rgb_out = _decode_png_to_rgb(png_bytes)

                        # Sanity check shape (handler may return non-upscaled if workflow mis-set)
                        if tile_rgb_out.shape[0] != out_tile_size or tile_rgb_out.shape[1] != out_tile_size:
                            log.warning(
                                f"tile {tile.idx} returned {tile_rgb_out.shape[:2]} "
                                f"expected ({out_tile_size},{out_tile_size}); resizing"
                            )
                            tile_rgb_out = np.array(
                                Image.fromarray(tile_rgb_out).resize(
                                    (out_tile_size, out_tile_size), Image.LANCZOS
                                )
                            )

                        await canvas.blit(
                            tile_rgb=tile_rgb_out,
                            x=tile.x0 * upscale,
                            y=tile.y0 * upscale,
                            mask=mask,
                        )
                        async with progress_lock:
                            done_counter["n"] += 1
                            await db.update(
                                job_id,
                                tiles_done=done_counter["n"],
                                progress_msg=f"processed {done_counter['n']}/{len(tiles)} tiles",
                            )

                await asyncio.gather(*[process_one(t) for t in tiles])

                # ---- 4. Stream-write the disk-backed canvas to the output GeoTIFF ----
                await db.update(job_id, status="WRITING", progress_msg="streaming geotiff to disk")

                # New transform: pixel size shrinks by upscale_factor.
                new_transform = src.transform * src.transform.scale(
                    1.0 / upscale, 1.0 / upscale
                )

                local_out = settings.tmp_dir / f"{job_id}_out.tif"
                await canvas.finalize_to_geotiff(
                    output_path=local_out,
                    crs=src.crs,
                    transform=new_transform,
                )

        # ---- 5. Upload to R2 ----
        await db.update(job_id, status="UPLOADING", progress_msg="uploading output to R2")
        output_key = f"outputs/{job_id}.tif"
        await storage.upload(local_out, output_key, content_type="image/tiff")

        await db.update(
            job_id,
            status="COMPLETED",
            output_key=output_key,
            progress_msg=f"done in {round(time.time() - t0, 1)}s",
        )
        log.info(f"job {job_id} COMPLETED in {round(time.time() - t0, 1)}s")

    except Exception as e:
        log.exception(f"job {job_id} FAILED")
        try:
            await db.update(job_id, status="FAILED", error=f"{type(e).__name__}: {e}")
        except Exception:
            pass
    finally:
        # Close + delete the disk-backed scratch canvas
        if canvas is not None:
            try:
                canvas.close()
            except Exception:
                log.exception("canvas.close() failed")
        if job_scratch is not None:
            try:
                # In case .close() left anything behind
                if job_scratch.exists() and not any(job_scratch.iterdir()):
                    job_scratch.rmdir()
            except OSError:
                pass
        for f in (local_in, local_out):
            if f is not None:
                try:
                    os.remove(f)
                except OSError:
                    pass
