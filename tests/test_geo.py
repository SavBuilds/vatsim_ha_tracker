"""Tests for the plain-Python geometry.  python -m unittest discover -s tests -v"""
import importlib.util
import math
import os
import random
import time
import unittest

_spec = importlib.util.spec_from_file_location(
    "geo", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "custom_components", "vatsim_atc", "geo.py"))
geo = importlib.util.module_from_spec(_spec)
import sys
sys.modules["geo"] = geo
_spec.loader.exec_module(geo)

SQUARE_WITH_HOLE = {"type": "Polygon", "coordinates": [
    [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
    [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]]]}


def winding_inside(x, y, ring):
    """Independent reference: non-zero winding number by angle summation."""
    total = 0.0
    for (ax, ay), (bx, by) in zip(ring, ring[1:]):
        total += math.atan2((ax - x) * (by - y) - (ay - y) * (bx - x), (ax - x) * (bx - x) + (ay - y) * (by - y))
    return abs(total) > math.pi


class Spherical(unittest.TestCase):
    def test_one_degree_of_latitude(self):
        self.assertAlmostEqual(geo.distance_nm(0, 0, 1, 0), 60.04, delta=0.01)

    def test_quarter_circumference(self):
        self.assertAlmostEqual(geo.distance_nm(0, 0, 0, 90), math.pi / 2 * geo.EARTH_RADIUS_NM, places=6)

    def test_zero_and_symmetry(self):
        self.assertEqual(geo.distance_nm(42.0, -71.0, 42.0, -71.0), 0.0)
        self.assertAlmostEqual(geo.distance_nm(10, 20, -30, 140), geo.distance_nm(-30, 140, 10, 20), places=9)

    def test_destination_round_trip(self):
        rnd = random.Random(1)
        for _ in range(2000):
            lat, lon = rnd.uniform(-80, 80), rnd.uniform(-180, 180)
            brg, dist = rnd.uniform(0, 360), rnd.uniform(0.1, 600)
            lat2, lon2 = geo.destination(lat, lon, brg, dist)
            self.assertAlmostEqual(geo.distance_nm(lat, lon, lat2, lon2), dist, delta=1e-6)
            self.assertTrue(-180.0 <= lon2 <= 180.0)

    def test_destination_cardinal(self):
        lat, lon = geo.destination(0, 0, 0, 60.04)
        self.assertAlmostEqual(lat, 1.0, delta=0.001)
        self.assertAlmostEqual(lon, 0.0, places=9)
        lat, lon = geo.destination(0, 179.5, 90, 60.04)
        self.assertAlmostEqual(lon, -179.5, delta=0.001)  # wraps across the antimeridian


class PointInPolygon(unittest.TestCase):
    def setUp(self):
        self.b = geo.boundary_from_geojson(SQUARE_WITH_HOLE)

    def test_inside_outside_hole(self):
        self.assertTrue(self.b.covers(1, 1))
        self.assertFalse(self.b.covers(5, 5), "inside the hole is outside the sector")
        self.assertFalse(self.b.covers(11, 5))
        self.assertFalse(self.b.covers(-0.0001, 5))

    def test_edges_and_vertices_are_covered(self):
        for x, y in ((0, 5), (10, 10), (5, 0), (4, 5), (6, 6)):
            self.assertTrue(self.b.covers(x, y), (x, y))

    def test_multipolygon_and_unclosed_rings(self):
        b = geo.boundary_from_geojson({"type": "MultiPolygon", "coordinates": [
            [[[0, 0], [1, 0], [1, 1], [0, 1]]], [[[5, 5], [6, 5], [6, 6], [5, 6]]]]})
        self.assertTrue(b.covers(0.5, 0.5) and b.covers(5.5, 5.5))
        self.assertFalse(b.covers(3, 3))
        self.assertEqual(b.bbox, (0.0, 0.0, 6.0, 6.0))

    def test_concave(self):
        b = geo.boundary_from_geojson({"type": "Polygon", "coordinates": [
            [[0, 0], [10, 0], [10, 10], [5, 3], [0, 10], [0, 0]]]})
        self.assertTrue(b.covers(1, 5))
        self.assertFalse(b.covers(5, 6), "in the notch")

    def test_bad_input_returns_none(self):
        for bad in (None, {}, {"type": "Point", "coordinates": [0, 0]}, {"type": "Polygon", "coordinates": []},
                    {"type": "Polygon", "coordinates": [[[0, 0], [1, 1]]]}, {"type": "Polygon", "coordinates": "x"}):
            self.assertIsNone(geo.boundary_from_geojson(bad), bad)

    def test_agrees_with_winding_number_on_random_polygons(self):
        rnd = random.Random(7)
        checked = 0
        for _ in range(60):
            n = rnd.randint(5, 40)  # star-shaped, so simple (no self-intersection)
            ring = [(50 + r * math.cos(a), 50 + r * math.sin(a)) for a, r in
                    sorted((rnd.uniform(0, 2 * math.pi), rnd.uniform(5, 40)) for _ in range(n))]
            b = geo.boundary_from_geojson({"type": "Polygon", "coordinates": [ring]})
            closed = b.polygons[0].exterior
            for _ in range(500):
                x, y = rnd.uniform(0, 100), rnd.uniform(0, 100)
                self.assertEqual(b.covers(x, y), winding_inside(x, y, closed), (ring, x, y))
                checked += 1
        self.assertEqual(checked, 30000)


class Crossing(unittest.TestCase):
    def setUp(self):
        self.b = geo.boundary_from_geojson(SQUARE_WITH_HOLE)

    def test_entry_point_is_the_nearest_crossing(self):
        x, y = geo.first_crossing(self.b, (-5, 2), (15, 2))
        self.assertAlmostEqual(x, 0.0)
        self.assertAlmostEqual(y, 2.0)

    def test_exit_from_inside_and_hole_edge(self):
        self.assertEqual(geo.first_crossing(self.b, (1, 1), (1, 20)), (1.0, 10.0))
        x, y = geo.first_crossing(self.b, (1, 5), (9, 5))
        self.assertAlmostEqual(x, 4.0)  # meets the hole first

    def test_miss(self):
        self.assertIsNone(geo.first_crossing(self.b, (-5, -5), (-1, -1)))
        self.assertIsNone(geo.first_crossing(self.b, (1, 1), (3, 3)))
        self.assertIsNone(geo.first_crossing(self.b, (20, 20), (30, 30)))

    def test_diagonal(self):
        x, y = geo.first_crossing(self.b, (-2, -1), (2, 3))
        self.assertAlmostEqual(x, 0.0)
        self.assertAlmostEqual(y, 1.0)


class Simplify(unittest.TestCase):
    def test_collinear_points_removed_shape_kept(self):
        ring = tuple([(float(i), 0.0) for i in range(11)] + [(10.0, float(i)) for i in range(1, 11)]
                     + [(float(i), 10.0) for i in range(9, -1, -1)] + [(0.0, float(i)) for i in range(9, -1, -1)])
        out = geo.simplify_ring(ring, 0.01)
        self.assertEqual(out[0], out[-1])
        self.assertEqual(sorted(set(out)), [(0.0, 0.0), (0.0, 10.0), (10.0, 0.0), (10.0, 10.0)])

    def test_within_tolerance_and_never_degenerate(self):
        rnd = random.Random(3)
        ring = tuple((50 + (30 + rnd.uniform(-0.4, 0.4)) * math.cos(a), 50 + (30 + rnd.uniform(-0.4, 0.4)) * math.sin(a))
                     for a in [i * 2 * math.pi / 400 for i in range(400)])
        ring = ring + (ring[0],)
        out = geo.simplify_ring(ring, 1.0)
        self.assertLess(len(out), len(ring) / 4)
        self.assertGreaterEqual(len(out), 4)
        simplified = geo.boundary_from_geojson({"type": "Polygon", "coordinates": [out]})
        original = geo.boundary_from_geojson({"type": "Polygon", "coordinates": [ring]})
        disagree = sum(simplified.covers(x, y) != original.covers(x, y)
                       for x, y in ((rnd.uniform(15, 85), rnd.uniform(15, 85)) for _ in range(4000)))
        self.assertLess(disagree / 4000, 0.03, "simplification should move the outline by about the tolerance only")

    def test_zero_tolerance_is_identity(self):
        ring = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0))
        self.assertIs(geo.simplify_ring(ring, 0.0), ring)


class GeoJson(unittest.TestCase):
    def test_round_trip_and_linestring_order(self):
        b = geo.boundary_from_geojson(SQUARE_WITH_HOLE)
        self.assertEqual(geo.boundary_to_geojson(b), {"type": "Polygon", "coordinates": [
            [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]],
            [[4.0, 4.0], [6.0, 4.0], [6.0, 6.0], [4.0, 6.0], [4.0, 4.0]]]})
        self.assertEqual(geo.linestring_geojson([(10.0, 20.0), (11.0, 21.0)]),
                         {"type": "LineString", "coordinates": [[20.0, 10.0], [21.0, 11.0]]})


class Speed(unittest.TestCase):
    def test_one_poll_worth_of_lookups_is_fast(self):
        """60 sectors of 2,000 vertices, 2 points each: more than a busy evening needs per poll."""
        rnd = random.Random(5)
        sectors = []
        for _ in range(60):
            cx, cy = rnd.uniform(-170, 170), rnd.uniform(-70, 70)
            ring = [(cx + 4 * math.cos(i * 2 * math.pi / 2000), cy + 4 * math.sin(i * 2 * math.pi / 2000)) for i in range(2000)]
            sectors.append(geo.boundary_from_geojson({"type": "Polygon", "coordinates": [ring]}))
        px, py = sectors[0].bbox[0] + 4, sectors[0].bbox[1] + 4
        start = time.perf_counter()
        hits = sum(s.covers(px, py) + s.covers(px + 1, py + 1) for s in sectors)
        crossing = geo.first_crossing(sectors[0], (px, py), (px + 10, py))
        elapsed = time.perf_counter() - start
        self.assertGreaterEqual(hits, 2)
        self.assertIsNotNone(crossing)
        print(f"\n  60 sectors x 2 lookups + 1 crossing: {elapsed * 1000:.1f} ms")
        self.assertLess(elapsed, 0.25)


if __name__ == "__main__":
    unittest.main()
