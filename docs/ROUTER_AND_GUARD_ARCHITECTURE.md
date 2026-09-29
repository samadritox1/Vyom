# VYOM Router + Guard Architecture

## Purpose

The router solves **which engine should receive the query**. The narration guard solves **how the engine result can be explained without the LLM inventing or reinterpreting technical facts**.

## Router precedence

1. Explicit persona selected in the UI.
2. For `General Citizen` only, deterministic domain keywords.
3. Ambiguous or tied routing stays on the general-weather path rather than guessing.

Current domains:

- `agro` -> VYOM KRISHI
- `marine` -> VYOM SAGAR
- `aviation` -> VYOM VAYU
- `adventure` -> VYOM ADVENTURE
- `disaster` -> VYOM RAKSHA
- `general` -> normal weather path

To add a future engine, add its persona/domain mapping and conservative keyword rules in `vyom_router.py`, import its engine in `main.py`, and add a compact narration policy in `engine_output_guard.py`.

## Why the LLM does not route everything

The scientific engine selection should not depend entirely on a free-form language-model response. The current router is deterministic and auditable. The LLM is used only as a fallback for extracting difficult route place names after deterministic route parsing fails.

## Narration pipeline

```text
engine output
  -> compact domain payload
  -> normalized source status
  -> official-warning verification state
  -> strict domain prompt
  -> LLM explanation
  -> prohibited-claim validation
  -> unsupported-number validation
  -> deterministic fallback if invalid
```

## Domain protections

### KRISHI

- no invented crop/soil facts
- no confirmed disease or pest diagnosis from weather screening
- no invented pesticide/chemical dose or application timing
- no guaranteed yield
- exact irrigation claims must come from engine-authoritative fields

### SAGAR

- no `safe to sail` or navigation clearance
- no invented UKC/CPA/TCPA/bathymetry/ice/cyclone results
- no helm/course commands
- official maritime warnings cannot be downgraded

### VAYU

- ambient/outside temperature cannot become cabin temperature
- no safe-to-fly/dispatch/takeoff/landing/airworthiness claim
- no invented SIGMET/PIREP/route/flight-level data
- warning-feed failure is not equivalent to “no warnings”

### ADVENTURE

- no route/climb/crossing/avalanche safety certification
- no technical hazardous-maneuver instructions
- no invented snowpack, river, wildfire-perimeter, rescue, or official bulletin information

### RAKSHA

- no invented evacuation order, shelter choice, rescue dispatch, or all-clear
- no earthquake prediction
- no inferred tsunami evacuation from earthquake magnitude alone
- official emergency-management instructions take precedence

## Numeric grounding

The output validator collects all numbers present in the reduced engine payload. Generated narration may repeat those numbers and may express a 0-1 probability as a percentage. New unsupported numbers cause the narration to be rejected and replaced by a deterministic engine-grounded fallback.

## Source status

A source reported as successful with zero records is normalized to `success_none`. Errors, stale feeds, unavailable services, and unconfigured connectors remain distinct. This prevents statements such as “no warnings” when the warning feed simply failed.

## Route/corridor safety

Road routing is reserved for normal road-weather questions. Specialized route queries are kept inside their own domain pipelines. Aviation uses its aviation corridor screen. Marine, Adventure, and Disaster use great-circle environmental corridor sampling and explicitly state that it is not an operational route clearance.
