#!/usr/bin/env python3
"""Merge the SimAware TRACON Project into one GeoJSON file for the tracker.

    git clone https://github.com/vatsimnetwork/simaware-tracon-project
    python tools/build_simaware.py simaware-tracon-project TRACONBoundaries.geojson

The project keeps one boundary per *.json file under Boundaries/. The tracker
reads a single GeoJSON file (simaware_boundaries_path), so this writes every
boundary into one FeatureCollection. Features without a callsign prefix or a
geometry are skipped and listed.

The output is deterministic (files sorted by path, compact JSON), so the
printed SHA256 changes only when the upstream data does. Run it again to
refresh, for example each AIRAC cycle.

Data: SimAware TRACON Project, CC BY-SA 4.0. The merged file is a copy of
that data under the same license. Keep it on your own system; this repository
does not redistribute it.
"""
import argparse
import glob
import hashlib
import json
import os
import subprocess
import sys


def main():
    ap = argparse.ArgumentParser(description="Merge SimAware TRACON boundaries into one GeoJSON file.")
    ap.add_argument("clone", help="path to a clone of vatsimnetwork/simaware-tracon-project")
    ap.add_argument("out", nargs="?", default="TRACONBoundaries.geojson")
    args = ap.parse_args()

    boundaries = os.path.join(args.clone, "Boundaries")
    if not os.path.isdir(boundaries):
        print(f"ERROR: no Boundaries folder under {args.clone}; is this a clone of the project?", file=sys.stderr)
        return 2
    try:
        rev = subprocess.run(["git", "-C", args.clone, "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip() or "unknown"
    except OSError:
        rev = "unknown"  # git not installed; the merge does not need it

    paths = sorted(glob.glob(os.path.join(boundaries, "**", "*.json"), recursive=True),
                   key=lambda p: os.path.relpath(p, args.clone).replace(os.sep, "/"))
    features, skipped = [], []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            document = json.load(fh)
        members = [document] if document.get("type") == "Feature" else document.get("features", [])
        for feature in members:
            properties = feature.get("properties") or {}
            if not properties.get("prefix") or not feature.get("geometry"):
                skipped.append(os.path.relpath(path, args.clone))
                continue
            features.append({"type": "Feature", "properties": properties, "geometry": feature["geometry"]})

    merged = {"type": "FeatureCollection",
              "source": f"vatsimnetwork/simaware-tracon-project@{rev}",
              "license": "CC BY-SA 4.0",
              "features": features}
    data = json.dumps(merged, separators=(",", ":"), ensure_ascii=True).encode()
    with open(args.out, "wb") as fh:
        fh.write(data)
    print(f"files {len(paths)} features {len(features)} skipped {len(skipped)} bytes {len(data)} "
          f"sha256 {hashlib.sha256(data).hexdigest().upper()} rev {rev}")
    for name in skipped:
        print(f"  skipped: {name}")
    if not features:
        print("ERROR: no usable boundaries found", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
