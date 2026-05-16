"""Disk-backed canvas + feathered blending of overlapping tiles.

The accumulator and weights live as numpy memmaps on disk (in `scratch_dir`).
This bounds RAM to roughly the OS page cache the kernel chooses for hot
regions — small fraction of the full canvas size — so the orchestrator can
stitch 25k×25k or larger outputs on an 8 GB box without OOM.

After all tiles are blitted, `finalize_to()` streams the canvas block-by-block,
divides by weights, casts to uint8, and writes a proper GeoTIFF (BIGTIFF,
DEFLATE-compressed, tiled, with overview pyramids).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

import numpy as np
import rasterio
from rasterio.windows import Window

log = logging.getLogger(__name__)


def _feather_1d(size: int, ramp_px: int) -> np.ndarray:
    w = np.ones(size, dtype=np.float32)
    if ramp_px <= 0:
        return w
    ramp_px = min(ramp_px, size // 2)
    ramp = np.linspace(0.0, 1.0, ramp_px + 2, dtype=np.float32)[1:-1]
    w[:ramp_px] = ramp
    w[-ramp_px:] = ramp[::-1]
    return w


def feather_mask_2d(size: int, ramp_px: int) -> np.ndarray:
    w = _feather_1d(size, ramp_px)
    return np.outer(w, w)


class DiskCanvas:
    """Memmap-backed accumulator + weights. Safe for concurrent blits via lock."""

    def __init__(self, height: int, width: int, channels: int, scratch_dir: Path | str):
        self.height = height
        self.width = width
        self.channels = channels
        self.scratch_dir = Path(scratch_dir)
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        self.accum_path = self.scratch_dir / "accum.raw"
        self.weights_path = self.scratch_dir / "weights.raw"

        # Pre-size the files (sparse on filesystems that support it)
        accum_bytes = height * width * channels * 4  # float32
        weights_bytes = height * width * 4
        with open(self.accum_path, "wb") as f:
            f.truncate(accum_bytes)
        with open(self.weights_path, "wb") as f:
            f.truncate(weights_bytes)

        # mode='r+' = read+write, file already exists; shape determines layout
        self.accum = np.memmap(
            self.accum_path, dtype=np.float32, mode="r+",
            shape=(height, width, channels),
        )
        self.weights = np.memmap(
            self.weights_path, dtype=np.float32, mode="r+",
            shape=(height, width),
        )
        # One coarse lock; the in-memory accumulate is fast, the bottleneck
        # is RunPod inference (parallel) not the blit step.
        self._lock = asyncio.Lock()

        log.info(
            f"DiskCanvas {width}x{height}x{channels} float32 = "
            f"{(accum_bytes + weights_bytes) / (1024**3):.2f} GiB on disk"
        )

    async def blit(self, tile_rgb: np.ndarray, x: int, y: int, mask: np.ndarray) -> None:
        async with self._lock:
            await asyncio.to_thread(self._blit_sync, tile_rgb, x, y, mask)

    def _blit_sync(self, tile_rgb: np.ndarray, x: int, y: int, mask: np.ndarray) -> None:
        th, tw = tile_rgb.shape[:2]
        if tile_rgb.ndim == 2:
            tile_rgb = np.stack([tile_rgb] * self.channels, axis=-1)
        if tile_rgb.shape[2] != self.channels:
            tile_rgb = tile_rgb[:, :, : self.channels]

        y1 = min(y + th, self.height)
        x1 = min(x + tw, self.width)
        th2, tw2 = y1 - y, x1 - x
        if th2 <= 0 or tw2 <= 0:
            return

        tile_f32 = tile_rgb[:th2, :tw2].astype(np.float32, copy=False)
        m = mask[:th2, :tw2]
        # In-place adds against the memmap = direct disk-backed write
        self.accum[y:y1, x:x1] += tile_f32 * m[..., np.newaxis]
        self.weights[y:y1, x:x1] += m

    async def finalize_to_geotiff(
        self,
        output_path: Path | str,
        crs,
        transform,
        block_size: int = 1024,
        build_overviews: bool = True,
    ) -> None:
        """Stream the canvas to a proper GeoTIFF, normalising along the way."""
        await asyncio.to_thread(
            self._finalize_sync, output_path, crs, transform, block_size, build_overviews
        )

    def _finalize_sync(
        self,
        output_path: Path | str,
        crs,
        transform,
        block_size: int,
        build_overviews: bool,
    ) -> None:
        self.accum.flush()
        self.weights.flush()

        with rasterio.open(
            output_path, "w",
            driver="GTiff",
            width=self.width, height=self.height,
            count=self.channels, dtype="uint8",
            crs=crs, transform=transform,
            compress="DEFLATE", predictor=2,
            tiled=True, blockxsize=512, blockysize=512,
            photometric="rgb",
            BIGTIFF="YES",
            num_threads="ALL_CPUS",
        ) as out:
            for row in range(0, self.height, block_size):
                for col in range(0, self.width, block_size):
                    bh = min(block_size, self.height - row)
                    bw = min(block_size, self.width - col)

                    a_block = self.accum[row:row + bh, col:col + bw]      # (h, w, c) float32
                    w_block = self.weights[row:row + bh, col:col + bw]    # (h, w)    float32
                    w_safe = np.maximum(w_block, 1e-6)
                    norm = a_block / w_safe[..., np.newaxis]
                    norm = np.clip(norm, 0.0, 255.0).astype(np.uint8)
                    # rasterio expects (bands, h, w)
                    out.write(np.transpose(norm, (2, 0, 1)),
                              window=Window(col, row, bw, bh))

            if build_overviews:
                try:
                    out.build_overviews([2, 4, 8, 16], rasterio.enums.Resampling.average)
                    out.update_tags(ns="rio_overview", resampling="average")
                except Exception as e:
                    log.warning(f"overview build failed (non-fatal): {e}")

    def close(self) -> None:
        # Release memmaps before deleting the backing files
        try:
            del self.accum
        except Exception:
            pass
        try:
            del self.weights
        except Exception:
            pass
        for p in (self.accum_path, self.weights_path):
            try:
                os.remove(p)
            except OSError:
                pass
