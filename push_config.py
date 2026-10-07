"""Deployment-owned push keys; no fallback to previously exposed repository keys."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path


def public_push_key() -> str | None:
    public = os.getenv('VAPID_PUBLIC_KEY', '').strip()
    private_path = os.getenv('VAPID_PRIVATE_KEY_PATH', '')
    subject = os.getenv('VAPID_SUBJECT', '')
    if not public or not private_path or not Path(private_path).is_file() or not subject.startswith(('mailto:', 'https://')):
        return None
    try:
        raw = base64.b64decode(public + '='*(-len(public)%4), altchars=b'-_', validate=True)
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        key = serialization.load_pem_private_key(Path(private_path).read_bytes(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            return None
        expected = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        if raw != expected:
            return None
        # Reject the exposed signing pair even if it is reconfigured on the host.
        if hashlib.sha256(raw).hexdigest() == REVOKED_PUBLIC_KEY_SHA256:
            return None
    except (ValueError, TypeError, OSError):
        return None
    return public


REVOKED_PUBLIC_KEY_SHA256 = '114057251765a6371dd12f41457f261767547c11e4c4cae00ad17deeb7464d03'
