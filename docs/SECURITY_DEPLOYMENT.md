# Deploying the critical security fixes

This patch requires server configuration before the protected features can work.
Personal-data routes reject anonymous requests. Missing authentication configuration
disables protected operations; it never permits them. Public weather/advisory requests
can still run without an account when they do not name a stored user identity.

## 1. Configure Clerk verification

Set these values in the deployment's secret/environment settings:

| Variable | Value |
| --- | --- |
| `CLERK_ISSUER` | Exact HTTPS `iss` for your Clerk session tokens, from the same Clerk instance as the frontend. |
| `CLERK_AUTHORIZED_PARTIES` | Comma-separated exact frontend origins, including scheme and port. For example `https://your-domain.example`. List development origins only in development. |
| `CLERK_SECRET_KEY` | Server-side Clerk secret key for resolving the signed-in user's verified email addresses. Never add it to HTML or Git. |
| `CLERK_JWT_PUBLIC_KEY` | Optional Clerk RSA PEM public key. Literal `\n` line breaks are supported. If omitted, the server obtains signing keys from the issuer's `/.well-known/jwks.json`. |
| `CLERK_AUDIENCE` | Optional exact audience, only if the session tokens you issue include one. |

The server accepts RS256 session tokens with valid signature, issuer, expiry,
not-before time, issued-at time, session ID, user subject and authorized-party origin.
Pending sessions are rejected. A supplied `clerk_id` must equal the token's subject;
omitting it uses the verified subject automatically. A valid token cannot select
another account's profile, locations, field state or vessel state.

The existing browser now sends `Authorization: Bearer <session token>` on weather
requests as well as profile/configuration requests. WebSockets send the token in
their first frame, check the browser origin and account ownership, and disconnect
when the token expires. The browser reconnects with a fresh session token.

## 2. Configure administration

Generate a unique random token on your deployment host, for example:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Store it as `VYOM_ADMIN_TOKEN` in your host's secret settings. It must contain at
least 32 characters. Send it only from trusted administration clients in the
`X-VYOM-Admin-Token` header. The token protects alert triggering, marine collection,
all four calibration endpoints, verified vessel outcomes and internal data-health
endpoints. Ordinary Clerk sign-in does not grant administration access.

## 3. Replace the exposed VAPID signing pair

The old private signing key was committed to Git. Treat it as exposed. The patch
removes the tracked key files and blocks that old pair from being used by this
application. Removing files does not revoke copies already obtained by others.

Generate the replacement **on the deployment host or its secure secret-provisioning
system**, without committing or sharing the generated private key:

```bash
python scripts/generate_vapid_keys.py --output /secure/path/vapid_private.pem
```

The command creates a P-256 private key with owner-only permissions on POSIX and
prints only the public key and private-file path. It refuses to overwrite an
existing file. Set the printed `VAPID_PUBLIC_KEY`, set `VAPID_PRIVATE_KEY_PATH` to
that readable private file, and set `VAPID_SUBJECT` to your real operator contact
(`mailto:` or HTTPS). Keep the private file outside web-served directories.

Deploy the backend and frontend together. Push remains disabled until a fresh,
matching pair is configured. The frontend retrieves the public key from
`GET /api/push/public-key` and renews subscriptions tied to another signing key
when the user enables alerts. Stored subscriptions are only delivered through
the matching current signing pair. HTTPS is required for production browser push.

Old Git history still contains the exposed key. This change does not rewrite
history or revoke browser push subscriptions at their push services. Replace
deployment secrets, renew affected subscriptions, and coordinate any later history
cleanup with collaborators. Public keys are not secrets; both old key files are
removed to avoid accidental reuse.

## 4. Ask subscribers to confirm their alert settings again

Existing alert records were writable without server authentication. They are
therefore inactive after this upgrade until the signed-in owner reviews the
location and enables alerts again. New authenticated consent stores version 2.
The API presents older settings as inactive and clears legacy push subscriptions
from the profile response. Enabling alerts requires an email address verified on
that user's Clerk account. The scanner rechecks verified addresses and consent;
there is no fallback to a caller-supplied profile email or scraped agency address.

Configure MongoDB (`MONGO_URI`) for user state and the new `alert_deliveries`
collection, and SMTP if you want email delivery. Configuration changes trigger
only the owner's scan; the periodic/admin scan can process all opted-in users.

## Alert behavior

- Official notifications forward active authority bulletins verified for the
  user's location. CAP feeds must provide usable polygon/circle geometry; a
  geocode-only or malformed record is withheld and the source marked degraded.
  Test/private, future, expired, canceled and superseded messages are excluded.
  NWS uses its active-point endpoint within its US service area.
- Weather threshold notifications use fresh Open-Meteo hourly forecasts in UTC:
  three consecutive hot hours, a wind threshold, or a three-hour rain total.
  Their text explicitly labels them as model advisories. They do not infer a
  flood, earthquake, confirmed disaster, evacuation order or all-clear.
- Alerts use deterministic text and the authority's original instructions.
  The previous LLM-authored disaster plans, earthquake prediction search and
  automatic email to web-searched government recipients are removed. Forecast
  templates are currently English; authority text is forwarded without translation.
- Delivery state is stored per user, event and channel. Completed channels do not
  resend during later scans; failed channels can retry. A five-minute lease blocks
  overlapping sends across processes. A process crash after an external send but
  before recording success can still cause a retry: this is not an exactly-once
  delivery guarantee. Do not delete delivery records for still-active events.
- Missing source data produces no invented warning or automatic all-clear.
  Live alert text is rendered as text in the browser and escaped in email HTML.

## Validation

```bash
python -m pip install -r requirements-test.txt
python -m pytest tests -q
node --test tests/test_frontend.cjs
python -m compileall -q main.py security.py notifications.py alert_policy.py cap_policy.py push_config.py
```

The regression suite uses generated test signing keys, mocked provider responses
and in-memory delivery state. It exercises authentication, cross-account denial,
admin access, alert consent, verified recipients, geography/validity, deduplication,
concurrent delivery claims, push endpoint restrictions and safe key generation.
It sends no real email or push notification and does not access production data.

After configuring and deploying, check with a test account: profile save/load;
a weather request with its token; denied requests without a token and with another
account's ID; WebSocket authentication; and alert enable/disable with your own
verified email. Test real delivery only to consenting test accounts. Test suite
success does not verify your live Clerk, MongoDB, SMTP or hosting configuration.
