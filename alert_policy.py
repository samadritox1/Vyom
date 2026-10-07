"""Deterministic notification facts: no LLM decisions, search recipients or evacuation plans."""
from __future__ import annotations

import base64
import hashlib
import html
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit


def utc_time(value) -> datetime | None:
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def finite_number(value) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class AlertEvent:
    event_id: str
    title: str
    text: str
    source: str
    expires_at: datetime

    @property
    def message_html(self) -> str:
        return '<p>' + html.escape(self.text).replace('\n', '<br>') + '</p>'


def _event_id(parts: list[str]) -> str:
    return hashlib.sha256('\x00'.join(parts).encode()).hexdigest()


def official_events(warnings: list[dict], now: datetime) -> list[AlertEvent]:
    events = []
    seen = set()
    for warning in warnings:
        expires = utc_time(warning.get('expires'))
        effective = utc_time(warning.get('effective'))
        if (warning.get('verified_for_location') is not True or expires is None or effective is None
                or not effective <= now < expires):
            continue
        identifier = str(warning.get('identifier') or '')
        source = str(warning.get('source') or '')
        if not identifier or not source:
            continue
        title = str(warning.get('headline') or warning.get('event') or 'Official warning')[:200]
        key = _event_id(['official', source, identifier, str(warning.get('info_identifier') or ''), str(warning.get('sent') or effective),
                         str(warning.get('severity') or ''), str(expires)])
        if key in seen:
            continue
        seen.add(key)
        text = (f'Official bulletin from {source}: {title}\n'
                f'Effective: {effective.isoformat()}; expires: {expires.isoformat()}.\n'
                + str(warning.get('description') or '')[:6000]
                + '\nAuthority instructions: ' + str(warning.get('instruction') or 'Consult the issuing authority.')[:6000]
                + '\nVYOM is forwarding the authority bulletin; it does not issue evacuation or all-clear orders.')
        events.append(AlertEvent(key, title, text, source, expires))
    return events


def forecast_threshold_events(data: dict, user: dict, now: datetime) -> list[AlertEvent]:
    """Retain opted-in weather thresholds while labelling them as model forecasts."""
    units = data.get('hourly_units') or {}
    if units.get('temperature_2m') != '°C' or units.get('wind_speed_10m') != 'km/h' or units.get('precipitation') != 'mm':
        return []
    hourly = data.get('hourly') or {}
    times = hourly.get('time') or []
    generated = utc_time(data.get('_fetched_at'))
    if generated is None or abs((now - generated).total_seconds()) > 300:
        return []
    rain = finite_number(user.get('alert_rain_threshold', 50))
    heat = finite_number(user.get('alert_heat_threshold', 42))
    wind = finite_number(user.get('alert_wind_threshold', 60))
    if any(v is None for v in (rain, heat, wind)) or rain <= 0 or wind <= 0:
        return []
    arrays = {key: hourly.get(key) or [] for key in ('temperature_2m', 'wind_speed_10m', 'precipitation')}
    def values(key, end, count):
        if end < count - 1:
            return None
        rows = [finite_number(arrays[key][j]) if j < len(arrays[key]) else None
                for j in range(end - count + 1, end + 1)]
        ts = [utc_time(times[j]) for j in range(end - count + 1, end + 1)]
        if any(x is None for x in rows) or any(t is None or t < now.replace(minute=0, second=0, microsecond=0) for t in ts):
            return None
        if any((ts[j] - ts[j-1]).total_seconds() != 3600 for j in range(1, len(ts))):
            return None
        return rows
    events = []
    found = set()
    for i, raw_time in enumerate(times[:48]):
        valid = utc_time(raw_time)
        if valid is None or valid < now.replace(minute=0, second=0, microsecond=0) or (valid-now).total_seconds() > 48*3600:
            continue
        candidates = []
        temperatures = values('temperature_2m', i, 3)
        winds = values('wind_speed_10m', i, 1)
        precipitation = values('precipitation', i, 3)
        if temperatures is not None and min(temperatures) >= heat:
            candidates.append(('heat', f'Forecast temperature reaches your {heat:g}°C threshold for three consecutive hours.'))
        if winds is not None and winds[0] >= wind:
            candidates.append(('wind', f'Forecast wind reaches your {wind:g} km/h threshold.'))
        if precipitation is not None and sum(precipitation) >= rain:
            candidates.append(('rain', f'Forecast rainfall reaches your {rain:g} mm threshold over three hours.'))
        for kind, fact in candidates:
            if kind in found:
                continue
            found.add(kind)
            title = f'Model forecast threshold advisory: {kind}'
            text = (f'{fact}\nForecast valid time: {valid.isoformat()}.\nSource: Open-Meteo numerical forecast.\n'
                    'This is a model-based advisory, not a confirmed disaster or an official warning. '
                    'Check current information from your local weather or emergency authority.')
            key = _event_id(['forecast', kind, valid.isoformat(), str(user.get('alert_lat')), str(user.get('alert_lon')),
                             str({'rain':rain, 'heat':heat, 'wind':wind}[kind])])
            events.append(AlertEvent(key, title, text, 'Open-Meteo model forecast', valid + timedelta(hours=1)))
    return events


def validate_push_subscription(subscription: dict | None) -> dict | None:
    if subscription is None:
        return None
    if not isinstance(subscription, dict):
        raise ValueError('Invalid push subscription')
    endpoint = subscription.get('endpoint')
    parsed = urlsplit(endpoint if isinstance(endpoint, str) else '')
    host = (parsed.hostname or '').lower()
    allowed = (host == 'fcm.googleapis.com' or host == 'updates.push.services.mozilla.com'
               or host.endswith('.push.services.mozilla.com') or host == 'web.push.apple.com'
               or host.endswith('.notify.windows.com'))
    if (not allowed or parsed.scheme != 'https' or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment or len(endpoint or '') > 2048):
        raise ValueError('Unsupported push service endpoint')
    keys = subscription.get('keys') or {}
    if not isinstance(keys, dict):
        raise ValueError('Invalid push keys')
    decoded = {}
    for name, size in [('auth', 16), ('p256dh', 65)]:
        raw = keys.get(name)
        if not isinstance(raw, str) or len(raw) > 128:
            raise ValueError('Invalid push key')
        try:
            decoded[name] = base64.b64decode(raw + '='*(-len(raw)%4), altchars=b'-_', validate=True)
        except (ValueError, TypeError):
            raise ValueError('Invalid push key') from None
        if len(decoded[name]) != size:
            raise ValueError('Invalid push key size')
    from cryptography.hazmat.primitives.asymmetric import ec
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decoded['p256dh'])
    except ValueError:
        raise ValueError('Invalid push public key') from None
    return {'endpoint': endpoint, 'keys': {'auth': keys['auth'], 'p256dh': keys['p256dh']}}
