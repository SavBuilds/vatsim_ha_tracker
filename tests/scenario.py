"""An invented world for the tests: two square sectors, one pilot. No real airspace, no real
callsigns, no network."""
import json
import time

BOUNDARIES_URL = "https://example.invalid/Boundaries.geojson"
DAT_URL = "https://example.invalid/VATSpy.dat"


def square(x0, y0, x1, y1):
    return [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]


BOUNDARIES = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"id": "ZZAA"},
     "geometry": {"type": "Polygon", "coordinates": square(20, 8, 24, 12)}},
    # A MultiPolygon with one member is published as a plain Polygon.
    {"type": "Feature", "properties": {"id": "ZZBB"},
     "geometry": {"type": "MultiPolygon", "coordinates": [square(24, 8, 28, 12)]}},
    {"type": "Feature", "properties": {"id": "ZZCC"}, "geometry": {"type": "Polygon", "coordinates": []}},
]}
DAT = "[FIRs]\nZZAA|Alpha|ZZA|ZZAA\nZZBB|Bravo|ZZB|ZZBB\nZZCC|Gamma|ZZC|ZZCC\n"


def feed(lon, lat=10.0, heading=90, groundspeed=480, controllers=("ZZA_CTR", "ZZB_CTR")):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.0000000Z", time.gmtime(time.time()))
    return {"general": {"update_timestamp": stamp},
            "pilots": [{"cid": 1234567, "callsign": "SIM123", "latitude": lat, "longitude": lon,
                        "altitude": 30000, "groundspeed": groundspeed, "heading": heading}],
            "controllers": [{"callsign": c, "facility": 6, "frequency": f"13{i}.000"}
                            for i, c in enumerate(controllers)],
            "atis": []}


def serve(url, payload):
    """Body text for a URL in this world. Anything that is not a boundary file gets the feed."""
    if url == BOUNDARIES_URL:
        return json.dumps(BOUNDARIES)
    if url == DAT_URL:
        return DAT
    return json.dumps(payload)
