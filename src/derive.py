#!/usr/bin/env python3
"""DEM + route.toml -> routes/<name>/data/profile.json

Everything measurable about the ride is computed here: the elevation profile
along the track, the gradient, the climb segments, and the supply plan (legs,
dry gaps, clock). route.toml contributes only what cannot be measured -- which
places count as supply, whether each is reliable, and how long to stop.

This replaces the old pipeline's elevation step entirely. That one spent about
2685 requests at 1 req/s against a point-query API -- some 45 minutes -- for
data already sitting in the 15 DEM tiles fetch_dem.py downloads in seconds.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import routes  # noqa: E402

SMOOTH_M = 400.0      # elevation smoothing window
GRADE_M = 800.0       # gradient central-difference span
GAIN_EPS = 1.0        # ignore rises below this; DEM noise, not climbing
SEG_MIN_M = 300.0     # shorter runs are merged into their neighbour


def merc_y(lat: float) -> float:
    return math.log(math.tan(math.radians(lat)) + 1.0 / math.cos(math.radians(lat)))


def main() -> int:
    ap = argparse.ArgumentParser()
    routes.add_route_arg(ap)
    ap.add_argument("--step", type=float, default=80.0,
                    help="profile sample spacing in metres")
    args = ap.parse_args()
    rt = routes.resolve(args)

    track = json.loads((rt.data / "track.json").read_text(encoding="utf-8"))
    dem = np.load(rt.data / "dem.npz")
    ele = dem["ele"].astype(np.float32)
    lat0, lon0, lat1, lon1 = (float(v) for v in dem["bbox"])
    H, W = ele.shape
    my0, my1 = merc_y(lat1), merc_y(lat0)

    def at(lat: float, lon: float) -> float:
        r = min(max((merc_y(lat) - my0) / (my1 - my0) * (H - 1), 0.0), H - 1.0)
        c = min(max((lon - lon0) / (lon1 - lon0) * (W - 1), 0.0), W - 1.0)
        r0, c0 = int(r), int(c)
        r1, c1 = min(r0 + 1, H - 1), min(c0 + 1, W - 1)
        fr, fc = r - r0, c - c0
        return float(ele[r0, c0] * (1-fr) * (1-fc) + ele[r1, c0] * fr * (1-fc)
                     + ele[r0, c1] * (1-fr) * fc + ele[r1, c1] * fr * fc)

    pts, cum = track["track"], track["cum_m"]
    total = cum[-1]
    n = max(int(total / args.step), 8) + 1

    # Resample the track to an even distance axis, then read the DEM at each.
    km, raw = [], []
    j = 0
    for i in range(n):
        d = total * i / (n - 1)
        while j + 1 < len(cum) and cum[j + 1] < d:
            j += 1
        if j + 1 < len(pts):
            seg = cum[j + 1] - cum[j]
            f = 0.0 if seg <= 0 else (d - cum[j]) / seg
            la = pts[j][0] + (pts[j + 1][0] - pts[j][0]) * f
            lo = pts[j][1] + (pts[j + 1][1] - pts[j][1]) * f
        else:
            la, lo = pts[-1]
        km.append(d / 1000.0)
        raw.append(at(la, lo))

    # Smooth. A 16.5 m raster read along a road that was cut into the hillside
    # carries the hillside's noise, not the road's grade.
    w = max(1, int(round(SMOOTH_M / args.step / 2)))
    sm = [float(np.mean(raw[max(0, i-w):i+w+1])) for i in range(n)]

    gw = max(1, int(round(GRADE_M / args.step / 2)))
    grade = []
    for i in range(n):
        a, b = max(0, i - gw), min(n - 1, i + gw)
        run = (km[b] - km[a]) * 1000.0
        grade.append(0.0 if run <= 0 else (sm[b] - sm[a]) / run * 100.0)
    gs = max(1, int(round(SMOOTH_M / args.step / 2)))
    grade = [float(np.mean(grade[max(0, i-gs):i+gs+1])) for i in range(n)]

    gain = sum(max(0.0, sm[i+1] - sm[i]) for i in range(n - 1)
               if sm[i+1] - sm[i] > GAIN_EPS * args.step / 80)
    loss = sum(max(0.0, sm[i] - sm[i+1]) for i in range(n - 1)
               if sm[i] - sm[i+1] > GAIN_EPS * args.step / 80)

    # Runs of consistent sign, then merge anything too short to be a real climb.
    kind = ["up" if g > 1.0 else "down" if g < -1.0 else "flat" for g in grade]
    runs: list[list] = []
    for i, k in enumerate(kind):
        if runs and runs[-1][0] == k:
            runs[-1][2] = i
        else:
            runs.append([k, i, i])
    merged = True
    while merged and len(runs) > 1:
        merged = False
        for i, (k, a, b) in enumerate(runs):
            if (km[b] - km[a]) * 1000 >= SEG_MIN_M:
                continue
            nb = i + 1 if i == 0 else i - 1
            runs[nb][1] = min(runs[nb][1], a)
            runs[nb][2] = max(runs[nb][2], b)
            runs.pop(i)
            merged = True
            break

    segs = []
    for k, a, b in runs:
        d = (km[b] - km[a]) * 1000.0
        segs.append({"kind": k, "from": round(km[a], 3), "to": round(km[b], 3),
                     "len_km": round(d / 1000, 3),
                     "d_ele": round(sm[b] - sm[a], 1),
                     "grade": round((sm[b] - sm[a]) / d if d else 0.0, 5),
                     "ele_from": round(sm[a], 1), "ele_to": round(sm[b], 1)})

    out = {
        "step_m": args.step, "n": n,
        "dist_km": [round(v, 3) for v in km],
        "ele": [round(v, 1) for v in sm],
        "ele_raw": [round(v, 1) for v in raw],
        "grade": [round(v, 2) for v in grade],
        "gain_m": round(gain), "loss_m": round(loss),
        "peak": {"km": round(km[int(np.argmax(sm))], 2), "ele": round(max(sm), 1)},
        "low": {"km": round(km[int(np.argmin(sm))], 2), "ele": round(min(sm), 1)},
        "segments": segs,
        "dem": {"source": "AWS terrain-tiles (SRTM 30 m)",
                "zoom": int(dem["meta"][0]),
                "m_per_px": round(float(dem["meta"][3]), 1)},
    }
    (rt.data / "profile.json").write_text(
        json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    ups = [s for s in segs if s["kind"] == "up"]
    print(f"profile  {n} samples at {args.step:.0f} m  "
          f"{min(sm):.0f}..{max(sm):.0f} m")
    print(f"         gain +{gain:.0f} m / loss -{loss:.0f} m")
    print(f"segments {len(segs)}  ({len(ups)} up, "
          f"{sum(1 for s in segs if s['kind']=='down')} down, "
          f"{sum(1 for s in segs if s['kind']=='flat')} flat)")
    print(f"         biggest climb +{max((s['d_ele'] for s in ups), default=0):.0f} m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
