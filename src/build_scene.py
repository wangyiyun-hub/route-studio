#!/usr/bin/env python3
"""Turn the raster + track into render-ready buffers -> site/scene.bin + scene.json

Three pieces of geometry come out of here:

1. Terrain mesh: a regular grid over the DEM, positions in local metres, with the
   satellite texture mapped by UV. Normals are computed from the elevation so the
   lighting reads as real relief rather than flat-shaded triangles.

2. Road ribbon: the reason the old page's glowing line read as a wire and not a
   road. A zero-width line has no body; this emits a real triangle strip of a
   given ground width, draped on the terrain and lifted slightly to win the
   depth test against it.

3. Climb classification: the ASO-style categories, recalibrated for this terrain
   (see CATEGORIES) because the original thresholds -- HC starts at 1000 m of
   gain -- collapse every climb in eastern-China hill country into "cat 4 or
   uncategorised" and throw away all the contrast that makes the chart useful.
   The rule in use is emitted into scene.json so the page can state it.

Everything measurable is computed here. Prose belongs in the page template, and
a number typed into a sentence is how a chart and its caption start disagreeing.
"""
from __future__ import annotations

import json
import math
import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import routes  # noqa: E402

SKIRT_DROP_M = 400.0    # terrain edge skirt, so the slab has visible sides


def merc_y(lat: float) -> float:
    return math.log(math.tan(math.radians(lat)) + 1.0 / math.cos(math.radians(lat)))


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    routes.add_route_arg(ap)
    args = ap.parse_args()
    rt = routes.resolve(args)
    rt.mkdirs()

    # Per-route knobs come from route.toml; see routes.py DEFAULTS for the
    # fallbacks and that file's docstring for the directory layout.
    ROAD_WIDTH_M = float(rt["road_width_m"])
    ROAD_LIFT_M = float(rt["road_lift_m"])
    ROAD_COLOUR = str(rt["road_colour"])
    ROAD_MIN_PX = float(rt["road_min_px"])
    CATEGORIES = [(c["cat"], float(c["min_gain_m"]), c["colour"])
                  for c in rt["categories"]]

    dem = np.load(rt.data / "dem.npz")
    ele = dem["ele"].astype(np.float32)
    lat0, lon0, lat1, lon1 = (float(v) for v in dem["bbox"])
    H, W = ele.shape

    track = json.loads((rt.data / "track.json").read_text(encoding="utf-8"))

    # Profile and segments come from derive.py (profile.json). A route carried
    # over from the older pipeline instead has elevation.json + segments.json,
    # and those are kept in preference: derive.py reproduces the elevation to
    # within a metre but segments it more coarsely, which would silently change
    # the published climb categories. route.toml's use_derived_profile forces
    # the derived pair.
    legacy = (rt.data / "elevation.json")
    if legacy.exists() and not rt.get("use_derived_profile", False):
        prof = json.loads(legacy.read_text(encoding="utf-8"))
        segs = json.loads((rt.data / "segments.json").read_text(encoding="utf-8"))
    else:
        prof = json.loads((rt.data / "profile.json").read_text(encoding="utf-8"))
        segs = prof["segments"]

    plan_f = rt.data / "plan.json"
    if plan_f.exists():
        plan = json.loads(plan_f.read_text(encoding="utf-8"))
    else:
        # No plan yet: a new route has no supply points until route.toml lists
        # them. Everything else on the page is measured, so it still renders.
        plan = {"points": [], "legs": [], "gaps": [], "schedule": {},
                "total": {}, "main_climb": {}}

    sat_f = rt.data / "sat.json"
    if sat_f.exists():
        sat = json.loads(sat_f.read_text(encoding="utf-8"))
    else:
        sat = {"source": None, "datum": "WGS-84", "attribution": "(未抓取影像)",
               "zoom": 0, "size": [0, 0], "m_per_px": 0}

    # Local metric frame, origin at the box centre, x east / y north / z up.
    mid_lat = (lat0 + lat1) / 2
    kx = 111320.0 * math.cos(math.radians(mid_lat))
    ky = 110540.0
    span_x = (lon1 - lon0) * kx
    span_y = (lat1 - lat0) * ky
    my0, my1 = merc_y(lat1), merc_y(lat0)

    def to_local(lat: float, lon: float) -> tuple[float, float]:
        """lon/lat -> local metres. Rows follow Mercator, matching the texture."""
        u = (lon - lon0) / (lon1 - lon0)
        v = (merc_y(lat) - my0) / (my1 - my0)
        return (u - 0.5) * span_x, (0.5 - v) * span_y

    def sample_ele(lat: float, lon: float) -> float:
        """Elevation on the rendered surface, not a bilinear blur of it.

        Bilinear interpolation of the four surrounding cells is the obvious
        thing and it is wrong here: the page draws each cell as two triangles,
        and a bilinear value sits off that plane wherever the quad is twisted.
        The road ribbon sampled that way dips under the terrain on side slopes
        and renders as a dashed line of z-fighting -- visible all along the
        chase-camera view before this was fixed.

        The mesh comes out of an instanced triangle strip where, for row r,
        gl_VertexID n gives column n>>1 and row r+(n&1). That splits each cell
        along the anti-diagonal from (c, r+1) to (c+1, r), so which of the two
        planes applies depends on whether fc + fr crosses 1.
        """
        r = min(max((merc_y(lat) - my0) / (my1 - my0) * (H - 1), 0.0), H - 1.0)
        c = min(max((lon - lon0) / (lon1 - lon0) * (W - 1), 0.0), W - 1.0)
        r0, c0 = int(r), int(c)
        r1, c1 = min(r0 + 1, H - 1), min(c0 + 1, W - 1)
        fr, fc = r - r0, c - c0
        z00, z01 = float(ele[r0, c0]), float(ele[r1, c0])
        z10, z11 = float(ele[r0, c1]), float(ele[r1, c1])
        if fc + fr <= 1.0:
            return z00 + fc * (z10 - z00) + fr * (z01 - z00)
        return z11 + (1.0 - fc) * (z01 - z11) + (1.0 - fr) * (z10 - z11)

    # ---- terrain: elevation raster only ------------------------------------
    # The first version of this emitted an interleaved vertex buffer plus a
    # 32-bit index buffer: 982k verts and 1.96M triangles came to 55 MB, which is
    # absurd for 1.9 MB of actual information. Every one of those attributes --
    # x, y, u, v, and all three normal components -- is derivable from the cell's
    # position in a regular grid, and the grid position is derivable from
    # gl_VertexID and gl_InstanceID. So ship only what cannot be computed: the
    # elevations, as Int16 (this terrain spans 1..478 m; decimetres would still
    # fit). The page draws the mesh as one instanced triangle strip, one instance
    # per row, and samples this raster as a texture.
    z_scale = 1.0           # metres per stored unit; Int16 covers +-32 km
    zgrid = np.rint(ele / z_scale).astype(np.int16)
    print(f"terrain  {W}x{H} raster = {zgrid.nbytes / 1e6:.2f} MB "
          f"(vs {(H*W*32 + (H-1)*(W-1)*24) / 1e6:.0f} MB as explicit geometry)")

    # ---- road ribbon ------------------------------------------------------
    pts = track["track"]
    cum = track["cum_m"]

    # Resample the centreline to the DEM's cell size. The GPX averages 44 m
    # between points, which spans two or three 16.5 m cells, so a straight
    # ribbon segment cuts through whatever the terrain does in between -- it
    # showed up as transverse stripes where the strip dipped below the surface,
    # and as flat streamers across the hairpins instead of a road on the slope.
    cell = span_x / (W - 1)
    step = cell * 0.8
    dense: list[tuple[float, float, float]] = []   # (lat, lon, dist along route)
    total = cum[-1]
    n_steps = max(int(total / step), 2)
    j = 0
    for k in range(n_steps + 1):
        d = total * k / n_steps
        while j + 1 < len(cum) and cum[j + 1] < d:
            j += 1
        if j + 1 >= len(pts):
            dense.append((pts[-1][0], pts[-1][1], d))
            continue
        seg = cum[j + 1] - cum[j]
        f = 0.0 if seg <= 0 else (d - cum[j]) / seg
        dense.append((pts[j][0] + (pts[j + 1][0] - pts[j][0]) * f,
                      pts[j][1] + (pts[j + 1][1] - pts[j][1]) * f, d))

    def surface_local(x: float, y: float) -> float:
        """Same triangle-exact sampling, addressed in local metres."""
        c = min(max((x / span_x + 0.5) * (W - 1), 0.0), W - 1.0)
        r = min(max((0.5 - y / span_y) * (H - 1), 0.0), H - 1.0)
        r0, c0 = int(r), int(c)
        r1, c1 = min(r0 + 1, H - 1), min(c0 + 1, W - 1)
        fr, fc = r - r0, c - c0
        z00, z01 = float(ele[r0, c0]), float(ele[r1, c0])
        z10, z11 = float(ele[r0, c1]), float(ele[r1, c1])
        if fc + fr <= 1.0:
            return z00 + fc * (z10 - z00) + fr * (z01 - z00)
        return z11 + (1.0 - fc) * (z01 - z11) + (1.0 - fr) * (z10 - z11)

    local = [to_local(a, b) for a, b, _ in dense]
    zs = [sample_ele(a, b) + ROAD_LIFT_M for a, b, _ in dense]
    cum = [d for _, _, d in dense]
    pts = dense

    # Drop near-duplicate points: they make the perpendicular undefined and show
    # up as a pinched or flipped quad in the strip.
    keep = [0]
    for i in range(1, len(local)):
        px, py = local[keep[-1]]
        qx, qy = local[i]
        if (qx - px) ** 2 + (qy - py) ** 2 > 1.0:
            keep.append(i)
    if keep[-1] != len(local) - 1:
        keep.append(len(local) - 1)

    # Find stretches the route rides twice and mark the second pass, so the
    # ribbon can be offset sideways into a second lane.
    #
    # This route is a lollipop: the stem from the start up to the loop is ridden
    # out and back, so two ribbons sit on the same road within a few metres.
    # Coincident geometry means the depth test picks a winner per fragment, and
    # the two passes -- a 特级 climb outbound in red, unclassified return in warm
    # -- rendered as alternating transverse bars the whole length of the stem.
    # It looked like a shader or z-offset bug and was neither; the pixels really
    # were two different legs of the ride.
    DUP_M, DUP_KM = 20.0, 2.0
    BUCKET = 60.0
    grid: dict[tuple[int, int], list[int]] = {}
    second = [False] * len(local)
    for i in keep:
        x, y = local[i]
        gx, gy = int(x // BUCKET), int(y // BUCKET)
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                for j in grid.get((gx + ox, gy + oy), ()):
                    if abs(cum[i] - cum[j]) < DUP_KM * 1000:
                        continue
                    jx, jy = local[j]
                    if (x - jx) ** 2 + (y - jy) ** 2 < DUP_M * DUP_M:
                        second[i] = True
                        break
                if second[i]:
                    break
            if second[i]:
                break
        grid.setdefault((gx, gy), []).append(i)
    n_dup = sum(1 for i in keep if second[i])
    print(f"         {n_dup} pts ({n_dup/len(keep)*100:.0f}%) are a second pass "
          f"-> offset into a parallel lane")

    # Emit the centreline plus a sideways unit vector, and let the shader decide
    # the half-width. Baking the offset in here means one fixed ground width, and
    # no single value works: 11 m is 0.9 px across the whole 15 km box -- the
    # first render showed the route as a dotted smear -- while a width chosen to
    # be visible zoomed out makes the road a fat stripe at street level. The
    # shader widens to a floor measured in pixels instead.
    # ---- climbs -----------------------------------------------------------
    # Classified before the ribbon is built, because the ribbon is coloured per
    # vertex from these bands: a categorised climb wears its category colour, and
    # everything else keeps the default warm trace.
    def categorise(gain: float) -> tuple[str, str] | tuple[None, None]:
        for label, lo, colour in CATEGORIES:
            if gain >= lo:
                return label, colour
        return None, None

    climbs = []
    for s in segs:
        if s["kind"] != "up" or s["d_ele"] < CATEGORIES[-1][1]:
            continue
        label, colour = categorise(s["d_ele"])
        if label is None:
            continue
        climbs.append({
            "from_km": round(s["from"], 2), "to_km": round(s["to"], 2),
            "len_km": round(s["len_km"], 2), "gain_m": round(s["d_ele"], 1),
            "grade_pct": round(s["grade"] * 100, 2),
            "top_m": round(s["ele_to"], 1), "cat": label, "colour": colour,
        })
    climbs.sort(key=lambda c: c["from_km"])

    def hex_rgb(h: str) -> tuple[float, float, float]:
        h = h.lstrip("#")
        return tuple(int(h[k:k + 2], 16) / 255.0 for k in (0, 2, 4))

    DEFAULT_RGB = hex_rgb(ROAD_COLOUR)
    bands = [(c["from_km"] * 1000.0, c["to_km"] * 1000.0, hex_rgb(c["colour"]))
             for c in climbs]

    def colour_at(dist_m: float) -> tuple[float, float, float]:
        for lo, hi, rgb in bands:
            if lo <= dist_m <= hi:
                return rgb
        return DEFAULT_RGB

    # ---- ribbon geometry --------------------------------------------------
    rv = []
    for n, i in enumerate(keep):
        x, y = local[i]
        # Tangent from the neighbours, so corners get a mitre instead of a notch.
        j0 = keep[max(n - 1, 0)]
        j1 = keep[min(n + 1, len(keep) - 1)]
        tx = local[j1][0] - local[j0][0]
        ty = local[j1][1] - local[j0][1]
        tl = math.hypot(tx, ty) or 1.0
        px, py = -ty / tl, tx / tl          # left normal
        s = cum[i] / cum[-1]
        cr, cg, cb = colour_at(cum[i])
        # Clear the terrain across the whole ribbon width, not just under the
        # centreline. A 7 m ribbon crosses a 16.5 m cell at an angle, so on a
        # side slope the downhill kerb goes under the surface: measured, 14% of
        # kerb samples were buried, up to 3.97 m, and that renders as transverse
        # z-fighting stripes along the road. Taking the max keeps the ribbon flat
        # across its width, like a real carriageway, and puts it on a small
        # embankment on steep ground rather than inside the hill.
        half = ROAD_WIDTH_M / 2.0
        if second[i]:
            # Always the same side, so the return pass reads as a consistent
            # second lane rather than weaving across the outbound one.
            x += px * ROAD_WIDTH_M
            y += py * ROAD_WIDTH_M
        z = max(surface_local(x, y),
                surface_local(x + px * half, y + py * half),
                surface_local(x - px * half, y - py * half)) + ROAD_LIFT_M
        rv.append((x, y, z, px, py, s, +1.0, cr, cg, cb))
        rv.append((x, y, z, px, py, s, -1.0, cr, cg, cb))
    road = np.array(rv, dtype=np.float32)
    n_cat = sum(1 for i in keep if colour_at(cum[i]) != DEFAULT_RGB)
    print(f"road     {len(keep)} centreline pts -> {len(road)} verts "
          f"(base {ROAD_WIDTH_M:.0f} m, +{ROAD_LIFT_M:.0f} m, screen-space floor)")
    print(f"         {n_cat} pts ({n_cat/len(keep)*100:.0f}%) on categorised climbs")
    print(f"climbs   {len(climbs)} categorised of {sum(1 for s in segs if s['kind']=='up')} ups")
    for c in climbs:
        print(f"           {c['from_km']:6.1f}km {c['cat']:>3} {c['len_km']:5.2f}km "
              f"+{c['gain_m']:5.0f}m {c['grade_pct']:4.1f}%")

    # ---- profile gradient --------------------------------------------------
    # Perceived gradient, as a central difference over about 400 m. The raw
    # sample-to-sample slope spans 80 m and is dominated by DEM noise: it swings
    # through +-15% on stretches a rider would call flat, which would make the
    # profile's colour banding meaningless.
    pk, pe = prof["dist_km"], prof["ele"]
    span_km = (pk[1] - pk[0]) if len(pk) > 1 else 0.08
    win = max(3, int(round(0.8 / span_km / 2)))     # +-400 m
    raw_g = []
    for i in range(len(pe)):
        a, b = max(0, i - win), min(len(pe) - 1, i + win)
        run = (pk[b] - pk[a]) * 1000.0
        raw_g.append(0.0 if run <= 0 else (pe[b] - pe[a]) / run * 100)
    # Then a moving average on the gradient itself. The central difference alone
    # still crosses a colour-band boundary and comes back within a few samples,
    # and the profile's per-sample fill turns that into vertical candy stripes
    # instead of readable bands -- the whole point of colouring by gradient.
    sm = max(4, int(round(0.4 / span_km)))
    grade = []
    for i in range(len(raw_g)):
        a, b = max(0, i - sm), min(len(raw_g), i + sm + 1)
        grade.append(round(sum(raw_g[a:b]) / (b - a), 2))

    # ---- km posts ----------------------------------------------------------
    # Every 10 km, in local metres, for the roadbook's top-down view.
    posts = []
    for km in range(10, int(total / 1000) + 1, 10):
        d = km * 1000.0
        i = min(range(len(cum)), key=lambda k: abs(cum[k] - d))
        x, y = local[i]
        posts.append({"km": km, "x": round(x, 1), "y": round(y, 1),
                      "z": round(zs[i], 1)})

    # ---- pack -------------------------------------------------------------
    blobs = [zgrid.tobytes(), road.tobytes()]
    offs, pos = [], 0
    for blk in blobs:
        offs.append(pos)
        pos += len(blk)
        pad = (-pos) % 4                    # keep every view 4-byte aligned
        pos += pad
    buf = bytearray(pos)
    p = 0
    for blk in blobs:
        buf[p:p + len(blk)] = blk
        p += len(blk)
        p += (-p) % 4

    site = rt.site
    (site / "scene.bin").write_bytes(bytes(buf))

    # Coordinates live in `wgs` (the GPX datum); `gcj` is the 高德-shifted copy
    # the 2D map needs. Use wgs here -- the terrain and the Esri texture are both
    # WGS-84, so taking gcj would offset every marker by ~490 m.
    markers = []
    for m in plan.get("points", []):
        wgs = m.get("wgs")
        if not wgs or len(wgs) < 2:
            continue
        lat, lon = float(wgs[0]), float(wgs[1])
        x, y = to_local(lat, lon)
        markers.append({
            "x": round(x, 1), "y": round(y, 1),
            "z": round(sample_ele(lat, lon) + ROAD_LIFT_M, 1),
            "km": round(m.get("km", 0), 2), "name": m.get("short") or m.get("nm", ""),
            "full": m.get("nm", ""), "kind": m.get("kind", ""),
            "mark": str(m.get("mk", "")), "reliable": bool(m.get("reliable")),
        })

    scene = {
        "frame": {
            "span_x": round(span_x, 1), "span_y": round(span_y, 1),
            "bbox": [lat0, lon0, lat1, lon1],
            "ele_min": round(float(ele.min()), 1), "ele_max": round(float(ele.max()), 1),
            "skirt_z": round(float(ele.min()) - SKIRT_DROP_M, 1),
        },
        # Int16 elevations, row-major from the north edge. The page uploads this
        # as an R16I texture and builds the mesh procedurally; "strip_verts" and
        # "instances" are what to pass to drawArraysInstanced.
        "terrain": {"nx": W, "ny": H, "offset": offs[0],
                    "bytes": int(zgrid.nbytes), "z_scale": z_scale,
                    "strip_verts": W * 2, "instances": H - 1,
                    "m_per_cell": round(span_x / (W - 1), 2)},
        # x, y, z, side_nx, side_ny, s, sign, r, g, b -> 10 floats
        "road": {"verts": len(road), "stride": 40, "offset": offs[1],
                 "width_m": ROAD_WIDTH_M, "lift_m": ROAD_LIFT_M,
                 "min_px": ROAD_MIN_PX, "base_colour": ROAD_COLOUR},
        "route": {
            "name": rt.title or track.get("name", ""), "total_km": round(track["total_km"], 2),
            # gain_m is the point-by-point accumulation the pipeline already
            # computed (with its 1 m noise threshold). Summing the "up" segments
            # instead gives 1455 m, because it discards every rise inside a
            # segment classified flat or flat-ish -- 586 m of real climbing on
            # this route, and a number that would contradict the published page.
            "ascent_m": prof["gain_m"],
            "descent_m": prof.get("loss_m"),
            # Cumulative ascent is method-dependent, so the page states the
            # method. This route's older published page reads 2041 m from an
            # opentopodata point-query pipeline; the same SRTM data sampled off
            # the DEM raster at this step gives 2174 m. Neither is wrong -- the
            # figure is sensitive to sample spacing and smoothing -- but a
            # number shown without its method invites exactly the "which one is
            # right" question that has no answer.
            "ascent_method": (
                f"DEM 栅格 {prof.get('step_m', 80):.0f} m 步长逐点累加"
                if "step_m" in prof else "逐点累加"),
            "ele_min": round(min(prof["ele"]), 1), "ele_max": round(max(prof["ele"]), 1),
            "is_loop": track.get("is_loop", False),
            "dem": {"source": "AWS terrain-tiles (SRTM 30 m)",
                    "zoom": int(dem["meta"][0]), "m_per_px": round(float(dem["meta"][3]), 1)},
        },
        "imagery": sat,
        "climbs": climbs,
        "climb_rule": {
            "note": "本地重标定口径，非 ASO 原始分级",
            "basis": "按单段净爬升分档（m）",
            "thresholds": [{"cat": c[0], "min_gain_m": c[1], "colour": c[2]}
                           for c in CATEGORIES],
            "aso_note": "ASO 原口径 HC 门槛为单段爬升 1000 m；本线最大单段 "
                        f"{max((s['d_ele'] for s in segs if s['kind']=='up'), default=0):.0f} m，"
                        "套用原口径全部落入 3 级及以下，失去区分度。",
        },
        "markers": markers,
        # dist_km is already the per-sample distance axis, not a scalar total.
        "profile": {"km": [round(v, 3) for v in prof["dist_km"]],
                    "ele": [round(v, 1) for v in prof["ele"]],
                    "grade": grade,
                    "peak": prof.get("peak"), "low": prof.get("low")},
        "km_posts": posts,
        # Straight from the planning step: the roadbook renders these as tables
        # and the profile puts the supply points on its distance axis.
        "plan": {"legs": plan.get("legs", []), "gaps": plan.get("gaps", []),
                 "schedule": plan.get("schedule", []), "total": plan.get("total", {}),
                 "main_climb": plan.get("main_climb", {}),
                 "start_hour": plan.get("start_hour"),
                 "elapsed_min": plan.get("elapsed_min")},
    }
    (site / "scene.json").write_text(json.dumps(scene, ensure_ascii=False,
                                                separators=(",", ":")), encoding="utf-8")
    print(f"\nscene.bin   {(site/'scene.bin').stat().st_size/1024/1024:.2f} MB")
    print(f"scene.json  {(site/'scene.json').stat().st_size/1024:.0f} KB")
    print(f"markers     {len(markers)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
