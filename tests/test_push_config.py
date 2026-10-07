import base64
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import push_config
from alert_policy import validate_push_subscription


def encode(raw): return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


@pytest.fixture
def key_pair(tmp_path, monkeypatch):
    private = ec.generate_private_key(ec.SECP256R1())
    raw = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    target = tmp_path/'private.pem'
    target.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    monkeypatch.setenv('VAPID_PRIVATE_KEY_PATH', str(target))
    monkeypatch.setenv('VAPID_PUBLIC_KEY', encode(raw))
    monkeypatch.setenv('VAPID_SUBJECT', 'mailto:operator@example.com')
    return raw, target


def test_fresh_matching_pair_is_enabled(key_pair):
    assert push_config.public_push_key() == encode(key_pair[0])


def test_mismatched_pair_disables_push(key_pair, monkeypatch):
    monkeypatch.setenv('VAPID_PUBLIC_KEY', encode(b'invalid-key'))
    assert push_config.public_push_key() is None


def test_exposed_pair_cannot_be_reenabled(key_pair, monkeypatch):
    monkeypatch.setattr(push_config, 'REVOKED_PUBLIC_KEY_SHA256', hashlib.sha256(key_pair[0]).hexdigest())
    assert push_config.public_push_key() is None


def test_missing_private_file_disables_push(key_pair):
    key_pair[1].unlink()
    assert push_config.public_push_key() is None


def subscription(endpoint='https://fcm.googleapis.com/fcm/send/test'):
    public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {'endpoint': endpoint, 'keys': {'p256dh': encode(public), 'auth': encode(os.urandom(16))},
            'extra': 'ignored'}


@pytest.mark.parametrize('endpoint', [
    'http://fcm.googleapis.com/test', 'https://127.0.0.1/test', 'https://169.254.169.254/test',
    'https://fcm.googleapis.com.attacker.invalid/test', 'https://attacker.invalid/test',
    'https://user:pass@fcm.googleapis.com/test', 'https://fcm.googleapis.com:999/test',
    'https://fcm.googleapis.com/test#fragment',
])
def test_push_cannot_target_arbitrary_or_internal_endpoints(endpoint):
    with pytest.raises(ValueError): validate_push_subscription(subscription(endpoint))


@pytest.mark.parametrize('endpoint', ['https://fcm.googleapis.com/fcm/send/test',
                                    'https://updates.push.services.mozilla.com/wpush/v2/test',
                                    'https://web.push.apple.com/test'])
def test_supported_push_services_accept_valid_subscriptions(endpoint):
    original = subscription(endpoint)
    result = validate_push_subscription(original)
    assert result == {'endpoint': endpoint, 'keys': original['keys']}


@pytest.mark.parametrize('bad_keys', [[], {'auth': 'bad', 'p256dh': 'bad'},
                                    {'auth': encode(b'a'*16), 'p256dh': encode(b'\x04'+b'\0'*64)}])
def test_malformed_push_keys_are_rejected(bad_keys):
    value = subscription(); value['keys'] = bad_keys
    with pytest.raises(ValueError): validate_push_subscription(value)


def test_generator_writes_private_key_without_printing_or_overwriting_it(tmp_path):
    script = Path(__file__).resolve().parents[1]/'scripts/generate_vapid_keys.py'
    target = tmp_path/'secrets/private.pem'
    result = subprocess.run([sys.executable, str(script), '--output', str(target)], capture_output=True, text=True)
    assert result.returncode == 0
    assert 'VAPID_PUBLIC_KEY=' in result.stdout and 'PRIVATE KEY' not in result.stdout
    if os.name == 'posix': assert target.stat().st_mode & 0o777 == 0o600
    original = target.read_bytes()
    again = subprocess.run([sys.executable, str(script), '--output', str(target)], capture_output=True, text=True)
    assert again.returncode != 0 and target.read_bytes() == original
