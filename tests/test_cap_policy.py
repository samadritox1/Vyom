from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from cap_policy import parse_active_cap
from disaster_connectors import CAPFeedConnector, NWSAlertsConnector

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
SENT = (NOW-timedelta(hours=1)).isoformat()
POLYGON = '<polygon>-1,-1 -1,1 1,1 1,-1 -1,-1</polygon>'


def bulletin(identifier='one', kind='Alert', status='Actual', scope='Public', geometry=POLYGON,
             sent=SENT, expires=None, effective=None, sender='authority', references='', info=True):
    expires = expires or (NOW+timedelta(hours=1)).isoformat()
    effective = effective or SENT
    block = (f'<info><language>en-US</language><event>Flood</event><effective>{effective}</effective>'
             f'<expires>{expires}</expires><description>Authority bulletin</description>'
             f'<area><areaDesc>Test</areaDesc>{geometry}</area></info>') if info else ''
    return (f'<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>{identifier}</identifier>'
            f'<sender>{sender}</sender><sent>{sent}</sent><status>{status}</status><scope>{scope}</scope>'
            f'<msgType>{kind}</msgType><references>{references}</references>{block}</alert>')


def parse(*records, lat=0, lon=0):
    return parse_active_cap(('<feed>'+''.join(records)+'</feed>').encode(), lat, lon, NOW)


def test_polygon_contains_location_including_boundary():
    warnings, invalid = parse(bulletin())
    assert invalid == 0 and len(warnings) == 1 and warnings[0]['verified_for_location'] is True
    assert len(parse(bulletin(), lat=1, lon=0)[0]) == 1
    assert parse(bulletin(), lat=5, lon=5) == ([], 0)


def test_dateline_polygon_does_not_include_greenwich():
    geometry = '<polygon>-10,170 -10,-170 10,-170 10,170 -10,170</polygon>'
    assert len(parse(bulletin(geometry=geometry), lon=179)[0]) == 1
    assert len(parse(bulletin(geometry=geometry), lon=-179)[0]) == 1
    assert parse(bulletin(geometry=geometry), lon=0) == ([], 0)


def test_circle_uses_radius_in_kilometers():
    record = bulletin(geometry='<circle>0,0 20</circle>')
    assert len(parse(record, lat=.1)[0]) == 1
    assert parse(record, lat=1) == ([], 0)


@pytest.mark.parametrize('changes', [
    {'status': 'Test'}, {'scope': 'Private'}, {'kind': 'Cancel'},
    {'expires': (NOW-timedelta(minutes=1)).isoformat()},
    {'effective': (NOW+timedelta(minutes=1)).isoformat()},
    {'sent': (NOW+timedelta(minutes=1)).isoformat()},
])
def test_inactive_test_private_and_future_bulletins_are_not_forwarded(changes):
    assert parse(bulletin(**changes))[0] == []


@pytest.mark.parametrize('geometry', ['', '<polygon>0,0 1,1</polygon>', '<circle>999,0 5</circle>'])
def test_unknown_or_bad_geometry_degrades_instead_of_claiming_no_warnings(geometry):
    assert parse(bulletin(geometry=geometry)) == ([], 1)


def test_missing_info_is_invalid():
    assert parse(bulletin(info=False)) == ([], 1)


def test_cancel_suppresses_only_referenced_authority_and_sent_time():
    reference = f'authority,one,{SENT}'
    assert parse(bulletin(), bulletin('cancel', kind='Cancel', references=reference, info=False)) == ([], 0)
    assert len(parse(bulletin(), bulletin('cancel', kind='Cancel', references=reference, sender='other', info=False))[0]) == 1
    wrong_time = f'authority,one,{(NOW-timedelta(hours=2)).isoformat()}'
    assert len(parse(bulletin(), bulletin('cancel', kind='Cancel', references=wrong_time, info=False))[0]) == 1


def test_future_cancel_cannot_suppress_current_bulletin():
    cancel = bulletin('cancel', kind='Cancel', references=f'authority,one,{SENT}',
                      sent=(NOW+timedelta(hours=1)).isoformat(), info=False)
    assert len(parse(bulletin(), cancel)[0]) == 1


def test_update_replaces_original_bulletin():
    warnings, invalid = parse(bulletin(), bulletin('two', kind='Update', references=f'authority,one,{SENT}'))
    assert invalid == 0 and [w['identifier'] for w in warnings] == ['two']


def test_malformed_update_does_not_erase_active_warning():
    warnings, invalid = parse(bulletin(), bulletin('two', kind='Update', references=f'authority,one,{SENT}', info=False))
    assert invalid == 1 and [w['identifier'] for w in warnings] == ['one']


def test_oversized_cap_feed_is_rejected():
    with pytest.raises(ValueError): parse_active_cap(b' ' * 2_000_001, 0, 0, NOW)


def test_cap_connector_preserves_unknown_geography_as_degraded(monkeypatch):
    response = MagicMock(content=bulletin(geometry='').encode())
    monkeypatch.setattr('disaster_connectors.requests.get', lambda *a, **kw: response)
    result = CAPFeedConnector('https://authority.invalid/cap').fetch(0, 0)
    assert result['patch']['official_warnings'] == []
    assert result['sources'][0]['status'] == 'degraded'


def test_nws_malformed_bulletin_preserves_unknown_source_state(monkeypatch):
    response = MagicMock()
    response.json.return_value = {'features': [{'properties': {'status': 'Actual', 'messageType': 'Alert',
                                           'id': 'warning', 'effective': SENT, 'expires': 'broken'}}]}
    monkeypatch.setattr('disaster_connectors.requests.get', lambda *a, **kw: response)
    result = NWSAlertsConnector().fetch(40, -74)
    assert result['sources'][0]['status'] == 'degraded'
    assert result['patch']['official_warnings'] == []


@pytest.mark.parametrize('content', [b'<html>access denied</html>', b'<feed><entry><link href="other.xml"/></entry></feed>'])
def test_unreadable_or_link_only_feeds_are_not_reported_as_clear(content):
    with pytest.raises(ValueError): parse_active_cap(content, 0, 0, NOW)


def test_multiple_hazards_in_one_bulletin_are_preserved():
    from alert_policy import official_events
    import xml.etree.ElementTree as ET
    record = bulletin()
    extra = ET.fromstring(bulletin('other')).find('{urn:oasis:names:tc:emergency:cap:1.2}info')
    extra.find('{urn:oasis:names:tc:emergency:cap:1.2}event').text = 'Heat'
    root = ET.fromstring(record); root.append(extra)
    warnings, invalid = parse_active_cap(ET.tostring(root), 0, 0, NOW)
    assert invalid == 0 and {w['event'] for w in warnings} == {'Flood', 'Heat'}
    assert len(official_events(warnings, NOW)) == 2
