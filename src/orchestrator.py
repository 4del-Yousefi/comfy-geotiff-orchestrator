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


def _read_tile_rgb_and_mask(src, tile: Tile) -> tuple[np.ndarray, np.ndarray]:
    """Read a tile and return (RGB uint8 HxWx3, valid_mask bool HxW).

    valid_mask[y, x] == True  → pixel has real data, send to SR
    valid_mask[y, x] == False → pixel is nodata/transparent; never let SR
                                hallucinate something for it, never let it
                                contaminate the neighboring tile blend.

    Detection priority:
      1. Alpha channel (band 4+) — pixel valid iff alpha > 0
      2. GeoTIFF nodata value — pixel valid iff ANY band differs from nodata
      3. All-zero fallback — pixel valid iff ANY band is non-zero
    """
    win = Window(tile.x0, tile.y0, tile.size, tile.size)
    arr = src.read(window=win)  # (count, H, W)

    if arr.size == 0:
        raise ValueError(f"empty tile read at ({tile.x0}, {tile.y0})")

    # ---- validity mask ----
    if arr.shape[0] >= 4:
        # 4-band imagery — assume last band is alpha
        valid_mask = arr[-1] > 0
    elif src.nodata is not None:
        # explicit nodata value on the source
        nodata = src.nodata
        bands = arr[: min(3, arr.shape[0])]
        valid_mask = np.any(bands != nodata, axis=0)
    else:
        # heuristic: a pixel is invalid if ALL bands are exactly 0
        bands = arr[: min(3, arr.shape[0])]
        valid_mask = np.any(bands != 0, axis=0)

    # ---- RGB build ----
    if src.count == 1:
        band = arr[0]
        if band.dtype != np.uint8:
            band = _normalize_to_uint8(band)
        rgb = np.stack([band, band, band], axis=-1)
    else:
        bands = arr[: min(3, src.count)]
        if bands.shape[0] < 3:
            pad = [bands[-1]] * (3 - bands.shape[0])
            bands = np.stack(list(bands) + pad, axis=0)
        if bands.dtype != np.uint8:
            bands = np.stack([_normalize_to_uint8(b) for b in bands], axis=0)
        rgb = np.transpose(bands, (1, 2, 0))

    return np.ascontiguousarray(rgb), valid_mask


def _read_tile_rgb(src, tile: Tile) -> np.ndarray:
    """Back-compat shim — returns only RGB."""
    rgb, _ = _read_tile_rgb_and_mask(src, tile)
    return rgb


def _upscale_mask_nearest(mask: np.ndarray, factor: int,
                          target_h: int, target_w: int) -> np.ndarray:
    """Nearest-neighbor upscale a bool mask to (target_h, target_w).
    Used to align the per-input-pixel validity mask with the SR output tile."""
    up = np.repeat(np.repeat(mask, factor, axis=0), factor, axis=1)
    h, w = up.shape
    if h < target_h or w < target_w:
        up = np.pad(up, ((0, max(0, target_h - h)), (0, max(0, target_w - w))),
                    mode="edge")
    return up[:target_h, :target_w]


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


def _color_match_quadratic(
    sr_rgb: np.ndarray,
    input_rgb: np.ndarray,
    valid_mask: np.ndarray | None = None,
    min_samples: int = 256,
) -> np.ndarray:
    """Per-channel quadratic color match: SR output → input's color statistics.

    For each RGB channel, downscale the SR to input resolution, then solve
    the least-squares fit  y = a*x^2 + b*x + c  where x is the downscaled SR
    pixel value and y is the corresponding input pixel value. Apply the
    fitted polynomial to the full-resolution SR output. The high-frequency
    detail from SR is preserved; only the per-channel response curve shifts
    to match the input.

    If `valid_mask` is given (HxW bool over input resolution), only those
    pixels contribute to the fit — important when the input has nodata
    regions that would otherwise skew the fit toward zero.

    Returns uint8 (H, W, 3). Falls back to passing the SR output through
    unchanged if there are fewer than `min_samples` valid pixels in a channel.
    """
    h, w = input_rgb.shape[:2]

    # Downscale SR back to input resolution for paired statistics.
    if sr_rgb.shape[:2] != (h, w):
        sr_lr = np.asarray(
            Image.fromarray(sr_rgb).resize((w, h), Image.LANCZOS)
        )
    else:
        sr_lr = sr_rgb

    sr_lr_f = sr_lr.astype(np.float32)
    inp_f = input_rgb.astype(np.float32)
    out_f = sr_rgb.astype(np.float32).copy()

    mask_flat = valid_mask.ravel() if valid_mask is not None else None

    for c in range(3):
        x = sr_lr_f[:, :, c].ravel()
        y = inp_f[:, :, c].ravel()
        if mask_flat is not None:
            x = x[mask_flat]
            y = y[mask_flat]
        if x.size < min_samples:
            continue  # not enough samples to fit reliably; leave channel as-is

        # Least-squares fit y = a*x^2 + b*x + k
        A = np.stack([x * x, x, np.ones_like(x)], axis=1)
        try:
            coefs, *_ = np.linalg.lstsq(A, y, rcond=None)
        except np.linalg.LinAlgError:
            continue
        a, b, k = coefs

        sr_c = out_f[:, :, c]
        out_f[:, :, c] = a * sr_c * sr_c + b * sr_c + k

    return np.clip(out_f, 0.0, 255.0).astype(np.uint8)


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
                        tile_rgb_in, valid_mask = _read_tile_rgb_and_mask(src, tile)

                        # Skip entirely-nodata tiles — never send to GPU (saves money)
                        # and never let them contaminate neighboring tile blends.
                        if not valid_mask.any():
                            log.info(
                                f"tile {tile.idx} (x={tile.x0},y={tile.y0}) "
                                f"is 100% nodata; skipping"
                            )
                            async with progress_lock:
                                done_counter["n"] += 1
                                await db.update(
                                    job_id,
                                    tiles_done=done_counter["n"],
                                    progress_msg=(
                                        f"processed {done_counter['n']}/{len(tiles)} tiles"
                                    ),
                                )
                            return

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

                        # Per-channel quadratic color match: bring the SR output's
                        # color statistics back in line with the input. Reduces
                        # visible seams between tiles and model color drift.
                        if settings.color_match:
                            tile_rgb_out = _color_match_quadratic(
                                sr_rgb=tile_rgb_out,
                                input_rgb=tile_rgb_in,
                                valid_mask=valid_mask,
                                min_samples=settings.color_match_min_samples,
                            )

                        # If the input had any nodata pixels, upscale the validity
                        # mask to the SR output resolution and zero those pixels in
                        # both the tile and the blend weight. That way:
                        #   - nodata regions don't get GPU-hallucinated content
                        #   - nodata pixels in tile A don't dilute valid pixels in
                        #     overlapping tile B
                        oh, ow = tile_rgb_out.shape[:2]
                        if not valid_mask.all():
                            upscaled_valid = _upscale_mask_nearest(
                                valid_mask, upscale, oh, ow
                            )
                            # Zero nodata pixels in the output tile
                            tile_rgb_out = (
                                tile_rgb_out.astype(np.float32)
                                * upscaled_valid[..., np.newaxis]
                            ).astype(np.uint8)
                            # Zero the blend weight for nodata pixels
                            blend_mask = mask[:oh, :ow] * upscaled_valid.astype(np.float32)
                        else:
                            blend_mask = mask[:oh, :ow]

                        await canvas.blit(
                            tile_rgb=tile_rgb_out,
                            x=tile.x0 * upscale,
                            y=tile.y0 * upscale,
                            mask=blend_mask,
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
