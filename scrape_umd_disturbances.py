
"""UMD outdoor disturbance scraper with automatic location matching."""

import json
import re
import difflib
import hashlib
from datetime import datetime, date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
SOURCE_URL = "https://facilities.umd.edu/info-resources/service-impacts"
BUILDINGS_API = "https://api.umd.io/v1/map/buildings"
OUTPUT = BASE / "disturbances.json"

# Approximate reference points, not exact closure locations.
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
    return " ".join(
        re.sub(r"[^\w\s]", " ", str(value or "").lower()).split()
    )


def valid_coords(lat, lng):
    try:
        return (
            lat is not None
            and lng is not None
            and 38.8 <= float(lat) <= 39.1
            and -77.1 <= float(lng) <= -76.8
        )
    except (ValueError, TypeError):
        return False


def fetch_buildings():
    directory = {}

    try:
        response = requests.get(BUILDINGS_API, timeout=20)
        response.raise_for_status()
        data = response.json()

        if isinstance(data, dict):
            data = data.get("data", [])

        for building in data if isinstance(data, list) else []:
            if not isinstance(building, dict):
                continue

            name = normalize(building.get("name"))
            lat = building.get("lat")
            lng = building.get("long", building.get("lng"))

            if name and valid_coords(lat, lng):
                directory[name] = (float(lat), float(lng))

    except (requests.RequestException, ValueError, TypeError) as e:
        print("Building API unavailable:", e)

    return directory


def match_location(name, directory):
    key = normalize(name)

    if not key:
        return None, None, "unmatched", True

    # Avoid assigning one marker to multiple locations.
    if (
        re.search(r"[,;/]", str(name))
        or re.search(r"\b(and|between|multiple)\b", key)
    ):
        return None, None, "multiple_or_ambiguous_locations", True

    if key in ALIASES:
        return *ALIASES[key], "approximate_road_reference", True

    if key in directory:
        return *directory[key], "building_reference", True

    matches = difflib.get_close_matches(
        key, list(directory), n=2, cutoff=0.92
    )

    if len(matches) == 1:
        return (
            *directory[matches[0]],
            "fuzzy_building_reference",
            True
        )

    return None, None, "unmatched", True


def parse_date(value):
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
                str(value or "").strip(), fmt
            ).date().isoformat()
        except ValueError:
            continue

    return None


def compute_status(start, end):
    today = date.today().isoformat()

    if end and end < today:
        return "expired"

    if start and start > today:
        return "upcoming"

    if not end:
        return "needs_verification"

    return "active"


def scrape():
    directory = fetch_buildings()
    print("Building locations loaded:", len(directory))

    response = requests.get(SOURCE_URL, timeout=25)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    results = []
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

            impact = " ".join((area, repair_type, description))

            if not (
                OUTDOOR.search(" ".join((location, impact)))
                and IMPACT.search(impact)
            ):
                continue

            start_date = parse_date(start)
            end_date = parse_date(finish)

            raw = "|".join((
                normalize(location),
                normalize(area),
                str(start_date or ""),
                normalize(repair_type)
            ))

            entry_id = hashlib.sha256(
                raw.encode("utf-8")
            ).hexdigest()[:16]

            if entry_id in seen:
                continue

            seen.add(entry_id)

            lat, lng, source, review = match_location(
                location, directory
            )

            results.append({
                "id": entry_id,
                "name": location,
                "area": area,
                "lat": lat,
                "lng": lng,
                "candidate_lat": lat,
                "candidate_lng": lng,
                "type": repair_type,
                "description": description,
                "impact_type": impact_type,
                "start_date": start_date,
                "end_date": end_date,
                "needs_review": review,
                "coordinate_source": source,
                "source_url": SOURCE_URL,
                "status": compute_status(start_date, end_date)
            })

    return results


def main():
    data = scrape()

    OUTPUT.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    mapped = sum(
        valid_coords(d["lat"], d["lng"])
        for d in data
    )

    print("Total notices:", len(data))
    print("Automatically mapped:", mapped)
    print("Unmapped:", len(data) - mapped)
    print("Saved:", OUTPUT)


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as e:
        print("Scraping failed:", e)
        raise SystemExit(1)
