"""Tile coordinate math for square tiles with overlap and no edge padding.

Requirements (per spec):
- Every tile is exactly tile_size × tile_size — including the rightmost / bottom
  edge tiles, which shift back instead of being padded.
- Tiles overlap by `overlap_ratio` of tile_size in both axes.
- Edge tiles may overlap their neighbour more than the standard overlap to
  guarantee full coverage with no padding.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Tile:
    idx: int          # global tile index, row-major
    col: int          # column index
    row: int          # row index
    x0: int           # pixel x origin in input image
    y0: int           # pixel y origin in input image
    size: int         # tile_size (same on x and y)


def compute_tiles(width: int, height: int, tile_size: int, overlap_ratio: float) -> list[Tile]:
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid image dims {width}x{height}")
    if tile_size <= 0:
        raise ValueError("tile_size must be > 0")
    if not (0.0 <= overlap_ratio < 1.0):
        raise ValueError("overlap_ratio must be in [0, 1)")
    if tile_size > width or tile_size > height:
        raise ValueError(f"tile_size {tile_size} larger than image {width}x{height}")

    overlap_px = int(round(tile_size * overlap_ratio))
    stride = tile_size - overlap_px
    if stride <= 0:
        raise ValueError("overlap_ratio leaves zero stride")

    def axis_origins(dim: int) -> list[int]:
        origins: list[int] = []
        x = 0
        # Add tile origins as long as the next stride still leaves a full tile
        while x + tile_size < dim:
            origins.append(x)
            x += stride
        # Final tile: align flush to the right edge (no padding)
        last = dim - tile_size
        if not origins or origins[-1] != last:
            origins.append(last)
        return origins

    xs = axis_origins(width)
    ys = axis_origins(height)

    tiles: list[Tile] = []
    for r, y0 in enumerate(ys):
        for c, x0 in enumerate(xs):
            tiles.append(Tile(idx=len(tiles), col=c, row=r, x0=x0, y0=y0, size=tile_size))
    return tiles
