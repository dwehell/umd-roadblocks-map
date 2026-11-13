
    
  
        if re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', normalized):
            hits.append((name, coords))
    hits.sort(key=lambda item: len(item[0]), reverse=True)
    chosen = []
    for name, coords in hits:
        if not any(name in bigger for bigger, _ in chosen):
            chosen.append((name, coords))
    return chosen

def locate(location, area, description, directory, roads):
    """Prefer specific building, intersection, then single named road."""
    buildings = directory
    # Notice's location field is most reliable, but search other fields too.
    for field, label in ((location, 'location'), (area, 'area'),
                         (description, 'description')):
        building_hits = find_mentions(field, buildings)
        unique_buildings = {tuple(coords) for _, coords in building_hits}
        road_hits = find_road_mentions(field, roads)

        # Two named roads in an intersection phrase: calculate a reference point.
        intersection = re.search(r'\b(intersection|corner|junction|\bat\b)\b', field, re.I)
        if intersection and len(road_hits) == 2:
            a, b = sorted(road_hits)
            point = road_intersection(a, b, roads)
            if point:
                return *point, f'approximate_osm_intersection_{label}', True

        # One unambiguous building reference.
        if len(unique_buildings) == 1 and not road_hits:
            lat, lng = next(iter(unique_buildings))
            return lat, lng, f'approximate_building_{label}', True

        # One unambiguous named road, including notices without a building.
        if len(road_hits) == 1 and not unique_buildings:
            road = next(iter(road_hits))
            point = road_reference(roads[road])
            if point:
                return *point, f'approximate_osm_road_{label}', True

        # A building near a road may indicate an access closure, but don't
        # pretend its coordinates are the precise closure location.
        if len(unique_buildings) == 1 and len(road_hits) == 1:
            lat, lng = next(iter(unique_buildings))
            return lat, lng, f'approximate_building_near_road_{label}', True

        if len(unique_buildings) > 1 or len(road_hits) > 1:
            return None, None, f'ambiguous_multiple_locations_{label}', True

    # Fallback to the existing small alias set only if dynamic lookup failed.
    aliases = {norm(k): v for k, v in ALIASES.items()}
    for field, label in ((location, 'location'), (area, 'area'),
                         (description, 'description')):
        hits = find_mentions(field, aliases)
        if len(hits) == 1:
            lat, lng = hits[0][1]
            return lat, lng, f'approximate_fallback_{label}', True
    return None, None, 'unmatched', True

def parse_date(value):
    for fmt in ('%m-%d-%Y %I:%M %p', '%m/%d/%Y %I:%M %p', '%m-%d-%Y', '%m/%d/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(str(value or '').strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None

def status(start, end):
    today = date.today().isoformat()
    if end and end < today:
        return 'expired'
    if start and start > today:
        return 'upcoming'
    return 'active' if end else 'needs_verification'

def scrape():
    directory = fetch_buildings()
    roads = fetch_roads()
    print('Building names loaded:', len(directory), '| Road names loaded:', len(roads))
    response = requests.get(SOURCE, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    output, seen = [], set()
    for table in soup.find_all('table'):
        for row in table.find_all('tr'):
            cells = [c.get_text(' ', strip=True) for c in row.find_all('td')]
            if len(cells) < 7:
                continue
            location, area, start, finish, repair, description, impact_type = cells[:7]
            impact = ' '.join((area, repair, description))
            if not (OUTDOOR.search(' '.join((location, impact))) and IMPACT.search(impact)):
                continue
            start_date, end_date = parse_date(start), parse_date(finish)
            raw = '|'.join((norm(location), norm(area), str(start_date or ''), norm(repair)))
            uid = hashlib.sha256(raw.encode()).hexdigest()[:16]
            if uid in seen:
                continue
            seen.add(uid)
            lat, lng, coord_source, review = locate(location, area, description, directory, roads)
            output.append(dict(id=uid, name=location, area=area, lat=lat, lng=lng,
                candidate_lat=lat, candidate_lng=lng, type=repair, description=description,
                impact_type=impact_type, start_date=start_date, end_date=end_date,
                needs_review=review, coordinate_source=coord_source,
                source_url=SOURCE, status=status(start_date, end_date)))
    return output

def main():
    data = scrape()
    OUTPUT.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    mapped = sum(valid(d['lat'], d['lng']) for d in data)
    print(f'Total: {len(data)} | Automatically mapped: {mapped} | Unmapped: {len(data)-mapped}')
    for d in data:
        if not valid(d['lat'], d['lng']):
            print('UNMAPPED:', d['name'], '| Area:', d['area'], '| Description:', d['description'], '| Source:', d['coordinate_source'])

if __name__ == '__main__':
    try:
        main()
    except requests.RequestException as exc:
        print('Scrape failed:', exc)
        raise SystemExit(1)

