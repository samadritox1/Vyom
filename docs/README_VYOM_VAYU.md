# VYOM-AVIATION
## Probabilistic Aviation Weather & Flight-Operations Screening Engine

**Version:** 1.1.0  
**Purpose:** weather-related aviation decision support and research prototype.  
**Not:** an ATC clearance, dispatch release, airworthiness determination, takeoff/landing clearance, substitute for METAR/TAF/SIGMET/NOTAMs, or replacement for an aircraft flight manual, approved operator procedures, licensed dispatcher, pilot-in-command, ATC, or national aviation authority.

---

## 1. Architecture

```text
User / FastAPI
      |
      v
main.py
      |
      +-- VYOM-AGRO
      +-- VYOM-MARINE
      `-- VYOM-AVIATION
              |
              +-- aviation_realtime_service.py
              |      +-- AWC METAR
              |      +-- AWC TAF
              |      +-- AWC PIREP/AIREP
              |      +-- AWC SIGMET
              |      `-- aviation_data_fabric.py
              |
              +-- multi-model numerical forecast fusion
              +-- surface / terminal analysis
              +-- pressure-level analysis
              +-- aircraft/runway screening
              +-- probabilistic scenarios
              +-- confidence + abstention
              +-- aviation_output_guard.py
              |      +-- reduced narration payload
              |      +-- strict aviation narrator contract
              |      +-- claim/number validation
              |      `-- deterministic fallback
              `-- validated explanation
```

Only the FastAPI application is started in normal deployment. The engine modules are imports.

---

## 2. Files

- `vyom_aviation_engine.py` — mathematical/analytical engine (v1.1).
- `aviation_connectors.py` — machine-to-machine aviation-weather adapters with explicit `success`, `success_none`, `error`, and `degraded` states.
- `aviation_data_fabric.py` — observation/forecast/verification/calibration store.
- `aviation_realtime_service.py` — live METAR/TAF/PIREP/SIGMET context and source-health propagation.
- `aviation_realtime_worker.py` — optional continuous collection/calibration process.
- `aviation_output_guard.py` — strict narration payload, claim validation, numerical grounding, and deterministic fallback.
- `main.py` — unified FastAPI backend.

### v1.1 reliability fix

Aviation answers are no longer generated from the generic `current_data` weather object. The final answer receives only a reduced VYOM VAYU payload. The guard rejects:

- flight/dispatch/takeoff/landing clearance language;
- ambient temperature being renamed as cabin temperature;
- "no SIGMET/no warning" claims unless the feed returned `success_none`;
- unrelated passenger/packing/entertainment advice;
- numerical values that are not present in the validated payload (probability-to-percent rendering is allowed).

If the narrator fails validation, VYOM returns a deterministic engine-grounded response instead.

Aviation route queries now use great-circle environmental screening samples and do not use OSRM road routing or road-segment red/yellow logic. Those screening points are not operational flight-plan or ATC waypoints.

## 3. What the engine calculates

### 3.1 Multi-model forecast fusion

Default numerical models:

- ECMWF IFS
- GFS
- ICON
- GEM

Each model can have variable/season/lead-time calibration:

```text
model × variable × season × lead-time bin
```

A model receives a reliability weight rather than being treated as equally skillful everywhere.

For scalar variable `x`:

```text
x_fused = sum(w_i x_i) / sum(w_i)
```

Inter-model spread is retained as an uncertainty input.

Wind direction is combined using circular statistics rather than ordinary averaging.

---

### 3.2 Standard-atmosphere and density-altitude screening

Pressure altitude approximation:

```text
PA ≈ field elevation + (1013.25 - QNH_hPa) × 27 ft
```

ISA temperature in the troposphere:

```text
T_ISA ≈ 15 - 0.0065 h_m
```

Density-altitude screening approximation:

```text
DA ≈ PA + 120 × (OAT - T_ISA)
```

The engine deliberately does **not** calculate certified takeoff or landing distance without approved AFM/POH/operator performance data.

---

### 3.3 Runway wind geometry

Let `alpha = wind_from_direction - runway_heading`.

```text
headwind  = V cos(alpha)
crosswind = |V sin(alpha)|
```

Gust crosswind is also computed. If an actual aircraft/operator crosswind or tailwind limit is supplied, the engine can flag an exceedance. If no limit is supplied, no aircraft-specific clearance is inferred.

---

### 3.4 Ceiling and visibility

Observed METAR ceiling is preferred.

When no observed ceiling exists, a convective-LCL approximation can be calculated from temperature-dew-point spread:

```text
cloud-base proxy ≈ 410 × (T - Td) ft AGL
```

It is explicitly tagged as an estimate and not treated as equivalent to an observed BKN/OVC/VV ceiling.

Common flight categories are reported as a display/screening aid:

- VFR
- MVFR
- IFR
- LIFR

Actual operational minima remain procedure/operator/authority dependent.

---

### 3.5 Low-level wind shear

Meteorological wind vectors are converted to Cartesian components. For two levels:

```text
Delta V = |V2_vector - V1_vector|
shear   = Delta V / Delta z
```

The engine uses available low-level pressure-level wind profiles to screen LLWS potential. Airport LLWS/microburst warning systems and current ATC/aerodrome reports supersede this model.

---

### 3.6 Gradient Richardson number

Where temperature, geopotential height and winds at adjacent pressure levels are available:

```text
Ri = (g/theta) (dtheta/dz) /
     [(du/dz)^2 + (dv/dz)^2]
```

Lower/negative values increase the turbulence screening signal. This is only one turbulence diagnostic.

---

### 3.7 Turbulence screening

The turbulence module combines:

- gradient Richardson number;
- vertical wind shear;
- upper-level wind strength / jet environment;
- convection;
- PIREP/AIREP context when available.

EDR is **not fabricated**. If a real/model EDR field is not supplied, `edr` remains null and the output is explicitly marked as a screening proxy.

The code includes a hook for an Ellrod-style diagnostic when horizontal deformation/convergence from a surrounding grid is available.

---

### 3.8 Icing-conduciveness screening

Pressure layers are examined for overlapping:

- subfreezing/supercooled temperature ranges;
- high relative humidity;
- precipitation/moisture;
- freezing-level context;
- PIREP icing reports;
- official warning context.

The output is meteorological icing **conduciveness**, not an airframe icing certification or clearance.

---

### 3.9 Convective hazard screening

The model can use:

- CAPE;
- CIN;
- Lifted Index;
- precipitation intensity;
- gusts;
- thunderstorm weather codes;
- SIGMET context.

An authoritative convective SIGMET cannot be downgraded by a lower model score.

---

### 3.10 Microburst / downdraft proxy

When DCAPE or dedicated airport detection is unavailable, VYOM computes a deliberately low-confidence proxy from instability, rainfall, gust behavior and thermodynamic spread. It is always labelled as a proxy.

---

### 3.11 Mountain-wave screening

When terrain relief and ridge orientation are supplied, VYOM estimates the wind component normal to the ridge and combines it with stability diagnostics.

Without terrain geometry, the module abstains.

---

### 3.12 Runway contamination weather proxy

Temperature, precipitation and snowfall can identify weather conducive to wet/snow/ice runway conditions.

Airport runway-condition reports and NOTAMs remain authoritative; VYOM does not invent runway braking action.

---

### 3.13 Route exposure

When route weather points are supplied, each point is analyzed with the same hazard engine and a maximum/mean route exposure is returned.

`route_clearance` is always null.

---

### 3.14 Monte-Carlo uncertainty

Inter-model spread is propagated into multiple plausible future weather states. Each scenario is re-evaluated by the hazard engine.

Outputs include:

- median risk;
- P10 / P90;
- probability risk exceeds 0.65;
- probability risk exceeds 0.85.

This prevents a benign ensemble mean from hiding dangerous members.

---

## 4. Real observational and authoritative data

The real-time connector uses NOAA/NWS Aviation Weather Center's machine-to-machine Data API when an ICAO aerodrome identifier is provided.

Supported adapters:

- METAR — terminal observation;
- TAF — terminal forecast;
- PIREP/AIREP — actual in-flight reports;
- SIGMET — significant aviation weather warnings;
- station/airport metadata.

Feed failures are treated as missing data, **not** as evidence that no hazard exists.

---

## 5. Continuous calibration

`aviation_data_fabric.py` stores:

```text
observations
forecasts
forecast-observation matches
calibration cells
source health
```

Forecast verification dimensions are designed around:

```text
model × variable × region × season × lead time
```

This is the correct place to calibrate the atmosphere. Aircraft class is not used to change whether a weather model predicted temperature/wind correctly. Aircraft class belongs in the separate consequence/risk layer.

Initial continuously verifiable terminal variables include:

- temperature;
- visibility;
- surface wind speed;
- pressure/altimeter context.

Statistics include bias, MAE and RMSE. The schema can be extended to CRPS/Brier/reliability when genuine ensemble probabilities are stored.

---

## 6. Risk vs confidence

The output deliberately keeps:

```text
risk != confidence != data quality
```

A high modeled risk with weak observations remains a high risk with lower confidence; uncertainty does not make the hazard safe.

---

## 7. Precision levels

Possible precision states include:

```text
WEATHER_ONLY
AIRCRAFT_AWARE
OPERATION_AWARE
CONTEXT_RICH
AUTHORITY_CROSSCHECKED
```

Missing aircraft limits, runway geometry, METAR/TAF context or multi-model agreement lower precision rather than being silently replaced with invented values.

---

## 8. FastAPI request

Standalone endpoint:

```text
POST /aviation/advisory
```

Example body:

```json
{
  "lat": 22.6547,
  "lon": 88.4467,
  "horizon_hours": 48,
  "scenario_count": 120,
  "aircraft": {
    "aircraft_class": "transport_jet",
    "aircraft_type_icao": "A320",
    "departure_icao": "VECC",
    "arrival_icao": "VABB",
    "heading_deg": 190,
    "true_airspeed_kt": 450,
    "cruise_altitude_ft": 35000,
    "airport_elevation_ft": 20,
    "max_crosswind_kt": 30,
    "max_tailwind_kt": 10,
    "icing_approved": true,
    "runways": [
      {"id": "19L", "heading_deg": 190},
      {"id": "01R", "heading_deg": 10}
    ]
  }
}
```

The crosswind/tailwind values above are **examples of caller-supplied data**, not universal A320 limits. Replace them with approved/operator data if you use that feature.

---

## 9. Output safety contract

The engine always returns:

```json
{
  "dispatch_clearance": null,
  "takeoff_landing_clearance": null,
  "airworthiness_determination": null
}
```

This is intentional.

---

## 10. Running the combined project

Keep these files beside the combined main backend:

```text
main.py
vyom_agro_engine.py
vyom_marine_engine.py
marine_realtime_service.py
marine_data_fabric.py
marine_connectors.py
vyom_aviation_engine.py
aviation_realtime_service.py
aviation_data_fabric.py
aviation_connectors.py
```

Then run:

```bash
uvicorn main:app --reload
```

Or rename `main.py` to `main.py` once you have backed up your current main file.

---

## 11. Continuous calibration worker

Optional:

```bash
python aviation_realtime_worker.py
```

This is useful in a production architecture but is not required for a single advisory request.

---

## 12. Tests

Run:

```bash
python test_vyom_aviation.py
```

The test is offline; it monkey-patches synthetic forecast members and verifies core mathematics plus the mandatory no-clearance contract.

---

## 13. Important future upgrades

The highest-value next upgrades are not random extra equations. They are better authoritative data and validation:

1. WAFS turbulence/icing/SIGWX grids through an authorized operational source.
2. Explicit EDR field and calibration.
3. Airport LLWS/microburst sensors where accessible.
4. Global radar/lightning feeds where licensing permits.
5. NOTAM/airport runway-condition integration from an authorized source.
6. Real route sampling at flight level, not only terminal point forecasts.
7. Aircraft-specific validated response/performance tables where legally and operationally appropriate.
8. Ensemble probability verification using CRPS/Brier/reliability diagrams.
9. Spatially blocked and seasonally blocked backtests.
10. Provenance/version logging for every operational source.

---

## 14. Design principle

VYOM-AVIATION is strongest when it says both:

> what the atmosphere is likely to do

and

> what it does **not** know well enough to decide.

That is why official warnings, current observations, approved aircraft data, ATC, dispatch and the pilot/operator remain above model-generated convenience guidance.

---

## Unified router integration

`vyom_router.py` now selects VYOM VAYU automatically for strong aviation queries when the user has not explicitly selected another specialized persona. Aviation keeps its dedicated `aviation_output_guard.py` because its terminology and clearance constraints require additional aviation-specific checks. Aviation corridor questions bypass the road router and use dedicated great-circle environmental screening.
