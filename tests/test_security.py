import os
import time
from unittest.mock import MagicMock
from types import SimpleNamespace

# Importing the API must not contact production services during this suite.
os.environ['MONGO_URI'] = ''
os.environ['GROQ_API_KEY'] = ''
os.environ['MISTRAL_API_KEY'] = ''
os.environ['VYOM_MARINE_BACKGROUND_WORKER'] = '0'

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import main
import security

ISSUER = 'https://example.clerk.accounts.dev'
ORIGIN = 'http://localhost:8000'
PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC = PRIVATE.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def token(user='user_a', **changes):
    now = int(time.time())
    claims = {'sub': user, 'sid': 'sess_test', 'iss': ISSUER, 'azp': ORIGIN,
              'iat': now-10, 'nbf': now-10, 'exp': now+300, **changes}
    return jwt.encode(claims, PRIVATE, algorithm='RS256')


@pytest.fixture(autouse=True)
def configuration(monkeypatch):
    monkeypatch.setenv('CLERK_ISSUER', ISSUER)
    monkeypatch.setenv('CLERK_AUTHORIZED_PARTIES', ORIGIN)
    monkeypatch.setenv('CLERK_JWT_PUBLIC_KEY', PUBLIC)
    monkeypatch.setenv('VYOM_ADMIN_TOKEN', 'a'*32)
    monkeypatch.setattr(main, 'users_collection', MagicMock())
    monkeypatch.setattr(main, 'agro_states_collection', MagicMock())
    monkeypatch.setattr(main, 'marine_states_collection', MagicMock())
    monkeypatch.setattr(main, 'scan_and_alert_users', lambda *args: None)
    monkeypatch.setattr(main, 'verified_email_addresses', lambda user: ('owner@example.com', ['owner@example.com']))


@pytest.fixture
def client():
    with TestClient(main.app) as instance:
        yield instance


@pytest.mark.parametrize('path,method,body', [
    ('/api/user/profile?clerk_id=user_a','get',None),
    ('/api/user/sync','post',{'clerk_id':'user_a'}),
    ('/api/user/locations','post',{'clerk_id':'user_a','locations':[]}),
    ('/api/alerts/live-location','post',{'clerk_id':'user_a','alert_lat':0,'alert_lon':0}),
    ('/api/alerts/config','post',{'clerk_id':'user_a'}),
])
def test_personal_endpoints_require_auth(client, path, method, body):
    response = client.request(method, path, json=body)
    assert response.status_code == 401
    main.users_collection.update_one.assert_not_called()
    main.users_collection.find_one.assert_not_called()


@pytest.mark.parametrize('changes', [
    {'exp':int(time.time())-60}, {'nbf':int(time.time())+60},
    {'iss':'https://attacker.invalid'}, {'azp':'https://attacker.invalid'},
    {'sts':'pending'}, {'sub':'user_a/../../other'},
])
def test_invalid_claims_rejected(client, changes):
    response = client.get('/api/user/profile', headers={'Authorization':'Bearer '+token(**changes)})
    assert response.status_code == 401
    main.users_collection.find_one.assert_not_called()


def test_forged_signature_rejected(client):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = jwt.decode(token(), options={'verify_signature':False})
    forged = jwt.encode(claims, other, algorithm='RS256')
    assert client.get('/api/user/profile', headers={'Authorization':'Bearer '+forged}).status_code == 401


def test_algorithm_confusion_rejected(client):
    claims = jwt.decode(token(), options={'verify_signature':False})
    forged = jwt.encode(claims, 'attacker-secret', algorithm='HS256')
    assert client.get('/api/user/profile', headers={'Authorization':'Bearer '+forged}).status_code == 401


def test_missing_auth_configuration_fails_closed(client, monkeypatch):
    monkeypatch.delenv('CLERK_AUTHORIZED_PARTIES')
    assert client.get('/api/user/profile', headers={'Authorization':'Bearer '+token()}).status_code == 503


def test_profile_id_comes_from_verified_subject(client):
    main.users_collection.find_one.return_value = {'clerk_id':'user_a','email':'owner@example.com'}
    response = client.get('/api/user/profile', headers={'Authorization':'Bearer '+token()})
    assert response.status_code == 200
    assert main.users_collection.find_one.call_args.args[0] == {'clerk_id':'user_a'}


@pytest.mark.parametrize('path,method,body', [
    ('/api/user/profile?clerk_id=user_b','get',None),
    ('/api/user/sync','post',{'clerk_id':'user_b'}),
    ('/api/user/locations','post',{'clerk_id':'user_b','locations':[]}),
    ('/api/alerts/live-location','post',{'clerk_id':'user_b','alert_lat':0,'alert_lon':0}),
    ('/api/alerts/config','post',{'clerk_id':'user_b'}),
    ('/agro/advisory','post',{'clerk_id':'user_b','lat':0,'lon':0}),
    ('/marine/advisory','post',{'clerk_id':'user_b','lat':0,'lon':0}),
    ('/weather','post',{'clerk_id':'user_b','lat':0,'lon':0}),
])
def test_foreign_ids_rejected_before_side_effects(client, path, method, body):
    response = client.request(method, path, json=body, headers={'Authorization':'Bearer '+token()})
    assert response.status_code == 403
    main.users_collection.update_one.assert_not_called()
    main.users_collection.find_one.assert_not_called()
    main.agro_states_collection.find_one.assert_not_called()
    main.marine_states_collection.find_one.assert_not_called()


def test_anonymous_request_cannot_load_persisted_state(client):
    response = client.post('/agro/advisory',json={'clerk_id':'user_a','lat':0,'lon':0})
    assert response.status_code == 401
    main.agro_states_collection.find_one.assert_not_called()


def test_signed_in_field_uses_only_own_checkpoint(client, monkeypatch):
    main.agro_states_collection.find_one.return_value = None
    monkeypatch.setattr(main, 'build_agro_advisory', lambda *args: {'status':'ok','state_checkpoint':{'depletion_mm':1}})
    response = client.post('/agro/advisory',json={'lat':0,'lon':0,'field':{'field_id':'field_test'}},
                           headers={'Authorization':'Bearer '+token()})
    assert response.status_code == 200
    assert main.agro_states_collection.find_one.call_args.args[0] == {'clerk_id':'user_a','field_id':'field_test'}
    assert main.agro_states_collection.update_one.call_args.args[0] == {'clerk_id':'user_a','field_id':'field_test'}


def test_profile_email_cannot_be_forged(client):
    response = client.post('/api/user/sync',json={'email':'third-party@example.com'},
                           headers={'Authorization':'Bearer '+token()})
    assert response.status_code == 200
    saved = main.users_collection.update_one.call_args.args[1]['$set']
    assert saved['clerk_id'] == 'user_a'
    assert saved['email'] == 'owner@example.com'


@pytest.mark.parametrize('path,method,body', [
    ('/api/alerts/trigger','post',None),
    ('/marine/data/collect','post',{'lat':0,'lon':0}),
    ('/marine/data/calibrate','post',None),
    ('/aviation/data/calibrate','post',None),
    ('/adventure/data/calibrate','post',None),
    ('/disaster/data/calibrate','post',None),
    ('/marine/data/vessel-case','post',{'case_id':'test','vessel_id':'v','vessel_class':'small','occurred_at':'2026-10-07T00:00:00Z','lat':0,'lon':0,'event_type':'roll','outcome':0,'predicted_probability':.5}),
    ('/marine/data/health','get',None), ('/aviation/data/health','get',None),
    ('/adventure/data/health','get',None), ('/disaster/data/health','get',None),
])
def test_admin_endpoints_deny_missing_token(client, path, method, body):
    assert client.request(method,path,json=body).status_code == 403


def test_admin_token_is_required_even_for_signed_in_user(client):
    assert client.post('/api/alerts/trigger',headers={'Authorization':'Bearer '+token()}).status_code == 403


def test_correct_admin_token_can_queue_scan(client):
    assert client.post('/api/alerts/trigger',headers={'X-VYOM-Admin-Token':'a'*32}).status_code == 200


def test_unconfigured_admin_token_disables_admin(client,monkeypatch):
    monkeypatch.delenv('VYOM_ADMIN_TOKEN')
    assert client.post('/api/alerts/trigger').status_code == 503


def test_alert_recipient_must_be_verified(client,monkeypatch):
    monkeypatch.setattr(security,'verified_email_addresses',lambda user:('owner@example.com',['owner@example.com']))
    body={'alert_email':'third-party@example.com','alert_lat':0,'alert_lon':0,'alert_enabled':True}
    response=client.post('/api/alerts/config',json=body,headers={'Authorization':'Bearer '+token()})
    assert response.status_code == 403
    main.users_collection.update_one.assert_not_called()


def test_verified_alert_config_records_new_consent_and_zero_coordinates(client,monkeypatch):
    monkeypatch.setattr(security,'verified_email_addresses',lambda user:('owner@example.com',['owner@example.com']))
    response=client.post('/api/alerts/config',json={'alert_email':'owner@example.com','alert_lat':0,'alert_lon':0,'alert_enabled':True},
                         headers={'Authorization':'Bearer '+token()})
    assert response.status_code == 200
    saved=main.users_collection.update_one.call_args.args[1]['$set']
    assert saved['alert_consent_version'] == 2
    assert saved['alert_lat'] == saved['alert_lon'] == 0


def test_alert_disable_does_not_require_email_provider(client):
    response=client.post('/api/alerts/config',json={'alert_enabled':False},headers={'Authorization':'Bearer '+token()})
    assert response.status_code == 200
    assert main.users_collection.update_one.call_args.args[1]['$set']['push_subscription'] is None


def test_websocket_requires_verified_user(client):
    with client.websocket_connect('/ws/user_a',headers={'Origin':ORIGIN}) as ws:
        ws.send_json({'token':token()})
        assert ws.receive_json() == {'type':'authenticated'}
        assert 'user_a' in main.manager.active_connections


def test_websocket_foreign_id_cannot_subscribe(client):
    with client.websocket_connect('/ws/user_b',headers={'Origin':ORIGIN}) as ws:
        ws.send_json({'token':token()})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4403
    assert 'user_b' not in main.manager.active_connections


def test_websocket_rejects_bad_token(client):
    with client.websocket_connect('/ws/user_a',headers={'Origin':ORIGIN}) as ws:
        ws.send_json({'token':'forged'})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401


def test_websocket_rejects_foreign_origin(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect('/ws/user_a',headers={'Origin':'https://attacker.invalid'}):
            pass
    assert exc.value.code == 4403


def test_verified_email_resolves_only_verified_clerk_rows(monkeypatch):
    monkeypatch.setenv('CLERK_SECRET_KEY','test-not-a-real-secret')
    response=MagicMock()
    response.json.return_value={'id':'user_a','primary_email_address_id':'verified', 'email_addresses':[
        {'id':'unverified','email_address':'third-party@example.com','verification':{'status':'unverified'}},
        {'id':'verified','email_address':'owner@example.com','verification':{'status':'verified'}}]}
    monkeypatch.setattr(security.requests,'get',lambda *a,**kw:response)
    assert security.verified_email_addresses('user_a') == ('owner@example.com',['owner@example.com'])


@pytest.mark.parametrize('claim', ['exp', 'nbf', 'iat', 'sub', 'sid', 'azp', 'iss'])
def test_required_session_claims_cannot_be_omitted(client, claim):
    claims = jwt.decode(token(), options={'verify_signature': False})
    claims.pop(claim)
    signed = jwt.encode(claims, PRIVATE, algorithm='RS256')
    assert client.get('/api/user/profile', headers={'Authorization': 'Bearer '+signed}).status_code == 401


def test_legacy_consent_is_shown_as_inactive(client):
    main.users_collection.find_one.return_value = {'clerk_id': 'user_a', 'alert_enabled': True}
    response = client.get('/api/user/profile', headers={'Authorization': 'Bearer '+token()})
    assert response.json()['profile']['alert_enabled'] is False
    assert response.json()['profile']['alert_requires_renewal'] is True


def test_config_enable_only_queues_owners_scan(client, monkeypatch):
    scan = MagicMock()
    monkeypatch.setattr(main, 'scan_and_alert_users', scan)
    monkeypatch.setattr(security, 'verified_email_addresses', lambda user: ('owner@example.com', ['owner@example.com']))
    response = client.post('/api/alerts/config', json={'alert_lat': 0, 'alert_lon': 0, 'alert_enabled': True},
                           headers={'Authorization': 'Bearer '+token()})
    assert response.status_code == 200
    scan.assert_called_once_with('user_a')


def test_multiple_sockets_for_one_user_do_not_disconnect_each_other(client):
    with client.websocket_connect('/ws/user_a', headers={'Origin': ORIGIN}) as first:
        first.send_json({'token': token()}); first.receive_json()
        with client.websocket_connect('/ws/user_a', headers={'Origin': ORIGIN}) as second:
            second.send_json({'token': token()}); second.receive_json()
            assert len(main.manager.active_connections['user_a']) == 2
        first.send_text('ping')
        assert len(main.manager.active_connections['user_a']) == 1


def test_jwks_mode_uses_only_the_configured_issuer(client, monkeypatch):
    monkeypatch.delenv('CLERK_JWT_PUBLIC_KEY')
    jwks = MagicMock()
    jwks.get_signing_key_from_jwt.return_value = SimpleNamespace(key=PRIVATE.public_key())
    factory = MagicMock(return_value=jwks)
    monkeypatch.setattr(security, '_jwks_client', factory)
    main.users_collection.find_one.return_value = None
    assert client.get('/api/user/profile', headers={'Authorization': 'Bearer '+token()}).status_code == 200
    factory.assert_called_once_with(ISSUER+'/.well-known/jwks.json')


def test_jwks_connection_failure_disables_access(client, monkeypatch):
    monkeypatch.delenv('CLERK_JWT_PUBLIC_KEY')
    jwks = MagicMock()
    jwks.get_signing_key_from_jwt.side_effect = security.PyJWKClientConnectionError('offline')
    monkeypatch.setattr(security, '_jwks_client', lambda url: jwks)
    assert client.get('/api/user/profile', headers={'Authorization': 'Bearer '+token()}).status_code == 503
    main.users_collection.find_one.assert_not_called()


def test_bad_server_verification_key_does_not_allow_access(client, monkeypatch):
    monkeypatch.setenv('CLERK_JWT_PUBLIC_KEY', 'invalid configuration')
    assert client.get('/api/user/profile', headers={'Authorization': 'Bearer '+token()}).status_code == 503
    main.users_collection.find_one.assert_not_called()


def test_push_dispatch_uses_only_host_key_and_supported_endpoint(monkeypatch):
    import base64
    import pywebpush
    from cryptography.hazmat.primitives.asymmetric import ec
    public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    def encode(raw): return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()
    subscription = {'endpoint': 'https://fcm.googleapis.com/fcm/send/test',
                    'keys': {'auth': encode(os.urandom(16)), 'p256dh': encode(public)}}
    monkeypatch.setattr(main, 'public_push_key', lambda: 'fresh-test-key')
    monkeypatch.setenv('VAPID_PRIVATE_KEY_PATH', '/secure/path/test-private.pem')
    monkeypatch.setenv('VAPID_SUBJECT', 'mailto:operator@example.com')
    sender = MagicMock()
    monkeypatch.setattr(pywebpush, 'webpush', sender)
    assert main.send_web_push(subscription, 'Test title', 'Test message') is True
    assert sender.call_args.kwargs['vapid_private_key'] == '/secure/path/test-private.pem'
    assert sender.call_args.kwargs['timeout'] == 10
    sender.reset_mock()
    assert main.send_web_push({**subscription, 'endpoint': 'https://127.0.0.1/'}, 'Title', 'Text') is False
    sender.assert_not_called()
