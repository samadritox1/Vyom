# VYOM-AGRO v2.0

## Advanced probabilistic crop–soil–weather decision engine for VYOM

VYOM-AGRO is the agriculture decision layer used by the VYOM weather application. It is intentionally separated from the language model.

The architecture is:

```text
Weather models / field observations / crop data
                    │
                    ▼
      Forecast calibration + uncertainty
                    │
                    ▼
         Crop / soil state estimator
                    │
                    ▼
   Water + stress + disease/pest models
                    │
                    ▼
    Probabilistic impact calculations
                    │
                    ▼
         Robust decision optimizer
                    │
                    ▼
       Structured VYOM-AGRO JSON
                    │
                    ▼
        NLP/LLM explanation layer
```

The LLM is not allowed to invent the agronomy. It may explain the structured output, translate it, summarize it, and communicate uncertainty.

---

# 1. Files

The package contains the following important files:

```text
main_with_agro.py
vyom_agro_engine.py
agro_calibration.example.json
.env.example
test_vyom_agro.py
README_VYOM_AGRO.md
```

### `main_with_agro.py`

This is the FastAPI application layer.

It handles:

- API requests
- authentication/user context already present in VYOM
- MongoDB
- state persistence
- weather/persona routing
- calling `build_agro_advisory()`
- returning the result to the frontend
- passing structured output to the NLP layer

It should **not** contain thousands of lines of agricultural mathematics.

### `vyom_agro_engine.py`

This is the mathematical/agronomic engine.

It now contains roughly 2,700+ lines because the advanced agricultural models belong here rather than inside `main.py`.

The intended relationship is:

```python
from vyom_agro_engine import build_agro_advisory
```

and then:

```python
result = build_agro_advisory(
    lat,
    lon,
    field_config,
    horizon_hours,
    scenario_count,
)
```

Keep the engine file beside the main application file.

---

# 2. Current implementation status

The important distinction is:

> **Implemented** means the mathematical/software framework exists and runs.
>
> It does **not** mean every coefficient has been locally calibrated for every crop, cultivar, soil and district.

| # | Module | v2 status | Important qualification |
|---|---|---|---|
| 1 | Multi-model forecast fusion | ✅ Implemented | Local verification improves weights |
| 2 | Persistent field-state estimation | ✅ Implemented | Mongo persistence is optional and requires user + field IDs |
| 3 | Crop phenology + GDD | ✅ Implemented | Cultivar calibration still improves stage timing |
| 4 | FAO-style root-zone water balance | ✅ Implemented | Best with measured FC/WP/root depth |
| 5 | Effective rainfall/runoff/infiltration | ✅ Implemented | Hybrid field-scale model; not watershed routing |
| 6 | Probabilistic irrigation + CVaR | ✅ Implemented | Requires field-state quality for exact volumes |
| 7 | VPD + heat/cold/water stress | ✅ Implemented | Canopy temperature remains a bounded proxy unless measured |
| 8 | Waterlogging model | ✅ Implemented | Explicit excess-water bucket + drainage + aeration stress |
| 9 | Disease-conducive weather | ✅ Implemented | Weather screening only, never diagnosis |
| 10 | Pest degree-day development | ✅ Implemented framework | Current built-in pest profile is limited; add local profiles |
| 11 | Crop-stage impact matrix | ✅ Implemented | Starter sensitivity coefficients require field validation |
| 12 | Biomass/yield model | ✅ Implemented framework | Forecast-horizon model; absolute yield gated by calibration |
| 13 | Day-of-year climate anomaly | ✅ Implemented | Uses seasonal day-of-year window instead of annual mean |
| 14 | Full farm-action optimization | ✅ Robust portfolio implemented | True economic optimum needs farm-specific costs/constraints |
| 15 | Risk ≠ confidence framework | ✅ Implemented | Separate outputs by design |
| 16 | Precision/abstention system | ✅ Implemented | Exact-looking outputs are suppressed when evidence is weak |

---

# 3. What changed from VYOM-AGRO v1

The v1 engine already had a useful core, but several components were intentionally conservative or incomplete.

V2 adds:

- variable-specific forecast model weights
- forecast-lead-specific verification weights
- seasonal verification overrides
- CRPS/reliability/sample-size hooks
- coupled weather scenarios instead of independent variable mixing
- persistent field state
- data assimilation between prior state and current root-zone observations
- explicit water stored above field capacity
- hourly gravitational drainage
- aeration/waterlogging stress
- event-based NRCS Curve Number runoff screening
- infiltration-capacity runoff
- VPD stress
- bounded canopy-temperature screening
- pathogen-specific weather-conduciveness profiles
- pest degree-day development
- crop-stage hazard sensitivity matrices
- AquaCrop/FAO-inspired production screening
- robust farm-action portfolio
- seasonal day-of-year climate anomalies
- stronger precision governance
- stronger LLM contract

---

# 4. Forecast fusion

The engine fetches independent forecast members such as:

```text
ECMWF IFS
GFS
ICON
UKMO
```

The engine does not automatically assume they are equally accurate.

For each model, variable, lead-time bucket and season, the calibration store can contain:

```text
bias
multiplier
RMSE
CRPS
reliability
sample size
```

The lead-time buckets currently supported are:

```text
0–24 h
25–72 h
73–168 h
```

Season groups are:

```text
monsoon
winter
summer
post_monsoon
```

Model weight is derived from forecast error, probabilistic skill, reliability, evidence size and completeness.

The engine can therefore trust one model more for precipitation while trusting another model more for temperature.

---

# 5. Scenario coupling

A major design rule is:

> Do not randomly combine temperature from one model with precipitation from another and humidity from a third as if the combination were one physically consistent atmosphere.

Instead, a probabilistic scenario begins with one complete model member.

Residual perturbations are then added using calibrated forecast-error distributions.

This keeps relationships among:

```text
temperature
humidity
precipitation
wind
ET0
```

more physically coherent.

The engine reports this in:

```json
"scenario_coupling": "whole-model member preserved before residual perturbation"
```

---

# 6. Persistent field state

Agriculture has memory.

Today's field condition is not determined only by today's forecast.

VYOM-AGRO stores a field-state checkpoint containing:

```json
{
  "timestamp": "...",
  "depletion_mm": 55.2,
  "surface_depletion_mm": 10.1,
  "excess_water_mm": 0.0,
  "cumulative_gdd": 650.4,
  "root_depth_m": 0.72
}
```

`main_with_agro.py` can persist this checkpoint in MongoDB collection:

```text
agro_field_states
```

State persistence requires:

```text
clerk_id
field_id
MongoDB
```

A new soil-water measurement remains more important than an old stored state.

When both are available, the engine performs a confidence-weighted assimilation rather than blindly replacing one with the other.

Old state confidence decays with age.

---

# 7. Crop phenology

VYOM-AGRO tracks crop development using thermal time.

A simplified daily Growing Degree Day relation is:

```text
GDD = max(0, mean(capped Tmin, capped Tmax) - Tbase)
```

The hourly forecast path accumulates thermal time continuously.

Crop stage is derived from maturity-GDD fractions.

Supported starter profiles currently include:

```text
generic
wheat
maize
rice
potato
tomato
```

The response also provides projected crop-stage transitions during the forecast horizon.

Example:

```json
"projected_stage_transitions": [
  {
    "hour_from_now": 54,
    "from": "development",
    "to": "mid"
  }
]
```

---

# 8. Root-zone soil water balance

The engine follows FAO-style crop-water concepts.

Total Available Water:

```text
TAW = 1000 × (thetaFC − thetaWP) × root_depth
```

Readily Available Water:

```text
RAW = p × TAW
```

Water-stress coefficient:

```text
Ks = 1                  when depletion <= RAW
Ks < 1                  when depletion > RAW
```

Crop evapotranspiration follows the dual-coefficient structure:

```text
ETc = (Ks × Kcb + Ke) × ET0
```

The engine separately tracks:

```text
crop transpiration
potential crop transpiration
soil evaporation
root-zone depletion
surface evaporation depletion
above-field-capacity excess water
runoff
deep percolation
```

This is fundamentally different from the old prototype relation:

```text
irrigation = ET0 - rain
```

V2 does not use that shortcut.

---

# 9. Root-zone excess water and waterlogging

V1 could detect wet soil, but it did not explicitly maintain water above field capacity.

V2 introduces:

```text
excess_water_mm
```

Excess infiltrated water is stored between field capacity and saturation.

Each hour:

1. root-zone depletion is refilled;
2. surplus fills the excess-water bucket;
3. drainage removes water according to soil drainage capacity;
4. overflow becomes deep percolation;
5. prolonged excess water contributes to aeration stress.

The engine now reports:

```text
waterlogging probability
severe waterlogging probability
aeration stress hours
final excess water
```

Rice receives a different stage sensitivity than crops that are less tolerant of saturated conditions.

---

# 10. Runoff and infiltration

The v2 hydrology layer combines two concepts.

## 10.1 Infiltration-capacity limit

Infiltration capacity changes with:

```text
soil texture
root-zone wetness
slope
```

## 10.2 Event Curve Number runoff

The engine maintains cumulative rainfall for an event and calculates the incremental runoff response using an NRCS-style Curve Number relation.

The base CN can be supplied explicitly:

```json
"curve_number": 72
```

If it is absent, a starter value from the soil profile is used and the engine reports that field-specific hydrology could improve precision.

This is a **field-scale screening model**.

It is not intended to replace watershed routing software.

---

# 11. Probabilistic irrigation optimizer

The irrigation system evaluates many possible futures instead of relying on one deterministic forecast.

Candidate decisions include:

```text
no irrigation
multiple irrigation depths
multiple starting times
```

Every candidate is simulated across forecast scenarios.

The decision objective is:

```text
robust objective = expected loss + 0.45 × CVaR80(loss)
```

CVaR penalizes poor tail outcomes.

This means an irrigation plan that performs well only when the forecast mean is exactly correct may lose to a slightly more conservative plan that remains safer when the forecast is wrong.

The objective considers:

```text
water stress
waterlogging/aeration stress
runoff
deep percolation
applied irrigation
canopy heat interaction
VPD stress
```

The returned irrigation result includes:

```text
recommended / not recommended
best gross depth
equivalent near-optimal depth range
starting time
robust benefit score
post-action waterlogging probability
post-action expected stress
runoff
deep percolation
```

---

# 12. Exact irrigation-volume governance

VYOM-AGRO deliberately distinguishes a planning estimate from an authoritative field-volume estimate.

Possible precision modes include:

```text
weather_only
crop_aware
crop_stage_aware
stateful_field_balance
field_balance
sensor_corrected_field_balance
```

When field evidence is insufficient:

```json
"liters_per_acre_authoritative": null
```

The engine still returns a planning depth/range but explicitly suppresses fake precision.

---

# 13. VPD and thermal stress

Vapour Pressure Deficit is derived from temperature and humidity.

The engine uses VPD as a measure of atmospheric drying demand.

V2 tracks:

```text
mean VPD
high-VPD stress hours
heat dose
cold dose
canopy heat dose
```

A bounded canopy-temperature proxy responds to:

```text
solar radiation
water-stress coefficient
VPD
wind cooling
```

It is explicitly labelled a proxy and should be replaced or corrected when real infrared canopy observations are available.

---

# 14. Crop-stage hazard matrix

A hazard does not have the same impact at every crop stage.

The engine therefore applies stage-specific multipliers to:

```text
heat
water stress
waterlogging
cold
lodging
disease-conducive weather
```

Crop-specific starter matrices exist for:

```text
wheat
maize
rice
potato
tomato
```

These coefficients are intended to be calibrated with agronomic trials, extension recommendations or local historical outcomes.

---

# 15. Disease-conducive weather

The engine now contains pathogen-specific weather-screening profiles for selected crop/disease combinations.

Current starter profiles include:

```text
potato late blight
tomato late blight
rice blast
wheat stripe rust
maize foliar blight
```

Inputs include:

```text
temperature
relative humidity
estimated leaf wetness
rainfall
continuous conducive duration
crop stage
```

The output is intentionally called:

```text
weather conduciveness
```

not:

```text
infection probability
confirmed disease
```

Disease requires a pathogen, a susceptible host and suitable conditions. Weather alone cannot prove that infection exists.

No chemical product, dose, mixing instruction or chemical-application timing is generated by this engine.

---

# 16. Pest degree-day development

The pest module supports temperature-driven pest phenology.

A `PestProfile` defines:

```text
base temperature
upper threshold
degree-days per generation
crop-stage sensitivity
```

The current built-in starter profile includes:

```text
fall_armyworm
```

for maize thermal-development screening.

The response can report:

```text
degree-days at start
degree-days added during forecast
degree-days at end
generation fraction
25/50/75/100% thermal milestones
thermal activity index
```

This does not claim the pest is present in the field.

It identifies a temperature-driven scouting window only.

Add locally validated pest profiles before expanding this to other insects.

---

# 17. Biomass and yield framework

The v2 production layer uses two complementary ideas.

## 17.1 Transpiration / water-stress yield response

The forecast-horizon water-response factor is derived from actual versus potential crop transpiration.

## 17.2 AquaCrop-inspired normalized transpiration

When a calibrated normalized biomass water-productivity parameter is provided:

```text
biomass increment ~ WP* × sum(Tr / ET0)
```

The engine reports:

```text
relative yield factor
P10–P90 relative factor
normalized biomass increment index
optional biomass increment
```

Absolute yield in tonnes/hectare is only returned as authoritative when:

```json
"potential_yield_t_ha": 8.0,
"yield_parameters_calibrated": true
```

Otherwise, an exact yield figure is suppressed or explicitly labelled planning-only.

This is still a **forecast-horizon production model**, not a complete replacement for a fully calibrated season-long AquaCrop/DSSAT/APSIM simulation.

---

# 18. Seasonal climate anomaly

The old implementation compared today's temperature with an average across all days in many historical years.

That mixes winter and summer and produces a poor climatological reference.

V2 uses a day-of-year window:

```text
target date ± 15 days
across previous historical years
```

It calculates climatological distributions for:

```text
daily mean temperature
daily precipitation
ET0
```

and reports:

```text
absolute anomaly
standardized z-score
P10/P90 historical values
```

The same day-of-year logic is now also used by the main backend's climate-anomaly field.

---

# 19. Farm action portfolio

V2 combines the separate risk engines into a ranked decision portfolio.

Possible action classes include:

```text
irrigation
drainage/waterlogging inspection
protect heat-sensitive operations
crop disease scouting
pest scouting
avoid unnecessary field traffic
```

Actions are ranked from modelled risk and robust decision benefit.

The engine does **not** automatically execute actions.

It also does not claim to know the farm's economics unless the farm supplies cost/value constraints.

Therefore the current system is a robust decision-priority optimizer, while a full profit-maximizing farm optimizer would require farm-specific economics.

---

# 20. Field-operation windows

V2 includes non-chemical weather screening for:

```text
field trafficability
harvest-weather windows
sowing-weather windows
```

These are screening outputs.

For example, harvest weather does not prove crop harvest maturity, because grain/tuber/fruit moisture and physical maturity are separate observations.

---

# 21. Risk and confidence are separate

This is a hard design rule.

The engine never lowers a danger score merely because the forecast is uncertain.

Example:

```json
{
  "waterlogging": {
    "probability": 0.74,
    "level": "High"
  },
  "confidence": {
    "overall": 0.51
  }
}
```

This means:

> The modeled risk is high, but evidence confidence is only moderate.

It does **not** mean the risk should be multiplied by 0.51.

---

# 22. Confidence model

Overall confidence combines:

```text
multi-model agreement
forecast completeness
local verification quality
field-water-state quality
crop/stage quality
soil-profile quality
```

A geometric mean is used so that one weak component meaningfully reduces confidence without forcing it to zero.

---

# 23. Precision and abstention

The engine includes explicit abstention rules.

It can suppress:

```text
exact irrigation volume
absolute yield
strong crop-stage conclusions
high-confidence soil-water claims
```

when supporting data is insufficient.

This is intentional.

A system that always returns a precise number is usually less trustworthy than one that knows when precision is not justified.

---

# 24. API usage

## 24.1 Standalone agriculture endpoint

```http
POST /agro/advisory
```

Example JSON:

```json
{
  "lat": 22.57,
  "lon": 88.36,
  "clerk_id": "user_123",
  "persist_state": true,
  "horizon_hours": 120,
  "scenario_count": 64,
  "field": {
    "field_id": "farm-field-01",
    "crop": "maize",
    "variety": "local_hybrid",
    "planting_date": "2026-07-15",
    "crop_stage": "flowering",
    "soil_type": "loam",
    "field_area_acres": 2.0,
    "root_zone_vwc": 0.21,
    "root_zone_sensor": true,
    "irrigation_efficiency": 0.82,
    "curve_number": 72,
    "slope_percent": 1.5,
    "pest": "fall_armyworm"
  }
}
```

---

# 25. Farmer mode inside `/weather`

The existing weather endpoint can use exactly the same engine.

Example:

```json
{
  "query": "What should I know for my field tomorrow?",
  "lat": 22.57,
  "lon": 88.36,
  "persona": "Farmer",
  "language": "English",
  "clerk_id": "user_123",
  "agro": {
    "field_id": "farm-field-01",
    "crop": "maize",
    "planting_date": "2026-07-15",
    "soil_type": "loam",
    "root_zone_vwc": 0.21,
    "root_zone_sensor": true
  }
}
```

The result is inserted into:

```text
telemetry.agro
```

The LLM receives that structured result.

---

# 26. Persistent-state behavior

When both are supplied:

```text
clerk_id
field.field_id
```

`main_with_agro.py` loads the prior checkpoint before running the engine.

After a successful run it saves the new checkpoint.

The engine may report state source such as:

```text
persistent_field_state
persistent_state_plus_root_zone_observation
persistent_state_plus_user_depletion_fraction
```

A field sensor observation can override/recalibrate older persisted state.

---

# 27. Calibration file

Copy:

```text
agro_calibration.example.json
```

to something like:

```text
agro_calibration.json
```

and set:

```env
VYOM_AGRO_CALIBRATION_PATH=agro_calibration.json
```

The engine accepts both the old simple calibration structure and the new nested v2 structure.

---

# 28. Building the verification database

The most important production upgrade is not adding more equations.

It is collecting forecast-vs-observation pairs.

For every location/field, store approximately:

```text
forecast issue time
valid time
model
lead time
forecast temperature
observed temperature
forecast rainfall
observed rainfall
forecast humidity
observed humidity
forecast wind
observed wind
forecast ET0 / calculated observed ET0
season
```

Then calculate rolling:

```text
bias
RMSE
MAE
CRPS where probabilistic observations exist
Brier/reliability metrics for threshold events
sample size
```

Populate the calibration JSON/database from those statistics.

That is how the engine becomes locally skill-weighted instead of merely multi-model.

---

# 29. Recommended field data hierarchy

From weakest to strongest:

```text
weather coordinates only
↓
crop identity
↓
planting date / verified stage
↓
soil texture
↓
measured field capacity / wilting point
↓
root depth
↓
root-zone moisture sensor
↓
rain gauge / field weather station
↓
canopy temperature / leaf wetness
↓
local historical crop outcomes
```

The engine's precision should rise as you move downward through this hierarchy.

---

# 30. MongoDB field-state collection

The backend uses:

```text
weathergpt.agro_field_states
```

A document is keyed by:

```text
clerk_id
field_id
```

and contains approximately:

```json
{
  "clerk_id": "user_123",
  "field_id": "farm-field-01",
  "state": {
    "timestamp": "...",
    "depletion_mm": 42.1,
    "surface_depletion_mm": 8.2,
    "excess_water_mm": 0.0,
    "cumulative_gdd": 811.2,
    "root_depth_m": 0.81
  }
}
```

---

# 31. Environment variables

Use `.env.example` as a template.

Important variables:

```env
MONGO_URI=
GROQ_API_KEY=
MISTRAL_API_KEY=
SMTP_EMAIL=
SMTP_APP_PASSWORD=
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
VYOM_AGRO_CALIBRATION_PATH=agro_calibration.json
```

Do not commit real credentials to GitHub.

The upgraded main backend no longer contains the SMTP app credential that existed in the earlier prototype.

Rotate any credential that was previously committed or shared.

---

# 32. Running the backend

If your dependencies are installed:

```bash
uvicorn main_with_agro:app --host 0.0.0.0 --port 8000 --reload
```

If you want the upgraded file to become the real application file, rename/copy it after you have backed up the original:

```text
main_with_agro.py -> main.py
```

Keep:

```text
vyom_agro_engine.py
```

in the same import path.

---

# 33. Running the offline regression test

Run:

```bash
python test_vyom_agro.py
```

The v2 test verifies:

- multi-model synthetic input
- advanced calibration structure
- lead-time-dependent weighting
- irrigation optimization
- disease-weather screening
- pest degree-day development
- biomass/yield gating
- operation windows
- action portfolio
- state checkpoint
- persistent-state assimilation

Expected final line:

```text
VYOM-AGRO v2 offline regression: PASS
```

---

# 34. LLM contract

The engine returns a strict contract for the NLP layer.

The NLP layer may:

```text
translate
summarize
explain risk
explain confidence
explain returned recommendations
state missing information
```

It may not:

```text
recalculate ET0 - rain as irrigation
invent crop stage
invent soil parameters
invent pest presence
call weather conduciveness a confirmed disease
turn a low-confidence estimate into an exact quantity
invent an absolute yield when the engine suppressed it
```

---

# 35. Why disease and pest outputs are deliberately conservative

Weather models cannot see whether a pathogen or insect is actually present in a field.

Therefore VYOM-AGRO distinguishes:

```text
weather suitability / thermal development
```

from:

```text
confirmed biological presence
```

Confirmation requires field scouting, traps, imagery, laboratory diagnosis or another direct observation source.

---

# 36. Why some values are starter defaults

A universal coefficient for every farm would be scientifically misleading.

Parameters that can vary materially include:

```text
cultivar heat tolerance
root depth
field capacity
wilting point
hydraulic conductivity
runoff Curve Number
irrigation efficiency
crop water productivity
harvest index
pest thermal constants
pathogen response functions
```

The built-in profiles make the system runnable.

The calibration flags tell the engine whether those values are locally verified.

---

# 37. Recommended next scientific improvements

V2 is considerably more advanced, but future work should prioritize observed data rather than adding arbitrary complexity.

Recommended order:

1. build district/field forecast-verification history;
2. connect real root-zone soil-moisture sensors;
3. connect field rain gauges;
4. add cultivar-specific crop profiles;
5. add measured field-capacity/wilting-point values;
6. add locally validated disease profiles;
7. add locally validated pest phenology profiles;
8. calibrate runoff/infiltration against actual fields;
9. calibrate yield against harvested outcomes;
10. add satellite vegetation observations;
11. add measured canopy temperature;
12. add measured leaf wetness;
13. add economic costs/benefits for true farm-profit optimization.

---

# 38. Satellite/data-assimilation direction

A future VYOM-AGRO state estimator can assimilate:

```text
NDVI / EVI
LAI
surface soil moisture
land-surface temperature
crop maps
canopy cover
field boundaries
```

with sensor observations and NWP fields.

The existing persistent-state architecture is designed so those observations can later update the internal field state instead of requiring a total rewrite.

---

# 39. Important distinction: model sophistication vs accuracy

A 3,000-line model is not automatically more accurate than a 300-line model.

Operational accuracy comes from:

```text
correct equations
correct inputs
local calibration
forecast verification
field observations
uncertainty propagation
validation on unseen seasons
abstaining when information is insufficient
```

V2 improves the equation/architecture side.

The next major performance gains should come from calibration and validation data.

---

# 40. Validation protocol before real deployment

For each crop-region combination, perform retrospective validation.

Split historical data by season rather than randomly mixing days.

For example:

```text
training/calibration seasons
validation season A
validation season B
```

Evaluate:

### Weather layer

```text
bias
MAE
RMSE
CRPS
Brier score
reliability diagrams
```

### Soil-water layer

```text
soil moisture error
root-zone depletion error
irrigation-event timing error
runoff/deep-percolation plausibility
```

### Impact layer

```text
water-stress event detection
waterlogging-event detection
heat-event detection
disease-conducive-period detection
```

### Advisory layer

```text
false alarm rate
miss rate
lead time
calibration of reported probabilities
water saved
yield protected
farmer acceptance
```

---

# 41. Reliability targets

Do not choose a probability threshold merely because it looks good in one dataset.

Thresholds should be selected from validation results according to the cost of misses versus false alarms.

For example, a low-cost scouting alert may tolerate more false alarms than a costly field intervention.

The action layer should eventually allow different loss functions for different farms.

---

# 42. Scientific references behind the architecture

The engine design is informed by established concepts including:

- FAO-56 crop evapotranspiration and soil-water stress
- FAO AquaCrop water-driven crop production concepts
- FAO crop yield response to water
- NRCS Curve Number runoff concepts
- growing-degree-day crop/pest phenology
- leaf-wetness / temperature / humidity disease-weather screening
- probabilistic forecast calibration and ensemble verification

Useful official/background references include:

- FAO-56, Crop evapotranspiration: https://www.fao.org/4/x0490e/x0490e00.htm
- FAO-56 soil-water stress chapter: https://www.fao.org/4/x0490e/x0490e0e.htm
- FAO AquaCrop: https://www.fao.org/aquacrop
- FAO crop yield response to water: https://www.fao.org/4/i2800e/i2800e.pdf
- USDA NRCS National Engineering Handbook: https://directives.nrcs.usda.gov/
- UC IPM rice blast background: https://ipm.ucanr.edu/agriculture/rice/rice-blast/
- UC IPM potato late blight models: https://ipm.ucanr.edu/DISEASE/DATABASE/potatolateblight.html

These references are not a substitute for local agricultural validation.

---

# 43. Production checklist

Before deploying VYOM-AGRO as a serious advisory system:

- [ ] Rotate any credentials previously hard-coded in source files.
- [ ] Use `.env` / secret management.
- [ ] Configure MongoDB securely.
- [ ] Set `VYOM_AGRO_CALIBRATION_PATH`.
- [ ] Build a local verification dataset.
- [ ] Validate crop parameters.
- [ ] Validate soil parameters.
- [ ] Validate irrigation efficiencies.
- [ ] Validate disease-weather profiles locally.
- [ ] Validate pest thermal profiles locally.
- [ ] Add monitoring/telemetry for API failures.
- [ ] Add unit tests to CI.
- [ ] Add replay tests against historical seasons.
- [ ] Calibrate reported probabilities.
- [ ] Keep the LLM downstream of the deterministic/probabilistic engine.
- [ ] Preserve abstention rules.

---

# 44. Architecture recommendation

Do not merge the 2,700+ lines of agricultural modeling into `main.py`.

Keep the system modular:

```text
main.py
│
├── vyom_agro_engine.py
│
├── future marine engine
│
├── future aviation engine
│
└── future disaster engine
```

The FastAPI backend should coordinate services.

The scientific modules should perform scientific calculations.

That separation will make testing, calibration and later SIH/project development much easier.

---

# 45. Final principle

The design target is not:

> "Produce the most precise-looking number."

The design target is:

> **Produce the most defensible decision possible from the available evidence, quantify uncertainty, and refuse false precision when the evidence is insufficient.**

That principle should remain the core of VYOM-AGRO as it grows.

---

## Guarded narration in the unified backend

In the final unified build, this engine's structured output is passed through `engine_output_guard.py` before any user-facing LLM explanation. The guard reduces the payload to domain-authoritative fields, normalizes real-feed status, preserves risk/confidence/precision separately, blocks unsupported operational claims, checks generated numerical claims against the engine payload, and falls back to deterministic engine-grounded wording if validation fails. Engine selection is handled by `vyom_router.py`; an explicit persona always overrides automatic query routing.
