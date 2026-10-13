#!/usr/bin/env python3
"""Fetch satellite imagery over the route and stitch one texture -> data/sat.jpg

Default source is ESRI World Imagery because it serves WGS-84, the same datum as
the GPX. 高德 is offered as an alternative for CN-licensing reasons, but its
GCJ-02 grid sits ~490 m off WGS-84 here, which at street level is wider than the
road itself -- the alignment the road ribbon depends on. Pass --source amap and
the shift becomes your problem to correct downstream.

Tiles are cached, so an interrupted run resumes for the cost of what is missing.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tiles import fetch, ground_res, pad_bbox, tile_bounds, tile_range  # noqa: E402
import routes  # noqa: E402

SOURCES = {
    # name: (url template, datum, attribution)
    "esri": ("https://services.arcgisonline.com/ArcGIS/rest/services/"
             "World_Imagery/MapServer/tile/{z}/{y}/{x}",
             "WGS-84", "Esri World Imagery"),
    "amap": ("https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}",
             "GCJ-02", "高德地图"),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    routes.add_route_arg(ap)
    ap.add_argument("--source", choices=sorted(SOURCES))
    ap.add_argument("--zoom", type=int)
    ap.add_argument("--max-px", type=int)
    ap.add_argument("--quality", type=int, default=86)
    ap.add_argument("--delay", type=float, default=0.12,
                    help="seconds between uncached requests")
    args = ap.parse_args()
    rt = routes.resolve(args)
    rt.mkdirs()
    # CLI overrides route.toml, which overrides the built-in default.
    args.source = args.source or rt["sat_source"]
    args.zoom = args.zoom or int(rt["sat_zoom"])
    args.max_px = args.max_px or int(rt["sat_max_px"])
    # Must match fetch_dem.py or texture and terrain cover different ground.
    PAD_M = float(rt["pad_m"])

    tpl, datum, attrib = SOURCES[args.source]
    track = json.loads((rt.data / "track.json").read_text(encoding="utf-8"))
    bbox = pad_bbox(tuple(track["bbox"]), PAD_M)
    lat0, lon0, lat1, lon1 = bbox
    xs, ys = tile_range(bbox, args.zoom)
    total = len(xs) * len(ys)
    mid_lat = (lat0 + lat1) / 2
    print(f"{args.source} ({datum})  z={args.zoom}  {len(xs)}x{len(ys)}={total} tiles  "
          f"{ground_res(mid_lat, args.zoom):.2f} m/px")

    cache = rt.cache / f"sat-{args.source}"
    mosaic = Image.new("RGB", (len(xs) * 256, len(ys) * 256))
    done = 0
    for jy, y in enumerate(ys):
        for jx, x in enumerate(xs):
            url = tpl.format(z=args.zoom, x=x, y=y, s=(x + y) % 4 + 1)
            raw = fetch(url, cache / f"{args.zoom}_{x}_{y}.jpg", delay=args.delay)
            try:
                tile = Image.open(io.BytesIO(raw)).convert("RGB")
            except Exception as e:                  # noqa: BLE001
                raise RuntimeError(f"tile {x},{y} is not an image: {e}") from e
            mosaic.paste(tile.resize((256, 256)) if tile.size != (256, 256) else tile,
                         (jx * 256, jy * 256))
            done += 1
        print(f"  row {jy + 1}/{len(ys)}  ({done}/{total})")

    # Crop to the padded bbox. Mercator y is non-linear in latitude, so rows come
    # from the projection rather than from interpolating degrees.
    import math

    def merc_y(lat: float) -> float:
        return math.log(math.tan(math.radians(lat)) + 1.0 / math.cos(math.radians(lat)))

    w0, _, _, n0 = tile_bounds(xs[0], ys[0], args.zoom)
    _, s1, e1, _ = tile_bounds(xs[-1], ys[-1], args.zoom)
    W, H = mosaic.size
    my0, my1 = merc_y(n0), merc_y(s1)
    r0 = round((merc_y(lat1) - my0) / (my1 - my0) * H)
    r1 = round((merc_y(lat0) - my0) / (my1 - my0) * H)
    c0 = round((lon0 - w0) / (e1 - w0) * W)
    c1 = round((lon1 - w0) / (e1 - w0) * W)
    img = mosaic.crop((max(0, c0), max(0, r0), min(W, c1), min(H, r1)))
    print(f"cropped to {img.size[0]}x{img.size[1]}")

    if max(img.size) > args.max_px:
        scale = args.max_px / max(img.size)
        img = img.resize((round(img.size[0] * scale), round(img.size[1] * scale)),
                         Image.LANCZOS)
        print(f"resized to {img.size[0]}x{img.size[1]}")

    out = rt.data / "sat.jpg"
    img.save(out, quality=args.quality, optimize=True, progressive=True)
    (rt.data / "sat.json").write_text(json.dumps({
        "source": args.source, "datum": datum, "attribution": attrib,
        "zoom": args.zoom, "bbox": [lat0, lon0, lat1, lon1],
        "size": list(img.size), "m_per_px": round(ground_res(mid_lat, args.zoom), 3),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsat.jpg  {out.stat().st_size / 1024:.0f} KB  {img.size[0]}x{img.size[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
