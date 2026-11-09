
"""UMD outdoor disturbance scraper with location matching.

Install:
    pip install requests beautifulsoup4

Run:
    python scrape_umd_disturbances.py

Files:
    disturbances.json       Automatically generated
    verified_locations.json Optional manual location approvals

Only verified coordinates are published as map-ready.
Building coordinates are approximate reference points, not
exact roadblock locations.
"""

import json
import re
import difflib
import hashlib
from datetime import datetime, date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent

SOURCE_URL = (
    "https://facilities.umd.edu/info-resources/service-impacts"
)

BUILDINGS_API = "https://api.umd.io/v1/map/buildings"

OUTPUT = BASE / "disturbances.json"
VERIFIED_FILE = BASE / "verified_locations.json"

# These are approximate reference points only.
# They must NOT be treated as verified closure locations.
ALIASES = {
    "regents drive": (38.9895, -76.9415),
    "parking lot ss1": (38.9905, -76.9455),
    "campus drive": (38.9869, -76.9426),
    "stadium drive": (38.9905, -76.9395),
    "parking lot z": (38.9950, -76.9480),
}

OUTDOOR = re.compile(
    r"\b(?:road(?:way)?|street|sidewalk|walkway|"
    r"path(?:way)?|pedestrian|crosswalk|intersection|"
    r"bridge|trail|bike\s*lane|parking\s*(?:lot|garage|area)|"
    r"bus\s*stop|shuttle\s*stop|vehicle\s*access|"
    r"traffic|detour|outdoor\s*access)\b",
    re.I
)

IMPACT = re.compile(
    r"\b(?:clos(?:ed|ure|ing)|block(?:ed|age|ing)?|"
    r"restrict(?:ed|ion|ions)?|detour(?:ed|s)?|"
    r"inaccessible|impassable|rerout(?:e|ed|ing)|"
    r"repair(?:s)?|construction|excavat(?:ion|ing)|"
    r"resurfac(?:ing|e)|pav(?:e|ing|ement)|"
    r"temporarily unavailable|partial(?:ly)?\s+closed)\b",
    re.I
)


def normalize(value):
    value = str(value or "").lower().strip()
    value = re.sub(r"[^\w\s]", " ", value)
    return " ".join(value.split())


def valid_coords(lat, lng):
    try:
        if lat is None or lng is None:
            return False
        if str(lat).strip() == "" or str(lng).strip() == "":
            return False

        lat = float(lat)
        lng = float(lng)

        return (
            38.8 <= lat <= 39.1
            and -77.1 <= lng <= -76.8
        )
    except (ValueError, TypeError):
        return False


def fetch_buildings():
    directory = {}

    try:
        response = requests.get(
            BUILDINGS_API,
            timeout=20
        )
        response.raise_for_status()

        data = response.json()

        if isinstance(data, dict):
            data = data.get("data", [])

        if not isinstance(data, list):
            return directory

        for building in data:
            name = normalize(building.get("name"))

            lat = building.get("lat")
            lng = building.get("long", building.get("lng"))

            if valid_coords(lat, lng) and name:
                directory[name] = (
                    float(lat),
                    float(lng)
                )

    except (requests.RequestException, ValueError, TypeError) as e:
        print("Building API unavailable:", e)

    return directory


def match_location(name, directory):
    key = normalize(name)

    normalized_aliases = {
        normalize(k): v for k, v in ALIASES.items()
    }

    if key in normalized_aliases:
        lat, lng = normalized_aliases[key]
        return lat, lng, "approximate_alias"

    if key in directory:
        lat, lng = directory[key]
        return lat, lng, "building_reference"

    matches = difflib.get_close_matches(
        key,
        list(directory.keys()),
        n=1,
        cutoff=0.85
    )

    if matches:
        lat, lng = directory[matches[0]]
        return lat, lng, "fuzzy_building_reference"

    return None, None, "unmatched"


def parse_date(value):
    value = str(value or "").strip()

    formats = [
        "%m-%d-%Y %I:%M %p",
        "%m/%d/%Y %I:%M %p",
        "%m-%d-%Y",
        "%m/%d/%Y",
        "%Y-%m-%d"
    ]

    for fmt in formats:
        try:
            return datetime.strptime(
                value, fmt
            ).date().isoformat()
        except ValueError:
            continue

    return None


def compute_status(entry):
    today = date.today().isoformat()

    start = entry.get("start_date")
    end = entry.get("end_date")

    if end and end < today:
        return "expired"

    if start and start > today:
        return "upcoming"

    if not end:
        return "needs_verification"

    return "active"


def is_outdoor(location, area, repair_type, description):
    impact = " ".join([
        area, repair_type, description
    ])

    full = " ".join([location, impact])

    return bool(
        OUTDOOR.search(full)
        and IMPACT.search(impact)
    )


def make_id(location, area, start, repair_type):
    raw = "|".join([
        normalize(location),
        normalize(area),
        str(start or ""),
        normalize(repair_type)
    ])

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:16]


def load_verified():
    if not VERIFIED_FILE.exists():
        return {}

    try:
        data = json.loads(
            VERIFIED_FILE.read_text(encoding="utf-8")
        )

        if not isinstance(data, dict):
            return {}

        return data

    except (OSError, ValueError) as e:
        print("Verification file error:", e)
        return {}


def scrape():
    directory = fetch_buildings()
    verified = load_verified()

    print("Building locations loaded:", len(directory))

    response = requests.get(
        SOURCE_URL,
        timeout=25
    )
    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    disturbances = []
    seen = set()

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):

            cells = [
                cell.get_text(" ", strip=True)
                for cell in row.find_all("td")
            ]

            if len(cells) < 7:
                continue

            (
                location,
                area,
                start,
                finish,
                repair_type,
                description,
                impact_type
            ) = cells[:7]

            if not is_outdoor(
                location,
                area,
                repair_type,
                description
            ):
                continue

            start_date = parse_date(start)
            end_date = parse_date(finish)

            entry_id = make_id(
                location,
                area,
                start_date,
                repair_type
            )

            if entry_id in seen:
                continue

            seen.add(entry_id)

            candidate_lat, candidate_lng, source = (
                match_location(location, directory)
            )

            # Candidate coordinates are not published
            # as verified map coordinates.
            entry = {
                "id": entry_id,
                "name": location,
                "area": area,
                "lat": None,
                "lng": None,
                "candidate_lat": candidate_lat,
                "candidate_lng": candidate_lng,
                "type": repair_type,
                "description": description,
                "impact_type": impact_type,
                "start_date": start_date,
                "end_date": end_date,
                "needs_review": True,
                "coordinate_source": source,
                "source_url": SOURCE_URL
            }

            entry["status"] = compute_status(entry)

            # Restore manually verified location
            # only if approved for this exact notice ID.
            approved = verified.get(entry_id)

            if isinstance(approved, dict):
                lat = approved.get("lat")
                lng = approved.get("lng")

                if (
                    approved.get("verified") is True
                    and valid_coords(lat, lng)
                ):
                    entry["lat"] = float(lat)
                    entry["lng"] = float(lng)
                    entry["needs_review"] = False
                    entry["coordinate_source"] = (
                        "human_verified"
                    )

            disturbances.append(entry)

    return disturbances


def main():
    data = scrape()

    OUTPUT.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    mapped = sum(
        1 for d in data
        if valid_coords(d["lat"], d["lng"])
        and not d["needs_review"]
    )

    print("Total notices:", len(data))
    print("Verified locations:", mapped)
    print("Awaiting location review:", len(data) - mapped)
    print("Saved:", OUTPUT)

    for d in data:
        if d["needs_review"]:
            print(
                "[REVIEW]",
                d["id"],
                d["name"],
                d["coordinate_source"]
            )


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as e:
        print("Scraping failed:", e)
        raise SystemExit(1)
