#!/usr/bin/env python3
"""GPX -> routes/<name>/data/track.json

Only the geometry is taken from the file. Any <ele> present is ignored: the
elevation for this project comes from the DEM, because route-planner exports
routinely carry zeros (the iGPSPORT export this was built against has <ele>0</ele>
on all 2533 points) and a half-trusted elevation source is worse than one that
is consistently derived.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from routes import Route  # noqa: E402

NS = {"g": "http://www.topografix.com/GPX/1/1",
      "g0": "http://www.topografix.com/GPX/1/0"}


def read_points(path: Path) -> tuple[list[tuple[float, float]], str, int]:
    root = ET.parse(path).getroot()
    pts, ele_seen, ele_nonzero = [], 0, 0
    # Accept 1.0 and 1.1, and files that declare no namespace at all.
    for tag in ("g:trk/g:trkseg/g:trkpt", "g0:trk/g0:trkseg/g0:trkpt",
                "trk/trkseg/trkpt", "g:rte/g:rtept", "rte/rtept"):
        for p in root.findall(tag, NS):
            lat, lon = p.get("lat"), p.get("lon")
            if lat is None or lon is None:
                continue
            pts.append((float(lat), float(lon)))
            for e in p:
                if e.tag.rsplit("}", 1)[-1] == "ele" and e.text:
                    ele_seen += 1
                    if abs(float(e.text)) > 0.5:
                        ele_nonzero += 1
        if pts:
            break
    name_el = root.find(".//g:trk/g:name", NS)
    if name_el is None:
        name_el = root.find(".//trk/name")
    name = (name_el.text or "").strip() if name_el is not None else ""
    return pts, name, ele_nonzero


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("gpx", type=Path)
    ap.add_argument("--route", required=True)
    ap.add_argument("--title", help="overrides the GPX track name")
    args = ap.parse_args()

    raw, gpx_name, ele_nonzero = read_points(args.gpx)
    if len(raw) < 2:
        raise SystemExit(f"{args.gpx}: found {len(raw)} track points")

    # Haversine, not an equirectangular approximation with a single scale factor
    # at the mean latitude. The approximation is fine for one 15 km box but the
    # error accumulates over 2500 segments: it came out 0.24% short, 110.98 km
    # against the 111.24 km already published for this route.
    R = 6371008.8       # mean Earth radius, the IUGG value

    def dist(a: tuple[float, float], b: tuple[float, float]) -> float:
        p1, p2 = math.radians(a[0]), math.radians(b[0])
        dp = p2 - p1
        dl = math.radians(b[1] - a[1])
        h = (math.sin(dp / 2) ** 2
             + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
        return 2 * R * math.asin(min(1.0, math.sqrt(h)))

    # Drop sub-metre repeats. Consecutive duplicates make a zero-length segment,
    # which leaves the ribbon's perpendicular undefined.
    pts = [raw[0]]
    for p in raw[1:]:
        if dist(pts[-1], p) > 1.0:
            pts.append(p)

    cum = [0.0]
    for a, b in zip(pts, pts[1:]):
        cum.append(cum[-1] + dist(a, b))

    closure = dist(pts[-1], pts[0])
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]

    rt = Route(args.route)
    rt.mkdirs()
    out = {
        "name": args.title or gpx_name or args.route,
        "source": args.gpx.name,
        "raw_points": len(raw), "points": len(pts),
        "total_km": round(cum[-1] / 1000, 3),
        "closure_m": round(closure, 1),
        # A loop if the ends meet within 500 m -- enough for "started and
        # finished at the same car park", tight enough to reject a point-to-point.
        "is_loop": closure < 500.0,
        "gpx_ele_usable": ele_nonzero > len(raw) * 0.5,
        "bbox": [min(lats), min(lons), max(lats), max(lons)],
        "track": [[round(a, 6), round(b, 6)] for a, b in pts],
        "cum_m": [round(v, 1) for v in cum],
    }
    (rt.data / "track.json").write_text(
        json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print(f"{out['name']}  {out['total_km']:.2f} km  {len(pts)} pts "
          f"({len(raw) - len(pts)} sub-metre repeats dropped)")
    print(f"  bbox {out['bbox']}")
    print(f"  {'loop' if out['is_loop'] else 'point-to-point'}, "
          f"ends {closure:.0f} m apart")
    print(f"  gpx elevation {'usable but ignored' if out['gpx_ele_usable'] else 'absent/zero'}"
          f" -- elevation comes from the DEM")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
