"""Bounded notification collection and per-user/channel delivery leases."""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

import requests
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from alert_policy import forecast_threshold_events, official_events
from disaster_connectors import CAPFeedConnector, NWSAlertsConnector
from security import verified_email_addresses

logger = logging.getLogger(__name__)
_scan_lock = threading.Lock()
CONSENT_VERSION = 2


def collect_events(user: dict, now: datetime):
    warnings = []
    if os.getenv('DISASTER_CAP_FEED_URL'):
        result = CAPFeedConnector().fetch(user['alert_lat'], user['alert_lon'])
        warnings.extend((result.get('patch') or {}).get('official_warnings') or [])
    lat, lon = user['alert_lat'], user['alert_lon']
    # NWS active-point alerts only cover its US service area. Other regions use configured CAP feeds.
    if os.getenv('DISASTER_ENABLE_NWS', '1') == '1' and 18 <= lat <= 72 and -180 <= lon <= -60:
        result = NWSAlertsConnector().fetch(lat, lon)
        warnings.extend((result.get('patch') or {}).get('official_warnings') or [])
    events = official_events(warnings, now)
    try:
        response = requests.get('https://api.open-meteo.com/v1/forecast', params={
            'latitude': lat, 'longitude': lon, 'timezone': 'UTC', 'forecast_hours': 48,
            'wind_speed_unit': 'kmh', 'precipitation_unit': 'mm',
            'hourly': 'temperature_2m,wind_speed_10m,precipitation',
        }, timeout=10)
        response.raise_for_status()
        forecast = response.json()
        forecast['_fetched_at'] = datetime.now(timezone.utc).isoformat()
        events.extend(forecast_threshold_events(forecast, user, now))
    except (requests.RequestException, ValueError, TypeError):
        logger.warning('Forecast collection unavailable during alert scan')
    return events


def _claim(deliveries, user_id: str, event_id: str, channel: str, now: datetime) -> str | None:
    key = hashlib.sha256(f'{user_id}\0{event_id}\0{channel}'.encode()).hexdigest()
    update = {'status': 'sending', 'lease_until': now + timedelta(minutes=5), 'attempted_at': now}
    existing = deliveries.find_one_and_update(
        {'_id': key, '$or': [{'status': 'failed'}, {'status': 'sending', 'lease_until': {'$lt': now}}]},
        {'$set': update}, return_document=ReturnDocument.AFTER,
    )
    if existing is not None:
        return key
    try:
        deliveries.insert_one({'_id': key, 'user_id': user_id, 'event_id': event_id,
                               'channel': channel, **update})
        return key
    except DuplicateKeyError:
        return None


def scan_users(users, deliveries, send_email, send_push, send_live, public_push_key: str | None, *, user_id=None):
    """Only explicitly renewed user consent permits delivery after the security upgrade."""
    if users is None or deliveries is None or not _scan_lock.acquire(blocking=False):
        return
    try:
        query = {'alert_enabled': True, 'alert_consent_version': CONSENT_VERSION,
                 'alert_lat': {'$ne': None}, 'alert_lon': {'$ne': None}}
        if user_id is not None:
            query['clerk_id'] = user_id
        for user in users.find(query):
            try:
                user_id = user.get('clerk_id')
                if not user_id:
                    continue
                _, emails = verified_email_addresses(user_id)
                recipient = user.get('alert_email')
                if not recipient or recipient not in emails:
                    continue
                for event in collect_events(user, datetime.now(timezone.utc)):
                    now = datetime.now(timezone.utc)
                    if event.expires_at <= now:
                        continue
                    # Recheck opt-in just before delivering; a revoked configuration must not send.
                    current = users.find_one({'clerk_id': user_id, 'alert_enabled': True,
                                               'alert_consent_version': CONSENT_VERSION})
                    if (not current or current.get('alert_email') != recipient
                            or current.get('alert_lat') != user.get('alert_lat')
                            or current.get('alert_lon') != user.get('alert_lon')
                            or current.get('alert_config_revision') != user.get('alert_config_revision')):
                        break
                    channels = [('email', lambda: send_email(recipient, 'VYOM: ' + event.title, event.message_html)),
                                ('websocket', lambda: send_live(user_id, event))]
                    subscription = current.get('push_subscription')
                    if subscription and public_push_key and current.get('push_vapid_public_key') == public_push_key:
                        channels.append(('push', lambda: send_push(subscription, event.title, event.text)))
                    for channel, deliver in channels:
                        claim = _claim(deliveries, user_id, event.event_id, channel, now)
                        if claim is None:
                            continue
                        success = False
                        try:
                            success = bool(deliver())
                        except Exception:
                            logger.warning('Notification delivery failed for channel %s', channel)
                        deliveries.update_one({'_id': claim}, {'$set': {
                            'status': 'sent' if success else 'failed', 'completed_at': datetime.now(timezone.utc)}})
            except Exception:
                # A provider/user failure must not expose PII or interrupt other subscribers.
                logger.warning('Alert subscriber could not be processed')
    finally:
        _scan_lock.release()
