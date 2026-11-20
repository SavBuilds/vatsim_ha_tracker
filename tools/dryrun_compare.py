#!/usr/bin/env python3
"""Compare what two versions of the tracker would publish, from the same input.

    python tools/dryrun_compare.py BASELINE.py CANDIDATE.py --apps-yaml PATH [options]

Read-only. It fetches the public VATSIM feed, the VATSpy boundary files, the
status document and natTrak once, then feeds the identical responses to both
versions through a stand-in for AppDaemon. Nothing is written to Home
Assistant and no AppDaemon is needed. Needs: pip install requests geopy shapely pyyaml

Both versions run with a frozen clock, so timestamps match. States are
compared the way Home Assistant ends up holding them when written through
AppDaemon: attributes that are None, False or 0 are dropped, True becomes the
string "true", and the per-write pub_token is ignored.

Options:
  --app-key NAME      key of the app block in apps.yaml (default vatsim_atc_tracker)
  --sample-online     ignore vatsim_cid and follow one pilot who is online with
                      a filed flight plan, so the coverage, lookahead and route
                      paths run even when you are not connected. The pilot is
                      not named in the output.
  --polls N           poll cycles to simulate (default 4; debounce needs 2, heading history 3)
  --cache DIR         save the fetched responses to DIR, or reuse them if present
                      (holds a full feed snapshot: keep it out of any repository)
  --map-path A=B      rewrite a path prefix from apps.yaml, e.g. /homeassistant=D:/ha-config
Exit status: 0 identical, 1 differences, 2 could not run.
"""
import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests"))
from _harness import load_tracker  # noqa: E402

POLL_SECONDS = 18.0


class Replay:
    """Serves each URL from memory after the first real fetch."""

    def __init__(self, cache_dir, real_get):
        self.cache_dir, self.real_get, self.mem = cache_dir, real_get, {}

    def _file(self, url):
        return os.path.join(self.cache_dir, hashlib.sha256(url.encode()).hexdigest()[:16] + ".txt")

    def get(self, url, **kwargs):
        if url not in self.mem:
            path = self._file(url) if self.cache_dir else None
            if path and os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    self.mem[url] = fh.read()
            else:
                resp = self.real_get(url, timeout=kwargs.get("timeout", 15), headers=kwargs.get("headers"))
                resp.raise_for_status()
                self.mem[url] = resp.text
                if path:
                    os.makedirs(self.cache_dir, exist_ok=True)
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(self.mem[url])
        return _Resp(self.mem[url])


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(self.text)


def as_ha_holds(attrs):
    out = {}
    for key, value in attrs.items():
        if key == "pub_token" or value is None or value is False or (
                isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0):
            continue
        out[key] = "true" if value is True else value
    return out


def run(path, name, args, replay, clock, polls, follow_cid):
    module = load_tracker(path, module_name=name)
    module.requests.get = replay.get
    real_time = time.time
    time.time = lambda: clock[0]
    try:
        app = module.VatsimAtcTracker(dict(args))
        app._load_config()
        if follow_cid:
            app.vatsim_cid = follow_cid
        app._init_state()
        app._load_last_aircraft = lambda: None       # never read the real save file
        app._persist_last_aircraft = lambda force=False: None   # and never write one
        # The navdata swap normally starts a NAT refresh on its own thread. Its timing
        # would make the two runs differ by chance, so the refresh is run once, inline, below.
        app._schedule_nat_track_refresh = lambda kwargs: None
        app._load_geometry_worker()
        if app.navdata_path:
            app._load_navdata_worker_body()
            if app.nat_tracks_url:
                app._load_nat_tracks_worker()
        for _ in range(polls):
            app._run_poll_cycle_body()
            clock[0] += POLL_SECONDS
    finally:
        time.time = real_time
    final = {eid: (state, as_ha_holds(attrs)) for eid, (state, attrs) in app.states.items()}
    sequence = [(eid, state, json.dumps(as_ha_holds(attrs), sort_keys=True, default=str))
                for eid, state, attrs in app.writes]
    problems = [(lvl, msg) for lvl, msg in app.logs if lvl in ("WARNING", "ERROR")]
    return final, sequence, problems


def main():
    ap = argparse.ArgumentParser(description="Compare the published states of two tracker versions.")
    ap.add_argument("baseline")
    ap.add_argument("candidate")
    ap.add_argument("--apps-yaml", required=True)
    ap.add_argument("--app-key", default="vatsim_atc_tracker")
    ap.add_argument("--sample-online", action="store_true")
    ap.add_argument("--polls", type=int, default=4)
    ap.add_argument("--cache")
    ap.add_argument("--map-path", action="append", default=[])
    opts = ap.parse_args()

    import requests
    import yaml

    class Loader(yaml.SafeLoader):
        pass
    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    with open(opts.apps_yaml, encoding="utf-8") as fh:
        args = (yaml.load(fh, Loader=Loader) or {}).get(opts.app_key)
    if not isinstance(args, dict):
        print(f"ERROR: no '{opts.app_key}' block in {opts.apps_yaml}", file=sys.stderr)
        return 2
    for mapping in opts.map_path:
        old, _, new = mapping.partition("=")
        for key, value in list(args.items()):
            if isinstance(value, str) and value.startswith(old):
                args[key] = new + value[len(old):]

    replay = Replay(opts.cache, requests.get)
    follow = None
    if opts.sample_online:
        feed = replay.get("https://data.vatsim.net/v3/vatsim-data.json").json()
        pick = next((p for p in feed.get("pilots", [])
                     if (p.get("flight_plan") or {}).get("route") and (p.get("groundspeed") or 0) > 250), None)
        if pick is None:
            print("ERROR: no airborne pilot with a filed route in the feed", file=sys.stderr)
            return 2
        follow = str(pick["cid"])

    start = time.time()
    results = []
    for label, path in (("baseline", opts.baseline), ("candidate", opts.candidate)):
        try:
            results.append(run(path, f"tracker_{label}", args, replay, [start], opts.polls, follow))
        except Exception as exc:
            print(f"ERROR: {label} could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    (final_a, seq_a, prob_a), (final_b, seq_b, prob_b) = results

    differences = 0
    for eid in sorted(set(final_a) | set(final_b)):
        a, b = final_a.get(eid), final_b.get(eid)
        if a == b:
            continue
        if a is None or b is None:
            differences += 1
            print(f"DIFF {eid}: published only by the {'candidate' if a is None else 'baseline'}")
            continue
        if a[0] != b[0]:
            differences += 1
            print(f"DIFF {eid}: state {a[0]!r} -> {b[0]!r}")
        for key in sorted(set(a[1]) | set(b[1])):
            if a[1].get(key) != b[1].get(key):
                differences += 1
                short = lambda v: (str(v)[:60] + "...") if len(str(v)) > 60 else str(v)
                print(f"DIFF {eid}.{key}: {short(a[1].get(key, '<absent>'))} -> {short(b[1].get(key, '<absent>'))}")
    if seq_a != seq_b:
        differences += 1
        print(f"DIFF write sequence: baseline made {len(seq_a)} writes, candidate {len(seq_b)}, "
              f"first mismatch at write {next((i for i, (x, y) in enumerate(zip(seq_a, seq_b)) if x != y), min(len(seq_a), len(seq_b))) + 1}")

    print(f"\nEntities published: baseline {len(final_a)}, candidate {len(final_b)}. "
          f"Writes: {len(seq_a)} vs {len(seq_b)}. Polls: {opts.polls}. "
          f"Pilot: {'sampled online pilot' if follow else 'vatsim_cid from apps.yaml'}.")
    connected = final_b.get("binary_sensor.vatsim_pilot_connected", ("?",))[0]
    print(f"Pilot connected in this snapshot: {connected}. "
          f"{'Coverage, lookahead and route paths were exercised.' if connected == 'on' else 'Only the not-connected path was exercised; rerun with --sample-online or while flying.'}")
    for label, problems in (("baseline", prob_a), ("candidate", prob_b)):
        for lvl, msg in problems[:8]:
            print(f"  {label} {lvl}: {msg[:160]}")
    print("RESULT: " + ("IDENTICAL" if differences == 0 else f"{differences} DIFFERENCE(S)"))
    return 0 if differences == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
