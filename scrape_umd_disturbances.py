"""Scrape UMD Facilities service impacts for OUTDOOR mobility disruptions.

Install: py -m pip install requests beautifulsoup4
Run:     py scrape_umd_disturbances.py
Output:  disturbances.json (in the current working directory)

Only explicit outdoor access disruptions are retained. Review output before
publishing: building coordinates and street aliases are approximate points,
not exact closure geometry. This is NOT a scraper for maps.umd.edu.
"""
import json
import re
import difflib
from datetime import datetime, date
from pathlib import Path
import requests
from bs4 import BeautifulSoup

SOURCE_URL = "https://facilities.umd.edu/info-resources/service-impacts"
UMD_BUILDINGS_API = "https://api.umd.io/v1/map/buildings"

ALIAS_OVERRIDES = {
    "regents drive": (38.9895, -76.9415),
    "parking lot ss1": (38.9905, -76.9455),
    "campus drive": (38.9869, -76.9426),
    "stadium drive": (38.9905, -76.9395),
    "purple line, campus-wide": (38.9869, -76.9426),
    "parking lot z": (38.9950, -76.9480),
}

# Only include entries explicitly describing outdoor mobility/access impacts.
OUTDOOR_ACCESS = re.compile(
    r"\b(?:road(?:way)?|street|sidewalk|walkway|path(?:way)?|pedestrian|"
    r"crosswalk|intersection|bridge|trail|bike\s*lane|cycle\s*track|"
    r"parking\s*(?:lot|garage|area)|bus\s*stop|shuttle\s*stop|"
    r"vehicle\s*access|traffic|detour|outdoor\s*access)\b", re.I
)
ACCESS_IMPACT = re.compile(
    r"\b(?:clos(?:ed|ure|ing)|block(?:ed|age|ing)?|restrict(?:ed|ion|ions)?|"
    r"detour(?:ed|s)?|inaccessible|impassable|rerout(?:e|ed|ing)|"
    r"repair(?:s)?|construction|excavat(?:ion|ing)|resurfac(?:ing|e)|"
    r"pav(?:e|ing|ement)|temporarily unavailable|partial(?:ly)?\s+closed)\b", re.I
)
INDOOR_ONLY = re.compile(
    r"\b(?:elevator|restroom|bathroom|hvac|air\s*conditioning|"
    r"heating|interior|indoors?|classroom|office|lab(?:oratory)?|"
    r"water\s*(?:outage|shutdown)|power\s*(?:outage|shutdown)|"
    r"fire\s*alarm|domestic\s*water)\b", re.I
)


def is_outdoor_disturbance(location, area, repair_type, description):
    """Conservative filter; ambiguous rows are excluded for human review."""
    impact = " ".join([area, repair_type, description])
    full = " ".join([location, impact])
    if not OUTDOOR_ACCESS.search(full) or not ACCESS_IMPACT.search(impact):
        return False
    # Reject obvious indoor-only issues unless outdoor access is explicit in impact text.
    if INDOOR_ONLY.search(impact) and not OUTDOOR_ACCESS.search(impact):
        return False
    return True


def fetch_building_directory():
    directory = {}
    try:
        response = requests.get(UMD_BUILDINGS_API, timeout=20)
        response.raise_for_status()
        for building in response.json():
            name = str(building.get("name") or "").strip().lower()
            lat, lng = building.get("lat"), building.get("long")
            if name and lat is not None and lng is not None:
                directory[name] = (float(lat), float(lng))
    except (requests.RequestException, ValueError, TypeError) as exc:
        print(f"[WARNING] Building directory unavailable: {exc}")
    return directory


def lookup_coords(location_name, building_directory):
    key = location_name.strip().lower()
    if key in ALIAS_OVERRIDES:
        return (*ALIAS_OVERRIDES[key], "approximate_alias")
    if key in building_directory:
        return (*building_directory[key], "building_location")
    matches = difflib.get_close_matches(key, building_directory.keys(), n=1, cutoff=0.8)
    if matches:
        return (*building_directory[matches[0]], "fuzzy_building_match")
    return (None, None, "unmatched")


def parse_date(value):
    value = value.strip()
    for fmt in ("%m-%d-%Y %I:%M %p", "%m/%d/%Y %I:%M %p", "%m-%d-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def compute_status(entry):
    today = date.today().isoformat()
    if entry["start_date"] and entry["start_date"] > today:
        return "upcoming"
    if entry["end_date"] and entry["end_date"] < today:
        return "expired"
    # Missing end date is not proof that a closure is still active.
    if not entry["end_date"]:
        return "needs_verification"
    return "active"


def scrape_disturbances():
    directory = fetch_building_directory()
    print(f"Loaded {len(directory)} UMD building locations.")
    response = requests.get(SOURCE_URL, timeout=20)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    disturbances = []
    inspected = 0
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
            if len(cells) < 7:
                continue
            inspected += 1
            location, area, start, finish, repair_type, description, impact_type = cells[:7]
            if not is_outdoor_disturbance(location, area, repair_type, description):
                continue
            lat, lng, coordinate_source = lookup_coords(location, directory)
            entry = {
                "id": len(disturbances) + 1,
                "name": location,
                "area": area,
                "lat": lat,
                "lng": lng,
                "type": repair_type,
                "description": description,
                "impact_type": impact_type,
                "start_date": parse_date(start),
                "end_date": parse_date(finish),
                "needs_review": coordinate_source in ("unmatched", "fuzzy_building_match", "approximate_alias"),
                "coordinate_source": coordinate_source,
                "source_url": SOURCE_URL,
            }
            entry["status"] = compute_status(entry)
            disturbances.append(entry)
    print(f"Checked {inspected} rows; kept {len(disturbances)} outdoor access disturbances.")
    return disturbances


if __name__ == "__main__":
    try:
        data = scrape_disturbances()
        output = Path(__file__).resolve().parent / "disturbances.json"
        output.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"Saved {len(data)} entries to {output}")
        for entry in data:
            if entry["needs_review"] or entry["status"] == "needs_verification":
                print(f"[REVIEW] {entry['name']}: {entry['coordinate_source']}; {entry['status']}")
        print("Review results manually before publishing. Filter may miss ambiguous outdoor notices.")
    except requests.RequestException as exc:
        print(f"[ERROR] Could not fetch Facilities page: {exc}")
        raise SystemExit(1)
