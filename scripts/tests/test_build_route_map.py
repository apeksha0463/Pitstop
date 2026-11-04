"""
scripts/tests/test_build_route_map.py
=====================================
Offline tests for scripts/build_route_map.py: side of the road, route order,
distance from the road and spur length, washroom colour buckets, a
collision-free layout of a tight cluster, and the site/data.js export
(every station, required fields, valid grades, position_check flags).
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent      # scripts/tests/
_SCRIPTS = _HERE.parent                      # scripts/
_ROOT = _SCRIPTS.parent                      # repository root
for _p in (str(_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from scripts import build_route_map as rm
except ImportError:
    import build_route_map as rm

# Due north along one meridian, then north-east
NORTH = rm.build_route([(13.0, 77.6), (13.2, 77.6)])
BENT = rm.build_route([(13.0, 77.6), (13.1, 77.6), (13.2, 77.7)])
KM_PER_DEG_LNG = rm.M_PER_DEG * rm.math.cos(rm.math.radians(rm.REF_LAT)) / 1000


class TestSide(unittest.TestCase):

    def test_west_of_northbound_road_is_left_and_drawn_above(self):
        _, _, signed = rm.locate(NORTH, 13.1, 77.59)
        self.assertGreater(signed, 0)
        self.assertEqual(rm.side_of(signed), "left")

    def test_east_of_northbound_road_is_right_and_drawn_below(self):
        _, _, signed = rm.locate(NORTH, 13.1, 77.61)
        self.assertLess(signed, 0)
        self.assertEqual(rm.side_of(signed), "right")

    def test_side_uses_the_nearest_segment(self):
        # north-west of the north-east leg: left of travel on that leg
        self.assertEqual(rm.side_of(rm.locate(BENT, 13.16, 77.64)[2]), "left")
        self.assertEqual(rm.side_of(rm.locate(BENT, 13.14, 77.66)[2]), "right")


class TestOrdering(unittest.TestCase):

    def test_along_distance_follows_the_route_not_the_latitude(self):
        points = {"c": (13.18, 77.681), "a": (13.02, 77.601), "b": (13.1, 77.599)}
        order = sorted(points, key=lambda k: rm.locate(BENT, *points[k])[0])
        self.assertEqual(order, ["a", "b", "c"])

    def test_along_distance_of_a_point_on_the_route(self):
        along, dist, _ = rm.locate(NORTH, 13.1, 77.6)
        self.assertAlmostEqual(along, 0.1 * rm.M_PER_DEG, delta=1)
        self.assertAlmostEqual(dist, 0, delta=0.01)


class TestSpur(unittest.TestCase):

    def test_distance_is_perpendicular_to_the_road(self):
        _, dist, _ = rm.locate(NORTH, 13.1, 77.61)
        self.assertAlmostEqual(dist / 1000, 0.01 * KM_PER_DEG_LNG, delta=0.005)

    def test_spur_length_is_proportional_and_clamped(self):
        self.assertEqual(rm.spur_px(10), rm.SPUR_MIN_PX)
        self.assertEqual(rm.spur_px(50_000), rm.SPUR_MAX_PX)
        self.assertAlmostEqual(rm.spur_px(2000), 2 * rm.SPUR_PX_PER_KM)
        self.assertLess(rm.spur_px(1200), rm.spur_px(2000))


class TestColourBuckets(unittest.TestCase):

    def test_thresholds(self):
        self.assertEqual(rm.washroom_bucket(5, 0.3), "good")
        self.assertEqual(rm.washroom_bucket(5, 0.29), "moderate")
        self.assertEqual(rm.washroom_bucket(5, -0.3), "moderate")
        self.assertEqual(rm.washroom_bucket(5, -0.31), "bad")

    def test_no_reviews_is_grey(self):
        self.assertEqual(rm.washroom_bucket(0, None), "none")
        self.assertEqual(rm.washroom_bucket(0, 0.9), "none")


class TestLayout(unittest.TestCase):

    def test_tight_cluster_has_no_collisions_and_keeps_order(self):
        stations = [{"along_m": 1000 + i, "side": "left", "label": f"Restaurant number {i}",
                     "spur_px": rm.spur_px(1200) if i % 2 else 0, "spur_label": "1.2 km"}
                    for i in range(6)]
        rm.layout(stations)
        xs = [s["rx"] for s in stations]
        self.assertEqual(xs, sorted(xs))
        for i, a in enumerate(stations):
            for b in stations[i + 1:]:
                self.assertFalse(rm.collides(a["shapes"], b["shapes"]))
        self.assertTrue(all(s["fan"] <= rm.MAX_FAN_PX for s in stations))


STATION_FIELDS = {
    "id", "name", "data_id", "km", "side", "detour", "detour_km", "dist_m", "rating", "google_reviews",
    "cuisine", "veg", "washroom", "grade", "low_data", "snippets", "maps_url", "position_check", "position_note",
}
CSV_COLUMNS = ["#", "Restaurant", "data_id", "Location", "Approx km from Mall of Asia (straight line)",
               "Google rating", "No. of reviews", "Cuisine", "Position", "Google Maps link"]


def read_data_js(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    prefix = "window.ROUTE_DATA = "
    assert prefix in text, "data.js must assign window.ROUTE_DATA"
    return json.loads(text[text.index(prefix) + len(prefix):].rstrip().rstrip(";"))


class TestSiteDataExport(unittest.TestCase):
    """site_data() for a small CSV in a temporary folder, on the straight NORTH route."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        rows = [  # number, name, lat, lng, CSV position
            (1, "Near West Veg", 13.05, 77.5995, "On the highway"),         # 50 m west: fine
            (2, "Far Highway", 13.08, 77.61, "On the highway"),             # 1.08 km east: flagged
            (3, "Real Detour", 13.10, 77.61, "Short detour (1-2 km)"),      # a detour as listed: fine
            (4, "Route End", 13.20, 77.6002, "On the highway"),             # end of the route: flagged
        ]
        self.csv = root / "restaurants.csv"
        with self.csv.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(CSV_COLUMNS)
            for num, name, lat, lng, pos in rows:
                w.writerow([num, name, f"0x{num}:0x{num}", "", "", "4.2", "100",
                            "Veg meals" if "Veg" in name else "Biryani", pos,
                            f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"])
        summary = root / "summary.csv"
        with summary.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["data_id", "washroom_reviews", "positive", "neutral", "negative", "mean_sentiment_score", "low_data"])
            w.writerow(["0x1:0x1", 6, 5, 1, 0, 0.6, "False"])
            w.writerow(["0x2:0x2", 2, 0, 0, 2, -0.8, "True"])
        self.patches = [patch.object(rm, "RESTAURANTS_CSV", self.csv), patch.object(rm, "SUMMARY_CSV", summary),
                        patch.object(rm, "MENTIONS_CSV", root / "no_mentions.csv")]
        for p in self.patches:
            p.start()
        self.data = rm.site_data(rm.load_stations(NORTH), NORTH, "test route")
        self.by_name = {s["name"]: s for s in self.data["stations"]}

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_every_station_is_exported_with_the_required_fields(self):
        self.assertEqual(sorted(self.by_name), ["Far Highway", "Near West Veg", "Real Detour", "Route End"])
        for st in self.data["stations"]:
            self.assertEqual(set(st), STATION_FIELDS)
            self.assertEqual(set(st["washroom"]), {"reviews", "positive", "neutral", "negative", "mean"})
        self.assertAlmostEqual(self.data["route_km"], 0.2 * rm.M_PER_DEG / 1000, places=1)
        self.assertEqual(self.data["landmarks"][0]["name"], "Phoenix Mall of Asia")
        self.assertEqual(self.data["landmarks"][-1]["name"], "Big Bay")

    def test_values(self):
        west, far = self.by_name["Near West Veg"], self.by_name["Far Highway"]
        self.assertEqual((west["side"], west["detour"], west["detour_km"], west["veg"]), ("west", False, None, True))
        self.assertEqual((far["side"], far["detour"], far["veg"]), ("east", True, False))
        self.assertAlmostEqual(far["detour_km"], 0.01 * KM_PER_DEG_LNG, delta=0.01)
        self.assertEqual((west["grade"], far["grade"], self.by_name["Real Detour"]["grade"]), ("good", "bad", "none"))
        self.assertTrue(west["maps_url"].startswith("https://www.google.com/maps/"))

    def test_position_check_flags(self):
        flags = {name: s["position_check"] for name, s in self.by_name.items()}
        self.assertEqual(flags, {"Near West Veg": False, "Far Highway": True, "Real Detour": False, "Route End": True})
        self.assertIn("on the highway but", self.by_name["Far Highway"]["position_note"])
        self.assertIn("end of the route", self.by_name["Route End"]["position_note"])
        self.assertEqual(self.by_name["Near West Veg"]["position_note"], "")

    def test_data_js_is_a_window_assignment_of_the_json(self):
        out = Path(self.tmp.name) / "site" / "data.js"
        rm.write_site_data(self.data, out)
        self.assertEqual(read_data_js(out), json.loads(json.dumps(self.data)))


class TestGeneratedSiteData(unittest.TestCase):
    """The generated site/data.js: all 47 restaurants, valid values and the known position checks."""

    def setUp(self):
        if not rm.SITE_DATA_OUT.exists():
            self.skipTest("site/data.js not generated yet (python scripts/build_route_map.py)")
        self.data = read_data_js(rm.SITE_DATA_OUT)

    def test_every_restaurant_in_the_csv_is_a_station(self):
        with rm.RESTAURANTS_CSV.open(encoding="utf-8-sig", newline="") as f:
            ids = [r["data_id"] for r in csv.DictReader(f)]
        self.assertEqual(sorted(s["data_id"] for s in self.data["stations"]), sorted(ids))

    def test_fields_and_valid_values(self):
        for st in self.data["stations"]:
            self.assertEqual(set(st), STATION_FIELDS, st["name"])
            self.assertIn(st["grade"], {"good", "moderate", "bad", "none"})
            self.assertIn(st["side"], {"west", "east"})
            self.assertTrue(0 <= st["km"] <= self.data["route_km"])
            self.assertEqual(st["detour_km"] is not None, st["detour"])
            self.assertLessEqual(len(st["snippets"]), 3)
            for sn in st["snippets"]:
                self.assertEqual(set(sn), {"text", "sentiment", "date"})
            w = st["washroom"]
            self.assertEqual(w["positive"] + w["neutral"] + w["negative"], w["reviews"])
            self.assertEqual(st["grade"], rm.washroom_bucket(w["reviews"], w["mean"]))

    def test_position_checks_are_the_known_ones(self):
        flagged = {s["id"] for s in self.data["stations"] if s["position_check"]}
        self.assertEqual(flagged, {4, 5, 6, 27, 38, 44, 45, 46, 47})


if __name__ == "__main__":
    unittest.main()
