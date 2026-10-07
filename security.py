"""Server-side Clerk authentication and resource ownership enforcement."""
from __future__ import annotations

import hmac
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import quote, urlsplit

import jwt
import requests
from fastapi import Header, HTTPException
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    expires_at: int


def allowed_origins() -> set[str]:
    return {s.strip().rstrip('/') for s in os.getenv('CLERK_AUTHORIZED_PARTIES', '').split(',') if s.strip()}


@lru_cache(maxsize=8)
def _jwks_client(url: str) -> PyJWKClient:
    return PyJWKClient(url, cache_jwk_set=True, lifespan=300, timeout=5)


def authenticate_token(token: str) -> AuthenticatedUser:
    issuer = os.getenv('CLERK_ISSUER', '').rstrip('/')
    origins = allowed_origins()
    parsed = urlsplit(issuer)
    if (not issuer or not origins or parsed.scheme != 'https' or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise HTTPException(503, 'Server authentication is not configured')
    if not isinstance(token, str) or not token or len(token) > 16384:
        raise HTTPException(401, 'Invalid session token', headers={'WWW-Authenticate': 'Bearer'})
    try:
        public_key = os.getenv('CLERK_JWT_PUBLIC_KEY', '').replace('\\n', '\n')
        if not public_key:
            public_key = _jwks_client(issuer + '/.well-known/jwks.json').get_signing_key_from_jwt(token).key
        audience = os.getenv('CLERK_AUDIENCE') or None
        claims = jwt.decode(
            token, public_key, algorithms=['RS256'], issuer=issuer, audience=audience,
            leeway=5,
            options={'require': ['exp', 'nbf', 'iat', 'iss', 'sub', 'sid', 'azp'],
                     'verify_aud': bool(audience)},
        )
        if claims.get('azp') not in origins or claims.get('sts') == 'pending':
            raise jwt.InvalidTokenError('Session origin or status is not allowed')
        user_id = claims.get('sub')
        if not isinstance(user_id, str) or not re.fullmatch(r'user_[A-Za-z0-9]+', user_id):
            raise jwt.InvalidTokenError('Invalid subject')
        return AuthenticatedUser(user_id, int(claims['exp']))
    except PyJWKClientConnectionError:
        raise HTTPException(503, 'Session verification is temporarily unavailable') from None
    except jwt.InvalidKeyError:
        raise HTTPException(503, 'Session verification key is not configured correctly') from None
    except (jwt.InvalidTokenError, PyJWKClientError, ValueError, TypeError):
        raise HTTPException(401, 'Invalid or expired session token', headers={'WWW-Authenticate': 'Bearer'}) from None


def optional_user(authorization: str | None = Header(None)) -> AuthenticatedUser | None:
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(' ')
    if scheme.lower() != 'bearer' or not token.strip():
        raise HTTPException(401, 'Bearer session token required', headers={'WWW-Authenticate': 'Bearer'})
    return authenticate_token(token.strip())


def require_user(authorization: str | None = Header(None)) -> AuthenticatedUser:
    user = optional_user(authorization)
    if user is None:
        raise HTTPException(401, 'Sign in required', headers={'WWW-Authenticate': 'Bearer'})
    return user


def owned_user_id(claimed_id: str | None, user: AuthenticatedUser | None) -> str | None:
    if claimed_id is not None and user is None:
        raise HTTPException(401, 'Sign in required for stored user data')
    if claimed_id is not None and user is not None and claimed_id != user.user_id:
        raise HTTPException(403, 'Cannot access another user')
    return user.user_id if user is not None else None


def require_admin(x_vyom_admin_token: str | None = Header(None)) -> None:
    expected = os.getenv('VYOM_ADMIN_TOKEN', '')
    if len(expected) < 32:
        raise HTTPException(503, 'Server administration is not configured')
    if not x_vyom_admin_token or not hmac.compare_digest(x_vyom_admin_token.encode(), expected.encode()):
        raise HTTPException(403, 'Admin token required')


def verified_email_addresses(user_id: str) -> tuple[str, list[str]]:
    """Resolve recipient addresses from Clerk, never from caller-supplied profile data."""
    secret = os.getenv('CLERK_SECRET_KEY')
    if not secret:
        raise HTTPException(503, 'Verified-email service is not configured')
    try:
        response = requests.get(
            'https://api.clerk.com/v1/users/' + quote(user_id, safe=''),
            headers={'Authorization': 'Bearer ' + secret}, timeout=5,
        )
        response.raise_for_status()
        profile = response.json()
        if profile.get('id') != user_id:
            raise ValueError('Clerk returned another identity')
        rows = [r for r in profile.get('email_addresses', [])
                if (r.get('verification') or {}).get('status') == 'verified']
        emails = [r['email_address'] for r in rows if isinstance(r.get('email_address'), str)]
        primary = next((r['email_address'] for r in rows
                        if r.get('id') == profile.get('primary_email_address_id')), '')
        return primary or (emails[0] if emails else ''), emails
    except (requests.RequestException, ValueError, KeyError, TypeError):
        raise HTTPException(503, 'Could not verify notification recipient') from None


def verified_alert_email(user_id: str, requested: str) -> str:
    primary, emails = verified_email_addresses(user_id)
    if not requested:
        if not primary:
            raise HTTPException(400, 'A verified email address is required')
        return primary
    match = next((e for e in emails if e.casefold() == requested.strip().casefold()), None)
    if match is None:
        raise HTTPException(403, 'Alerts can only use your verified Clerk email addresses')
    return match
