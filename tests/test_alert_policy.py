from datetime import datetime, timedelta, timezone

import pytest

from alert_policy import forecast_threshold_events, official_events

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
USER = {'alert_lat': 0, 'alert_lon': 0, 'alert_heat_threshold': 42,
        'alert_wind_threshold': 60, 'alert_rain_threshold': 50}


def forecast():
    return {'_fetched_at': NOW.isoformat(),
            'hourly_units': {'temperature_2m': '°C', 'wind_speed_10m': 'km/h', 'precipitation': 'mm'},
            'hourly': {'time': [(NOW + timedelta(hours=i)).isoformat() for i in range(6)],
                       'temperature_2m': [43]*6, 'wind_speed_10m': [61]*6, 'precipitation': [20]*6}}


def warning(**changes):
    return {'verified_for_location': True, 'identifier': 'bulletin-1', 'source': 'Test authority',
            'effective': (NOW-timedelta(hours=1)).isoformat(), 'expires': (NOW+timedelta(hours=1)).isoformat(),
            'headline': 'Flood warning', 'description': '<script>unsafe()</script>',
            'instruction': 'Follow authority instructions.', **changes}


def test_thresholds_report_facts_and_deduplicate_each_kind():
    events = forecast_threshold_events(forecast(), USER, NOW)
    assert len(events) == 3
    assert {e.title.rsplit(': ', 1)[1] for e in events} == {'rain', 'wind', 'heat'}
    assert all('not a confirmed disaster or an official warning' in e.text for e in events)
    assert all(e.expires_at > NOW for e in events)
    assert [e.event_id for e in events] == [e.event_id for e in forecast_threshold_events(forecast(), USER, NOW)]


@pytest.mark.parametrize('change', ['stale', 'wrong_units', 'missing_values', 'nonfinite', 'past', 'no_timestamp'])
def test_bad_forecast_cannot_create_notifications(change):
    data = forecast()
    if change == 'stale': data['_fetched_at'] = (NOW-timedelta(minutes=6)).isoformat()
    if change == 'no_timestamp': data.pop('_fetched_at')
    if change == 'wrong_units': data['hourly_units']['wind_speed_10m'] = 'm/s'
    if change == 'missing_values':
        for key in ('temperature_2m', 'wind_speed_10m', 'precipitation'): data['hourly'][key] = []
    if change == 'nonfinite':
        for key in ('temperature_2m', 'wind_speed_10m', 'precipitation'): data['hourly'][key] = [float('inf')]*6
    if change == 'past': data['hourly']['time'] = [(NOW-timedelta(hours=i+1)).isoformat() for i in range(6)]
    assert forecast_threshold_events(data, USER, NOW) == []


def test_sustained_thresholds_require_consecutive_hours():
    data = forecast()
    data['hourly']['wind_speed_10m'] = [0]*6
    data['hourly']['time'] = [(NOW+timedelta(hours=i*2)).isoformat() for i in range(6)]
    assert forecast_threshold_events(data, USER, NOW) == []


def test_missing_rain_is_not_treated_as_measured_zero():
    data = forecast()
    data['hourly'] = {'time': data['hourly']['time'][:3], 'temperature_2m': [20]*3,
                      'wind_speed_10m': [0]*3, 'precipitation': [30, None, 30]}
    assert forecast_threshold_events(data, USER, NOW) == []


def test_official_forwarding_preserves_instruction_and_escapes_html():
    events = official_events([warning(), warning()], NOW)
    assert len(events) == 1
    assert 'Follow authority instructions.' in events[0].text
    assert 'does not issue evacuation or all-clear orders' in events[0].text
    assert '<script>' not in events[0].message_html
    assert '&lt;script&gt;' in events[0].message_html


@pytest.mark.parametrize('changes', [
    {'verified_for_location': False}, {'expires': NOW.isoformat()},
    {'effective': (NOW+timedelta(hours=2)).isoformat()}, {'expires': None},
    {'identifier': ''}, {'source': ''},
])
def test_unverified_expired_or_incomplete_warning_is_withheld(changes):
    assert official_events([warning(**changes)], NOW) == []
