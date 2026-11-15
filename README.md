# FIRECTOR - Burned Earth Service Fix

Replace **only `index.html`** at the root of your GitHub repository with the included `index.html`, then commit to `main`. Render auto-deploys on `main`, or use Manual Deploy > Deploy latest commit. Do not change the NASA key or your existing `world_server.py`.

Changes:
- Replaces the older JRC URL as the primary WMS endpoint with the Copernicus `/gwis` map host documented as used by the EFFIS/GWIS current-situation viewer.
- Falls back to the older JRC endpoint if every initial tile fails, or if the first response stalls for 45 seconds.
- Avoids calling a single failed/aborted image tile a failure for the entire overlay.
- Better messages and official GWIS viewer as manual fallback.
- Zoom in (9+) for detailed burn perimeters; not every location will show recent burns.

**Limits**: Burned areas are estimated MODIS/VIIRS NRT products, not photographic burned ground, and no uptime guarantee applies to public WMS services. Browser network access to the map hosts is needed. Neither endpoint's actual GetMap response could be tested from this environment, so the online result is not yet confirmed.

The rest of the app is unchanged.
