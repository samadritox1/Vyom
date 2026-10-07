"""CAP validity and geographic filtering shared by disaster analysis and alerts."""
from __future__ import annotations

import math
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from alert_policy import utc_time


def text(element, name):
    return next((child.text for child in element if child.tag.split('}')[-1] == name), None)


def children(element, name):
    return [child for child in element if child.tag.split('}')[-1] == name]


def _in_polygon(raw: str, lat: float, lon: float) -> bool:
    coords = [tuple(map(float, pair.split(','))) for pair in raw.split()]
    if len(coords) < 4 or coords[0] != coords[-1]:
        raise ValueError('CAP polygon must be closed')
    if any(not (-90 <= y <= 90 and -180 <= x <= 180) for y, x in coords):
        raise ValueError('Invalid CAP coordinate')
    # Unwrap the ring first. Wrapping each point around the query independently
    # would incorrectly make a dateline polygon include Greenwich.
    longitudes = [coords[0][1]]
    for _, x in coords[1:]:
        longitudes.append(longitudes[-1] + ((x-longitudes[-1]+180) % 360)-180)
    if abs(longitudes[-1] - longitudes[0]) > 1e-9 or max(longitudes)-min(longitudes) > 180:
        raise ValueError('CAP polygon longitude span is ambiguous')
    center = (min(longitudes) + max(longitudes)) / 2
    query_lon = lon + 360*round((center-lon)/360)
    points = [(x-query_lon, y-lat) for (y, _), x in zip(coords, longitudes)]
    inside = False
    for i in range(len(points)-1):
        x1, y1 = points[i]; x2, y2 = points[i+1]
        cross = x1*y2 - x2*y1
        if abs(cross) < 1e-9 and min(x1,x2) <= 0 <= max(x1,x2) and min(y1,y2) <= 0 <= max(y1,y2):
            return True
        if (y1 > 0) != (y2 > 0) and 0 < x1 + (x2-x1)*(-y1)/(y2-y1):
            inside = not inside
    return inside


def _in_circle(raw: str, lat: float, lon: float) -> bool:
    center, radius = raw.split()
    cy, cx = map(float, center.split(',')); radius = float(radius)
    if not (-90 <= cy <= 90 and -180 <= cx <= 180 and 0 <= radius <= 20040):
        raise ValueError('Invalid CAP circle')
    dy, dx = math.radians(cy-lat), math.radians(cx-lon)
    a = math.sin(dy/2)**2 + math.cos(math.radians(lat))*math.cos(math.radians(cy))*math.sin(dx/2)**2
    distance = 12742*math.asin(math.sqrt(max(0, min(1, a))))
    return distance <= radius


def _area_matches(info, lat, lon):
    geometries = []
    for area in children(info, 'area'):
        geometries.extend((_in_polygon, p.text) for p in children(area, 'polygon'))
        geometries.extend((_in_circle, p.text) for p in children(area, 'circle'))
    if not geometries:
        raise ValueError('CAP geographic relevance cannot be verified')
    return any(check(raw or '', lat, lon) for check, raw in geometries)


def parse_active_cap(content: bytes, lat: float, lon: float, now=None) -> tuple[list[dict], int]:
    if len(content) > 2_000_000:
        raise ValueError('CAP feed exceeds the configured parser limit')
    now = now or datetime.now(timezone.utc)
    root = ET.fromstring(content)
    alerts = [e for e in root.iter() if e.tag.split('}')[-1] == 'alert']
    if not alerts and (root.tag.split('}')[-1] not in {'feed', 'alerts'} or list(root)):
        raise ValueError('Feed contains no readable CAP alert records')
    superseded = set()
    for alert in alerts:
        if text(alert, 'status') != 'Actual' or text(alert, 'scope') != 'Public':
            continue
        sender, sent = text(alert, 'sender'), utc_time(text(alert, 'sent'))
        if not sender or sent is None or sent > now:
            continue
        if text(alert, 'msgType') == 'Update':
            # A malformed or not-yet-effective update cannot suppress an active bulletin.
            active_info = any(
                (effective := utc_time(text(info, 'effective') or text(alert, 'sent'))) is not None
                and (expires := utc_time(text(info, 'expires'))) is not None
                and effective <= now < expires
                for info in children(alert, 'info')
            )
            if not active_info:
                continue
        if text(alert, 'msgType') in {'Cancel', 'Update'}:
            for reference in (text(alert, 'references') or '').split():
                parts = reference.split(',')
                if len(parts) == 3 and parts[0] == sender and utc_time(parts[2]) is not None:
                    superseded.add((parts[0], parts[1], utc_time(parts[2])))
    warnings = []; invalid = 0
    for alert in alerts:
        status = text(alert, 'status'); scope = text(alert, 'scope'); kind = text(alert, 'msgType')
        if not status or not scope or not kind:
            invalid += 1; continue
        if status != 'Actual' or scope != 'Public' or kind not in {'Alert', 'Update'}:
            continue
        identifier, sender, sent = text(alert, 'identifier'), text(alert, 'sender'), text(alert, 'sent')
        sent_time = utc_time(sent)
        if (sender, identifier, sent_time) in superseded:
            continue
        if not identifier or not sender or sent_time is None:
            invalid += 1; continue
        if sent_time > now:
            continue
        infos = children(alert, 'info')
        if not infos:
            invalid += 1; continue
        # Prefer English when available, but retain every hazard/area block in that language.
        english = [info for info in infos if (text(info, 'language') or 'en-US').lower().startswith('en')]
        preferred = (text(infos[0], 'language') or 'en-US').lower()
        selected = english or [info for info in infos if (text(info, 'language') or 'en-US').lower() == preferred]
        for info in selected:
            effective = utc_time(text(info, 'effective') or sent)
            expires = utc_time(text(info, 'expires'))
            if effective is None or expires is None or expires <= effective:
                invalid += 1; continue
            if not effective <= now < expires:
                continue
            try:
                if not _area_matches(info, lat, lon):
                    continue
            except (ValueError, TypeError, OverflowError):
                invalid += 1; continue
            info_identifier = hashlib.sha256('\0'.join(str(text(info, field) or '') for field in
                ('event', 'severity', 'headline', 'description', 'instruction', 'effective', 'expires')).encode()).hexdigest()
            warnings.append({'source': sender, 'identifier': identifier, 'info_identifier': info_identifier, 'sent': sent,
                             'event': text(info,'event'), 'severity': text(info,'severity'),
                             'certainty': text(info,'certainty'), 'urgency': text(info,'urgency'),
                             'headline': text(info,'headline'), 'description': text(info,'description'),
                             'instruction': text(info,'instruction'), 'effective': effective.isoformat(),
                             'expires': expires.isoformat(), 'cap': True, 'verified_for_location': True})
    return warnings, invalid
