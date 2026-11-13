
    
  
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

def find_mentions(text, locations):
    """Find whole-name matches, preferring longer names over substrings."""
    normalized = norm(text)
    hits = []
    for name, coords in locations.items():
        if re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', normalized):
            hits.append((name, coords))
    hits.sort(key=lambda item: len(item[0]), reverse=True)
    chosen = []
    for name, coords in hits:
        if not any(name in bigger for bigger, _ in chosen):
            chosen.append((name, coords))
    return chosen

def locate(location, area, description, directory):
    """Use the location field first, then area, then description.
    Never silently select one of several different mentioned places.
    """
    references = {**directory, **{norm(k): v for k, v in ALIASES.items()}}
    for field, label in ((location, 'location'), (area, 'area'), (description, 'description')):
        hits = find_mentions(field, references)
        unique = {tuple(coords) for _, coords in hits}
        if len(unique) == 1:
            coords = next(iter(unique))
            return coords[0], coords[1], f'approximate_{label}_reference', True
        if len(unique) > 1:

