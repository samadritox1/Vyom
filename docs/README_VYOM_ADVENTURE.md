# VYOM ADVENTURE
## Outdoor Environmental Risk & Route-Exposure Decision Engine

**Engine:** `VYOM-ADVENTURE`  
**Public name:** VYOM ADVENTURE  
**Version:** 1.0.0

VYOM ADVENTURE is the fourth scientific engine in the VYOM platform. It is a conservative environmental decision-support system for hiking, trekking, camping and general outdoor travel. It is **not** a route-clearance, technical-climbing, avalanche, river-crossing, wildfire-escape, rescue or medical system.

The engine follows the same architecture as VYOM KRISHI, VYOM SAGAR and VYOM VAYU:

```text
main_with_agro_marine_aviation_adventure.py
                 |
                 +-- VYOM KRISHI
                 +-- VYOM SAGAR
                 +-- VYOM VAYU
                 +-- VYOM ADVENTURE
                          |
                          +-- vyom_adventure_engine.py
                          +-- adventure_connectors.py
                          +-- adventure_data_fabric.py
                          +-- adventure_realtime_service.py
                          +-- adventure_realtime_worker.py
```

Only the combined FastAPI backend must be started. The scientific files are imported automatically.

---

## 1. Core design contract

The engine never returns `safe_to_proceed = true`.

It returns environmental hazard probabilities, confidence, missing information, official-warning conflicts and conservative safety flags. It intentionally returns:

```json
{
  "safe_to_proceed": null,
  "technical_climbing_clearance": null,
  "river_crossing_clearance": null,
  "avalanche_clearance": null
}
```

Official local authorities, park/land managers, rescue agencies and avalanche/weather services always outrank model-generated convenience guidance.

---

## 2. Main hazard modules

### Multi-model numerical forecast fusion

The engine requests multiple numerical weather models and retains model disagreement. Calibration can be learned separately by:

```text
model × variable × season × lead time
```

The current default numerical members are ECMWF, GFS, ICON and GEM when available.

For a forecast variable `x`, a quality-weighted fused mean is used:

```text
μ = Σ(wᵢ xᵢ) / Σwᵢ
```

and uncertainty contains both inter-model spread and historical calibration error.

### Thermal environment

The engine calculates:

- Stull wet-bulb approximation
- heat-index screening
- wind-chill screening
- solar-adjusted WBGT-like environmental proxy

The WBGT result is explicitly labelled a screening proxy rather than a certified on-site WBGT measurement.

### Altitude exposure

Pressure decreases with altitude and the engine calculates a physical pressure/oxygen-pressure proxy. It does not diagnose altitude illness.

### Lightning / convection

The lightning-weather screen combines:

- thunderstorm weather codes
- CAPE
- precipitation
- wind gusts
- optional lightning-density input

It does not predict the exact time/location of an individual lightning strike.

### Wind exposure

Wind and gust exposure can be adjusted by route context such as sheltered terrain, forest, open terrain, ridge or summit. These are exposure multipliers, not guarantees of local wind at a particular rock face or trail segment.

### Visibility

Visibility risk considers modeled visibility, fog weather codes, humidity and precipitation.

### Surface / trail condition context

A weather-only surface-condition proxy considers rainfall, shallow soil moisture, freezing temperature and fresh snow. It is not a direct trail survey.

### Snow / ice context

The engine uses:

- temperature
- fresh snowfall
- snow depth
- freezing-level height
- route elevation
- wind

It produces a **snow/ice environmental risk** and a separate **snow-instability weather context**. The latter is **not an avalanche forecast**. Avalanche interpretation requires an official local avalanche bulletin and local snowpack/terrain information.

### Flood context

The optional Open-Meteo Flood API connector provides GloFAS river-discharge model context. GloFAS is relatively coarse and is not treated as a local crossing gauge. The engine never produces a river-crossing clearance.

### Wildfire / smoke

NASA FIRMS can provide near-real-time satellite fire detections when a `NASA_FIRMS_MAP_KEY` is configured. Satellite detections indicate fire activity, not evacuation routes or official fire-perimeter safety.

Smoke/air-quality context can also use PM2.5, AQI and dust forecasts.

### Route-terrain summary

When route points include elevation, the engine calculates:

- sampled route distance
- total ascent/descent
- minimum/maximum elevation
- maximum sampled segment slope

These are analytical route descriptors only. They are not a recommendation to attempt a route.

---

## 3. Real data connectors

`adventure_connectors.py` currently contains:

1. **Open-Meteo Air Quality API** — PM2.5, PM10, U.S. AQI, dust and UV context.
2. **Open-Meteo Flood API / GloFAS** — model river-discharge context.
3. **NASA FIRMS** — optional satellite fire detections; requires a free MAP_KEY.
4. **NWS Alerts API** — optional U.S.-only official CAP/JSON-LD warning context.
5. **Configured official warning adapter** — lets a deployment connect its jurisdiction-specific authoritative warning service.
6. **Configured avalanche bulletin adapter** — lets a deployment normalize an actual local avalanche-service bulletin.

Open-Meteo documents global GloFAS river discharge at approximately 5 km resolution and warns that the closest represented river may not always match a requested location. Therefore VYOM treats this as broad hydrologic context rather than a crossing-grade measurement.

NASA FIRMS requires a free MAP_KEY for API use and returns satellite active-fire detections. VYOM treats them as detections rather than evacuation/incident-command information.

The U.S. NWS alerts service distributes watches, warnings and advisories in JSON-LD/CAP formats. It is only enabled when configured for a U.S. deployment/context.

---

## 4. Continuous calibration

`adventure_data_fabric.py` stores:

```text
observations
forecasts
matches
calibration
source_status
model_versions
```

Forecasts can later be matched against verified observations, producing:

```text
bias
MAE
RMSE
reliability
```

Calibration keys are region-aware in the database and are grouped by model, variable, season and lead-time band.

The background worker can periodically collect configured environmental feeds and recompute calibration.

---

## 5. Probabilistic scenarios

The engine perturbs weather variables according to calibrated uncertainty and inter-model disagreement. For each scenario it recomputes environmental hazards, then reports distributions such as:

```json
{
  "median_peak_risk": 0.57,
  "p10": 0.34,
  "p90": 0.81,
  "p_risk_ge_0_70": 0.31,
  "p_risk_ge_0_88": 0.08
}
```

This prevents a low average forecast from hiding dangerous tail scenarios.

---

## 6. Risk is not confidence

A result can be:

```json
{
  "risk": 0.82,
  "confidence": 0.46
}
```

Low confidence does not make high risk safe. It means evidence is incomplete/disagreeing and the response should be more conservative.

---

## 7. Precision levels

The engine can operate at different information levels:

```text
SCREENING_ONLY
WEATHER_AWARE
ROUTE_AWARE
LOCALLY_CONTEXTUALIZED
```

Missing route, terrain, official warning or real-feed information is exposed in `precision.missing_or_unverified` and `precision.abstention_reasons`.

---

## 8. API usage

The combined backend exposes:

```text
POST /adventure/advisory
GET  /adventure/data/health
POST /adventure/data/calibrate
```

Example body:

```json
{
  "lat": 27.5,
  "lon": 88.5,
  "horizon_hours": 72,
  "scenario_count": 120,
  "activity": {
    "activity_type": "hiking",
    "country_code": "IN",
    "terrain_context": {
      "exposure": "open",
      "max_elevation_m": 2800,
      "near_channel_or_gorge": false,
      "snow_terrain": false
    },
    "route_points": [
      {"lat": 27.50, "lon": 88.50, "elevation_m": 1700},
      {"lat": 27.51, "lon": 88.51, "elevation_m": 1950}
    ]
  }
}
```

The output is a structured risk report. It is not an instruction to undertake the activity.

---

## 9. Environment variables

Copy `.env.adventure.example` into the main `.env` and configure only the services you actually use.

Most important variables:

```text
VYOM_ADVENTURE_REAL_FEEDS_ENABLED
VYOM_ADVENTURE_DB_PATH
VYOM_ADVENTURE_DYNAMIC_CALIBRATION
NASA_FIRMS_MAP_KEY
ADVENTURE_OFFICIAL_ALERT_JSON_URL
ADVENTURE_AVALANCHE_JSON_URL
ADVENTURE_ENABLE_NWS
```

Never hard-code credentials into Python source.

---

## 10. Testing

Run:

```bash
python test_vyom_adventure.py
```

Expected result:

```text
VYOM-ADVENTURE offline regression: PASS
```

The regression checks thermal formulas, altitude exposure, convective risk, terrain wind exposure, route summarization, composite hazard analysis, Monte Carlo uncertainty, official-warning handling and the calibration database.

---

## 11. Limitations that are intentional

The engine does not:

- certify that a route is safe;
- provide technical climbing instructions;
- provide avalanche-route selection or snow-slope clearance;
- provide river-crossing instructions;
- provide wildfire escape/evacuation routing;
- replace a park, mountain rescue, avalanche center, weather service or emergency authority;
- diagnose heat illness, hypothermia or altitude illness;
- infer precise local trail condition from a coarse weather model.

Those are not missing features. They are reliability and safety boundaries.

---

## 12. Recommended next scientific upgrades

Future reliability can be improved by connecting locally verified:

- weather-station observations;
- stream/river gauges;
- park/land-manager closures and warnings;
- official avalanche bulletins;
- lightning-network observations where licensed/available;
- high-resolution terrain and route elevation data;
- wildfire incident perimeters and official emergency alerts;
- snow telemetry/SNOTEL-type observations where available;
- local trail or ranger observations.

The correct architecture is always:

```text
verified observations + numerical models + official warnings
                         ↓
                calibrated risk engine
                         ↓
                structured VYOM output
                         ↓
                 LLM explanation only
```

---

## Guarded narration in the unified backend

In the final unified build, this engine's structured output is passed through `engine_output_guard.py` before any user-facing LLM explanation. The guard reduces the payload to domain-authoritative fields, normalizes real-feed status, preserves risk/confidence/precision separately, blocks unsupported operational claims, checks generated numerical claims against the engine payload, and falls back to deterministic engine-grounded wording if validation fails. Engine selection is handled by `vyom_router.py`; an explicit persona always overrides automatic query routing.
