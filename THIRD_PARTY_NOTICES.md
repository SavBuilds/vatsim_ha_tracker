# Third-party notices

This repository contains only original code under the MIT license (see `LICENSE`).
It ships **no third-party data**. Everything below is either fetched by the app
at run time from its publisher, or supplied by you from your own licensed copy.

## Fetched at run time

| Source | What the app reads | Terms |
|---|---|---|
| VATSIM data feed, `https://data.vatsim.net/v3/vatsim-data.json` | Your own pilot record, online controllers, ATIS | Public feed, no key. Use is subject to the VATSIM User Agreement. The feed regenerates about every 15 seconds; do not set `poll_interval_seconds` below 15. |
| VAT-Spy Client Data Update Project, `github.com/vatsimnetwork/vatspy-data-project` (`Boundaries.geojson`, `VATSpy.dat`) | FIR / CTR / FSS boundaries and callsign prefixes | CC BY-SA 4.0 |
| natTrak, `https://nattrak.vatsim.net/api/tracks` | North Atlantic Tracks in force | VATSIM service; no separate license is published for the API |

## Supplied by you

| Source | Used for | Terms |
|---|---|---|
| SimAware TRACON Project, `github.com/vatsimnetwork/simaware-tracon-project` | Optional APP / DEP boundaries (`simaware_boundaries_path`) | CC BY-SA 4.0 |
| Navigation data in X-Plane 12 format (`earth_fix.dat`, `earth_nav.dat`, `earth_awy.dat`, `earth_aptmeta.dat`) | Route plotting | Navigraph data is licensed to the subscriber and must not be shared. The default data shipped with X-Plane is published by Laminar Research under the GNU GPL. This repository redistributes neither. |

Map output drawn from VAT-Spy or SimAware boundaries is derived from CC BY-SA 4.0
data. If you publish screenshots or exports of those boundaries, credit the
projects above.

## Python packages (installed by you into AppDaemon, not bundled)

`requests` (Apache-2.0), `geopy` (MIT), `shapely` (BSD-3-Clause), and AppDaemon itself (Apache-2.0).

## Affiliation

Not affiliated with or endorsed by VATSIM, Navigraph, Laminar Research, or SimAware.
