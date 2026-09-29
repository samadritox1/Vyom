# VYOM — Final Unified Multi-Domain Backend

This package contains the final unified VYOM backend and the five scientific decision-support engines developed in this project.

## Engines

- **VYOM KRISHI** — agriculture and crop/weather decision support (`vyom_agro_engine.py`)
- **VYOM SAGAR** — marine/ocean and vessel-risk decision support (`vyom_marine_engine.py`)
- **VYOM VAYU** — aviation weather decision support (`vyom_aviation_engine.py`)
- **VYOM ADVENTURE** — outdoor/terrain/weather risk decision support (`vyom_adventure_engine.py`)
- **VYOM RAKSHA** — multi-hazard disaster-management decision support (`vyom_disaster_engine.py`)

The unified API entry point is **`main.py`**. Do not run the engine files manually for normal use; `main.py` imports them.

## Main files

```text
main.py
index.html
manifest.json
sw.js
requirements.txt
vyom_router.py
engine_output_guard.py
aviation_output_guard.py

vyom_agro_engine.py

vyom_marine_engine.py
marine_connectors.py
marine_data_fabric.py
marine_realtime_service.py
marine_realtime_worker.py

vyom_aviation_engine.py
aviation_connectors.py
aviation_data_fabric.py
aviation_realtime_service.py
aviation_realtime_worker.py

vyom_adventure_engine.py
adventure_connectors.py
adventure_data_fabric.py
adventure_realtime_service.py
adventure_realtime_worker.py

vyom_disaster_engine.py
disaster_connectors.py
disaster_data_fabric.py
disaster_realtime_service.py
disaster_realtime_worker.py

docs/
```

No test files or example configuration/data files are included in this final package.

## Install

Create a virtual environment:

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Linux/macOS:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

## Required environment variables

At minimum, configure the services used by your deployment. Common variables include:

```text
GROQ_API_KEY
MISTRAL_API_KEY
MONGO_URI
```

See `ENVIRONMENT_VARIABLES.md` for the engine-specific optional variables.

## Run

```bash
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open:

```text
http://127.0.0.1:8000
```

For deployment, remove `--reload`.

## Realtime workers

The API can perform request-time real-feed collection through the realtime services. The worker files are included for deployments that also want continuous ingestion/calibration. They are optional background processes, not additional user-facing applications.

Examples:

```bash
python marine_realtime_worker.py
python aviation_realtime_worker.py
python adventure_realtime_worker.py
python disaster_realtime_worker.py
```

Worker locations are supplied through environment variables as either a JSON array or a path to your own JSON file. No example location files are required.

## Safety contracts

The specialized engines are decision-support systems. They intentionally abstain where authoritative inputs are missing and do not create navigation clearance, flight clearance, technical-climbing clearance, evacuation orders, all-clear orders, or other authority-only decisions.

## Documentation

Read the domain-specific documents under `docs/` for detailed equations, data-source architecture, calibration logic, caveats and integration notes.

## Guarded narration and automatic engine routing

This build adds two reliability layers that sit between `main.py`, the scientific engines, and the language model:

- `vyom_router.py` — a conservative deterministic router. An explicit UI persona always wins. If the persona is `General Citizen`, strong domain cues can select KRISHI, SAGAR, VAYU, ADVENTURE, or RAKSHA. Ambiguous/tied queries remain on the general-weather path instead of guessing.
- `engine_output_guard.py` — a shared narration guard for KRISHI, SAGAR, ADVENTURE, and RAKSHA. VAYU retains its more aviation-specific `aviation_output_guard.py` and also follows the same risk/confidence/data-quality principles.

The protected flow is:

```text
user query
   -> router
   -> selected scientific engine
   -> structured engine result
   -> reduced narration payload
   -> strict LLM explainer
   -> output validation
   -> deterministic engine-grounded fallback if validation fails
   -> user
```

The generic LLM weather narrator is no longer used to rewrite the outputs of specialized engines. This prevents raw weather fields from being reinterpreted into unsupported domain claims.

### Corridor routing

Specialized route/corridor queries are never sent blindly through the driving-road router:

- Aviation uses the dedicated VYOM VAYU great-circle aviation corridor screen.
- Marine, Adventure, and Disaster use a great-circle **environmental corridor screen** and are explicitly marked as screening-only, not navigation/hiking/evacuation clearance.
- General road-weather questions may still use the normal driving-route handler.

### Source-status semantics

The guard normalizes source states such as `success`, `success_none`, `error`, `degraded`, `stale`, `unavailable`, and `unconfigured`. A final answer is not allowed to say “no official warnings” unless an authoritative warning source was successfully checked and explicitly returned no matching warnings.

### Engine diagnostics

Specialized `/weather` responses now include `engine_diagnostics` with the selected engine, version, routing reason/confidence, source status, official-warning verification state, and whether narration validation passed. This is intended for development/debugging and can be hidden by the frontend in production.

### Router diagnostics endpoint

During development you can inspect routing without running an engine:

```http
POST /api/router/preview
Content-Type: application/json

{"query":"METAR and turbulence for a flight from London to Los Angeles","persona":"General Citizen"}
```

The response shows the selected domain, routing source, confidence, route intent, and keyword scores.
