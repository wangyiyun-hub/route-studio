#!/usr/bin/env python3
"""route.toml's [[supply]] entries + the profile -> routes/<name>/data/plan.json

Only what cannot be measured goes in route.toml: which places count as supply,
whether each is reliable, how long to stop, and the prose. Everything else --
each point's coordinates and elevation, the legs between them, the dry gaps, the
clock -- is computed here.

The reliability flag is the one that matters most. A scenic-area kiosk or a
mountain guesthouse counts as NOT supply, and mislabelling one hides the longest
stretch of the route with nowhere to refill.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import routes  # noqa: E402

R_EARTH = 6371008.8


def dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_EARTH * math.asin(min(1.0, math.sqrt(h)))


def wgs_to_gcj(lat: float, lon: float) -> tuple[float, float]:
    """WGS-84 -> GCJ-02, for the 2D map's 高德 tiles.

    Only the 3D view uses WGS-84 directly (its Esri imagery shares that datum);
    anything drawn on Chinese tiles needs this or it sits ~490 m off the road.
    """
    if not (0.8293 < lat < 55.8271 and 72.004 < lon < 137.8347):
        return lat, lon                      # outside the shifted region
    ee, a = 0.00669342162296594323, 6378245.0
    x, y = lon - 105.0, lat - 35.0
    dlat = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
            + 0.2 * math.sqrt(abs(x))
            + (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
            + (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
            + (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0)
    dlon = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
            + (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
            + (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
            + (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0)
    rad = lat / 180.0 * math.pi
    magic = 1 - ee * math.sin(rad) ** 2
    sq = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sq) * math.pi)
    dlon = (dlon * 180.0) / (a / sq * math.cos(rad) * math.pi)
    return lat + dlat, lon + dlon


def hhmm(minutes: float) -> str:
    m = int(round(minutes))
    return f"{m // 60 % 24:02d}:{m % 60:02d}"


def main() -> int:
    ap = argparse.ArgumentParser()
    routes.add_route_arg(ap)
    args = ap.parse_args()
    rt = routes.resolve(args)

    supply = rt.get("supply", [])
    if not supply:
        print("route.toml has no [[supply]] entries -- nothing to plan.")
        print("The page renders without a plan; add entries to get the roadbook"
              " tables and the clock.")
        return 0

    track = json.loads((rt.data / "track.json").read_text(encoding="utf-8"))
    prof_f = rt.data / "profile.json"
    prof = json.loads(prof_f.read_text(encoding="utf-8"))
    pk, pe = prof["dist_km"], prof["ele"]
    pts, cum = track["track"], track["cum_m"]
    total_km = track["total_km"]

    def ele_at(km: float) -> float:
        i = min(range(len(pk)), key=lambda j: abs(pk[j] - km))
        return pe[i]

    def coord_at(km: float) -> tuple[float, float]:
        d = km * 1000.0
        i = min(range(len(cum)), key=lambda j: abs(cum[j] - d))
        return pts[i][0], pts[i][1]

    # Speed model: a flat cruise, slowed on climbs in proportion to the grade.
    # Crude, but it only has to make the arrival times plausible, and a rider
    # reading the roadbook cares about the ordering and the gaps, not minutes.
    v_flat = float(rt["speed_kmh"])
    v_climb = float(rt["climb_speed_kmh"])

    def ride_min(a_km: float, b_km: float) -> tuple[float, float, float]:
        """(minutes, ascent, descent) over a stretch, walking the profile."""
        up = down = mins = 0.0
        idx = [j for j in range(len(pk)) if a_km <= pk[j] <= b_km]
        for j, k in zip(idx, idx[1:]):
            seg_km = pk[k] - pk[j]
            if seg_km <= 0:
                continue
            d_ele = pe[k] - pe[j]
            if d_ele > 0:
                up += d_ele
            else:
                down -= d_ele
            grade = d_ele / (seg_km * 1000) * 100
            # Calibrated against this route's hand-built plan: 17.9 km/h on the
            # flat and 15.6 km/h averaged over its climbing legs. Reaching the
            # climbing speed at 6% (the first cut) was far too pessimistic and
            # added 56 minutes over 111 km; 10% matches the measured legs.
            if grade > 0:
                t = min(1.0, grade / 10.0)
                v = max(4.5, v_flat * (1 - t) + v_climb * t)
            else:
                # Descents are faster but not unboundedly so -- braking, bends
                # and the fact that a -8% hairpin is slower than a -3% straight.
                v = min(v_flat * 1.55, v_flat * (1 + min(-grade, 6.0) / 11.0))
            mins += seg_km / v * 60
        return mins, up, down

    # ---- points -------------------------------------------------------------
    points = []
    for e in sorted(supply, key=lambda x: float(x["km"])):
        km = float(e["km"])
        lat, lon = coord_at(km)
        points.append({
            "km": round(km, 2),
            "mk": str(e.get("mark", "")),
            "nm": e.get("name", ""),
            "short": e.get("short") or e.get("name", ""),
            "kind": e.get("kind", "ok"),
            "reliable": bool(e.get("reliable", False)),
            "stop": int(e.get("stop", 0)),
            "off": int(e.get("off_m", 0)),
            "note": e.get("note", ""),
            "buy": e.get("buy", []),
            "ele": round(ele_at(km), 1),
            "wgs": [round(lat, 6), round(lon, 6)],
            "gcj": [round(v, 6) for v in wgs_to_gcj(lat, lon)],
        })

    # ---- legs between consecutive points ------------------------------------
    legs = []
    for a, b in zip(points, points[1:]):
        mins, up, down = ride_min(a["km"], b["km"])
        km = b["km"] - a["km"]
        legs.append({
            "frm": a["nm"], "to": b["nm"],
            "frm_short": a["short"], "to_short": b["short"],
            "from_km": a["km"], "to_km": b["km"], "km": round(km, 2),
            "up": round(up), "down": round(down), "min": round(mins),
            "grade": round((b["ele"] - a["ele"]) / (km * 1000), 5) if km else 0.0,
            "ele_a": a["ele"], "ele_b": b["ele"],
        })

    # ---- dry gaps, reliable points only -------------------------------------
    rel = [p for p in points if p["reliable"]]
    gaps = []
    # The start and finish bracket the route: a rider leaves with what they
    # carry, so the stretch before the first reliable stop is a gap too.
    marks = ([points[0]] if points and not points[0]["reliable"] else []) + rel
    if points and not points[-1]["reliable"]:
        marks = marks + [points[-1]]
    for a, b in zip(marks, marks[1:]):
        mins, up, _ = ride_min(a["km"], b["km"])
        gaps.append({
            "frm": a["nm"], "to": b["nm"],
            "from_km": a["km"], "to_km": b["km"],
            "km": round(b["km"] - a["km"], 2),
            "up": round(up), "ride_min": round(mins),
        })

    # ---- clock --------------------------------------------------------------
    start_h = float(rt["start_hour"])
    t = start_h * 60
    schedule = []
    for i, p in enumerate(points):
        if i:
            t += ride_min(points[i - 1]["km"], p["km"])[0]
        arrive = hhmm(t)
        t += p["stop"]
        schedule.append({"mk": p["mk"], "nm": p["nm"], "km": p["km"],
                         "ele": round(p["ele"]), "arrive": arrive,
                         "depart": hhmm(t), "stop": p["stop"]})
    elapsed = t - start_h * 60

    tot_min, tot_up, tot_down = ride_min(0.0, total_km)
    ups = [s for s in prof["segments"] if s["kind"] == "up"]
    main = max(ups, key=lambda s: s["d_ele"], default=None)

    out = {
        "points": points, "legs": legs, "gaps": gaps, "schedule": schedule,
        "total": {"km": round(total_km, 2), "up": prof["gain_m"],
                  "down": prof["loss_m"], "min": round(tot_min),
                  "ele_a": round(pe[0], 1), "ele_b": round(pe[-1], 1)},
        "main_climb": {"km": main["len_km"], "up": round(main["d_ele"]),
                       "grade": main["grade"], "from_km": main["from"],
                       "to_km": main["to"]} if main else {},
        "start_hour": start_h, "elapsed_min": round(elapsed),
    }
    (rt.data / "plan.json").write_text(
        json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print(f"plan     {len(points)} supply points ({len(rel)} reliable)")
    print(f"         {len(legs)} legs, {len(gaps)} dry gap(s), "
          f"longest {max((g['km'] for g in gaps), default=0):.1f} km")
    print(f"         ride {tot_min/60:.1f} h + stops {sum(p['stop'] for p in points)} min "
          f"-> {elapsed/60:.1f} h, {hhmm(start_h*60)} to {schedule[-1]['depart']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
