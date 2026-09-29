# VYOM RAKSHA
## Multi-Hazard Disaster Intelligence & Emergency Decision-Support Engine

**Engine ID:** `VYOM-RAKSHA`  
**Version:** `1.0.0`  
**Role:** Disaster-management decision support, impact screening, uncertainty analysis, authoritative-warning fusion, continuous forecast verification.

> VYOM RAKSHA is not an emergency authority. It must never replace official warnings, evacuation orders, incident command, emergency services, structural/geotechnical engineering assessment, or local disaster-management SOPs.

---

## 1. Why this engine exists

The old Disaster Management persona in VYOM was essentially a weather threshold system: high rain, strong wind, heat, web-search snippets, then an LLM generated an emergency message. That architecture is not strong enough for a flagship multi-hazard platform.

VYOM RAKSHA changes the pipeline to:

```text
Authoritative warnings + observations + numerical forecasts + exposure + vulnerability
                                  |
                                  v
                           VYOM RAKSHA
                                  |
              +-------------------+------------------+
              |                   |                  |
              v                   v                  v
          Hazard model       Impact model       Uncertainty
              |                   |                  |
              +-------------------+------------------+
                                  |
                                  v
                       Structured decision object
                                  |
                                  v
                       LLM explains, not calculates
```

The engine keeps four quantities separate:

- **Hazard probability / hazard severity proxy**
- **Impact risk**
- **Confidence**
- **Data quality / precision mode**

A dangerous event with low confidence is not treated as safe.

---

## 2. Files

```text
vyom_disaster_engine.py
    Core multi-hazard mathematical engine

disaster_connectors.py
    USGS, GDACS, Open-Meteo/GloFAS, NASA FIRMS, IMD,
    NWS, CAP, tsunami-feed and local-context adapters

disaster_data_fabric.py
    SQLite forecast/observation/event/warning/calibration store

disaster_realtime_service.py
    Feed orchestration, normalization, source health, calibration

disaster_realtime_worker.py
    Periodic collection + forecast verification worker

main_with_agro_marine_aviation_adventure_disaster.py
    Combined VYOM backend with all five engines

test_vyom_disaster.py
    Offline scientific/data-fabric regression

disaster_calibration.example.json
.env.disaster.example
disaster_worker_locations.example.json
disaster_context.example.json
requirements_vyom_disaster.txt
```

---

## 3. Implemented hazard modules

### 3.1 Flood and flash flood

Inputs may include:

- 1 h / 3 h / 6 h / 24 h / 72 h precipitation
- surface soil moisture
- GloFAS discharge context
- local drainage capacity
- impervious fraction
- terrain slope
- antecedent rainfall

The engine calculates separate:

```text
river_flood
flash_flood
```

A coarse global flood model is never treated as a local river-gauge or hydraulic model.

### 3.2 Landslide susceptibility

Weather/terrain screening combines:

```text
24 h rain
72 h rain
7-day antecedent rain
soil saturation proxy
slope
local susceptibility input
```

This is not a geotechnical slope-stability calculation.

### 3.3 Severe convection

Uses:

```text
CAPE
thunderstorm weather codes
gusts
optional lightning density
```

### 3.4 Wind hazard

Uses sustained wind, gusts and optional local exposure context.

### 3.5 Heat / cold

Includes:

- wet-bulb-temperature screening
- heat-duration accumulation
- wind-chill screening
- configurable local heat/cold thresholds

### 3.6 Wildfire / smoke environment

Combines:

```text
temperature
relative humidity
wind/gust
recent rain
NASA FIRMS detections
PM2.5
```

NASA FIRMS detections remain satellite thermal-anomaly context only and are never interpreted as an official fire perimeter or evacuation order.

### 3.7 Earthquake observed-event context

Uses real earthquake-event information when supplied by USGS or another verified source.

The engine calculates a **screening shaking-risk proxy** based on magnitude and distance.

It does **not**:

- predict earthquakes
- replace ShakeMap
- calculate building-specific structural safety
- claim an area is safe after an earthquake

### 3.8 Tsunami context

Official tsunami warnings are a hard authority layer.

An earthquake feed's `tsunami` flag may raise attention, but VYOM does not infer:

```text
evacuation_order
all_clear
```

from magnitude alone.

### 3.9 Cyclone context

Can use IMD official cyclone track data and other normalized cyclone-track inputs.

Risk depends on track-point distance and wind/intensity context.

### 3.10 Drought

Supports validated/local:

```text
SPI-3
SPI-6
SPEI-3
```

If they are not supplied, drought output abstains instead of inventing an index.

### 3.11 Coastal inundation

Optional context accepts trusted:

```text
storm surge
tide
wave setup
ground elevation
water-level uncertainty
```

This is impact screening only and is not navigation-, evacuation-, or engineering-grade.

---

## 4. Multi-model weather fusion

Default forecast members:

```text
ECMWF IFS
GFS
ICON
GEM
```

Calibration is designed around:

```text
model x variable x season x lead-time bin
```

Each calibration record can contain:

```text
bias
RMSE
reliability
samples
```

The corrected member becomes approximately:

```text
F_corrected = F_raw - bias
```

while uncertainty combines local calibration RMSE and inter-model disagreement.

---

## 5. Monte Carlo uncertainty

VYOM RAKSHA generates coherent future scenarios by perturbing fused forecast variables with scenario-level and local hourly components.

The output includes:

```text
median peak weather hazard
P10
P90
P(peak risk >= 0.70)
P(peak risk >= 0.90)
```

The scenario system does not convert uncertainty into false certainty.

---

## 6. Exposure and vulnerability

The hazard itself is not the full disaster risk.

Optional impact context includes:

```text
population
population density
critical facilities count
infrastructure exposure index
vulnerability index
preparedness index
```

The engine uses a bounded impact relationship conceptually similar to:

```text
ImpactRisk = Hazard * ExposureFactor * VulnerabilityFactor
```

with bounded multipliers to avoid runaway values.

If exposure data are missing, VYOM still reports hazard information but downgrades precision.

---

## 7. Compound hazards

The engine does not assume all hazards are independent.

Explicit dependency terms currently include:

```text
flood x landslide
flash flood x landslide
wildfire x wind
cyclone x flood
cyclone x coastal inundation
earthquake x tsunami context
convection x wind
```

The final risk uses dependency-aware fusion plus an authoritative-warning floor.

---

## 8. Official warnings

Official warnings are never averaged away.

Supported architecture:

```text
OASIS CAP feed
IMD official cyclone feeds
NWS alerts where applicable
configured official tsunami feed
configured local authority feed/context
```

For CAP, VYOM understands key concepts such as:

```text
severity
urgency
certainty
event
instruction
effective time
expiry time
```

If an authoritative warning is more severe than the model estimate, it becomes the minimum final warning level.

---

## 9. Real data connectors

### USGS Earthquake

Uses the official USGS GeoJSON feed for recent observed earthquakes.

### GDACS

Consumes GDACS multi-hazard event data for contextual awareness.

### Open-Meteo / GloFAS

Uses GloFAS river-discharge model context. GloFAS is coarse global/regional hydrological context, not a local gauge.

### NASA FIRMS

Uses near-real-time VIIRS active-fire detections when `NASA_FIRMS_MAP_KEY` is configured.

### IMD

Uses documented IMD cyclone-track / cyclone-wind endpoints where available.

### CAP

A deployment may connect any official OASIS CAP 1.2 XML feed using `DISASTER_CAP_FEED_URL`.

### Official tsunami

The package deliberately does not scrape a human-facing tsunami-warning website or invent an undocumented API. Configure an authorized/normalized official feed with `DISASTER_TSUNAMI_JSON_URL`.

### Local impact context

`DISASTER_LOCAL_CONTEXT_JSON_URL` can supply local terrain, drainage, exposure, vulnerability, gauge, coastal or incident information.

### Local observations

`DISASTER_OBSERVATION_JSON_URL` can provide normalized observations for continuous model verification.

---

## 10. Continuous verification

The data fabric stores:

```text
observations
forecasts
forecast-observation matches
calibration cells
source health
model versions
hazard events
official warnings
```

Forecast-observation matching uses temporal and spatial tolerances.

Calibration metrics currently include:

```text
Bias
MAE
RMSE
Reliability proxy
```

The architecture can later be extended with CRPS, Brier score, calibration diagrams and event-detection metrics for ensemble/probabilistic products.

---

## 11. Precision modes

Possible modes include:

```text
WEATHER_ONLY
HAZARD_CONTEXT
IMPACT_AWARE
OFFICIAL_AWARE
OFFICIAL_IMPACT_AWARE
```

The response includes `missing_for_higher_precision` so the application knows what information would improve the assessment.

---

## 12. Safety levels

The simplified UI classification is:

```text
NORMAL
CAUTION
HIGH RISK
CRITICAL
```

The underlying probabilities remain available.

Do not use these labels as replacements for an official emergency classification.

---

## 13. Actions the engine intentionally does not issue

These fields remain null unless your application explicitly inserts a genuine authority-issued order:

```json
{
  "evacuation_order": null,
  "shelter_selection": null,
  "rescue_dispatch": null,
  "all_clear": null
}
```

That is intentional.

VYOM can prioritize **management attention** but should not invent operational emergency orders.

---

## 14. API

### Standalone advisory

```text
POST /disaster/advisory
```

Example body:

```json
{
  "lat": 22.5726,
  "lon": 88.3639,
  "horizon_hours": 120,
  "scenario_count": 160,
  "collect_live_context": true,
  "context": {
    "country_code": "IN",
    "terrain_context": {
      "slope_deg": 12,
      "landslide_susceptibility": 0.4
    },
    "exposure_context": {
      "population": 250000,
      "population_density_km2": 1800,
      "critical_facilities_count": 20,
      "vulnerability_index": 0.55,
      "preparedness_index": 0.6
    }
  }
}
```

### Feed/data-fabric health

```text
GET /disaster/data/health
```

### Recompute calibration

```text
POST /disaster/data/calibrate?lookback_days=30
```

---

## 15. Main VYOM integration

The included combined backend supports:

```text
Farmer               -> VYOM KRISHI
Marine/Fisherman     -> VYOM SAGAR
Aviation             -> VYOM VAYU
Adventurer            -> VYOM ADVENTURE
Disaster Management  -> VYOM RAKSHA
```

For Disaster Management, the main backend can automatically collect live disaster context before calling the engine.

The LLM receives the structured result and is explicitly instructed not to invent emergency orders or recompute hazard risk from raw weather.

---

## 16. Environment variables

Copy:

```text
.env.disaster.example
```

into your main `.env`, then configure the sources you actually have authorization to use.

The most important variables are:

```text
NASA_FIRMS_MAP_KEY
DISASTER_CAP_FEED_URL
DISASTER_TSUNAMI_JSON_URL
DISASTER_LOCAL_CONTEXT_JSON_URL
DISASTER_OBSERVATION_JSON_URL
VYOM_DISASTER_DB_PATH
VYOM_DISASTER_DYNAMIC_CALIBRATION
```

---

## 17. Background worker

Run:

```bash
python disaster_realtime_worker.py
```

It will:

1. load configured watch locations
2. collect available event/official/context feeds
3. collect numerical-model forecasts for verification
4. ingest configured local observations
5. match mature forecasts with observations
6. recompute calibration
7. export dynamic calibration JSON

This should eventually run as a separate worker/service in production, although the end user still sees one VYOM application.

---

## 18. Tests

Run:

```bash
python test_vyom_disaster.py
```

The regression tests cover:

- flood / flash-flood response
- landslide screening
- severe convection
- wind risk
- heat/cold calculations
- wildfire context
- earthquake observed-event context
- tsunami official-warning gate
- cyclone context
- coastal inundation context
- exposure/vulnerability
- dependency-aware fusion
- scenario statistics
- SQLite forecast verification
- event and warning persistence

---

## 19. Recommended future upgrades

The next scientific improvements should come from better data, not random formulas:

1. official national/regional CAP feeds
2. CWC/state/local river gauges and hydraulic-model outputs where authorized
3. radar-derived quantitative precipitation estimates
4. verified lightning networks
5. DEM/landslide-susceptibility rasters
6. population and critical-infrastructure geospatial layers
7. real storm-surge ensembles
8. ShakeMap/PAGER-style earthquake impact products
9. official tsunami bulletins in a machine-readable authorized feed
10. probabilistic ensemble calibration (CRPS/Brier/reliability)
11. blocked spatial/temporal hindcast validation
12. incident replay / post-event validation
13. model drift detection
14. data provenance, audit logs and incident-command integration

---

## 20. Final principle

The engine is strongest when it says both:

```text
what the current evidence suggests
```

and:

```text
what it does not know reliably enough to decide
```

For disaster management, abstention is a feature, not a failure.

---

## Guarded narration in the unified backend

In the final unified build, this engine's structured output is passed through `engine_output_guard.py` before any user-facing LLM explanation. The guard reduces the payload to domain-authoritative fields, normalizes real-feed status, preserves risk/confidence/precision separately, blocks unsupported operational claims, checks generated numerical claims against the engine payload, and falls back to deterministic engine-grounded wording if validation fails. Engine selection is handled by `vyom_router.py`; an explicit persona always overrides automatic query routing.
