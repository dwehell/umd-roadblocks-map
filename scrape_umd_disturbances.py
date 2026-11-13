
import hashlib
import json
import math
import re
from datetime import date, datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent

SOURCE = "https://facilities.umd.edu/info-resources/service-impacts"
BUILDINGS = "https://api.umd.io/v1/map/buildings"
OVERPASS = "https://overpass.kumi.systems/api/interpreter"

OUTPUT = ROOT / "disturbances.json"

CAMPUS_BBOX = (38.974, -76.960, 39.001, -76.925)

# Approximate reference coordinates, not exact closures.
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


def norm(value):
    value = str(value or "").lower()
    value = value.replace("\u2019", "'")
    return " ".join(
        re.sub(r"[^\w\s]", " ", value).split()
    )


def valid(lat, lng):
    try:
        return (
            lat is not None
            and lng is not None
            and 38.974 <= float(lat) <= 39.001
            and -76.960 <= float(lng) <= -76.925
        )
    except (ValueError, TypeError):
        return False


def fetch_buildings():
    directory = {}

    try:
        url = BUILDINGS
        visited = set()

        for _ in range(30):
            if url in visited:
                break

            visited.add(url)
            response = requests.get(url, timeout=20)
            response.raise_for_status()

            payload = response.json()

            if isinstance(payload, list):
                rows = payload
            elif isinstance(payload, dict):
                rows = payload.get("data", [])
            else:
                rows = []

            if isinstance(rows, dict):
                rows = [rows]

            if not isinstance(rows, list):
                break

            for building in rows:
                if not isinstance(building, dict):
                    continue

                lat = building.get("lat")
                lng = building.get(
                    "long", building.get("lng")
                )

                if not valid(lat, lng):
                    continue

                coords = (float(lat), float(lng))

                for label in (
                    building.get("name"),
                    building.get("code")
                ):
                    key = norm(label)

                    if len(key) >= 3:
                        directory[key] = coords

            next_url = response.links.get(
                "next", {}
            ).get("url")

            if not next_url:
                break

            url = next_url

    except (
        requests.RequestException,
        ValueError,
        TypeError
    ) as error:
        print("Building API error:", error)

    return directory


def fetch_roads():
    south, west, north, east = CAMPUS_BBOX

    query = (
        f'[out:json][timeout:25];'
        f'way["highway"]["name"]'
        f'({south},{west},{north},{east});'
        f'out geom;'
    )

    try:
        response = requests.post(
            OVERPASS,
            data={"data": query},
            timeout=40
        )

        response.raise_for_status()

        roads = {}

        for item in response.json().get(
            "elements", []
        ):
            name = norm(
                item.get("tags", {}).get("name")
            )

            points = [
                (p["lat"], p["lon"])
                for p in item.get("geometry", [])
                if valid(p.get("lat"), p.get("lon"))
            ]

            if name and points:
                roads.setdefault(
                    name, []
                ).append(points)

        return roads

    except (
        requests.RequestException,
        ValueError,
        KeyError,
        TypeError
    ) as error:
        print("Road API error:", error)
        return {}


def find_mentions(text, locations):
    normalized = norm(text)
    hits = []

    for name, coords in locations.items():
        if re.search(
            r"(?<!\w)" + re.escape(name) + r"(?!\w)",
            normalized
        ):
            hits.append((name, coords))

    hits.sort(
        key=lambda item: len(item[0]),
        reverse=True
    )

    chosen = []

    for name, coords in hits:
        if not any(
            name in longer
            for longer, _ in chosen
        ):
            chosen.append((name, coords))

    return chosen


def find_road_mentions(text, roads):
    normalized = norm(text)

    return {
        name
        for name in roads
        if len(name) >= 5
        and re.search(
            r"(?<!\w)" + re.escape(name) + r"(?!\w)",
            normalized
        )
    }


def distance_m(a, b):
    dlat = (a[0] - b[0]) * 111000

    dlng = (
        (a[1] - b[1])
        * 111000
        * math.cos(math.radians(a[0]))
    )

    return math.hypot(dlat, dlng)


def cluster_point(points):
    # Only group buildings within 150 meters.
    if len(points) < 2:
        return None

    for a in points:
        for b in points:
            if distance_m(a, b) > 150:
                return None

    return (
        sum(p[0] for p in points) / len(points),
        sum(p[1] for p in points) / len(points)
    )


def road_reference(segments):
    points = [
        point
        for segment in segments
        for point in segment
    ]

    if not points:
        return None

    return points[len(points) // 2]


def road_intersection(a, b, roads):
    points_a = [
        p for segment in roads[a] for p in segment
    ]

    points_b = [
        p for segment in roads[b] for p in segment
    ]

    if not points_a or not points_b:
        return None

    closest = min(
        (
            distance_m(x, y),
            x,
            y
        )
        for x in points_a
        for y in points_b
    )

    distance, x, y = closest

    if distance > 50:
        return None

    return (
        (x[0] + y[0]) / 2,
        (x[1] + y[1]) / 2
    )


def locate(location, area, description, buildings, roads):
    reason = "unmatched"

    fields = [
        (location, "location"),
        (area, "area"),
        (description, "description")
    ]

    for field, label in fields:
        building_hits = find_mentions(
            field, buildings
        )

        points = list({
            tuple(coords)
            for _, coords in building_hits
        })

        road_hits = find_road_mentions(
            field, roads
        )

        # Intersection of two roads
        if (
            re.search(
                r"\b(intersection|corner|junction|at)\b",
                field,
                re.I
            )
            and len(road_hits) == 2
        ):
            a, b = sorted(road_hits)

            point = road_intersection(
                a, b, roads
            )

            if point:
                return (
                    *point,
                    f"approximate_intersection_{label}",
                    True
                )

        # Several nearby buildings
        if len(points) > 1 and not road_hits:
            point = cluster_point(points)

            if point:
                return (
                    *point,
                    f"approximate_building_cluster_{label}",
                    True
                )

            reason = (
                f"ambiguous_multiple_buildings_{label}"
            )
            continue

        # One building
        if len(points) == 1 and len(road_hits) <= 1:
            return (
                *points[0],
                f"approximate_building_{label}",
                True
            )

        # One road
        if len(road_hits) == 1 and not points:
            road = next(iter(road_hits))

            point = road_reference(
                roads[road]
            )

            if point:
                return (
                    *point,
                    f"approximate_road_{label}",
                    True
                )

        if points or road_hits:
            reason = (
                f"ambiguous_multiple_locations_{label}"
            )

    # Fallback coordinates
    aliases = {
        norm(k): v
        for k, v in ALIASES.items()
    }

    for field, label in fields:
        hits = find_mentions(
            field, aliases
        )

        if len(hits) == 1:
            lat, lng = hits[0][1]

            return (
                lat,
                lng,
                f"approximate_fallback_{label}",
                True
            )

    return None, None, reason, True


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
                str(value or "").strip(),
                fmt
            ).date().isoformat()

        except ValueError:
            continue

    return None


def status(start, end, completed=False):
    if completed:
        return "completed"

    today = date.today().isoformat()

    if end and end < today:
        return "expired"

    if start and start > today:
        return "upcoming"

    if not end:
        return "needs_verification"

    return "active"


def scrape():
    buildings = fetch_buildings()
    roads = fetch_roads()

    print("Buildings loaded:", len(buildings))
    print("Roads loaded:", len(roads))

    response = requests.get(
        SOURCE, timeout=25
    )
    response.raise_for_status()

    soup = BeautifulSoup(
        response.text, "html.parser"
    )

    output = []
    seen = set()

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = [
                c.get_text(" ", strip=True)
                for c in row.find_all("td")
            ]

            if len(cells) < 7:
                continue

            (
                location,
                area,
                start,
                finish,
                repair,
                description,
                impact_type
            ) = cells[:7]

            completed = bool(
                re.search(
                    r"\*\*\s*completed\s*\*\*",
                    " ".join(
                        (location, area, description)
                    ),
                    re.I
                )
            )

            # Remove formatting from source text
            location = re.sub(
                r"\*\*\s*completed\s*\*\*",
                "",
                location,
                flags=re.I
            ).strip()

            area = re.sub(
                r"\*\*\s*see map attached\s*\*\*",
                "",
                area,
                flags=re.I
            ).strip()

            impact = " ".join(
                (area, repair, description)
            )

            if not (
                OUTDOOR.search(
                    " ".join((location, impact))
                )
                and IMPACT.search(impact)
            ):
                continue

            start_date = parse_date(start)
            end_date = parse_date(finish)

            raw = "|".join((
                norm(location),
                norm(area),
                str(start_date or ""),
                norm(repair)
            ))

            uid = hashlib.sha256(
                raw.encode("utf-8")
            ).hexdigest()[:16]

            if uid in seen:
                continue

            seen.add(uid)

            lat, lng, source, review = locate(
                location,
                area,
                description,
                buildings,
                roads
            )

            entry = {
                "id": uid,
                "name": location,
                "area": area,
                "lat": lat,
                "lng": lng,
                "candidate_lat": lat,
                "candidate_lng": lng,
                "type": repair,
                "description": description,
                "impact_type": impact_type,
                "start_date": start_date,
                "end_date": end_date,
                "needs_review": review,
                "coordinate_source": source,
                "source_url": SOURCE,
                "status": status(
                    start_date,
                    end_date,
                    completed
                )
            }

            output.append(entry)

    return output


def main():
    data = scrape()

    OUTPUT.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    mapped = sum(
        valid(d["lat"], d["lng"])
        for d in data
    )

    print("Total notices:", len(data))
    print("Automatically mapped:", mapped)
    print("Unmapped:", len(data) - mapped)

    for d in data:
        if not valid(d["lat"], d["lng"]):
            print(
                "UNMAPPED:",
                d["name"],
                "| Area:",
                d["area"],
                "| Reason:",
                d["coordinate_source"]
            )

    print("Saved:", OUTPUT)


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as error:
        print("Scraping failed:", error)
        raise SystemExit(1)
