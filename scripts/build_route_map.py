"""
scripts/build_route_map.py
==========================
Metro-style route map of the listed restaurants between Phoenix Mall of Asia
and Big Bay, coloured by washroom sentiment (0 credits).

The highway is drawn as one straight horizontal line, like the Namma Metro
map, with two lanes. West is drawn above the road, so a driver heading right
(towards Big Bay) has the top on their left: Indian traffic keeps left, so the
top lane is northbound and the bottom lane southbound. Every restaurant is a
station:

  - Its position along the line is its distance along the route polyline
    (OSRM driving route, or fixed waypoints when OSRM is unreachable).
  - Its side is the sign of the cross product with the nearest route segment:
    left of the direction of travel (west) is drawn above the road, right
    (east) below.
  - On-highway restaurants sit at the road edge. Detour restaurants
    (Position "Short detour", or more than 300 m from the route) hang at the
    end of a spur whose length is proportional to their distance from the road.
  - Stations are spaced by real distance, but pushed apart (and close spurs
    fanned out) until no label, dot or spur collides with another.

Reads:
  - data/raw_data/restaurants_route_above_3.5.csv
  - data/processed/washroom_sentiment_summary.csv
  - data/processed/washroom_mentions_scored.csv
Writes:
  - data/output/route_map.html   self-contained: inline SVG, no external libraries
  - data/output/route_map.png    screenshot of the HTML (needs Playwright)
  - site/data.js                 window.ROUTE_DATA for the route website (site/index.html)

Usage (from the repository root):

    python scripts/build_route_map.py             # OSRM route, falls back to waypoints
    python scripts/build_route_map.py --offline   # waypoints only, no network
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
import sys
import urllib.request
from itertools import pairwise
from pathlib import Path

_HERE = Path(__file__).resolve().parent          # scripts/
_ROOT = _HERE.parent                              # repository root
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from scripts import utils
except ImportError:
    import utils

# ---------------------------------------------------------------------------
# Washroom colour thresholds (mean_sentiment_score, from -1 to +1)
# ---------------------------------------------------------------------------
GOOD_MIN = 0.3        # >= 0.3: green
BAD_MAX = -0.3        # <  -0.3: red; anything in between: yellow
COLOURS = {"good": "#0ca30c", "moderate": "#fab219", "bad": "#d03b3b", "none": "#9aa0a6"}
BUCKET_NAMES = {"good": "Good", "moderate": "Moderate", "bad": "Bad", "none": "No washroom reviews"}

DETOUR_MIN_M = 300    # further than this from the route: detour, whatever the CSV says
AMBIGUOUS_M = 30      # closer than this to the centre line: side worth a manual check
ROUTE_END_M = 50      # this close to the end of the route: km and side measured past the polyline

RESTAURANTS_CSV = utils.RAW_DATA_DIR / "restaurants_route_above_3.5.csv"
SUMMARY_CSV = utils.PROCESSED_DIR / "washroom_sentiment_summary.csv"
MENTIONS_CSV = utils.PROCESSED_DIR / "washroom_mentions_scored.csv"
HTML_OUT = utils.OUTPUT_DIR / "route_map.html"
PNG_OUT = utils.OUTPUT_DIR / "route_map.png"
SITE_DATA_OUT = utils.PROJECT_ROOT / "site" / "data.js"

START = (13.0706, 77.5914)   # Phoenix Mall of Asia
END = (13.6050, 77.7870)     # Big Bay
OSRM_URL = ("https://router.project-osrm.org/route/v1/driving/"
            "{a[1]},{a[0]};{b[1]},{b[0]}?overview=full&geometries=geojson")
FALLBACK_WAYPOINTS = [
    (13.0706, 77.5914), (13.0940, 77.5950), (13.1040, 77.6000), (13.1220, 77.6110),
    (13.1520, 77.6210), (13.1670, 77.6320), (13.1910, 77.6460), (13.2630, 77.7170),
    (13.2810, 77.7230), (13.4510, 77.7360), (13.6050, 77.7870),
]
LANDMARKS = [  # interchange-style markers on the line
    ("Yelahanka", 13.0940, 77.5950),
    ("Airport toll", 13.1936, 77.6498),   # Navayuga Devanahalli/Sadahalli toll plaza (OSM)
    ("Devanahalli", 13.2630, 77.7170),
    ("Chikkaballapur", 13.4510, 77.7360),
]

# Local flat projection: metres east/north of a reference point mid-route
REF_LAT, REF_LNG = 13.3, 77.7
M_PER_DEG = 111_195.0

# Layout, in SVG pixels
X0 = 190                 # x of km 0 (room for the terminal name on the left)
PX_PER_KM = 40
MIN_ROAD_GAP = 3         # keeps stations on opposite sides in route order
STEP_PX = 2
LANE_H = 16
EDGE_GAP = 2             # road edge to station dot
DOT_R = 6
SPUR_PX_PER_KM = 45
SPUR_MIN_PX, SPUR_MAX_PX = 44, 140
MAX_FAN_PX = 16         # "slight" fan of a spur end; beyond this the road stretches instead
FAN_NEAR_PX = 40        # fan only when the previous spur on that side starts this close
FONT, SPUR_FONT = 11, 9.5
CHAR_W = 0.6             # text width per character, as a share of the font size
NAME_MAX_CHARS = 30
LABEL_OFFSET = DOT_R + 4
DIAG = 1 / math.sqrt(2)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def to_xy(lat: float, lng: float) -> tuple[float, float]:
    """Metres east (x) and north (y) of the reference point."""
    return ((lng - REF_LNG) * M_PER_DEG * math.cos(math.radians(REF_LAT)),
            (lat - REF_LAT) * M_PER_DEG)


def build_route(latlngs: list[tuple[float, float]]) -> tuple[list[tuple[float, float]], list[float]]:
    """Route polyline in metres plus the cumulative distance at each vertex."""
    pts = [to_xy(lat, lng) for lat, lng in latlngs]
    cum = [0.0]
    for (ax, ay), (bx, by) in pairwise(pts):
        cum.append(cum[-1] + math.hypot(bx - ax, by - ay))
    return pts, cum


def locate(route, lat: float, lng: float) -> tuple[float, float, float]:
    """(along_m, dist_m, signed_m) of a point against its nearest route segment.

    signed_m is the cross product over the segment length: positive left of the
    direction of travel, negative right.
    """
    pts, cum = route
    px, py = to_xy(lat, lng)
    best = None
    for i, ((ax, ay), (bx, by)) in enumerate(pairwise(pts)):
        vx, vy = bx - ax, by - ay
        seg = math.hypot(vx, vy)
        if seg == 0:
            continue
        t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / (seg * seg)))
        dist = math.hypot(px - ax - t * vx, py - ay - t * vy)
        if best is None or dist < best[1]:
            best = (cum[i] + t * seg, dist, (vx * (py - ay) - vy * (px - ax)) / seg)
    return best


def side_of(signed_m: float) -> str:
    """'left' (west, drawn above the road) or 'right' (east, below)."""
    return "left" if signed_m >= 0 else "right"


def spur_px(dist_m: float) -> float:
    """Spur length on the map, proportional to the distance from the road."""
    return max(SPUR_MIN_PX, min(SPUR_MAX_PX, dist_m / 1000 * SPUR_PX_PER_KM))


def washroom_bucket(reviews: int, mean: float | None) -> str:
    if not reviews or mean is None:
        return "none"
    if mean >= GOOD_MIN:
        return "good"
    if mean >= BAD_MAX:
        return "moderate"
    return "bad"


# ---------------------------------------------------------------------------
# Collision-free layout. Shapes are capsules (x1, y1, x2, y2, radius) in
# side-local coordinates: x along the map, y away from the road edge.
# ---------------------------------------------------------------------------

def text_width(text: str, size: float) -> float:
    return len(text) * size * CHAR_W


def _pt_seg(px, py, x1, y1, x2, y2) -> float:
    vx, vy = x2 - x1, y2 - y1
    l2 = vx * vx + vy * vy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - x1) * vx + (py - y1) * vy) / l2))
    return math.hypot(px - x1 - t * vx, py - y1 - t * vy)


def seg_dist(a, b) -> float:
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    a1, a2, b1, b2 = a[0:2], a[2:4], b[0:2], b[2:4]
    if orient(a1, a2, b1) * orient(a1, a2, b2) < 0 and orient(b1, b2, a1) * orient(b1, b2, a2) < 0:
        return 0.0
    return min(_pt_seg(*a1, *b1, *b2), _pt_seg(*a2, *b1, *b2), _pt_seg(*b1, *a1, *a2), _pt_seg(*b2, *a1, *a2))


def collides(shapes, placed) -> bool:
    for s in shapes:
        for p in placed:
            pad = s[4] + p[4]
            if max(s[0], s[2]) + pad < min(p[0], p[2]) or max(p[0], p[2]) + pad < min(s[0], s[2]):
                continue
            if seg_dist(s, p) < pad:
                return True
    return False


def spur_geometry(rx: float, fan: float, h: float):
    """Dot centre, then the spur label's centre and the spur's unit direction."""
    dx, dy = rx + fan, EDGE_GAP + DOT_R + h
    length = math.hypot(fan, dy)
    ux, uy = fan / length, dy / length
    cx, cy = rx + fan / 2 - uy * 8, dy / 2 + ux * 8      # 8 px to the left of the spur
    return (dx, dy), (cx, cy), (ux, uy)


def station_shapes(st: dict, rx: float, fan: float) -> list[tuple]:
    (dx, dy), (cx, cy), (ux, uy) = spur_geometry(rx, fan, st["spur_px"])
    shapes = [(dx, dy, dx, dy, DOT_R + 3)]
    if st["spur_px"]:
        half = text_width(st["spur_label"], SPUR_FONT) / 2
        shapes += [(rx, 0, dx, dy, 2),
                   (cx - ux * half, cy - uy * half, cx + ux * half, cy + uy * half, SPUR_FONT / 2 + 1)]
    sx, sy = dx + LABEL_OFFSET * DIAG, dy + LABEL_OFFSET * DIAG
    w = text_width(st["label"], FONT)
    shapes.append((sx, sy, sx + w * DIAG, sy + w * DIAG, FONT / 2 + 2))
    return shapes


def layout(stations: list[dict]) -> None:
    """Give each station (sorted by route distance) rx (road x), fan and shapes.

    Each station starts at its real distance and is pushed right only as far
    as needed; a detour close to the previous spur on its side first tries
    fanning its spur end out before moving.
    """
    placed = {"left": [], "right": []}
    last_spur_rx = {"left": -math.inf, "right": -math.inf}
    last_rx = -math.inf
    for st in stations:
        rx = max(X0 + st["along_m"] / 1000 * PX_PER_KM, last_rx + MIN_ROAD_GAP)
        near_spur = st["spur_px"] and rx - last_spur_rx[st["side"]] < FAN_NEAR_PX
        fans = range(0, MAX_FAN_PX + 1, 4) if near_spur else (0,)
        while True:
            fit = next(((fan, shapes) for fan in fans
                        if not collides(shapes := station_shapes(st, rx, fan), placed[st["side"]])), None)
            if fit:
                break
            rx += STEP_PX
        st["rx"], (st["fan"], st["shapes"]) = rx, fit
        placed[st["side"]].extend(st["shapes"])
        if st["spur_px"]:
            last_spur_rx[st["side"]] = rx
        last_rx = rx


def km_to_x(km: float, anchors: list[tuple[float, float]]) -> float:
    """Piecewise-linear map from route km to x, through the placed stations."""
    for (k0, x0), (k1, x1) in pairwise(anchors):
        if k0 <= km <= k1:
            return x0 if k1 == k0 else x0 + (x1 - x0) * (km - k0) / (k1 - k0)
    return anchors[-1][1]


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def fetch_route(offline: bool) -> tuple[list[tuple[float, float]], str]:
    if not offline:
        try:
            with urllib.request.urlopen(OSRM_URL.format(a=START, b=END), timeout=20) as resp:
                coords = json.load(resp)["routes"][0]["geometry"]["coordinates"]
            return [(lat, lng) for lng, lat in coords], "OSRM driving route"
        except (OSError, ValueError, KeyError, IndexError) as exc:
            print(f"OSRM unavailable ({exc}); using the fixed waypoints.")
    return FALLBACK_WAYPOINTS, "fixed waypoints"


def _float(value: str) -> float | None:
    return float(value) if value not in ("", None) else None


def load_stations(route) -> list[dict]:
    summary, mentions = {}, {}
    if SUMMARY_CSV.exists():
        with SUMMARY_CSV.open(encoding="utf-8-sig", newline="") as f:
            summary = {r["data_id"]: r for r in csv.DictReader(f)}
    if MENTIONS_CSV.exists():
        with MENTIONS_CSV.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                mentions.setdefault(r["data_id"], []).append(r)

    route_len = route[1][-1]
    stations = []
    with RESTAURANTS_CSV.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("latitude") and r.get("longitude"):
                lat, lng = float(r["latitude"]), float(r["longitude"])
            else:
                lat, lng = map(float, re.search(r"query=(-?[\d.]+),(-?[\d.]+)", r["Google Maps link"]).groups())
            along, dist, signed = locate(route, lat, lng)
            csv_detour = r["Position"].lower().startswith("short detour")
            detour = csv_detour or dist > DETOUR_MIN_M
            checks = []
            if not csv_detour and dist > DETOUR_MIN_M:
                checks.append(f"listed as on the highway but {dist / 1000:.2f} km from the route")
            if route_len - along < ROUTE_END_M:
                checks.append("at the end of the route, so its km and side are measured past the route line")
            w = summary.get(r["data_id"], {})
            reviews = int(w.get("washroom_reviews") or 0)
            mean = _float(w.get("mean_sentiment_score", ""))
            name = r["Restaurant"].strip()
            stations.append({
                "num": int(r["#"]), "name": name, "data_id": r["data_id"],
                "label": name if len(name) <= NAME_MAX_CHARS else name[:NAME_MAX_CHARS - 1] + "…",
                "rating": r["Google rating"], "cuisine": r["Cuisine"], "position": r["Position"],
                "google_reviews": int(r["No. of reviews"] or 0), "maps_url": r["Google Maps link"],
                "veg": bool(re.search(r"(?<!non-)\bveg\b", f"{name} {r['Cuisine']}", re.I)),
                "position_check": bool(checks), "position_note": "; ".join(checks),
                "lat": lat, "lng": lng,
                "along_m": along, "dist_m": dist, "signed_m": signed, "side": side_of(signed),
                "detour": detour,
                "spur_px": spur_px(dist) if detour else 0,
                "spur_label": f"{dist / 1000:.1f} km",
                "reviews": reviews, "positive": int(w.get("positive") or 0),
                "neutral": int(w.get("neutral") or 0), "negative": int(w.get("negative") or 0),
                "mean": mean, "low_data": w.get("low_data", "True") == "True",
                "bucket": washroom_bucket(reviews, mean),
                "snippets": pick_snippets(mentions.get(r["data_id"], []), limit=3),
            })
    return sorted(stations, key=lambda s: (s["along_m"], s["num"]))


def pick_snippets(rows: list[dict], limit: int = 3) -> list[dict]:
    """The most confident snippet, then the most confident one of another sentiment
    if any, then the next most confident ones."""
    rows = sorted(rows, key=lambda r: -float(r.get("confidence") or 0))
    picked = rows[:1] + [r for r in rows[1:] if r["sentiment"] != rows[0]["sentiment"]][:1]
    picked += [r for r in rows if r not in picked][:max(0, limit - len(picked))]
    return [{"text": r["washroom_snippet"][:240] + ("…" if len(r["washroom_snippet"]) > 240 else ""),
             "sentiment": r["sentiment"], "date": (r.get("iso_date") or "")[:10]} for r in picked[:limit]]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_svg(stations: list[dict], route_len_m: float, route) -> tuple[str, int]:
    esc = html.escape
    total_km = route_len_m / 1000
    last = stations[-1]["rx"]
    end_x = max(X0 + total_km * PX_PER_KM, last) + 30
    anchors = [(0.0, X0)] + [(s["along_m"] / 1000, s["rx"]) for s in stations] + [(total_km, end_x)]

    def extent(side):
        return max((max(sh[1], sh[3]) + sh[4] for s in stations if s["side"] == side for sh in s["shapes"]),
                   default=0)

    road_top = 24 + extent("left")
    road_mid, road_bottom = road_top + LANE_H, road_top + 2 * LANE_H
    axis_y = road_bottom + extent("right") + 24
    height = int(axis_y + 56)
    width = int(max([end_x + 130] + [max(sh[0], sh[2]) + sh[4] + 20 for s in stations for sh in s["shapes"]]))

    def to_svg(side, x, y):
        return x, (road_top - y if side == "left" else road_bottom + y)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}" role="img" aria-label="Route map of restaurants from '
           f'Phoenix Mall of Asia to Big Bay">']

    # landmark guide lines and km axis (behind everything)
    marks = [(name, km_to_x(locate(route, lat, lng)[0] / 1000, anchors)) for name, lat, lng in LANDMARKS]
    for name, x in marks:
        out.append(f'<line x1="{x:.1f}" y1="{road_bottom}" x2="{x:.1f}" y2="{axis_y}" class="guide"/>')
    out.append(f'<line x1="{X0}" y1="{axis_y}" x2="{end_x:.1f}" y2="{axis_y}" class="axis"/>')
    for km in range(0, int(total_km) + 1, 10):
        x = km_to_x(km, anchors)
        out.append(f'<line x1="{x:.1f}" y1="{axis_y}" x2="{x:.1f}" y2="{axis_y + 6}" class="axis"/>'
                   f'<text x="{x:.1f}" y="{axis_y + 18}" class="tick">{km} km</text>')
    for name, x in marks:
        out.append(f'<text x="{x:.1f}" y="{axis_y + 38}" class="landmark-name">{esc(name)}</text>')

    # road: two lanes and a dashed median
    out.append(f'<rect x="{X0}" y="{road_top}" width="{end_x - X0:.1f}" height="{2 * LANE_H}" class="road"/>'
               f'<line x1="{X0}" y1="{road_top + 1.5}" x2="{end_x:.1f}" y2="{road_top + 1.5}" class="edge"/>'
               f'<line x1="{X0}" y1="{road_bottom - 1.5}" x2="{end_x:.1f}" y2="{road_bottom - 1.5}" class="edge"/>'
               f'<line x1="{X0}" y1="{road_mid}" x2="{end_x:.1f}" y2="{road_mid}" class="median"/>')
    # lane names in the two widest gaps between stations and landmarks
    xs = sorted([s["rx"] for s in stations] + [x for _, x in marks] + [X0, end_x])
    gaps = sorted(pairwise(xs), key=lambda g: g[0] - g[1])[:2]
    for a, b in gaps:
        cx = (a + b) / 2
        out.append(f'<text x="{cx:.1f}" y="{road_top + LANE_H / 2}" class="lane">'
                   f'Northbound lane · towards Big Bay ▶</text>'
                   f'<text x="{cx:.1f}" y="{road_mid + LANE_H / 2}" class="lane">'
                   f'◀ Southbound lane · towards Mall of Asia</text>')

    # terminals and landmark interchanges
    out.append(f'<circle cx="{X0}" cy="{road_mid}" r="14" class="terminal"/>'
               f'<circle cx="{end_x:.1f}" cy="{road_mid}" r="14" class="terminal"/>'
               f'<text x="{X0 - 22}" y="{road_mid - 7}" class="terminal-name" text-anchor="end">Phoenix</text>'
               f'<text x="{X0 - 22}" y="{road_mid + 9}" class="terminal-name" text-anchor="end">Mall of Asia</text>'
               f'<text x="{end_x + 22:.1f}" y="{road_mid - 7}" class="terminal-name">Big Bay</text>'
               f'<text x="{end_x + 22:.1f}" y="{road_mid + 9}" class="terminal-sub">{total_km:.1f} km</text>')
    for name, x in marks:
        out.append(f'<circle cx="{x:.1f}" cy="{road_mid}" r="10" class="interchange"><title>{esc(name)}</title></circle>')

    # stations
    for i, s in enumerate(stations):
        side, rx = s["side"], s["rx"]
        (dx, dy), (cx, cy), (ux, uy) = spur_geometry(rx, s["fan"], s["spur_px"])
        g = [f'<g class="st" data-i="{i}" tabindex="0" role="button" aria-label="{esc(s["name"])}">']
        if s["spur_px"]:
            x1, y1 = to_svg(side, rx, -1)
            x2, y2 = to_svg(side, dx, dy)
            sx, sy = to_svg(side, cx, cy)
            angle = math.degrees(math.atan2(-uy if side == "left" else uy, ux))
            g.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" class="spur"/>'
                     f'<text x="{sx:.1f}" y="{sy:.1f}" class="spur-label" '
                     f'transform="rotate({angle:.1f} {sx:.1f} {sy:.1f})">{esc(s["spur_label"])}</text>')
        px, py = to_svg(side, dx, dy)
        g.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="13" class="hit"/>')
        if s["bucket"] == "none":
            g.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{DOT_R - 1}" class="dot hollow"/>')
        else:
            g.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{DOT_R}" class="dot" fill="{COLOURS[s["bucket"]]}"/>')
            if s["low_data"]:
                g.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{DOT_R + 3.5}" class="ring"/>')
        lx, ly = to_svg(side, dx + LABEL_OFFSET * DIAG, dy + LABEL_OFFSET * DIAG)
        g.append(f'<text x="{lx:.1f}" y="{ly:.1f}" class="name" '
                 f'transform="rotate({-45 if side == "left" else 45} {lx:.1f} {ly:.1f})">{esc(s["label"])}</text>')
        out.append("".join(g) + "</g>")
    out.append("</svg>")
    return "\n".join(out), width


def tooltip_data(s: dict) -> dict:
    side = ("West: left of the direction of travel, drawn above the road" if s["side"] == "left"
            else "East: right of the direction of travel, drawn below the road")
    where = (f"Detour: {s['dist_m'] / 1000:.1f} km from the road" if s["detour"]
             else f"On the highway ({s['dist_m']:.0f} m from the route line)")
    return {"name": s["name"], "rating": s["rating"], "cuisine": s["cuisine"], "km": round(s["along_m"] / 1000, 1),
            "reviews": s["reviews"], "positive": s["positive"], "neutral": s["neutral"], "negative": s["negative"],
            "mean": s["mean"], "low_data": s["low_data"], "bucket": BUCKET_NAMES[s["bucket"]],
            "colour": COLOURS[s["bucket"]], "side": side, "where": where, "snippets": s["snippets"][:2]}


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pitstop Route Map</title>
<style>
  :root { --ink:#1f2328; --muted:#5f6670; --road:#4a4f5a; --bg:#fbfbf8; --panel:#ffffff; --line:#d8dadf; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 system-ui, "Segoe UI", Roboto, sans-serif; }
  header { padding:16px 16px 4px; max-width:1100px; }
  h1 { font-size:20px; margin:0 0 4px; }
  header p { margin:0 0 10px; color:var(--muted); }
  .legend { display:flex; flex-wrap:wrap; gap:6px 18px; padding:0; margin:0 0 8px; list-style:none; font-size:13px; }
  .legend li { display:flex; align-items:center; gap:6px; }
  .map-scroll { overflow-x:auto; overflow-y:hidden; -webkit-overflow-scrolling:touch; border-top:1px solid var(--line); border-bottom:1px solid var(--line); background:var(--panel); }
  .map-scroll svg { display:block; }
  svg text { font-family:system-ui, "Segoe UI", Roboto, sans-serif; }
  .road { fill:var(--road); }
  .edge { stroke:#fff; stroke-width:1; opacity:.7; }
  .median { stroke:#f4d35e; stroke-width:2; stroke-dasharray:9 7; }
  .lane { fill:#fff; font-size:9.5px; text-anchor:middle; dominant-baseline:central; opacity:.9; }
  .terminal { fill:#fff; stroke:var(--ink); stroke-width:4; }
  .terminal-name { font-size:14px; font-weight:700; fill:var(--ink); dominant-baseline:central; }
  .terminal-sub { font-size:11px; fill:var(--muted); dominant-baseline:central; }
  .interchange { fill:#fff; stroke:var(--ink); stroke-width:3; }
  .guide { stroke:#b9bdc4; stroke-dasharray:2 4; }
  .axis { stroke:var(--muted); }
  .tick { font-size:11px; fill:var(--muted); text-anchor:middle; }
  .landmark-name { font-size:12px; font-weight:700; fill:var(--ink); text-anchor:middle; }
  .spur { stroke:var(--road); stroke-width:3; stroke-linecap:round; }
  .spur-label { font-size:9.5px; fill:var(--ink); text-anchor:middle; dominant-baseline:central;
                paint-order:stroke; stroke:#fff; stroke-width:3px; }
  .name { font-size:11px; fill:var(--ink); dominant-baseline:central; paint-order:stroke; stroke:#fff; stroke-width:3px; }
  .dot { stroke:var(--ink); stroke-width:1.5; }
  .dot.hollow { fill:#fff; stroke:#9aa0a6; stroke-width:2.5; }
  .ring { fill:none; stroke:var(--ink); stroke-width:1; stroke-dasharray:2 2; }
  .hit { fill:transparent; }
  .st { cursor:pointer; outline:none; }
  .st:hover .name, .st:focus .name, .st.active .name { font-weight:700; }
  .st:focus-visible .hit, .st.active .hit { stroke:#2563eb; stroke-width:2; }
  #tip { position:fixed; z-index:10; max-width:340px; background:var(--panel); border:1px solid var(--line);
         border-radius:8px; box-shadow:0 6px 24px rgba(0,0,0,.15); padding:10px 12px; font-size:13px; display:none; }
  #tip h2 { font-size:15px; margin:0 0 4px; }
  #tip p { margin:2px 0; }
  #tip .muted { color:var(--muted); }
  #tip .sw { display:inline-block; width:10px; height:10px; border-radius:50%; border:1px solid var(--ink); margin-right:4px; vertical-align:-1px; }
  #tip blockquote { margin:6px 0 0; padding-left:8px; border-left:3px solid var(--line); color:var(--ink); font-style:italic; }
  #tip blockquote span { font-style:normal; color:var(--muted); font-size:12px; }
</style>
</head>
<body>
<header>
  <h1>Pitstop: Mall of Asia to Big Bay</h1>
  <p>__COUNT__ restaurants on the highway, in route order (__SOURCE__, __KM__ km). Hover, tap or focus a station for details. Scroll sideways to follow the road.</p>
  <ul class="legend">
    <li><svg width="16" height="16"><circle cx="8" cy="8" r="6" fill="__GOOD__" stroke="#1f2328" stroke-width="1.5"/></svg>Good washroom (score &ge; __GOOD_MIN__)</li>
    <li><svg width="16" height="16"><circle cx="8" cy="8" r="6" fill="__MOD__" stroke="#1f2328" stroke-width="1.5"/></svg>Moderate (__BAD_MAX__ to __GOOD_MIN__)</li>
    <li><svg width="16" height="16"><circle cx="8" cy="8" r="6" fill="__BAD__" stroke="#1f2328" stroke-width="1.5"/></svg>Bad (&lt; __BAD_MAX__)</li>
    <li><svg width="16" height="16"><circle cx="8" cy="8" r="5" fill="#fff" stroke="#9aa0a6" stroke-width="2.5"/></svg>No washroom reviews</li>
    <li><svg width="24" height="24"><circle cx="12" cy="12" r="6" fill="__MOD__" stroke="#1f2328" stroke-width="1.5"/><circle cx="12" cy="12" r="9.5" fill="none" stroke="#1f2328" stroke-dasharray="2 2"/></svg>Dashed ring: fewer than 5 washroom reviews, colour tentative</li>
    <li><svg width="56" height="22"><rect x="0" y="3" width="56" height="16" fill="#4a4f5a"/><line x1="0" y1="11" x2="56" y2="11" stroke="#f4d35e" stroke-width="2" stroke-dasharray="6 4"/></svg>Top lane northbound, bottom lane southbound (keep left)</li>
    <li><svg width="26" height="30"><rect x="0" y="22" width="26" height="8" fill="#4a4f5a"/><line x1="13" y1="22" x2="13" y2="8" stroke="#4a4f5a" stroke-width="3"/><circle cx="13" cy="6" r="5" fill="__GOOD__" stroke="#1f2328" stroke-width="1.5"/></svg>Spur: detour, length grows with distance from the road (labelled in km)</li>
    <li><svg width="22" height="22"><circle cx="11" cy="11" r="8" fill="#fff" stroke="#1f2328" stroke-width="3"/></svg>Landmark</li>
    <li>Above the road: west side &middot; below: east side</li>
  </ul>
</header>
<div class="map-scroll">
__SVG__
</div>
<div id="tip" role="tooltip"></div>
<script>
const DATA = __DATA__;
const tip = document.getElementById("tip");
let pinned = null;
function el(tag, text, cls) { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; }
function fill(d) {
  tip.replaceChildren();
  tip.append(el("h2", d.name));
  tip.append(el("p", "Google rating " + d.rating + " \\u2605 \\u00b7 " + d.cuisine, "muted"));
  const w = el("p"); const sw = el("span", null, "sw"); sw.style.background = d.colour;
  w.append(sw, "Washroom: " + d.bucket + (d.mean == null ? "" : " (mean score " + (d.mean > 0 ? "+" : "") + d.mean.toFixed(2) + ")"));
  tip.append(w);
  tip.append(el("p", d.reviews + " washroom review" + (d.reviews == 1 ? "" : "s") + ": " + d.positive + " positive / " + d.neutral + " neutral / " + d.negative + " negative"
    + (d.low_data && d.reviews ? " (low data, colour tentative)" : "")));
  tip.append(el("p", d.side));
  tip.append(el("p", d.where + " \\u00b7 route km " + d.km.toFixed(1)));
  for (const s of d.snippets) { const q = el("blockquote", "\\u201c" + s.text + "\\u201d "); q.append(el("span", "(" + s.sentiment + ")")); tip.append(q); }
}
function show(g) {
  fill(DATA[g.dataset.i]);
  tip.style.display = "block";
  const r = g.querySelector(".dot").getBoundingClientRect();
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let x = r.right + 12, y = r.top - 10;
  if (x + tw > innerWidth - 8) x = Math.max(8, r.left - tw - 12);
  if (y + th > innerHeight - 8) y = Math.max(8, innerHeight - th - 8);
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
function hide() { if (!pinned) tip.style.display = "none"; }
document.querySelectorAll(".st").forEach(g => {
  g.addEventListener("mouseenter", () => { if (!pinned) show(g); });
  g.addEventListener("mouseleave", hide);
  g.addEventListener("focus", () => { if (!pinned) show(g); });
  g.addEventListener("blur", hide);
  g.addEventListener("click", e => {
    e.stopPropagation();
    if (pinned) pinned.classList.remove("active");
    pinned = pinned === g ? null : g;
    if (pinned) { g.classList.add("active"); show(g); } else hide();
  });
  g.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); g.dispatchEvent(new MouseEvent("click")); } });
});
document.addEventListener("click", () => { if (pinned) { pinned.classList.remove("active"); pinned = null; hide(); } });
document.addEventListener("keydown", e => { if (e.key === "Escape" && pinned) { pinned.classList.remove("active"); pinned = null; hide(); } });
document.querySelector(".map-scroll").addEventListener("scroll", () => { if (!pinned) tip.style.display = "none"; });
</script>
</body>
</html>
"""


def render_html(svg: str, stations: list[dict], source: str, route_len_m: float) -> str:
    data = json.dumps([tooltip_data(s) for s in stations], ensure_ascii=False).replace("</", "<\\/")
    subs = {"__SVG__": svg, "__DATA__": data, "__COUNT__": str(len(stations)), "__SOURCE__": source,
            "__KM__": f"{route_len_m / 1000:.1f}", "__GOOD__": COLOURS["good"], "__MOD__": COLOURS["moderate"],
            "__BAD__": COLOURS["bad"], "__GOOD_MIN__": f"{GOOD_MIN:g}", "__BAD_MAX__": f"{BAD_MAX:g}"}
    page = PAGE
    for key, value in subs.items():
        page = page.replace(key, value)
    return page


def screenshot(html_path: Path, png_path: Path, width: int) -> bool:
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        print("Playwright is not installed; skipped route_map.png (pip install playwright).")
        return False
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="msedge")
        except Error:
            browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 800})
        page.goto(html_path.resolve().as_uri())
        page.screenshot(path=str(png_path), full_page=True)
        browser.close()
    return True


def site_data(stations: list[dict], route, source: str) -> dict:
    """Everything site/index.html needs, as plain JSON values."""
    route_len = route[1][-1]
    landmarks = [{"name": "Phoenix Mall of Asia", "km": 0.0, "terminal": True}]
    landmarks += [{"name": name, "km": round(locate(route, lat, lng)[0] / 1000, 2), "terminal": False}
                  for name, lat, lng in LANDMARKS]
    landmarks.append({"name": "Big Bay", "km": round(route_len / 1000, 2), "terminal": True})
    return {
        "route_km": round(route_len / 1000, 2),
        "route_source": source,
        "thresholds": {"good_min": GOOD_MIN, "bad_max": BAD_MAX, "low_data_reviews": 5,
                       "detour_min_m": DETOUR_MIN_M},
        "landmarks": landmarks,
        "stations": [{
            "id": s["num"], "name": s["name"], "data_id": s["data_id"],
            "km": round(s["along_m"] / 1000, 2),
            "side": "west" if s["side"] == "left" else "east",
            "detour": s["detour"],
            "detour_km": round(s["dist_m"] / 1000, 2) if s["detour"] else None,
            "dist_m": round(s["dist_m"]),
            "rating": float(s["rating"]) if s["rating"] else None,
            "google_reviews": s["google_reviews"], "cuisine": s["cuisine"], "veg": s["veg"],
            "washroom": {"reviews": s["reviews"], "positive": s["positive"], "neutral": s["neutral"],
                         "negative": s["negative"], "mean": s["mean"]},
            "grade": s["bucket"], "low_data": s["low_data"],
            "snippets": s["snippets"], "maps_url": s["maps_url"],
            "position_check": s["position_check"], "position_note": s["position_note"],
        } for s in stations],
    }


def write_site_data(data: dict, path: Path = SITE_DATA_OUT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("// Generated by scripts/build_route_map.py. Do not edit by hand.\n"
                    "window.ROUTE_DATA = " + json.dumps(data, ensure_ascii=False, indent=1) + ";\n",
                    encoding="utf-8")


def report(stations: list[dict], source: str, route_len_m: float) -> None:
    print(f"Route: {source}, {route_len_m / 1000:.1f} km, {len(stations)} restaurants")
    for side, label in (("left", "West / above the road"), ("right", "East / below the road")):
        print(f"  {label:24}: {sum(s['side'] == side for s in stations)}")
    ambiguous = [s for s in stations if abs(s["signed_m"]) < AMBIGUOUS_M]
    print(f"\nAmbiguous side (within {AMBIGUOUS_M} m of the centre line): {len(ambiguous)}")
    for s in ambiguous:
        print(f"  #{s['num']:<3} {s['name'][:40]:40} {s['signed_m']:+6.1f} m -> {s['side']}, km {s['along_m'] / 1000:.1f}")
    detours = [s for s in stations if s["detour"]]
    print(f"\nDetours: {len(detours)}")
    print(f"  {'#':<4}{'Restaurant':40} {'side':6} {'route km':>8} {'from road':>10}  CSV position")
    for s in detours:
        print(f"  {s['num']:<4}{s['name'][:40]:40} {s['side']:6} {s['along_m'] / 1000:8.1f} "
              f"{s['dist_m'] / 1000:8.2f} km  {s['position']}")
    checks = [s for s in stations if s["position_check"]]
    print(f"\nPosition not verified (fix the source CSV): {len(checks)}")
    for s in checks:
        print(f"  #{s['num']:<3} {s['name'][:40]:40} km {s['along_m'] / 1000:5.1f}  {s['position_note']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--offline", action="store_true", help="skip OSRM, use the fixed waypoints")
    parser.add_argument("--no-png", action="store_true", help="skip the Playwright screenshot")
    args = parser.parse_args(argv)

    latlngs, source = fetch_route(args.offline)
    route = build_route(latlngs)
    route_len = route[1][-1]
    stations = load_stations(route)
    layout(stations)
    svg, width = render_svg(stations, route_len, route)
    utils.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    HTML_OUT.write_text(render_html(svg, stations, source, route_len), encoding="utf-8")
    print(f"Wrote {utils.to_project_relative(HTML_OUT)}")
    write_site_data(site_data(stations, route, source))
    print(f"Wrote {utils.to_project_relative(SITE_DATA_OUT)}")
    if not args.no_png and screenshot(HTML_OUT, PNG_OUT, width):
        print(f"Wrote {utils.to_project_relative(PNG_OUT)}")
    print()
    report(stations, source, route_len)
    return 0


if __name__ == "__main__":
    sys.exit(main())
