"""Plain-Python geometry for the VATSIM ATC tracker.

Everything the tracker needs from a geometry library, with no dependency:
great-circle distance and projection, point-in-polygon for sector boundaries,
the point where a track crosses a boundary, and outline simplification.

Polygon maths is planar in (longitude, latitude) degrees, the same model the
boundary data is drawn in. Distances and projections are spherical.
A boundary that crosses the antimeridian is not handled specially.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

# Mean Earth radius (6371.0088 km) in nautical miles.
EARTH_RADIUS_NM = 3440.0695
_EDGE_EPS = 1e-12

Coord = tuple[float, float]          # (x, y) = (longitude, latitude)
Ring = tuple[Coord, ...]


def distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(h)))


def destination(lat: float, lon: float, bearing_deg: float, dist_nm: float) -> tuple[float, float]:
    """Point reached from (lat, lon) after dist_nm along an initial true bearing. Returns (lat, lon)."""
    d = dist_nm / EARTH_RADIUS_NM
    b, p1, l1 = math.radians(bearing_deg), math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def _close(ring: Iterable[Sequence[float]]) -> Ring:
    pts = [(float(p[0]), float(p[1])) for p in ring]
    if len(pts) >= 2 and pts[0] != pts[-1]:
        pts.append(pts[0])
    return tuple(pts)


def _on_segment(x: float, y: float, ax: float, ay: float, bx: float, by: float) -> bool:
    cross = (bx - ax) * (y - ay) - (by - ay) * (x - ax)
    if abs(cross) > _EDGE_EPS * max(1.0, abs(bx - ax) + abs(by - ay)):
        return False
    return min(ax, bx) - _EDGE_EPS <= x <= max(ax, bx) + _EDGE_EPS and min(ay, by) - _EDGE_EPS <= y <= max(ay, by) + _EDGE_EPS


def ring_relation(x: float, y: float, ring: Ring) -> int:
    """1 inside, 0 on the edge, -1 outside (even-odd ray casting)."""
    inside = False
    for (ax, ay), (bx, by) in zip(ring, ring[1:]):
        if _on_segment(x, y, ax, ay, bx, by):
            return 0
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            inside = not inside
    return 1 if inside else -1


@dataclass(frozen=True)
class Polygon:
    exterior: Ring
    holes: tuple[Ring, ...]
    bbox: tuple[float, float, float, float]

    def covers(self, x: float, y: float) -> bool:
        """True for a point inside or on the edge (the edge of a hole included)."""
        x0, y0, x1, y1 = self.bbox
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            return False
        rel = ring_relation(x, y, self.exterior)
        if rel <= 0:
            return rel == 0
        for hole in self.holes:
            rel = ring_relation(x, y, hole)
            if rel >= 0:
                return rel == 0
        return True


@dataclass(frozen=True)
class Boundary:
    """One or more polygons making up a sector."""
    polygons: tuple[Polygon, ...]
    bbox: tuple[float, float, float, float]

    def covers(self, x: float, y: float) -> bool:
        x0, y0, x1, y1 = self.bbox
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            return False
        return any(p.covers(x, y) for p in self.polygons)

    def rings(self) -> Iterable[Ring]:
        for p in self.polygons:
            yield p.exterior
            yield from p.holes


def _bbox(points: Iterable[Coord]) -> tuple[float, float, float, float]:
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def _polygon(coords: Sequence) -> Optional[Polygon]:
    if not coords:
        return None
    exterior = _close(coords[0])
    if len(exterior) < 4:
        return None
    holes = tuple(h for h in (_close(r) for r in coords[1:]) if len(h) >= 4)
    return Polygon(exterior, holes, _bbox(exterior))


def boundary_from_geojson(geometry: Optional[dict]) -> Optional[Boundary]:
    """Build a Boundary from a GeoJSON Polygon or MultiPolygon. None if it has no usable ring."""
    if not isinstance(geometry, dict):
        return None
    kind, coords = geometry.get("type"), geometry.get("coordinates") or []
    try:
        if kind == "Polygon":
            polys = [_polygon(coords)]
        elif kind == "MultiPolygon":
            polys = [_polygon(c) for c in coords]
        else:
            return None
    except (TypeError, ValueError, IndexError):
        return None
    polys = tuple(p for p in polys if p is not None)
    if not polys:
        return None
    boxes = [p.bbox for p in polys]
    return Boundary(polys, (min(b[0] for b in boxes), min(b[1] for b in boxes),
                            max(b[2] for b in boxes), max(b[3] for b in boxes)))


def first_crossing(boundary: Boundary, start: Coord, end: Coord) -> Optional[Coord]:
    """Where the segment start -> end first meets the boundary's outline, nearest to start. None if it does not."""
    (px, py), (qx, qy) = start, end
    rx, ry = qx - px, qy - py
    sx0, sx1, sy0, sy1 = min(px, qx), max(px, qx), min(py, qy), max(py, qy)
    bx0, by0, bx1, by1 = boundary.bbox
    if sx1 < bx0 or sx0 > bx1 or sy1 < by0 or sy0 > by1:
        return None
    best: Optional[float] = None
    for ring in boundary.rings():
        for (ax, ay), (bx, by) in zip(ring, ring[1:]):
            if max(ax, bx) < sx0 or min(ax, bx) > sx1 or max(ay, by) < sy0 or min(ay, by) > sy1:
                continue
            ex, ey = bx - ax, by - ay
            denom = rx * ey - ry * ex
            if denom == 0.0:
                continue  # parallel: a track running along an edge has no single crossing point
            t = ((ax - px) * ey - (ay - py) * ex) / denom
            u = ((ax - px) * ry - (ay - py) * rx) / denom
            if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0 and (best is None or t < best):
                best = t
    if best is None:
        return None
    return px + best * rx, py + best * ry


def simplify_ring(ring: Ring, tolerance: float) -> Ring:
    """Douglas-Peucker on a closed ring. Keeps at least a triangle."""
    if tolerance <= 0 or len(ring) <= 5:
        return ring
    pts = list(ring[:-1])
    # Split at the point farthest from the start so a closed ring has two open chains.
    far = max(range(len(pts)), key=lambda i: (pts[i][0] - pts[0][0]) ** 2 + (pts[i][1] - pts[0][1]) ** 2)
    keep = [False] * len(pts)
    keep[0] = keep[far] = True
    stack = [(0, far), (far, len(pts))]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]
        bx, by = pts[b % len(pts)]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        worst, index = 0.0, -1
        for i in range(a + 1, b):
            x, y = pts[i]
            d = abs(dx * (y - ay) - dy * (x - ax)) / norm if norm else math.hypot(x - ax, y - ay)
            if d > worst:
                worst, index = d, i
        if index >= 0 and worst > tolerance:
            keep[index] = True
            stack.extend(((a, index), (index, b)))
    out = [p for p, k in zip(pts, keep) if k]
    if len(out) < 3:
        return ring
    return tuple(out + [out[0]])


def boundary_to_geojson(boundary: Boundary, tolerance: float = 0.0) -> dict:
    def rings(p: Polygon) -> list:
        return [[list(c) for c in simplify_ring(r, tolerance)] for r in (p.exterior, *p.holes)]
    if len(boundary.polygons) == 1:
        return {"type": "Polygon", "coordinates": rings(boundary.polygons[0])}
    return {"type": "MultiPolygon", "coordinates": [rings(p) for p in boundary.polygons]}


def linestring_geojson(lat_lon_points: Iterable[tuple[float, float]]) -> dict:
    """GeoJSON LineString from (lat, lon) pairs. GeoJSON order is lon, lat."""
    return {"type": "LineString", "coordinates": [[lon, lat] for lat, lon in lat_lon_points]}
