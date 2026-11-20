# Third-party notices

This repository contains only original code under the MIT licence (see `LICENSE`).
It ships **no third-party data**. Everything below is either fetched by the integration
at run time from its publisher, or supplied by you from your own licensed copy.

## Fetched at run time

| Source | What the integration reads | Terms |
|---|---|---|
| VATSIM data feed, `https://data.vatsim.net/v3/vatsim-data.json` | Your own pilot record, online controllers, ATIS | Public feed, no key. Use is subject to the VATSIM User Agreement. The feed regenerates about every 15 seconds; the integration polls about every 18. |
| VAT-Spy Client Data Update Project, `github.com/vatsimnetwork/vatspy-data-project` (`Boundaries.geojson`, `VATSpy.dat`) | FIR / CTR / FSS boundaries and callsign prefixes | CC BY-SA 4.0 |
| natTrak, `https://nattrak.vatsim.net/api/tracks` | North Atlantic Tracks in force | VATSIM service; no separate licence is published for the API |
| VATSIM METAR service, `https://metar.vatsim.net/` | One airport's METAR, when it has no ATIS online | VATSIM service |
| Aviation Weather Center, `https://aviationweather.gov/api/data/metar` | The same METAR, when VATSIM's service does not answer | US government public data |

## Supplied by you

| Source | Used for | Terms |
|---|---|---|
| SimAware TRACON Project, `github.com/vatsimnetwork/simaware-tracon-project` | Optional approach and departure boundaries | CC BY-SA 4.0 |
| Navigation data in X-Plane 12 format (`earth_fix.dat`, `earth_nav.dat`, `earth_awy.dat`, `earth_aptmeta.dat`) | Route plotting | Navigraph data is licensed to the subscriber and must not be shared. The default data shipped with X-Plane is published by Laminar Research under the GNU GPL. This repository redistributes neither. |

Map output drawn from VAT-Spy or SimAware boundaries is derived from CC BY-SA 4.0
data. If you publish screenshots or exports of those boundaries, credit the
projects above.

## Dependencies

None beyond Home Assistant itself. The dashboard uses three separately installed
cards: button-card, Mushroom and map-card.

## Affiliation

Not affiliated with or endorsed by VATSIM, Navigraph, Laminar Research, or SimAware.
