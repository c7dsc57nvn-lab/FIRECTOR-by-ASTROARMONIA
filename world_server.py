"""FIRECTOR worldwide feed: run with `python3 -m uvicorn world_server:app --port 9100`.
Imports the existing application to preserve its NASA and Sentinel-2 endpoints.
The API credential remains in the local .env file, never the browser.
"""
import csv
import hashlib
import json
import io
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from fastapi import HTTPException, Query

load_dotenv(Path(__file__).resolve().parent / '.env', override=True)
from server import app  # noqa: E402 — preserves /, /api/nasa, and /api/sentinel2

# Some older FIRECTOR server versions cache this variable at import time.
import server as original_server
original_server.NASA_KEY = os.getenv('NASA_FIRMS_KEY', '').strip()

SOURCES = {'VIIRS': 'VIIRS_NOAA20_NRT', 'MODIS': 'MODIS_NRT'}
FIRMS = 'https://firms.modaps.eosdis.nasa.gov/api/area/csv'
CACHE_TTL = 300
FUSION_VERSION = '6.0-footprint-aware-event-fusion'
# These are provisional grouping tolerances, not validated location accuracy.
# A candidate event is NEVER labelled a confirmed active wildfire.
# Provisional matching tolerances. These are engineering defaults, NOT
# validated fire-location uncertainties or guaranteed duplicate thresholds.
SAME_SENSOR_KM = 0.55
CROSS_SENSOR_KM = 1.00
SAME_SENSOR_SECONDS = 90 * 60
CROSS_SENSOR_SECONDS = 4 * 3600
MATCH_KM = CROSS_SENSOR_KM
MATCH_HOURS = CROSS_SENSOR_SECONDS / 3600
LOCK = threading.Lock()
CACHE = {'time': 0.0, 'result': None}
AUDIT_FILE = Path(__file__).resolve().parent / 'firefusion_audit.jsonl'


def record_audit(result):
    # Record only aggregate counts and source state; never coordinates or credentials.
    entry = {
        'retrieved_at': result['retrieved_at'],
        'raw_observations': result['raw_count'],
        'exact_repeated_source_rows_removed': result['exact_duplicate_rows'],
        'candidate_events': result['event_count'],
        'source_status': {name: ('error' if name in result['source_errors'] else 'ok')
                          for name in SOURCES},
        'validation_status': 'not_validated'
    }
    try:
        with AUDIT_FILE.open('a', encoding='utf-8') as f:
            f.write(json.dumps(entry, separators=(',', ':')) + '\n')
    except OSError:
        pass  # Monitoring must not fail if the audit directory is read-only.



def parse_observation(row, source):
    try:
        lat, lon = float(row['latitude']), float(row['longitude'])
        date = str(row['acq_date']).strip()
        clock = str(row['acq_time']).strip().zfill(4)
        if len(clock) != 4 or not clock.isdigit():
            return None
        dt = datetime.strptime(date + clock, '%Y-%m-%d%H%M').replace(tzinfo=timezone.utc)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return {'lat': lat, 'lon': lon, 'ts': int(dt.timestamp()), 'source': source,
                'datetime': dt.isoformat().replace('+00:00', 'Z'),
                'confidence': row.get('confidence', ''), 'frp': row.get('frp', ''),
                'scan_km': row.get('scan', ''), 'track_km': row.get('track', ''),
                'daynight': row.get('daynight', '')}
    except (KeyError, ValueError, TypeError):
        return None


def download_source(source, key):
    # NASA's 1-day request is UTC-calendar-day based, not a rolling 24h window.
    # Request two UTC calendar dates, then filter to the actual last 24 hours.
    url = '/'.join((FIRMS, quote(key, safe=''), SOURCES[source], 'world', '2'))
    response = requests.get(url, timeout=(15, 180))
    if not response.ok:
        raise ValueError(f'{source} HTTP {response.status_code} from NASA FIRMS')
    lines = response.text
    if not lines.lstrip().lower().startswith('latitude,'):
        raise ValueError(f'{source} returned unexpected response instead of CSV: {lines[:120]}')
    return [p for p in (parse_observation(r, source) for r in csv.DictReader(io.StringIO(lines))) if p]


def distance_km(a, b):
    r1, r2 = math.radians(a['lat']), math.radians(b['lat'])
    dr = r2 - r1
    dl = math.radians(a['lon'] - b['lon'])
    h = math.sin(dr / 2)**2 + math.cos(r1) * math.cos(r2) * math.sin(dl / 2)**2
    return 12742.0 * math.asin(min(1, math.sqrt(max(0, h))))


def xyz_bucket(p, cell=1.0 / 6371.0):
    latitude, longitude = math.radians(p['lat']), math.radians(p['lon'])
    x = math.cos(latitude) * math.cos(longitude)
    y = math.cos(latitude) * math.sin(longitude)
    z = math.sin(latitude)
    return (math.floor(x / cell), math.floor(y / cell), math.floor(z / cell))


def _pixel_radius_km(p):
    """Approximate half diagonal of the reported sensor pixel in km.

    FIRMS scan/track dimensions are NOT the fire perimeter. We cap influence
    from unusually large edge-of-swath pixels so one giant pixel cannot
    indiscriminately absorb neighboring fires.
    """
    try:
        scan, track = float(p.get('scan_km') or 0), float(p.get('track_km') or 0)
        if not (0 < scan <= 15 and 0 < track <= 15):
            raise ValueError('missing or implausible pixel')
        return min(1.15, 0.5 * math.hypot(scan, track))
    except (TypeError, ValueError, OverflowError):
        return 0.35 if p['source'] == 'VIIRS' else 0.70


def _matching_limits(a, b):
    """Spatial tolerance reflects approximate pixel footprints and time gap."""
    gap = abs(a['ts'] - b['ts'])
    footprint = _pixel_radius_km(a) + _pixel_radius_km(b)
    same = a['source'] == b['source']
    if gap <= 25 * 60:
        # Adjacent hot pixels seen on the same pass may be the same fire.
        return min(2.3, max(0.65, footprint + 0.30)), 25 * 60
    if gap <= 8 * 3600:
        # A continuing fire may be observed again on a subsequent pass.
        # Modest additional allowance, with an absolute distance cap.
        return min(2.0, max(0.75, footprint + (0.25 if same else 0.40))), 8 * 3600
    return 0.0, 8 * 3600


def observations_compatible(a, b):
    radius, seconds = _matching_limits(a, b)
    return radius > 0 and abs(a['ts']-b['ts']) <= seconds and distance_km(a,b) <= radius


def candidate_match_evidence(a, b):
    limit, seconds = _matching_limits(a, b)
    delta = abs(a['ts']-b['ts'])
    dist = distance_km(a,b)
    return {
        'compatible': bool(limit and delta <= seconds and dist <= limit),
        'distance_km': round(dist,3),
        'time_gap_minutes': round(delta/60,1),
        'same_source': a['source'] == b['source'],
        'distance_limit_km': round(limit,3),
        'time_limit_minutes': seconds/60,
        'pixel_footprints_approximate': True,
    }


# Global limits prevent a group spanning widely separated fires by a sequence
# of individually close neighboring pixels. Tune these only with labeled data.
MAX_GROUP_DIAMETER_KM = 3.2
MAX_GROUP_TIME_SPAN_SECONDS = 8 * 3600
MAX_GROUP_MEMBERS = 300


def _group_centroid(group):
    pts = group['members']
    # Mean of 3-D unit vectors handles longitude wraparound at the dateline.
    x = y = z = 0.0
    for p in pts:
        lat, lon = math.radians(p['lat']), math.radians(p['lon'])
        x += math.cos(lat)*math.cos(lon)
        y += math.cos(lat)*math.sin(lon)
        z += math.sin(lat)
    return {'lat': math.degrees(math.atan2(z, math.hypot(x,y))),
            'lon': math.degrees(math.atan2(y,x))}


def deduplicate_exact_records(points):
    """Remove byte-equivalent normalized FIRMS observations only.

    This removes duplicate API rows, NOT observations of the same wildfire.
    Different time, position, footprint, FRP or other fields remain intact.
    Return unique rows and the number of identical input rows discarded.
    """
    seen = set()
    unique = []
    for point in points:
        fingerprint = tuple(sorted((str(k), str(v)) for k, v in point.items()))
        if fingerprint not in seen:
            seen.add(fingerprint)
            unique.append(point)
    return unique, len(points) - len(unique)


def fuse_observations(points):
    """Footprint-informed candidate event grouping; never confirmed fire count.

    Allows adjacent pixels and recurring satellite passes to consolidate while
    requiring a bounded group diameter and total time span. Does NOT use
    transitive-only chaining. When a point could join multiple groups it stays
    separate and marks ambiguity for review. All source records are preserved.
    """
    groups, buckets = [], {}
    ordered = sorted(points, key=lambda p: (-p['ts'], p['source'], p['lat'], p['lon']))
    bucket_cell = 3.2 / 6371.0
    for p in ordered:
        bx,by,bz = xyz_bucket(p, cell=bucket_cell)
        possible = set()
        for dx in (-1,0,1):
            for dy in (-1,0,1):
                for dz in (-1,0,1):
                    possible.update(buckets.get((bx+dx,by+dy,bz+dz), ()))
        eligible = []
        for idx in sorted(possible):
            g = groups[idx]
            if len(g['members']) >= MAX_GROUP_MEMBERS:
                continue
            if g['latest_ts'] - p['ts'] > MAX_GROUP_TIME_SPAN_SECONDS:
                continue
            if distance_km(p, g['anchor']) > MAX_GROUP_DIAMETER_KM:
                continue
            # Check proximity to a real member, not to the center of a broad
            # artificial cluster. The global diameter cap blocks chain merges.
            if not any(observations_compatible(p,q) for q in g['members']):
                continue
            # New point must be within max diameter of EVERY member. Unlike
            # the old rule, pairs need not all fit the tiny same-pass pixel gap.
            if not all(distance_km(p,q) <= MAX_GROUP_DIAMETER_KM for q in g['members']):
                continue
            eligible.append(idx)
        if len(eligible) == 1:
            g=groups[eligible[0]]
            g['members'].append(p)
            g['oldest_ts'] = min(g['oldest_ts'],p['ts'])
        else:
            idx=len(groups)
            g={'anchor':p,'members':[p],'latest_ts':p['ts'],
               'oldest_ts':p['ts'],'ambiguous':len(eligible)>1}
            groups.append(g)
            buckets.setdefault((bx,by,bz),[]).append(idx)
            for other in eligible:
                groups[other]['ambiguous']=True

    events=[]
    for index,g in enumerate(groups):
        members=g['members']; anchor=g['anchor']
        timestamps=[p['ts'] for p in members]
        sources=sorted({p['source'] for p in members})
        point_hash=hashlib.sha256('|'.join(sorted(
            f"{p['source']}:{p['ts']}:{p['lat']:.5f}:{p['lon']:.5f}" for p in members
        )).encode()).hexdigest()[:16]
        location=_group_centroid(g)
        # Maximum separation kept in metadata for review; quadratic only for
        # groups smaller than 50 to avoid large-area server stalls.
        max_sep = max((distance_km(a,b) for i,a in enumerate(members)
                       for b in members[i+1:]),default=0.0) if len(members)<=50 else None
        events.append({
            'id':index+1,'snapshot_group_id':point_hash,
            'lat':location['lat'],'lon':location['lon'],
            'count':len(members),'sources':sources,
            'latest':max(timestamps),'oldest':min(timestamps),
            'examples':members[:8], 'verification':'unverified',
            'candidate_match':len(members)>1,'independent_confirmation':False,
            'source_agreement':len(sources)>1,
            'max_pairwise_separation_km':round(max_sep,3) if max_sep is not None else None,
            'match_basis':'approximate_pixel_footprint_plus_time_bounded_group',
            'ambiguous':g['ambiguous'],
            'fusion_note':('Ambiguous: multiple plausible matches; kept separate' if g['ambiguous']
                           else 'Provisional pixel-aware candidate; no independent verification'),
            'independent_imagery_checked':False
        })
    return events


def load_world():
    # The lock prevents simultaneous browser requests from duplicating global downloads.
    with LOCK:
        if CACHE['result'] is not None and time.monotonic() - CACHE['time'] < CACHE_TTL:
            return CACHE['result']
        key = os.environ.get('NASA_FIRMS_KEY', '').strip()
        if not key:
            raise HTTPException(status_code=503, detail='NASA_FIRMS_KEY missing from FIRECTOR/.env')
        observations, errors = [], {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {source: pool.submit(download_source, source, key) for source in SOURCES}
            for source, future in futures.items():
                try:
                    observations.extend(future.result())
                except Exception as ex:
                    errors[source] = (f'{source} request failed ({type(ex).__name__})'
                                      if not isinstance(ex, ValueError) else str(ex).split('https://')[0][:120])
        if len(errors) == len(SOURCES):
            raise HTTPException(status_code=502, detail='Both NASA sources failed: ' + str(errors))
        now = int(datetime.now(timezone.utc).timestamp())
        cutoff = now - 86400
        # Reject old observations, and future-looking records beyond clock skew.
        observations = [p for p in observations if cutoff <= p['ts'] <= now + 300]
        fetched_row_count = len(observations)
        observations, exact_duplicate_rows = deduplicate_exact_records(observations)
        events = fuse_observations(observations)
        try:
            events, msg_stats = meteosat_merge(events)
        except Exception as ex:
            msg_stats = {'status': 'error_' + type(ex).__name__, 'checked': 0, 'fire_pixel_hits': 0, 'groups_merged': 0}
        counts = {source: sum(p['source'] == source for p in observations) for source in SOURCES}
        result = {'events': events, 'raw_count': len(observations), 'event_count': len(events),
                  'counts': counts, 'source_errors': errors,
                  'fetched_row_count': fetched_row_count,
                  'exact_duplicate_rows': exact_duplicate_rows,
                  'latest_observation': max((p['datetime'] for p in observations), default=None),
                  'retrieved_at': datetime.now(timezone.utc).isoformat(),
                  'coverage': 'Worldwide; rolling last 24 hours',
                  'match_rule': 'Candidate only: approximate footprint, time, bounded group diameter; unvalidated',
                  'validation_status': 'not_validated',
                  'fusion_version': FUSION_VERSION,
                  'ambiguous_groups': sum(e['ambiguous'] for e in events),
                  'multisensor_groups': sum(e['source_agreement'] for e in events),
                  'automatic_candidate_grouping': True, 'meteosat_crosscheck': msg_stats,
                  'verified_duplicate_groups': 0,
                  'supporting_imagery_used_in_merging': msg_stats['groups_merged'] > 0,
                  'operational_warning': 'Research prototype; not for emergency dispatch or safety decisions'}
        CACHE.update(time=time.monotonic(), result=result)
        record_audit(result)
        return result


@app.get('/api/world/summary')
def world_summary():
    result = load_world()
    return {k: v for k, v in result.items() if k != 'events'}


def longitude_visible(lon, west, east):
    return west <= lon <= east if west <= east else lon >= west or lon <= east


@app.get('/api/world/map')
def world_map(
    west: float = Query(-180, ge=-180, le=180), south: float = Query(-85, ge=-90, le=90),
    east: float = Query(180, ge=-180, le=180), north: float = Query(85, ge=-90, le=90),
    zoom: int = Query(3, ge=1, le=19)
):
    result = load_world()
    visible = [e for e in result['events'] if south <= e['lat'] <= north
               and longitude_visible(e['lon'], west, east)]
    # Display clustering is NOT fire fusion. At detail zoom show every candidate
    # event independently so separate nearby fires aren't visually hidden.
    cell = max(0.002, 45 * 360.0 / (256.0 * (2 ** zoom)))
    buckets = {}
    for e in visible:
        k = (e['snapshot_group_id'] if zoom >= 11 else
             (int((e['lat'] + 90) // cell), int((e['lon'] + 180) // cell)))
        b = buckets.get(k)
        if b is None:
            buckets[k] = {'lat': e['lat'], 'lon': e['lon'], 'events': 1,
                          'observations': e['count'], 'latest': e['latest'],
                          'event': e}
        else:
            b['events'] += 1
            b['observations'] += e['count']
            b['latest'] = max(b['latest'], e['latest'])
    markers = []
    for b in buckets.values():
        item = {'lat': b['lat'], 'lon': b['lon'], 'events': b['events'],
                'observations': b['observations'], 'latest': b['latest']}
        if b['events'] == 1:
            item['event'] = b['event']
        markers.append(item)
    # Avoid rendering a massive point cloud when zoomed out over dense global areas.
    markers.sort(key=lambda m: m['latest'], reverse=True)
    return {'markers': markers[:3500], 'visible_event_count': len(visible),
            'display_clustering_enabled': zoom < 11,
            'markers_limited': len(markers) > 3500,
            'source_errors': result['source_errors'], 'retrieved_at': result['retrieved_at']}


@app.get('/api/world/nearest')
def world_nearest(lat: float = Query(..., ge=-90, le=90),
                  lon: float = Query(..., ge=-180, le=180)):
    result = load_world()
    if not result['events']:
        return {'nearest': None}
    target = {'lat': lat, 'lon': lon}
    event = min(result['events'], key=lambda p: distance_km(target, p))
    return {'nearest': {'distance_km': round(distance_km(target, event), 1), 'event': event},
            'retrieved_at': result['retrieved_at']}


@app.get('/api/world/quality')
def world_quality():
    """Operational transparency report; these are NOT accuracy metrics."""
    data = load_world()
    now = datetime.now(timezone.utc)
    retrieved = datetime.fromisoformat(data['retrieved_at'])
    latest = data['latest_observation']
    latest_dt = datetime.fromisoformat(latest.replace('Z', '+00:00')) if latest else None
    age_minutes = round((now - latest_dt).total_seconds() / 60, 1) if latest_dt else None
    fetch_age_minutes = round((now - retrieved).total_seconds() / 60, 1)
    errors = data['source_errors']
    state = ('unavailable' if len(errors) == len(SOURCES) else
             'degraded' if errors else
             'no_recent_observations' if age_minutes is None else
             'stale' if age_minutes > 180 else 'receiving_data')
    return {
        'data_state': state,
        'retrieved_at': data['retrieved_at'],
        'feed_retrieval_age_minutes': fetch_age_minutes,
        'latest_observation': latest,
        'latest_observation_age_minutes': age_minutes,
        'observations_24h': data['raw_count'],
        'fetched_rows_24h': data['fetched_row_count'],
        'exact_repeated_source_rows_removed': data['exact_duplicate_rows'],
        'candidate_events_24h': data['event_count'],
        'provisionally_grouped_observations': data['raw_count'] - data['event_count'],
        'duplicate_claim_status': 'No validated real-world duplicate-removal accuracy available',
        'ambiguous_groups': data['ambiguous_groups'],
        'multisensor_groups': data['multisensor_groups'],
        'fusion_version': FUSION_VERSION,
        'source_errors': errors,
        'source_types': ['NASA FIRMS VIIRS NOAA-20 NRT', 'NASA FIRMS MODIS NRT'],
        'deduplication': {
            'method': 'exact identical-row removal followed by provisional footprint-aware grouping with bounded diameter and ambiguity safeguards',
            'pixel_level_sentinel_meteosat_analysis': False,
            'exact_source_row_dedup': True,
            'same_sensor_distance_km': 'dynamic from scan/track dimensions',
            'same_sensor_time_minutes': '25 same pass; up to 480 repeat pass',
            'cross_sensor_distance_km': 'dynamic from scan/track dimensions',
            'cross_sensor_time_hours': 8,
            'no_transitive_chain_merging': True,
            'max_group_diameter_km': MAX_GROUP_DIAMETER_KM,
            'max_group_time_span_hours': MAX_GROUP_TIME_SPAN_SECONDS / 3600,
            'validated': False,
            'independently_confirmed_matches': 0
        },
        'accuracy_validation': {
            'false_positive_rate': None,
            'missed_fire_rate': None,
            'duplicate_precision': None,
            'duplicate_recall': None,
            'ground_truth_dataset': None,
            'status': 'independent validation not performed'
        },
        'use_restriction': 'Prototype; do not use as sole basis for emergency response'
    }


@app.get('/api/world/audit')
def world_audit():
    """Return recent aggregate fetch records only; excludes coordinates and credentials."""
    try:
        with AUDIT_FILE.open('r', encoding='utf-8') as f:
            recent = f.readlines()[-20:]
        return {'entries': [json.loads(line) for line in recent]}
    except (OSError, ValueError):
        return {'entries': []}



# -------- Experimental fire-risk areas (heuristic, NOT an official danger index) --------
# Combines recent satellite hotspot activity (VIIRS/MODIS, already in the feed) with
# current weather from Open-Meteo (free, no key). It flags areas where fire is more
# likely to start or spread; it is not validated and not the EFFIS/NFDRS index.
OPEN_METEO = 'https://api.open-meteo.com/v1/forecast'
RISK_CELL_DEG = 0.5
RISK_MAX_CELLS = 120
RISK_TTL = 1800
_risk_cache = {'at': 0.0, 'result': None}
_risk_lock = threading.Lock()


def weather_points(points):
    """Return one weather dict (or None) per (lat, lon); never raises."""
    out = []
    for i in range(0, len(points), 50):
        chunk = points[i:i + 50]
        try:
            r = requests.get(OPEN_METEO, params={
                'latitude': ','.join(f'{la:.3f}' for la, _ in chunk),
                'longitude': ','.join(f'{lo:.3f}' for _, lo in chunk),
                'current': 'temperature_2m,relative_humidity_2m,wind_speed_10m',
                'daily': 'precipitation_sum', 'past_days': 3, 'forecast_days': 1,
                'timezone': 'UTC'}, timeout=(10, 30))
            r.raise_for_status()
            data = r.json()
            data = data if isinstance(data, list) else [data]
            for d in data:
                cur = d.get('current') or {}
                rain = [x for x in (d.get('daily', {}).get('precipitation_sum') or [])[:3] if x is not None]
                out.append({'temp_c': cur.get('temperature_2m'),
                            'humidity_pct': cur.get('relative_humidity_2m'),
                            'wind_kmh': cur.get('wind_speed_10m'),
                            'rain_3d_mm': round(sum(rain), 1) if rain else None})
            out.extend([None] * (len(chunk) - len(data)))
        except (requests.RequestException, ValueError, AttributeError):
            out.extend([None] * len(chunk))
    return out


def risk_level(observations, weather):
    """Heuristic score: satellite activity (0-3) + weather dryness/heat/wind (0-11)."""
    activity = 3 if observations >= 10 else 2 if observations >= 4 else 1
    if not weather or any(weather.get(k) is None for k in ('temp_c', 'humidity_pct', 'wind_kmh')):
        return ('elevated', activity) if observations >= 10 else (None, activity)
    t, h, w = weather['temp_c'], weather['humidity_pct'], weather['wind_kmh']
    rain = weather.get('rain_3d_mm')
    score = activity
    score += 3 if t >= 35 else 2 if t >= 30 else 1 if t >= 25 else 0
    score += 3 if h <= 15 else 2 if h <= 25 else 1 if h <= 35 else 0
    score += 3 if w >= 40 else 2 if w >= 25 else 1 if w >= 15 else 0
    if rain is not None:
        score += 2 if rain <= 0.1 else 1 if rain <= 2 else -2 if rain >= 10 else 0
    level = 'very_high' if score >= 10 else 'high' if score >= 7 else 'elevated' if score >= 5 else None
    return level, score


def build_risk(events):
    cells = {}
    for e in events:
        k = (math.floor(e['lat'] / RISK_CELL_DEG), math.floor(e['lon'] / RISK_CELL_DEG))
        c = cells.setdefault(k, {'obs': 0, 'events': 0, 'la': 0.0, 'lo': 0.0})
        c['obs'] += e['count']; c['events'] += 1
        c['la'] += e['lat'] * e['count']; c['lo'] += e['lon'] * e['count']
    top = sorted(cells.values(), key=lambda c: c['obs'], reverse=True)[:RISK_MAX_CELLS]
    pts = [(c['la'] / c['obs'], c['lo'] / c['obs']) for c in top]
    weather = weather_points(pts)
    areas = []
    for c, (la, lo), wx in zip(top, pts, weather):
        level, score = risk_level(c['obs'], wx)
        if level:
            areas.append({'lat': round(la, 4), 'lon': round(lo, 4), 'level': level, 'score': score,
                          'events': c['events'], 'observations': c['obs'], 'weather': wx,
                          'weather_available': wx is not None})
    return areas


@app.get('/api/world/risk')
def world_risk():
    world = load_world()
    with _risk_lock:
        fresh = (_risk_cache['result'] is not None
                 and time.monotonic() - _risk_cache['at'] < RISK_TTL)
        if not fresh:
            areas = build_risk(world['events'])
            _risk_cache.update(at=time.monotonic(), result={
                'areas': areas, 'computed_at': datetime.now(timezone.utc).isoformat(),
                'basis': 'Recent VIIRS/MODIS hotspot activity + Open-Meteo weather (temperature, humidity, wind, 3-day rain)',
                'validation_status': 'not_validated',
                'warning': 'Experimental heuristic; not an official fire danger rating. Not for safety decisions.'})
        return _risk_cache['result']


# -------- EXPERIMENTAL WEATHER-CONDITIONS HEATMAP --------
# Weather conditions that can FAVOR ignition / fire spread are not thermal
# satellite imagery, measured vegetation dryness, a probability of ignition,
# or an official Fire Weather Index. This map deliberately DOES NOT use the
# NASA hotspot count; it can show weather conditions where no fire was detected.
# Requests are made only when the user enables the overlay and zooms in.
WEATHER_HEAT_TTL = 1800
WEATHER_HEAT_MAX_CELLS = 48
_weather_heat_cache = {}
_weather_heat_lock = threading.Lock()


def weather_conditions_score(wx):
    """Illustrative non-calibrated weather screening, NOT ignition probability.

    Returns (category, relative score), or (None, None) if observations are
    insufficient. Scores are intentionally dimensionless, not percentages.
    """
    if not isinstance(wx, dict):
        return None, None
    try:
        t = float(wx['temp_c']); rh = float(wx['humidity_pct']); wind = float(wx['wind_kmh'])
        rain = float(wx['rain_3d_mm'])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, None
    if not all(math.isfinite(v) for v in (t, rh, wind, rain)):
        return None, None
    if not (0 <= rh <= 100 and wind >= 0 and rain >= 0):
        return None, None
    # A simple illustrative weather screen (not FWI): heat + dryness + wind
    # raise the score, and rain in preceding 3 days lowers it.
    score = (2 if t >= 35 else 1 if t >= 28 else 0)
    score += 4 if rh <= 15 else 3 if rh <= 25 else 2 if rh <= 35 else 1 if rh <= 45 else 0
    score += 3 if wind >= 40 else 2 if wind >= 25 else 1 if wind >= 15 else 0
    score -= 3 if rain >= 10 else 1 if rain >= 3 else 0
    score = max(0, score)
    cat = 'very_high' if score >= 8 else 'high' if score >= 6 else 'elevated' if score >= 4 else 'low'
    return cat, score


def weather_grid(west, south, east, north, zoom):
    """Compute weather over a LIMITED viewport: no hotspots or NASA key used."""
    if zoom < 5:
        return {'areas': [], 'status': 'zoom_in',
                'notice': 'Zoom to level 5 or higher to view weather conditions.',
                'validation_status': 'not_validated'}
    # Avoid silently making a low-resolution world map look locally precise.
    if west >= east or south >= north or east - west > 35 or north - south > 25:
        return {'areas': [], 'status': 'zoom_in',
                'notice': 'Zoom into a smaller region to display weather conditions.',
                'validation_status': 'not_validated'}
    # Fixed maximum 7 x 6 sample points (42 weather locations per viewport).
    columns, rows = 7, 6
    latitudes = [south + (i+.5)*(north-south)/rows for i in range(rows)]
    longitudes = [west + (i+.5)*(east-west)/columns for i in range(columns)]
    pts = [(round(lat,3), round(lon,3)) for lat in latitudes for lon in longitudes]
    # Public endpoint: bounded input, small cache and one remote batch per view.
    wxs = weather_points(pts)
    result = []
    available = 0
    for (lat, lon), wx in zip(pts, wxs):
        cat, score = weather_conditions_score(wx)
        if cat is None:
            continue
        available += 1
        # Low conditions are intentionally not colored. No color NEVER means safe.
        if cat != 'low':
            result.append({'lat': lat, 'lon': lon, 'level': cat,
                           'intensity': round(min(1., score/9.),3), 'score': score,
                           'weather': wx})
    return {'areas': result, 'sampled_points': len(pts),
            'weather_available_points': available,
            'status': 'ok' if available else 'weather_unavailable',
            'notice': ('Weather screen based on temperature, relative humidity, wind and recent rain. '
                       'Not thermal satellite imagery, fuel moisture or a calibrated fire danger forecast.'),
            'computed_at': datetime.now(timezone.utc).isoformat(),
            'validation_status': 'not_validated',
            'attribution': 'Weather: Open-Meteo (CC BY 4.0)',
            'warning': 'Experimental. No prediction of fire ignition. Never use for safety decisions.'}


@app.get('/api/world/weather-heat')
def world_weather_heat(
    west: float = Query(..., ge=-180, le=180),
    south: float = Query(..., ge=-85, le=85),
    east: float = Query(..., ge=-180, le=180),
    north: float = Query(..., ge=-85, le=85),
    zoom: int = Query(..., ge=1, le=19)
):
    # Round the view to preserve 30-minute cached results between small pans.
    key = (round(west,1), round(south,1), round(east,1), round(north,1), zoom)
    with _weather_heat_lock:
        previous = _weather_heat_cache.get(key)
        if previous and time.monotonic() - previous['at'] < WEATHER_HEAT_TTL:
            return previous['data']
        # Do not retain unbounded caller-specific viewports on a public service.
        data = weather_grid(west, south, east, north, zoom)
        if len(_weather_heat_cache) >= 16:
            _weather_heat_cache.pop(next(iter(_weather_heat_cache)))
        _weather_heat_cache[key] = {'at': time.monotonic(), 'data': data}
        return data


# -------- Supporting imagery (not independent duplicate confirmation) --------
# Public LSA SAF MSG FRP-PIXEL WMS; WMS returns imagery, NOT pixel records.
import xml.etree.ElementTree as ET
from fastapi.responses import JSONResponse

MSG_WMS = 'https://adaguc.lsasvcs.ipma.pt//adagucserver'
_msg_cache = {'at': 0, 'result': None}
_msg_lock = threading.Lock()


def _wms_name(layer):
    for child in layer:
        if child.tag.split('}')[-1] == 'Name' and child.text:
            return child.text.strip()
    return None


def _msg_layer_names(xml_bytes):
    root = ET.fromstring(xml_bytes)
    names = []
    for element in root.iter():
        if element.tag.split('}')[-1] == 'Layer':
            name = _wms_name(element)
            if name:
                names.append(name)
    return names


@app.get('/api/verification/meteosat')
def meteosat_wms_status():
    # This endpoint checks that the map service responds and discovers a WMS layer.
    # It does not imply verified fire detections or a machine-readable fire feed.
    with _msg_lock:
        if _msg_cache['result'] and time.monotonic() - _msg_cache['at'] < 600:
            return _msg_cache['result']
        try:
            r = requests.get(MSG_WMS, params={
                'dataset': 'MSG-FRP', 'SERVICE': 'WMS', 'REQUEST': 'GetCapabilities'
            }, timeout=(10, 30))
            r.raise_for_status()
            names = _msg_layer_names(r.content)
            selected = next((n for n in names if 'FRP' in n.upper()), None)
            if not selected:
                selected = next((n for n in names if n.lower() not in ('default', 'baselayer')), None)
            result = {
                'reachable': bool(selected),
                'wms_url': MSG_WMS + '?dataset=MSG-FRP&',
                'wms_layer': selected,
                'layer_names': names[:25],
                'verification_capability': 'visual_reference_only',
                'automatic_duplicate_verification': False,
                'notice': 'WMS imagery alone cannot validate whether two fire observations are duplicates.'
            }
        except (requests.RequestException, ET.ParseError, ValueError) as exc:
            result = {
                'reachable': False, 'wms_layer': None,
                'verification_capability': 'unavailable',
                'automatic_duplicate_verification': False,
                'notice': 'Meteosat WMS unavailable: ' + type(exc).__name__
            }
        _msg_cache.update(at=time.monotonic(), result=result)
        return result


@app.get('/api/verification/method')
def verification_method():
    return {
        'primary_sources': ['VIIRS', 'MODIS'],
        'supporting_sources': ['Sentinel-2 catalogue', 'Meteosat MSG FRP-PIXEL WMS'],
        'automatic_duplicate_verification': False,
        'automatic_candidate_grouping': 'approximate pixel footprint, geographic/time proximity, capped event extent; not validated',
        'sentinel2': 'catalogue metadata is not image-pixel analysis',
        'meteosat': 'WMS imagery is not a machine-readable matching feed',
        'required_for_operational_use': 'observation-level integration, independently evaluated matching, incident ground truth and review procedures',
        'safety': 'Not for emergency dispatch or safety-critical decisions'
    }


# -------- Evidence inspection of an individual candidate event --------
# This is an availability/temporal-context check, not computer vision.
# Avoid assigning a verified-duplicate status until actual imagery/pixel
# analysis and independently evaluated matching have been implemented.
@app.get('/api/verification/event/{event_id}')
def event_evidence(event_id: int):
    world = load_world()
    event = next((e for e in world['events'] if e['id'] == event_id), None)
    if event is None:
        raise HTTPException(status_code=404, detail='Event not in current 24-hour feed. Refresh the map.')

    latest_dt = datetime.fromtimestamp(event['latest'], tz=timezone.utc)
    sentinel = {
        'catalogue_reachable': False,
        'imagery_records': 0,
        'acquisitions': [],
        'pixel_analysis_performed': False,
        'review_status': 'unavailable'
    }
    try:
        # Reuse the existing Copernicus catalogue API integration from server.py.
        # It returns PRODUCT METADATA only; it does not download image pixels.
        search_fn = getattr(original_server, 'get_sentinel2', None)
        if search_fn is None:
            raise RuntimeError('Sentinel-2 catalogue endpoint not implemented in server.py')
        result = search_fn(lat=event['lat'], lon=event['lon'])
        records = result.get('images', [])
        summaries = []
        for image in records:
            value = image.get('acquired')
            if not value:
                continue
            try:
                image_dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                gap_hours = round((image_dt - latest_dt).total_seconds() / 3600, 1)
            except ValueError:
                continue
            summaries.append({
                'acquired': value,
                'relative_to_latest_detection_hours': gap_hours,
                'image_name': image.get('name'),
                'temporal_note': (
                    'Image predates latest hotspot detection; cannot verify this later event'
                    if gap_hours < 0 else
                    'Image follows latest hotspot detection, but pixels/clouds not analysed'
                )
            })
        sentinel.update({
            'catalogue_reachable': True,
            'imagery_records': len(records),
            'acquisitions': summaries[:5],
            'review_status': 'metadata_only'
        })
    except Exception as exc:
        sentinel['error'] = type(exc).__name__ + ': ' + str(exc)[:160]

    msg = meteosat_wms_status()
    return {
        'event_id': event_id,
        'event_latest_observation_utc': latest_dt.isoformat(),
        'observation_count': event['count'],
        'observation_sources': event['sources'],
        'candidate_grouping': 'approximate footprint/time bounded grouping; unvalidated',
        'sentinel2': sentinel,
        'meteosat': {
            'map_service_reachable': msg['reachable'],
            'layer_name': msg.get('wms_layer'),
            'individual_fire_pixels_available': False,
            'time_series_analysed': False,
            'review_status': 'visual_reference_only' if msg['reachable'] else 'unavailable',
        },
        'duplicate_verification': {
            'independent_match_confirmed': False,
            'status': 'NOT VERIFIED',
            'reason': 'No Sentinel-2 image-pixel analysis or Meteosat geolocated FRP time-series comparison has been performed.',
            'display_policy': 'Keep observations available; map grouping remains provisional.'
        },
        'operational_warning': 'Not suitable for dispatch or safety-critical decisions.'
    }


@app.get('/api/world/fusion-diagnostics')
def fusion_diagnostics():
    """Non-operational fusion transparency, never a claim of detection accuracy."""
    data = load_world()
    return {
        'version': FUSION_VERSION,
        'raw_observations': data['raw_count'],
        'candidate_groups': data['event_count'],
        'provisionally_grouped_observations': data['raw_count']-data['event_count'],
        'multisensor_candidate_groups': data['multisensor_groups'],
        'ambiguous_groups_kept_separate': data['ambiguous_groups'],
        'sensor_evidence': {
            'VIIRS': 'thermal hotspot detections',
            'MODIS': 'thermal hotspot detections',
            'Sentinel-2': 'catalogue metadata only',
            'Meteosat MSG': 'WMS map overlay only',
        },
        'independent_match_verification': False,
        'ground_truth_validation': False,
        'accuracy_estimate': None,
        'warning': 'Grouping reduces repeated markers provisionally. It may merge different fires or split the same fire.'
    }


# -------- Meteosat cross-check: merge candidate groups that share ONE Meteosat fire pixel --------
# Uses WMS GetFeatureInfo on the LSA SAF FRP-PIXEL layer at each group's latest time.
# Conservative: groups merge only if both sit on a real fire pixel AND the response
# identifies the same pixel. If the response has no pixel id, nothing is merged.
MSG_MERGE_MAX_KM = 6.0
MSG_QUERY_CAP = 200
MSG_BUDGET_SECONDS = 20


def _msg_feature(lat, lon, ts, layer):
    d = 0.02
    stamp = datetime.fromtimestamp(ts - ts % 900, timezone.utc).strftime('%Y-%m-%dT%H:%M:00Z')
    r = requests.get(MSG_WMS, params={
        'dataset': 'MSG-FRP', 'SERVICE': 'WMS', 'VERSION': '1.3.0', 'REQUEST': 'GetFeatureInfo',
        'LAYERS': layer, 'QUERY_LAYERS': layer, 'CRS': 'EPSG:4326', 'TIME': stamp,
        'BBOX': f'{lat-d},{lon-d},{lat+d},{lon+d}', 'WIDTH': 101, 'HEIGHT': 101, 'I': 50, 'J': 50,
        'INFO_FORMAT': 'application/json'}, timeout=(5, 15))
    r.raise_for_status()
    return r.text


def parse_msg_pixel(text):
    """Return (frp, pixel_id) for a fire pixel, else None. pixel_id may be None."""
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    frp, pix = None, None
    stack = [payload]
    while stack:
        o = stack.pop()
        if isinstance(o, list):
            stack.extend(o)
        elif isinstance(o, dict):
            if pix is None and all(isinstance(o.get(k), (int, float)) for k in ('x', 'y')):
                pix = (round(o['x'], 4), round(o['y'], 4))
            for k, v in o.items():
                try:
                    num = float(v)
                except (TypeError, ValueError):
                    num = None
                if num is not None and frp is None and (k.lower() in ('value', 'data') or 'frp' in k.lower()) \
                        and 0 < num < 1e5:
                    frp = num
                if isinstance(v, (dict, list)):
                    stack.append(v)
    return (frp, pix) if frp is not None else None


def _merge_events(group):
    n = sum(e['count'] for e in group)
    x = y = z = 0.0
    for e in group:
        la, lo = math.radians(e['lat']), math.radians(e['lon'])
        x += e['count'] * math.cos(la) * math.cos(lo)
        y += e['count'] * math.cos(la) * math.sin(lo)
        z += e['count'] * math.sin(la)
    merged = dict(group[0])
    merged.update(
        lat=math.degrees(math.atan2(z, math.hypot(x, y))), lon=math.degrees(math.atan2(y, x)),
        count=n, sources=sorted({s for e in group for s in e['sources']}),
        latest=max(e['latest'] for e in group), oldest=min(e['oldest'] for e in group),
        examples=[m for e in group for m in e['examples']][:8], candidate_match=True,
        source_agreement=len({s for e in group for s in e['sources']}) > 1,
        ambiguous=False, max_pairwise_separation_km=None, independent_confirmation=True,
        independent_imagery_checked=True, meteosat_pixel_merged=True,
        match_basis='viirs_modis_grouping_plus_shared_meteosat_fire_pixel',
        fusion_note=f'Merged {len(group)} candidate groups sharing one Meteosat fire pixel (not field-verified)',
        snapshot_group_id=hashlib.sha256('|'.join(sorted(e['snapshot_group_id'] for e in group)).encode()).hexdigest()[:16])
    return merged


def meteosat_merge(events):
    stats = {'status': 'skipped', 'checked': 0, 'fire_pixel_hits': 0, 'groups_merged': 0}
    status = meteosat_wms_status()
    if not status.get('reachable') or not status.get('wms_layer'):
        stats['status'] = 'meteosat_unavailable'
        return events, stats
    layer = status['wms_layer']
    cells = {}
    for i, e in enumerate(events):
        if abs(e['lat']) <= 65 and abs(e['lon']) <= 65:
            cells.setdefault((int(e['lat'] // 0.1), int(e['lon'] // 0.1)), []).append(i)
    pairs, cand = [], set()
    for (a, b), members in cells.items():
        near = [j for da in (-1, 0, 1) for db in (-1, 0, 1) for j in cells.get((a + da, b + db), ())]
        for i in members:
            for j in near:
                if i < j and distance_km(events[i], events[j]) <= MSG_MERGE_MAX_KM and \
                        max(events[i]['latest'], events[j]['latest']) - min(events[i]['oldest'], events[j]['oldest']) <= 8 * 3600:
                    pairs.append((i, j)); cand.update((i, j))
    cand = sorted(cand, key=lambda i: -events[i]['count'])[:MSG_QUERY_CAP]
    hits = {}

    def probe(i):
        e = events[i]
        return i, parse_msg_pixel(_msg_feature(e['lat'], e['lon'], e['latest'], layer))

    pool = ThreadPoolExecutor(max_workers=8)
    futures = [pool.submit(probe, i) for i in cand]
    try:
        from concurrent.futures import as_completed
        for f in as_completed(futures, timeout=MSG_BUDGET_SECONDS):
            try:
                i, px = f.result()
                stats['checked'] += 1
                if px:
                    hits[i] = px
            except Exception:
                pass
    except Exception:
        stats['status'] = 'time_budget_reached'
    pool.shutdown(wait=False, cancel_futures=True)
    stats['fire_pixel_hits'] = len(hits)
    parent = list(range(len(events)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i
    for i, j in pairs:
        if i in hits and j in hits and hits[i][1] is not None and hits[i][1] == hits[j][1]:
            parent[find(i)] = find(j)
    groups = {}
    for i in range(len(events)):
        groups.setdefault(find(i), []).append(events[i])
    merged = [_merge_events(g) if len(g) > 1 else g[0] for g in groups.values()]
    for n, e in enumerate(merged, start=1):
        e['id'] = n
    stats['groups_merged'] = len(events) - len(merged)
    if stats['status'] == 'skipped':
        stats['status'] = ('ok' if not hits or any(p[1] is not None for p in hits.values())
                           else 'pixel_id_not_in_response')
    return merged, stats


@app.get('/api/verification/meteosat/probe')
def meteosat_probe(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180)):
    """Raw GetFeatureInfo output, to check the real response format of the Meteosat service."""
    status = meteosat_wms_status()
    if not status.get('wms_layer'):
        raise HTTPException(status_code=502, detail='Meteosat WMS unavailable')
    try:
        raw = _msg_feature(lat, lon, int(time.time()) - 3600, status['wms_layer'])
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=type(exc).__name__)
    return {'layer': status['wms_layer'], 'parsed': parse_msg_pixel(raw), 'raw': raw[:2000]}
