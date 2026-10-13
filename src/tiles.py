#!/usr/bin/env python3
"""Web-Mercator tile maths and a polite tile fetcher, shared by the DEM and
imagery steps.

Both sources are XYZ tile pyramids, so the slippy-map conversion, the on-disk
cache and the retry policy belong in one place. The cache is what makes the
682-tile imagery pass restartable: a mirror hiccup or a Ctrl-C costs only the
tiles still missing, not the whole budget.
"""
from __future__ import annotations

import math
import time
import urllib.request
from pathlib import Path

R_EARTH = 6378137.0
ORIGIN = 2 * math.pi * R_EARTH / 2  # 20037508.34, half the Mercator span


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    """Tile indices containing a point. y grows southward, as in the URL scheme."""
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def tile_bounds(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    """(lon0, lat0, lon1, lat1) of a tile: west, south, east, north."""
    n = 2 ** z
    lon0 = x / n * 360.0 - 180.0
    lon1 = (x + 1) / n * 360.0 - 180.0
    lat1 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat0 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return lon0, lat0, lon1, lat1


def tile_range(bbox: tuple[float, float, float, float], z: int):
    """Tile x/y ranges covering a (lat0, lon0, lat1, lon1) box, inclusive."""
    lat0, lon0, lat1, lon1 = bbox
    x0, y0 = lonlat_to_tile(lon0, lat1, z)   # north-west corner
    x1, y1 = lonlat_to_tile(lon1, lat0, z)   # south-east corner
    return range(x0, x1 + 1), range(y0, y1 + 1)


def ground_res(lat: float, z: int) -> float:
    """Metres per pixel for 256 px tiles at this latitude."""
    return 156543.03392804097 * math.cos(math.radians(lat)) / (2 ** z)


def pad_bbox(bbox, metres: float):
    """Grow a (lat0, lon0, lat1, lon1) box by a ground distance on all sides."""
    lat0, lon0, lat1, lon1 = bbox
    mid = (lat0 + lat1) / 2
    dlat = metres / 110540.0
    dlon = metres / (111320.0 * math.cos(math.radians(mid)))
    return lat0 - dlat, lon0 - dlon, lat1 + dlat, lon1 + dlon


def fetch(url: str, cache: Path, *, delay: float = 0.0, tries: int = 4,
          ua: str = "route-studio/0.1 (cycling route report)") -> bytes:
    """GET with an on-disk cache. Cached hits skip both the request and the delay."""
    if cache.exists() and cache.stat().st_size > 0:
        return cache.read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    # These hosts are public internet; the WSL proxy env var points at a Clash
    # instance that does not need to be in the path for them, but leaving it
    # alone is harmless -- unlike the intranet gateway, these resolve either way.
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            if not data:
                raise RuntimeError("empty body")
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(data)
            if delay:
                time.sleep(delay)
            return data
        except Exception as e:                      # noqa: BLE001 - report and retry
            last = e
            if attempt == tries - 1:
                break
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{url} failed after {tries} tries: {last}")
