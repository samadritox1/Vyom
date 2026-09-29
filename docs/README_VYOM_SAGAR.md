# VYOM-MARINE v4 — Real-Time Ocean Data Fabric & Continuous Calibration

> Research / decision-support prototype. It is **not** a replacement for a master/captain, approved bridge equipment, official hydrographic products, certified stability information, COLREG/SOLAS obligations, GMDSS/NAVAREA/METAREA warnings, port instructions, or competent maritime authorities.

## 1. What changed in v4

VYOM-MARINE v4 keeps the complete Ocean Guardian-X / VYOM-MARINE v3.1 mathematical engine and adds the operational layer that was previously missing:

- real observational feeds;
- authoritative warning feeds;
- model-file ingestion;
- provenance and source-health tracking;
- forecast-versus-observation matching;
- region × season × lead-time × variable × model calibration;
- dynamically learned model weights;
- hierarchical shrinkage when local samples are sparse;
- drift monitoring;
- vessel-response outcome calibration kept separate from environmental-model calibration;
- persistent SQLite data fabric for the prototype;
- a standalone real-time worker;
- API endpoints for collection, health, environmental recalibration, and verified vessel-event labels.

The most important design rule is:

```text
ENVIRONMENTAL FORECAST CALIBRATION
  model × variable × region × season × lead time

is separate from

VESSEL-RISK CALIBRATION
  vessel class × loading × region × sea state × encounter geometry × event type
```

The ocean forecast does not become more accurate because a vessel is larger. Vessel class belongs in the *impact/response* calibration layer, not in the wave/weather-model calibration layer.

---

## 2. Recommended project layout

```text
project/
├── main_with_agro_marine.py          # FastAPI application/orchestration
├── vyom_agro_engine.py               # agricultural model
├── vyom_marine_engine.py             # Ocean Guardian-X mathematical model
├── marine_data_fabric.py             # observations/forecasts/matches/calibration DB
├── marine_connectors.py              # provider adapters
├── marine_realtime_service.py        # real-feed orchestrator
├── marine_realtime_worker.py         # continuous collection/calibration worker
├── marine_calibration.example.json   # optional static prior calibration
├── .env                              # secrets/configuration; never commit
├── data/
│   ├── vyom_marine_data.db
│   ├── marine_calibration.dynamic.json
│   └── incois_cache/
└── tests/
```

Do **not** paste the real-feed modules into `main.py`. The separation is intentional.

---

## 3. End-to-end architecture

```text
         OBSERVATIONS                   FORECASTS                  WARNINGS
              │                             │                         │
      NDBC / CO-OPS /                  Open-Meteo /               IMD / CAP /
      INCOIS ERDDAP /                  INCOIS WW3 /               METAREA feeds /
      other buoys                      HYCOM / Copernicus          port authorities
              │                             │                         │
              └──────────────┬──────────────┴──────────────┬──────────┘
                             ▼                             ▼
                       SOURCE ADAPTERS              AUTHORITY GATE
                             │                             │
                             ▼                             │
                       NORMALIZATION                       │
                             │                             │
                             ▼                             │
                    MARINE DATA FABRIC                     │
                   SQLite prototype store                  │
                             │                             │
                     ┌───────┴────────┐                    │
                     ▼                ▼                    │
                MATCHER          LATEST STATE              │
                     │                │                    │
                     ▼                │                    │
              CALIBRATION             │                    │
          bias / MAE / RMSE /         │                    │
          Brier / reliability         │                    │
                     │                │                    │
                     ▼                │                    │
             DYNAMIC MODEL WEIGHTS    │                    │
                     └────────┬───────┘                    │
                              ▼                            │
                     VYOM-MARINE ENGINE                    │
                              │                            │
                              ├── vessel-wave response     │
                              ├── stability screening      │
                              ├── collision / grounding    │
                              ├── cyclone / ice / surge    │
                              ├── route exposure           │
                              └── uncertainty              │
                              │                            │
                              └─────────────┬──────────────┘
                                            ▼
                                      FINAL ADVISORY
```

Official warnings are not averaged away by the numerical model. They remain highest-priority inputs.

---

## 4. Implemented real-source connectors

### 4.1 IMD

`IMDConnector` ingests the public IMD API endpoints used for:

- port warnings;
- sea-area bulletins;
- coastal bulletins;
- cyclone track;
- cyclone wind information;
- cyclone cone-of-uncertainty context;
- optional configurable fishermen-warning endpoint.

The connector normalizes these into authoritative `Warning` records and cyclone context.

### 4.2 NOAA NDBC

`NDBCConnector` supports:

- automatic discovery of nearby active stations from `activestations.xml`;
- standard real-time `.txt` station data;
- significant wave height;
- peak and mean wave period;
- mean wave direction;
- wind and gusts;
- pressure;
- visibility;
- sea-surface temperature;
- air temperature.

Manual station configuration is still supported and takes precedence over auto-discovery.

### 4.3 NOAA CO-OPS

`NOAA_COOPS_Connector` supports:

- automatic nearest-station discovery through the CO-OPS Metadata API;
- water level;
- air pressure;
- wind speed/gust/direction;
- visibility;
- water temperature.

CO-OPS coverage is mainly useful in U.S. waters, so the connector will naturally contribute little or nothing elsewhere.

### 4.4 INCOIS RSMC model products

`INCOISRSMCConnector` supports operator-supplied WW3 and HYCOM NetCDF files.

The connector reads the nearest grid point and normalizes supported variables such as:

- significant wave height;
- peak wave period;
- wave direction;
- SST;
- SSH;
- current U/V and derived current speed/direction.

The connector also checks the public RSMC download page for the latest advertised WW3/HYCOM filenames. It intentionally does **not** invent a fragile direct download URL when INCOIS does not expose one consistently.

### 4.5 INCOIS ERDDAP

`ERDDAPConnector` supports arbitrary verified INCOIS ERDDAP table datasets through a declarative JSON mapping.

This is useful because actual public dataset IDs and column names may change or differ by programme. Instead of hard-coding an unverified dataset ID, configure:

```json
{
  "dataset_id": "verified_dataset_id",
  "time_col": "time",
  "lat_col": "latitude",
  "lon_col": "longitude",
  "station_col": "platform_id",
  "variables": {
    "wave_height_m": {"column": "significant_wave_height", "unit": "m"}
  }
}
```

See `incois_erddap_datasets.example.json`.

### 4.6 GEBCO

`GEBCOConnector` reads a local GEBCO NetCDF grid or configured OPeNDAP dataset and extracts nearest-cell water depth.

**Important:** GEBCO is explicitly marked `navigation_grade: false`. By default it is context only. It is not used as a certified under-keel-clearance depth unless the operator intentionally enables non-navigation-grade screening with:

```env
VYOM_ALLOW_GEBCO_SCREENING_DEPTH=1
```

Even with that switch enabled, the result remains *screening*, not navigation clearance.

### 4.7 AIS

`AISHubConnector` supports area-based AIS retrieval when an AISHub account is configured.

Normalized fields include:

- MMSI;
- position;
- SOG;
- COG;
- heading;
- ROT;
- navigational status;
- vessel name;
- IMO;
- draught when present.

The existing Ocean Guardian collision engine consumes these contacts for CPA/TCPA and traffic-complexity calculations.

`MarineTrafficConnector` is also implemented for explicit MMSI/IMO lookups when a MarineTraffic API key is available.

AIS is disabled by default because credentials/provider access are external dependencies.

### 4.8 Copernicus Marine

`CopernicusConnector` uses the official `copernicusmarine` Python toolbox. It can open a configured dataset around the requested point and normalize common aliases for:

- sea-ice concentration;
- SST;
- sea level;
- currents;
- wave height;
- wave period.

Dataset IDs and variables are configuration, not hard-coded assumptions.

### 4.9 CAP / METAREA / WMO-style warning feeds

`CAPWarningConnector` ingests CAP 1.x XML feeds supplied in `WMO_CAP_FEEDS`.

It preserves:

- sender/authority;
- identifier;
- issue/onset/expiry times;
- severity;
- certainty/urgency metadata;
- area descriptions;
- polygons when present.

Because warning-distribution endpoints differ by authority and METAREA, the feed URLs are intentionally configurable.

### 4.10 Cyclone ensemble tracks

`CycloneEnsembleConnector` accepts normalized ensemble-track JSON from any upstream ECMWF/GEFS/RSMC processing job.

This avoids pretending that a single provider-specific cyclone file format is universal.

### 4.11 Sea ice

`SeaIceNetCDFConnector` accepts a configured local NetCDF or OPeNDAP product and normalizes nearest-cell concentration.

It is used as risk context, not as navigation clearance.

---

## 5. Canonical data records

### Observation

```text
source
station_id
observed_at
received_at
lat / lon
variable
value / unit
qc
source_quality
sigma
provenance
```

### Forecast

```text
source
model
cycle_time
valid_time
lead_hours
lat / lon
variable
value / unit
ensemble member
model_version
provenance
```

### Warning

```text
authority
warning_type
severity
issued_at
valid_from / valid_until
text
source_id
authoritative
geometry
region_text
raw provenance
```

### AIS track

```text
provider
MMSI
observed_at
position
SOG / COG / heading / ROT
navigation status
ship name / IMO / draught
```

---

## 6. Data fabric database

Prototype storage is SQLite/WAL for portability. Tables are created automatically:

```text
observations
forecasts
matches
calibration
warnings
ais_tracks
risk_predictions
vessel_labeled_cases
vessel_calibration
source_status
model_versions
```

For a high-volume production deployment, migrate the same logical schema to PostgreSQL/PostGIS + TimescaleDB or another appropriate geospatial/time-series stack.

---

## 7. Forecast-observation matching

The verifier matches a stored forecast against an observation using:

- same normalized variable;
- valid-time proximity;
- spatial proximity;
- forecast cycle and lead time retained;
- quality/provenance retained.

For ordinary scalar variables:

```text
error = forecast - observation
```

For directional variables, circular error is used:

```text
atan2(sin(theta_f-theta_o), cos(theta_f-theta_o))
```

so 359° versus 1° is treated as a 2° error rather than 358°.

---

## 8. Environmental calibration cells

Calibration keys are:

```text
model × variable × region × season × lead-band
```

Lead bands currently include:

```text
0–24 h
25–72 h
73–120 h
121–168 h
169 h+
```

Regions are hierarchical:

```text
5° cell → 10° cell → named ocean basin → GLOBAL
```

This permits local skill estimates while still providing a fallback in sparse data areas.

Computed statistics include:

- sample size;
- bias;
- MAE;
- RMSE;
- Brier score where an event threshold is defined;
- reliability proxy;
- time-window metadata.

---

## 9. Hierarchical shrinkage

Sparse local statistics are not trusted as if they were based on thousands of cases.

The lookup uses parent-region information through a shrinkage concept equivalent to:

```text
b* = n/(n+k) * b_local + k/(n+k) * b_parent
```

As local sample size grows, local statistics dominate. With sparse samples, broader-region calibration stabilizes the estimate.

---

## 10. Dynamic model weights

The engine no longer has to rely permanently on fixed provider weights.

Calibration quality incorporates RMSE, absolute bias, Brier/reliability information and sample size. Better-calibrated models receive higher weights for the specific:

```text
variable × region × season × lead time
```

The resulting calibration snapshot is exported to:

```text
./data/marine_calibration.dynamic.json
```

and consumed by the existing `CalibrationStore` in `vyom_marine_engine.py`.

If there is not enough verified data, the system falls back to prior/default quality rather than fabricating a learned weight.

---

## 11. Drift detection

`MarineDataFabric` can compare recent model RMSE with a longer baseline. This is intended to detect:

- model upgrades;
- unusual regime failures;
- source degradation;
- suddenly poor regional skill.

A production scheduler should use the drift report to flag review and, if desired, reduce trust in a degraded model rather than making a one-observation weight change.

---

## 12. Sensor assimilation

Recent nearby observations are transformed into the marine engine's sensor schema. The engine can then perform its existing Bayesian/Kalman-style corrections for supported variables.

Observation quality decays with:

- age;
- distance;
- provider/source quality.

The best recent candidate for each supported variable is selected rather than blindly averaging unrelated stations.

---

## 13. Vessel-response calibration is separate

Environmental forecast skill and vessel response are intentionally not mixed.

The API can store **verified** event or control cases through the admin-only endpoint:

```text
POST /marine/data/vessel-case
```

with `X-VYOM-Admin-Token`.

A vessel calibration cell can include:

```text
vessel class
loading state
region
sea-state band
encounter band
event type
mean predicted probability
observed event rate
Brier score
calibration gap
```

The system deliberately does **not** treat “no reported accident” as an automatic negative label. Labels must be explicitly verified; otherwise safety calibration would be corrupted by missing-report bias.

---

## 14. Official-warning logic

The real-time service determines whether a warning is geographically relevant using:

- polygon intersection when CAP geometry exists;
- conservative named-area matching for certain basin/coastal bulletins.

A non-spatial port/coastal warning is **not** applied globally.

Caller-supplied official-warning information always has precedence over automatically fetched context.

Model output must not downgrade an authoritative closure/warning.

---

## 15. Source conflict and abstention

The marine mathematical engine already keeps risk and confidence separate. The real-feed layer strengthens this by retaining:

- provider errors;
- source-health status;
- timestamps;
- model disagreement;
- calibration sample sizes;
- observation age;
- data provenance.

When critical inputs are absent, stale, contradictory, or outside the validated domain, the correct behavior is lower confidence / abstention—not false precision.

---

## 16. Installation

Create a virtual environment, then:

```bash
pip install -r requirements_vyom_marine_v4.txt
```

Copy:

```text
.env.marine.v4.example → .env
```

Fill only the sources you have access to.

---

## 17. Minimal mode

With no paid credentials, you can still run:

- IMD public marine/cyclone endpoints;
- NOAA NDBC auto-discovery/public station data where geographically relevant;
- NOAA CO-OPS public stations where geographically relevant;
- Open-Meteo models already used by the core engine;
- locally downloaded INCOIS WW3/HYCOM files;
- locally downloaded/configured GEBCO;
- configured public CAP feeds;
- configured local/OPeNDAP sea-ice products.

Credentialed providers are optional enhancements.

---

## 18. Run the continuous worker

Recommended:

```bash
python marine_realtime_worker.py
```

The worker:

1. loads `MARINE_WORKER_LOCATIONS_JSON`;
2. collects configured feeds at each point;
3. persists observations, forecasts, warnings and AIS;
4. periodically matches forecasts to observations;
5. recomputes calibration cells;
6. exports a dynamic calibration snapshot.

Keep the worker separate from the API in production.

---

## 19. Run FastAPI

```bash
uvicorn main_with_agro_marine:app --host 0.0.0.0 --port 8000 --reload
```

Important marine endpoints:

```text
POST /marine/advisory
GET  /marine/data/health
POST /marine/data/collect
POST /marine/data/calibrate?lookback_days=10
POST /marine/data/vessel-case
```

---

## 20. Example real-feed collection

```json
POST /marine/data/collect
{
  "lat": 18.0,
  "lon": 87.0,
  "run_calibration": false,
  "vessel": {
    "vessel_id": "demo-001",
    "vessel_type": "fishing_vessel",
    "length_m": 18,
    "beam_m": 5.5,
    "speed_kn": 7,
    "heading_deg": 210
  }
}
```

The response includes normalized real-feed context and provenance. The heavier marine analysis is performed by `/marine/advisory` or the normal Vyom marine-persona route.

---

## 21. Calibration endpoint

After forecasts have matured and observations have arrived:

```text
POST /marine/data/calibrate?lookback_days=30
```

This performs forecast-observation matching, recomputes environmental calibration cells, and exports the dynamic calibration file.

Do not expect meaningful calibration after only a few forecast/observation pairs.

---

## 22. Automatic NDBC and CO-OPS discovery

By default:

```env
VYOM_AUTO_NDBC=1
VYOM_AUTO_COOPS=1
```

The connector discovers nearby active stations, ranks them by distance and ingests the nearest few.

You can disable auto-discovery or supply exact station lists through:

```env
NDBC_STATIONS_JSON=./ndbc_stations.example.json
NOAA_COOPS_STATIONS_JSON=./noaa_coops_stations.example.json
```

Manual lists are preferable for a controlled production deployment because you can validate station suitability in advance.

---

## 23. INCOIS ERDDAP setup

Enable only after you verify the dataset ID and field names:

```env
VYOM_ENABLE_INCOIS_ERDDAP=1
INCOIS_ERDDAP_DATASETS_JSON=./incois_erddap_datasets.example.json
```

Never leave the placeholder dataset in the example file and assume it is real.

---

## 24. INCOIS WW3/HYCOM operational workflow

A robust production workflow is:

```text
scheduled downloader
        ↓
verify checksum / file readable
        ↓
atomic replace of local latest file
        ↓
INCOIS_WW3_NETCDF_PATH
INCOIS_HYCOM_NETCDF_PATH
        ↓
VYOM connector
```

This is more reliable than downloading large NetCDF files during each user request.

---

## 25. GEBCO workflow

Download/cache the required GEBCO grid once and configure:

```env
GEBCO_NETCDF_PATH=/path/to/gebco.nc
```

The data should be queried locally for speed.

Again: do not use this as a replacement for official charted depth in grounding/UKC decisions.

---

## 26. AIS setup

AISHub area feed:

```env
VYOM_ENABLE_AIS=1
AISHUB_USERNAME=your-account
```

The connector is disabled by default so the API does not repeatedly call a credentialed provider when credentials are absent.

If a future national VTS/AIS source becomes available, implement another adapter that emits the same `AISTrack` dataclass. No marine mathematics needs to change.

---

## 27. Copernicus setup

Install the toolbox from the requirements file and configure your normal Copernicus Marine credentials.

Then set:

```env
VYOM_ENABLE_COPERNICUS=1
COPERNICUS_DATASET_ID=<verified dataset id>
COPERNICUS_VARIABLES=variable1,variable2,...
```

Dataset IDs are intentionally not guessed in the code.

---

## 28. Sea-ice setup

Point the connector at a validated product:

```env
VYOM_ENABLE_SEA_ICE=1
SEA_ICE_NETCDF_PATH=/path/to/ice.nc
```

or:

```env
SEA_ICE_OPENDAP_URL=https://...
```

Sea-ice concentration is treated as risk context. Operational polar routing still requires appropriate official ice information and vessel capability data.

---

## 29. CAP warning setup

```env
VYOM_ENABLE_WMO_CAP=1
WMO_CAP_FEEDS=https://authority.example/feed1.xml,https://authority.example/feed2.xml
```

Only use official authority URLs that you have verified.

---

## 30. Cyclone ensemble setup

The connector expects normalized points:

```json
{
  "member": "m01",
  "time": "2026-09-27T00:00:00Z",
  "lat": 15.0,
  "lon": 88.0,
  "wind_kn": 55,
  "radius_km": 180
}
```

Enable with:

```env
VYOM_ENABLE_CYCLONE_ENSEMBLE=1
CYCLONE_ENSEMBLE_JSON_PATH=./cyclone_ensemble.example.json
```

The JSON may be produced by an upstream GRIB/ensemble-processing job.

---

## 31. Model-version/provenance requirements

Every production forecast ingester should retain:

```text
provider
model name
model version when known
cycle time
valid time
lead time
ensemble member
source file / dataset ID
ingestion time
```

Without these fields, changes in forecast skill cannot be diagnosed later.

---

## 32. Quality-control philosophy

The connector layer does not silently repair impossible provider data.

Where practical it:

- skips provider missing-value sentinels;
- retains provider QC labels;
- records ingestion errors;
- stores source quality;
- retains provenance;
- keeps observation uncertainty (`sigma`) when available.

A production deployment should add provider-specific QC rules for each operational network.

---

## 33. Continuous-learning schedule

Suggested prototype schedule:

```text
Every 15–60 min    ingest fast feeds appropriate to provider limits
Every hour         ingest slower model/observation feeds
Every 6 h          run forecast-observation matching
Daily              recompute calibration cells
Weekly             inspect learned model weights and drift
Monthly            blocked spatial/temporal backtest and model review
```

Provider limits and licenses must be respected. Do not poll a provider faster than its documented limits.

---

## 34. Why calibration is not performed after every observation

Immediate weight changes are unstable and can overfit one anomalous observation.

VYOM therefore stores the evidence first and updates calibrated statistics over a defined window with minimum-sample requirements and hierarchical fallback.

This provides much more stable model weighting.

---

## 35. Validation strategy

Use blocked rather than random validation wherever possible.

Recommended validation slices include:

```text
leave-region-out
leave-season-out
leave-year-out
cyclone-event hindcasts
high-wave-event hindcasts
low-visibility traffic scenarios
UKC/grounding voyage replays when trusted data exist
```

Core metrics:

```text
Bias
MAE
RMSE
Brier score
reliability / calibration error
CRPS when complete ensemble distributions are available
false-negative rate for safety events
```

---

## 36. Important limitation: CRPS

The existing calibration database stores a CRPS field, and the Ocean Guardian validation framework supports CRPS conceptually. A fully rigorous CRPS calculation requires the complete ensemble predictive distribution paired to an observation.

If only one deterministic value per model/lead is stored, the system must not claim a true ensemble CRPS. Keep the CRPS field empty/unavailable until ensemble-member distributions are retained for the verification pair.

This is intentional scientific honesty.

---

## 37. Important limitation: UKC

The mathematical engine supports probabilistic UKC/grounding risk, but operational UKC needs trusted:

```text
charted/hydrographic depth
vertical datum consistency
trusted tide/water level
actual draft/loading
squat model
wave/motion allowance
position uncertainty
required safety margin
```

GEBCO is not a certified substitute.

---

## 38. Important limitation: AIS

AIS can be delayed, absent, spoofed, incorrectly entered, or incomplete. CPA/TCPA generated by this research system is supplementary analytics only and does not replace COLREG compliance, radar/ARPA, bridge watchkeeping, or VTS instructions.

---

## 39. Important limitation: warnings

Authority APIs can be unavailable or change schema. VYOM records source health and must not interpret an unreachable warning feed as “no warning exists.”

A production interface should visibly distinguish:

```text
NO ACTIVE WARNING FOUND
```

from:

```text
WARNING FEED UNAVAILABLE
```

---

## 40. Tests

Run:

```bash
python -m py_compile \
  marine_data_fabric.py \
  marine_connectors.py \
  marine_realtime_service.py \
  marine_realtime_worker.py \
  vyom_marine_engine.py \
  main_with_agro_marine.py

python test_vyom_marine.py
python test_vyom_marine_v4.py
python test_vyom_marine_realtime.py
```

`test_vyom_marine_v4.py` verifies the data-fabric/calibration logic with deterministic synthetic truth. `test_vyom_marine_realtime.py` verifies connector parsers with mocked provider responses so CI does not depend on the internet.

---

## 41. Security

Never commit:

```text
AISHub credentials
MarineTraffic API keys
Copernicus credentials
SMTP credentials
Groq/Mistral keys
MongoDB connection secrets
admin token
```

`/marine/data/vessel-case` is admin-gated because malicious or accidental labels can corrupt a safety calibration model.

---

## 42. Production upgrades after the prototype

The current implementation is intentionally deployable without requiring Kafka/PostGIS/TimescaleDB, but high-volume operation should eventually add:

- PostgreSQL/PostGIS or equivalent for spatial objects;
- TimescaleDB or another robust time-series store;
- object storage for GRIB/NetCDF/raster archives;
- queue/stream processing;
- immutable raw-source archive;
- signed/auditable model versions;
- source-specific retries/circuit breakers;
- data-latency dashboards;
- blocked automated backtesting;
- incident-oriented safety review.

The Python interfaces were designed so these infrastructure changes do not require rewriting the core marine mathematics.

---

## 43. Files included in the v4 package

```text
main_with_agro_marine.py
vyom_marine_engine.py
vyom_agro_engine.py
marine_data_fabric.py
marine_connectors.py
marine_realtime_service.py
marine_realtime_worker.py
test_vyom_marine.py
test_vyom_marine_v4.py
test_vyom_marine_realtime.py
marine_calibration.example.json
.env.marine.v4.example
requirements_vyom_marine_v4.txt
ndbc_stations.example.json
noaa_coops_stations.example.json
marine_worker_locations.example.json
cyclone_ensemble.example.json
incois_erddap_datasets.example.json
README_VYOM_MARINE_V4.md
```

---

## 44. Final design principle

VYOM-MARINE v4 is no longer:

```text
weather API → LLM → maritime advice
```

It is designed as:

```text
observations + forecasts + authoritative warnings
                     ↓
             provenance / QC
                     ↓
              persistent evidence
                     ↓
         forecast-observation verification
                     ↓
        continuous calibrated model weights
                     ↓
        probabilistic digital-ocean state
                     ↓
        vessel-specific hazard analytics
                     ↓
             official-warning gate
                     ↓
          structured decision-support output
                     ↓
             LLM explanation only
```

The system should become more confident only when evidence justifies it. Missing observations, conflicting feeds, sparse calibration, non-navigation-grade data, or unavailable warnings must reduce confidence rather than be hidden from the user.

---

## Guarded narration in the unified backend

In the final unified build, this engine's structured output is passed through `engine_output_guard.py` before any user-facing LLM explanation. The guard reduces the payload to domain-authoritative fields, normalizes real-feed status, preserves risk/confidence/precision separately, blocks unsupported operational claims, checks generated numerical claims against the engine payload, and falls back to deterministic engine-grounded wording if validation fails. Engine selection is handled by `vyom_router.py`; an explicit persona always overrides automatic query routing.
