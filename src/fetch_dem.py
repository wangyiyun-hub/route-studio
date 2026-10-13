#!/usr/bin/env python3
"""Build an elevation raster over the route from AWS terrain-tiles -> data/dem.npz

Why tiles instead of the opentopodata point API the existing pipeline uses: that
API allows 100 locations per request at 1 req/s, so a 30 m grid over this route
is ~2685 requests, about 45 minutes. The same ground at z13 is 15 PNG tiles that
download in seconds, and it is the *same* SRTM data underneath.

Terrarium encoding: elevation = (R * 256 + G + B / 256) - 32768 metres.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tiles import fetch, ground_res, pad_bbox, tile_bounds, tile_range  # noqa: E402
import routes  # noqa: E402

URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    routes.add_route_arg(ap)
    args = ap.parse_args()
    rt = routes.resolve(args)
    rt.mkdirs()
    CACHE = rt.cache / "dem"
    ZOOM = int(rt["dem_zoom"])
    PAD_M = float(rt["pad_m"])

    track = json.loads((rt.data / "track.json").read_text(encoding="utf-8"))
    bbox = pad_bbox(tuple(track["bbox"]), PAD_M)
    lat0, lon0, lat1, lon1 = bbox
    xs, ys = tile_range(bbox, ZOOM)
    mid_lat = (lat0 + lat1) / 2

    print(f"z={ZOOM}  {len(xs)}x{len(ys)}={len(xs)*len(ys)} tiles  "
          f"{ground_res(mid_lat, ZOOM):.1f} m/px")

    # Stitch the tiles into one raster, then crop to the padded bbox. Working in
    # whole tiles first keeps the arithmetic simple and the seams exact.
    mosaic = np.zeros((len(ys) * 256, len(xs) * 256), dtype=np.float32)
    for jy, y in enumerate(ys):
        for jx, x in enumerate(xs):
            raw = fetch(URL.format(z=ZOOM, x=x, y=y), CACHE / f"{ZOOM}_{x}_{y}.png")
            rgb = np.asarray(Image.open(__import__("io").BytesIO(raw)).convert("RGB"),
                             dtype=np.float32)
            ele = rgb[:, :, 0] * 256.0 + rgb[:, :, 1] + rgb[:, :, 2] / 256.0 - 32768.0
            mosaic[jy * 256:(jy + 1) * 256, jx * 256:(jx + 1) * 256] = ele
        print(f"  row {jy + 1}/{len(ys)}")

    # Mosaic extent in lon/lat, from the corner tiles.
    w0, s0, _, n0 = tile_bounds(xs[0], ys[0], ZOOM)
    _, s1, e1, _ = tile_bounds(xs[-1], ys[-1], ZOOM)
    m_lon0, m_lon1, m_lat0, m_lat1 = w0, e1, s1, n0

    # Crop to the padded route box. Mercator y is non-linear in latitude, so the
    # row index must come from the Mercator projection, not from lerping degrees.
    import math

    def merc_y(lat: float) -> float:
        return math.log(math.tan(math.radians(lat)) + 1.0 / math.cos(math.radians(lat)))

    H, W = mosaic.shape
    my0, my1 = merc_y(m_lat1), merc_y(m_lat0)       # top, bottom of mosaic
    r0 = int((merc_y(lat1) - my0) / (my1 - my0) * H)
    r1 = int((merc_y(lat0) - my0) / (my1 - my0) * H)
    c0 = int((lon0 - m_lon0) / (m_lon1 - m_lon0) * W)
    c1 = int((lon1 - m_lon0) / (m_lon1 - m_lon0) * W)
    r0, r1 = max(0, r0), min(H, max(r1, r0 + 2))
    c0, c1 = max(0, c0), min(W, max(c1, c0 + 2))
    grid = mosaic[r0:r1, c0:c1]

    # Terrarium codes ocean as 0; this route has none, so a 0 inland is a void.
    bad = grid <= 0.5
    if bad.any():
        print(f"  {bad.sum()} void samples, filling from neighbours")
        grid = grid.copy()
        grid[bad] = np.nan
        # Two passes of neighbour averaging close the small holes seen in practice.
        for _ in range(2):
            if not np.isnan(grid).any():
                break
            pad = np.pad(grid, 1, mode="edge")
            stack = np.stack([pad[:-2, 1:-1], pad[2:, 1:-1], pad[1:-1, :-2], pad[1:-1, 2:]])
            with np.errstate(invalid="ignore"):
                fill = np.nanmean(stack, axis=0)
            grid = np.where(np.isnan(grid), fill, grid)
        grid = np.nan_to_num(grid, nan=float(np.nanmedian(grid)))

    out = rt.data / "dem.npz"
    np.savez_compressed(
        out, ele=grid.astype(np.float32),
        bbox=np.array([lat0, lon0, lat1, lon1]),
        meta=np.array([ZOOM, grid.shape[1], grid.shape[0], ground_res(mid_lat, ZOOM)]))
    print(f"\ndem.npz  {out.stat().st_size / 1024:.0f} KB   grid {grid.shape[1]}x{grid.shape[0]}")
    print(f"  elevation {grid.min():.1f} .. {grid.max():.1f} m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
