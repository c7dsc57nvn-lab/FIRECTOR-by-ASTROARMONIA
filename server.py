"""Small public-facing website backend. Fire Fusion is in world_server.py."""
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
NASA_KEY = os.getenv('NASA_FIRMS_KEY', '').strip()
app = FastAPI(title='FIRECTOR by ASTROARMONIA')

@app.get('/')
def home():
    return FileResponse(ROOT / 'index.html')

@app.get('/healthz')
def healthz():
    return {'app': 'FIRECTOR', 'status': 'running', 'nasa_key_configured': bool(os.getenv('NASA_FIRMS_KEY'))}

COPERNICUS_URL = 'https://catalogue.dataspace.copernicus.eu/odata/v1/Products'

@app.get('/api/sentinel2')
def get_sentinel2(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180)):
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    end = now.strftime('%Y-%m-%dT%H:%M:%S.000Z')
    point = f'POINT({lon} {lat})'
    filt = ("Collection/Name eq 'SENTINEL-2' and contains(Name,'MSIL2A') "
            f"and OData.CSC.Intersects(area=geography'SRID=4326;{point}') "
            f'and ContentDate/Start gt {start} and ContentDate/Start lt {end}')
    try:
        resp = requests.get(COPERNICUS_URL, params={'$filter': filt, '$orderby': 'ContentDate/Start desc', '$top': 5}, timeout=45)
        resp.raise_for_status()
        results = [{'id': p.get('Id'), 'name': p.get('Name'),
                    'acquired': p.get('ContentDate', {}).get('Start'),
                    'footprint': p.get('GeoFootprint')} for p in resp.json().get('value', [])]
        return {'source': 'Sentinel-2', 'status': 'connected', 'location': {'lat':lat, 'lon':lon},
                'count': len(results), 'images': results,
                'purpose':'High-resolution imagery review',
                'note':'Catalogue metadata only; it does not verify fire pixels or duplicated incidents.'}
    except (requests.RequestException, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f'Copernicus connection failed: {type(exc).__name__}')
