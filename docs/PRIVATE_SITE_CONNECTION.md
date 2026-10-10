# Connect the private VYOM website

The website remains owner-only at https://vyom-advisory-main.sapchap924.chatgpt.site.
Its server calls this repository's existing `post_live_weather` handler through a
protected, weather-only `site_api:app` entry point. Engine equations, routing,
narration guards, and official-warning semantics are unchanged.

## Run on your existing PC

Keep your existing `GROQ_API_KEY` and other configured backend values in your
local `.env`. Add a random service token of at least 32 characters to
`VYOM_SITE_SERVICE_TOKEN`. Generate one locally with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Then run:

```powershell
python -m uvicorn site_api:app --host 127.0.0.1 --port 8000
```

Make this process reachable through your existing HTTPS tunnel. The PC and tunnel
must stay running. Never point a tunnel at the unrestricted `main:app` for this
connection: `site_api:app` exposes only health and authenticated weather requests.

## Optional container packaging

`Dockerfile.site` builds the existing Python dependencies and starts `site_api:app`.
Supply the existing API and data-source secrets through the process environment.
The image does not copy `.env`, PEM keys, or Git metadata. It does not deploy a
service or change the private website audience.

## Connect the website server

Set these two production runtime values on the existing private Site:

| Name | Value |
| --- | --- |
| `VYOM_BACKEND_URL` | HTTPS base URL of this service or PC tunnel |
| `VYOM_BACKEND_TOKEN` | Same value as `VYOM_SITE_SERVICE_TOKEN`, stored as a secret |

Redeploy the Site to apply the runtime revision. The browser uses same-origin
`POST /api/weather`; it never receives the service token. No cross-origin browser
requests or CORS changes are needed. Retain the Site's owner-only access policy.

## Data contract

The exact persona strings and nested `agro`, `marine`, `aviation`, `adventure`,
and `disaster` fields are sent to `WeatherRequest`. Smart city planning uses the
existing `Smart City Planner` path. Context that has no scientific input field is
included as descriptive query context, without inventing numerical observations.
Body measurements and profile identity are not sent; saved user state and alert
endpoints are not exposed through the new application.

The backend's report and structured engine assessment remain unchanged.
`frontend_forecast` supplies a separate, 72-hour Open-Meteo forecast in UTC for
the parameter charts. Marine wave values are aligned by timestamp. Missing feeds
remain unavailable. Graphs and demonstrative frontend windows are separate from
engine risk or operational clearance.

## Verify after hosting

1. Confirm `/health` returns `status: ok`.
2. An unauthenticated `POST /weather` must return 401.
3. The private website must report a configured weather service.
4. Request an advisory in each perspective and check engine identity, supplied
   location, report, and hourly data. The marine persona must be `Marine/Fisherman`.
5. Confirm 24/48/72-hour charts use supplied forecast values and show missing
   parameters as unavailable. Confirm failed requests never become live sample data.

Automated tests cover authentication, the protected API contract, timestamp
alignment, missing feeds, and frontend-to-proxy request mapping. Live engines and
provider availability require the actual hosted service and configured secrets.
