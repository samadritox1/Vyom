# VYOM Environment Variables

Create your own `.env` in the project root. Do not commit secrets to Git.

## Core backend

- `GROQ_API_KEY` — primary LLM API key.
- `MISTRAL_API_KEY` — fallback LLM API key.
- `MONGO_URI` — MongoDB connection string.
- `SMTP_EMAIL`, `SMTP_APP_PASSWORD`, `SMTP_HOST`, `SMTP_PORT` — optional email alerts.
- `CLERK_ISSUER` — exact HTTPS issuer for frontend Clerk session tokens.
- `CLERK_AUTHORIZED_PARTIES` — comma-separated exact allowed frontend origins.
- `CLERK_SECRET_KEY` — server-only Clerk key for verifying email recipients.
- `CLERK_JWT_PUBLIC_KEY` — optional RSA PEM verification key; otherwise issuer JWKS is used.
- `CLERK_AUDIENCE` — optional session-token audience to enforce when configured.
- `VYOM_ADMIN_TOKEN` — server-only token of at least 32 characters; required by administrative endpoints through `X-VYOM-Admin-Token`.
- `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY_PATH`, `VAPID_SUBJECT` — fresh matching P-256 push pair and real operator contact. No repository-key fallback.
- `VYOM_USER_AGENT` — descriptive contact User-Agent for external public-data services.

See [SECURITY_DEPLOYMENT.md](docs/SECURITY_DEPLOYMENT.md) for required setup,
push-key replacement and alert consent renewal. `.env.example` contains placeholders,
not working credentials. Host environment values take precedence over `.env`.

## Agriculture

- `VYOM_AGRO_CALIBRATION_PATH` — optional agriculture calibration JSON path.

## Marine / VYOM SAGAR

- `VYOM_REAL_FEEDS_ENABLED`
- `VYOM_MARINE_DB`
- `VYOM_MARINE_CALIBRATION_PATH`
- `VYOM_DYNAMIC_CALIBRATION_PATH`
- `INCOIS_ERDDAP_BASE`
- `INCOIS_ERDDAP_DATASETS_JSON`
- `INCOIS_WW3_NETCDF_PATH`
- `INCOIS_HYCOM_NETCDF_PATH`
- `INCOIS_CACHE_DIR`
- `INCOIS_RSMC_FILE_BASE`
- `GEBCO_NETCDF_PATH`
- `GEBCO_OPENDAP_URL`
- `SEA_ICE_NETCDF_PATH`
- `SEA_ICE_OPENDAP_URL`
- `COPERNICUS_DATASET_ID`
- `COPERNICUS_VARIABLES`
- `VYOM_ENABLE_COPERNICUS`
- `VYOM_ENABLE_INCOIS_ERDDAP`
- `VYOM_ENABLE_SEA_ICE`
- `VYOM_ENABLE_AIS`
- `AISHUB_USERNAME`
- `MARINETRAFFIC_API_KEY`
- `IMD_FISHERMEN_ENDPOINT`
- `WMO_CAP_FEEDS`
- `CYCLONE_ENSEMBLE_JSON_PATH`
- `CYCLONE_ENSEMBLE_JSON_URL`
- `MARINE_WORKER_LOCATIONS_JSON` — JSON array or path to your own location file.
- `MARINE_WORKER_INTERVAL_SEC`
- `MARINE_CALIBRATE_EVERY_CYCLES`

## Aviation / VYOM VAYU

- `VYOM_AVIATION_REAL_FEEDS_ENABLED`
- `VYOM_AVIATION_DB_PATH`
- `VYOM_AVIATION_CALIBRATION_PATH`
- `VYOM_AVIATION_DYNAMIC_CALIBRATION`
- `AWC_API_BASE`
- `VYOM_AVIATION_USER_AGENT`
- `VYOM_AVIATION_WORKER_LOCATIONS` — JSON array or path to your own location file.
- `VYOM_AVIATION_WORKER_INTERVAL_SECONDS`

## Adventure / VYOM ADVENTURE

- `VYOM_ADVENTURE_REAL_FEEDS_ENABLED`
- `VYOM_ADVENTURE_DB_PATH`
- `VYOM_ADVENTURE_CALIBRATION_PATH`
- `VYOM_ADVENTURE_DYNAMIC_CALIBRATION`
- `NASA_FIRMS_MAP_KEY`
- `NASA_FIRMS_SOURCE`
- `ADVENTURE_OFFICIAL_ALERT_JSON_URL`
- `ADVENTURE_AVALANCHE_JSON_URL`
- `ADVENTURE_ENABLE_NWS`
- `ADVENTURE_WORKER_LOCATIONS` — JSON array or path to your own location file.
- `ADVENTURE_WORKER_INTERVAL_SECONDS`

## Disaster / VYOM RAKSHA

- `VYOM_DISASTER_DB_PATH`
- `VYOM_DISASTER_CALIBRATION`
- `VYOM_DISASTER_DYNAMIC_CALIBRATION`
- `DISASTER_CAP_FEED_URL`
- `DISASTER_TSUNAMI_JSON_URL`
- `DISASTER_LOCAL_CONTEXT_JSON_URL`
- `DISASTER_OBSERVATION_JSON_URL`
- `DISASTER_ENABLE_IMD`
- `DISASTER_ENABLE_NWS`
- `DISASTER_LIVE_CONTEXT_ON_QUERY`
- `USGS_EARTHQUAKE_FEED`
- `NASA_FIRMS_MAP_KEY`
- `NASA_FIRMS_SOURCE`
- `DISASTER_WORKER_LOCATIONS` — JSON array or path to your own location file.
- `DISASTER_WORKER_INTERVAL_SECONDS`

Only configure sources you actually use. When a source is unavailable or unconfigured, the scientific engines should preserve the missing-data/abstention state rather than inventing observations.
