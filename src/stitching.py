"""Output canvas + feathered blending of overlapping tiles.

Strategy: accumulate (tile × weight) and weight separately, then divide.
Weight is a 2-D linear ramp from 0 at the edge to 1 in the inner region.
This produces smooth seams without visible overlap discontinuities.
"""

import numpy as np


def _feather_1d(size: int, ramp_px: int) -> np.ndarray:
    """1D weight: 0 at outermost pixel, linearly to 1 at `ramp_px` in, then 1, then ramp back."""
    w = np.ones(size, dtype=np.float32)
    if ramp_px <= 0:
        return w
    ramp_px = min(ramp_px, size // 2)
    # linspace(0,1,ramp_px+2)[1:-1] gives `ramp_px` values strictly in (0,1)
    ramp = np.linspace(0.0, 1.0, ramp_px + 2, dtype=np.float32)[1:-1]
    w[:ramp_px] = ramp
    w[-ramp_px:] = ramp[::-1]
    return w


def feather_mask_2d(size: int, ramp_px: int) -> np.ndarray:
    """Return (size, size) float32 mask with feathered edges."""
    w = _feather_1d(size, ramp_px)
    return np.outer(w, w)


class Canvas:
    """Pre-allocated output canvas that accumulates weighted tile contributions."""

    def __init__(self, height: int, width: int, channels: int = 3):
        self.height = height
        self.width = width
        self.channels = channels
        self.accum = np.zeros((height, width, channels), dtype=np.float32)
        self.weights = np.zeros((height, width), dtype=np.float32)

    def blit(self, tile_rgb: np.ndarray, x: int, y: int, mask: np.ndarray) -> None:
        """Add tile_rgb at (x, y) weighted by mask. Skips on shape mismatch (logged upstream)."""
        th, tw = tile_rgb.shape[:2]
        if tile_rgb.ndim == 2:
            tile_rgb = np.stack([tile_rgb] * self.channels, axis=-1)
        if tile_rgb.shape[2] != self.channels:
            tile_rgb = tile_rgb[:, :, : self.channels]
        if tile_rgb.dtype != np.float32:
            tile_rgb = tile_rgb.astype(np.float32)

        y1 = min(y + th, self.height)
        x1 = min(x + tw, self.width)
        th2 = y1 - y
        tw2 = x1 - x
        if th2 <= 0 or tw2 <= 0:
            return

        m = mask[:th2, :tw2]
        self.accum[y:y1, x:x1] += tile_rgb[:th2, :tw2] * m[..., None]
        self.weights[y:y1, x:x1] += m

    def finalize(self) -> np.ndarray:
        """Return uint8 RGB image (H, W, C) with weighted average applied."""
        w = np.maximum(self.weights, 1e-6)
        out = self.accum / w[..., None]
        return np.clip(out, 0.0, 255.0).astype(np.uint8)
