#!/usr/bin/env python3
"""Route directory layout and route.toml loading.

One route lives in routes/<name>/ with a hand-edited route.toml and a data/
directory of derived artefacts. Everything in data/ is reproducible from the GPX
plus route.toml; nothing there should ever be edited by hand.

    routes/<name>/
      route.toml        the only hand-edited file
      data/
        track.json      <- parse_gpx.py
        dem.npz         <- fetch_dem.py
        sat.jpg|json    <- fetch_sat.py
        profile.json    <- derive.py
        cache/          tile cache, never published
      site/             <- build_scene.py + the page templates
"""
from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROUTES = ROOT / "routes"

DEFAULTS = {
    # Rendering
    "road_width_m": 7.0,
    "road_lift_m": 1.2,
    "road_colour": "#ffb071",
    "road_min_px": 3.2,
    "dem_zoom": 13,
    "sat_zoom": 16,
    "sat_source": "esri",
    "sat_max_px": 4096,
    "pad_m": 2000.0,
    # Planning
    "start_hour": 7.0,
    "speed_kmh": 17.0,
    "climb_speed_kmh": 9.0,
    # Climb categories, recalibrated per route. ASO's own thresholds start HC at
    # 1000 m of gain, which flattens every climb in low hill country into one
    # bucket; see the note emitted into scene.json.
    "categories": [
        {"cat": "特级", "min_gain_m": 240, "colour": "#e5484d"},
        {"cat": "1级", "min_gain_m": 180, "colour": "#f76b15"},
        {"cat": "2级", "min_gain_m": 120, "colour": "#ffb224"},
        {"cat": "3级", "min_gain_m": 80, "colour": "#46a758"},
        {"cat": "4级", "min_gain_m": 40, "colour": "#5b9dd9"},
    ],
}


class Route:
    def __init__(self, name: str):
        self.name = name
        self.dir = ROUTES / name
        self.data = self.dir / "data"
        self.site = self.dir / "site"
        self.cache = self.data / "cache"
        cfg = dict(DEFAULTS)
        toml = self.dir / "route.toml"
        if toml.exists():
            loaded = tomllib.loads(toml.read_text(encoding="utf-8"))
            # Shallow merge is enough: every nested key is a whole-value
            # replacement (the category table, the supply list).
            cfg.update(loaded)
        self.cfg = cfg

    def __getitem__(self, k):
        return self.cfg[k]

    def get(self, k, d=None):
        return self.cfg.get(k, d)

    @property
    def title(self) -> str:
        return self.cfg.get("title") or self.name

    def mkdirs(self) -> None:
        for p in (self.data, self.site, self.cache):
            p.mkdir(parents=True, exist_ok=True)


def discover() -> list[str]:
    if not ROUTES.exists():
        return []
    return sorted(p.name for p in ROUTES.iterdir()
                  if p.is_dir() and not p.name.startswith("."))


def add_route_arg(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--route", help="route name under routes/; "
                                    "defaults to the only one present")


def resolve(args) -> Route:
    name = getattr(args, "route", None)
    if name:
        return Route(name)
    found = discover()
    if len(found) == 1:
        return Route(found[0])
    if not found:
        raise SystemExit("no routes yet -- run parse_gpx.py --route <name> <file.gpx>")
    raise SystemExit(f"several routes present, pass --route: {', '.join(found)}")
