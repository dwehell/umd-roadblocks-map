
    
  
"""Scrape UMD outdoor impacts and automatically locate clear campus references.
Install: pip install requests beautifulsoup4
"""
import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
SOURCE = 'https://facilities.umd.edu/info-resources/service-impacts'
BUILDINGS = 'https://api.umd.io/v1/map/buildings'
OUTPUT = ROOT / 'disturbances.json'
# Approximate reference locations only, NOT verified closures.
ALIASES = {
    'regents drive': (38.9895, -76.9415),
    'parking lot ss1': (38.9905, -76.9455),
    'campus drive': (38.9869, -76.9426),
    'stadium drive': (38.9905, -76.9395),
    'parking lot z': (38.9950, -76.9480),
}
OUTDOOR = re.compile(r'\b(?:road(?:way)?|street|sidewalk|walkway|path(?:way)?|pedestrian|crosswalk|intersection|bridge|trail|bike\s*lane|parking\s*(?:lot|garage|area)|bus\s*stop|shuttle\s*stop|vehicle\s*access|traffic|detour|outdoor\s*access)\b', re.I)
IMPACT = re.compile(r'\b(?:clos(?:ed|ure|ing)|block(?:ed|age|ing)?|restrict(?:ed|ion|ions)?|detour(?:ed|s)?|inaccessible|impassable|rerout(?:e|ed|ing)|repair(?:s)?|construction|excavat(?:ion|ing)|resurfac(?:ing|e)|pav(?:e|ing|ement)|temporarily unavailable|partial(?:ly)?\s+closed)\b', re.I)

def norm(s):
    return ' '.join(re.sub(r'[^\w\s]', ' ', str(s or '').lower()).split())

def valid(lat, lng):
    try:
        return lat is not None and lng is not None and 38.8 <= float(lat) <= 39.1 and -77.1 <= float(lng) <= -76.8
    except (TypeError, ValueError):
        return False

def fetch_buildings():
    result = {}
    try:
        # Endpoint may be paginated; fetch successive pages if indicated.
        url = BUILDINGS
        visited = set()
        for _ in range(30):
            if url in visited:
                break
            visited.add(url)
            response = requests.get(url, timeout=20)
            response.raise_for_status()
            payload = response.json()
            rows = payload if isinstance(payload, list) else payload.get('data', [])
            if isinstance(rows, dict):
                rows = [rows]
            if not isinstance(rows, list):
                break
            for b in rows:
                if not isinstance(b, dict):
                    continue
                if valid(b.get('lat'), b.get('long', b.get('lng'))):
                    coords = (float(b['lat']), float(b.get('long', b.get('lng'))))
                    for label in (b.get('name'), b.get('code')):
                        key = norm(label)
                        if key and len(key) >= 3:
                            result[key] = coords
            next_url = response.links.get('next', {}).get('url')
            if not next_url:
                break
            url = next_url
    except (requests.RequestException, ValueError, TypeError) as exc:
        print('Building API unavailable or incomplete:', exc)
    return result

# OpenStreetMap roads are loaded dynamically; no manually maintained road list needed.
OVERPASS = "https://overpass.kumi.systems/api/interpreter"
CAMPUS_BBOX = (38.974, -76.960, 39.001, -76.925)

def fetch_roads():
    """Return named road geometries near campus; fail safely if offline."""
    south, west, north, east = CAMPUS_BBOX
    query = (f'[out:json][timeout:25];way["highway"]["name"]'
             f'({south},{west},{north},{east});out geom;')
    try:
        response = requests.post(OVERPASS, data={"data": query}, timeout=40)
        response.raise_for_status()
        roads = {}
        for item in response.json().get("elements", []):
            name = norm(item.get("tags", {}).get("name"))
            points = [(p["lat"], p["lon"]) for p in item.get("geometry", [])
                      if valid(p.get("lat"), p.get("lon"))]
            if name and points:
                roads.setdefault(name, []).append(points)
        return roads
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        print("Road data unavailable:", exc)
        return {}


def road_reference(road_segments):

