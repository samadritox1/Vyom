import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from pymongo.errors import DuplicateKeyError

import notifications
from alert_policy import AlertEvent


def matches(row, query):
    for key, expected in query.items():
        if key == '$or':
            if not any(matches(row, q) for q in expected): return False
        elif isinstance(expected, dict):
            value = row.get(key)
            if '$ne' in expected and value == expected['$ne']: return False
            if '$lt' in expected and (value is None or not value < expected['$lt']): return False
        elif row.get(key) != expected:
            return False
    return True


class Collection:
    """Small atomic in-memory collection for exercising delivery races without MongoDB."""
    def __init__(self, rows=()):
        self.rows = list(copy.deepcopy(rows))
        self.lock = threading.Lock()

    def find(self, query):
        with self.lock: return [copy.deepcopy(r) for r in self.rows if matches(r, query)]

    def find_one(self, query):
        return next(iter(self.find(query)), None)

    def find_one_and_update(self, query, update, **kwargs):
        with self.lock:
            for row in self.rows:
                if matches(row, query):
                    row.update(update['$set'])
                    return copy.deepcopy(row)
        return None

    def insert_one(self, row):
        with self.lock:
            if any(r['_id'] == row['_id'] for r in self.rows): raise DuplicateKeyError('duplicate')
            self.rows.append(copy.deepcopy(row))

    def update_one(self, query, update):
        self.find_one_and_update(query, update)


@pytest.fixture
def scan(monkeypatch):
    user = {'clerk_id': 'user_a', 'alert_enabled': True, 'alert_consent_version': 2,
            'alert_lat': 0, 'alert_lon': 0, 'alert_email': 'owner@example.com'}
    event = AlertEvent('event-one', 'Official warning', 'Authority text', 'Authority',
                       datetime.now(timezone.utc)+timedelta(hours=1))
    users, deliveries = Collection([user]), Collection()
    monkeypatch.setattr(notifications, 'verified_email_addresses', lambda user_id: ('owner@example.com', ['owner@example.com']))
    monkeypatch.setattr(notifications, 'collect_events', lambda *a: [event])
    email, push, live = [MagicMock(return_value=True) for _ in range(3)]
    def run(): notifications.scan_users(users, deliveries, email, push, live, 'fresh-public-key')
    return users, deliveries, email, push, live, run


def test_successful_channels_send_once_across_repeated_scans(scan):
    users, deliveries, email, push, live, run = scan
    run(); run()
    email.assert_called_once_with('owner@example.com', 'VYOM: Official warning', '<p>Authority text</p>')
    live.assert_called_once()
    push.assert_not_called()
    assert all(r['status'] == 'sent' for r in deliveries.rows)


def test_failed_channel_retries_without_repeating_successful_channel(scan):
    users, deliveries, email, push, live, run = scan
    live.side_effect = [False, True]
    run(); run(); run()
    assert email.call_count == 1 and live.call_count == 2
    assert all(r['status'] == 'sent' for r in deliveries.rows)


@pytest.mark.parametrize('change', [{'alert_consent_version': 1}, {'alert_enabled': False},
                                  {'alert_email': 'third-party@example.com'}])
def test_legacy_disabled_and_unverified_recipients_receive_nothing(scan, change):
    users, deliveries, email, push, live, run = scan
    users.rows[0].update(change)
    run()
    email.assert_not_called(); push.assert_not_called(); live.assert_not_called()


def test_revoked_consent_is_rechecked_before_delivery(scan, monkeypatch):
    users, deliveries, email, push, live, run = scan
    def collect(*a):
        users.rows[0]['alert_enabled'] = False
        return [AlertEvent('event', 'Warning', 'Text', 'Authority', datetime.now(timezone.utc)+timedelta(hours=1))]
    monkeypatch.setattr(notifications, 'collect_events', collect)
    run()
    email.assert_not_called(); live.assert_not_called()


def test_expired_events_cannot_send(scan, monkeypatch):
    users, deliveries, email, push, live, run = scan
    monkeypatch.setattr(notifications, 'collect_events', lambda *a: [
        AlertEvent('old', 'Old warning', 'Text', 'Authority', datetime.now(timezone.utc)-timedelta(seconds=1))])
    run()
    assert deliveries.rows == []
    email.assert_not_called(); live.assert_not_called()


def test_email_verification_failure_cannot_send(scan, monkeypatch):
    users, deliveries, email, push, live, run = scan
    def unavailable(*a): raise RuntimeError('Provider offline')
    monkeypatch.setattr(notifications, 'verified_email_addresses', unavailable)
    run()
    email.assert_not_called(); push.assert_not_called(); live.assert_not_called()


@pytest.mark.parametrize('key,expected', [('old-key', 0), ('fresh-public-key', 1)])
def test_push_requires_subscription_for_current_signing_key(scan, key, expected):
    users, deliveries, email, push, live, run = scan
    users.rows[0].update(push_subscription={'endpoint': 'https://push.invalid'}, push_vapid_public_key=key)
    run(); run()
    assert push.call_count == expected


def test_concurrent_delivery_claims_allow_only_one_sender():
    deliveries = Collection()
    now = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: notifications._claim(deliveries, 'user_a', 'event', 'email', now), range(20)))
    assert sum(c is not None for c in claims) == 1


def test_delivery_lease_prevents_overlap_but_recovers_after_crash():
    deliveries = Collection()
    now = datetime.now(timezone.utc)
    claim = notifications._claim(deliveries, 'user_a', 'event', 'email', now)
    assert claim is not None
    assert notifications._claim(deliveries, 'user_a', 'event', 'email', now+timedelta(minutes=1)) is None
    assert notifications._claim(deliveries, 'user_a', 'event', 'email', now+timedelta(minutes=6)) == claim


def test_provider_failure_does_not_create_an_invented_alert(monkeypatch):
    monkeypatch.delenv('DISASTER_CAP_FEED_URL', raising=False)
    monkeypatch.setenv('DISASTER_ENABLE_NWS', '0')
    def unavailable(*a, **kw): raise notifications.requests.ConnectionError('offline')
    monkeypatch.setattr(notifications.requests, 'get', unavailable)
    assert notifications.collect_events({'alert_lat': 0, 'alert_lon': 0}, datetime.now(timezone.utc)) == []


def test_owner_scoped_scan_cannot_notify_other_subscribers(scan):
    users, deliveries, email, push, live, run = scan
    other = {**users.rows[0], 'clerk_id': 'user_b'}
    users.rows.append(other)
    notifications.scan_users(users, deliveries, email, push, live, 'fresh-public-key', user_id='user_a')
    assert {row['user_id'] for row in deliveries.rows} == {'user_a'}


def test_configuration_change_during_collection_withholds_old_events(scan, monkeypatch):
    users, deliveries, email, push, live, run = scan
    users.rows[0]['alert_config_revision'] = 'original'
    def collect(*args):
        users.rows[0]['alert_config_revision'] = 'replacement'
        return [AlertEvent('old-config', 'Warning', 'Text', 'Authority', datetime.now(timezone.utc)+timedelta(hours=1))]
    monkeypatch.setattr(notifications, 'collect_events', collect)
    run()
    email.assert_not_called(); live.assert_not_called()
