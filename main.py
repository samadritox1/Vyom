import io
import json
import logging
import ssl
import uuid
import re
import math
import requests
import statistics
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from fastapi import FastAPI, Response, BackgroundTasks, WebSocket, WebSocketDisconnect, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.concurrency import run_in_threadpool
import edge_tts
import asyncio
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from groq import Groq
from pymongo import MongoClient
import os
from dotenv import load_dotenv
from vyom_agro_engine import build_agro_advisory, fetch_day_of_year_climatology, standardized_anomaly
from vyom_marine_engine import build_marine_advisory
from marine_realtime_service import MarineRealtimeService
from vyom_aviation_engine import build_aviation_advisory
from aviation_realtime_service import AviationRealtimeService
from aviation_output_guard import (
    build_aviation_narration_payload,
    build_aviation_narrator_messages,
    validate_aviation_narration,
    deterministic_aviation_fallback,
    build_route_narration_payload,
    deterministic_aviation_route_fallback,
)
from engine_output_guard import (
    build_engine_narration_payload,
    build_engine_narrator_messages,
    validate_engine_narration,
    deterministic_engine_fallback,
    build_engine_diagnostics,
)
from vyom_router import route_query, extract_route_endpoints, is_specialized
from vyom_adventure_engine import build_adventure_advisory
from adventure_realtime_service import AdventureRealtimeService
from vyom_disaster_engine import build_disaster_advisory
from disaster_realtime_service import DisasterRealtimeService
import smtplib
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
import httpx
from ddgs import DDGS

from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

load_dotenv(override=False)

from security import (AuthenticatedUser, optional_user, require_user, require_admin, owned_user_id,
                      authenticate_token, allowed_origins, verified_email_addresses, verified_alert_email)
from alert_policy import validate_push_subscription
from notifications import scan_users, CONSENT_VERSION
from push_config import public_push_key

logger = logging.getLogger(__name__)

# ==========================================
# MONGODB SETUP (Replaced Supabase)
# ==========================================
MONGO_URI = os.getenv("MONGO_URI")
try:
    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is not configured")
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = mongo_client["weathergpt"]
    users_collection = db["users"]
    agro_states_collection = db["agro_field_states"]
    marine_states_collection = db["marine_voyage_states"]
    alert_deliveries_collection = db["alert_deliveries"]
    mongo_client.server_info() # Trigger connection check
    print("MongoDB Connected Successfully!")
except Exception as e:
    print(f"MongoDB connection warning: {e}")
    users_collection = None
    agro_states_collection = None
    marine_states_collection = None
    alert_deliveries_collection = None

client = Groq(api_key=os.getenv("GROQ_API_KEY")) if os.getenv("GROQ_API_KEY") else None
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")

def safe_ai_completion(messages, temp=0.2):
    """Attempts Groq first; falls back to Mistral AI instantly on failure."""
    try:
        response = client.chat.completions.create(
            messages=messages,
            model="openai/gpt-oss-20b",
            temperature=temp
        )
        return response.choices[0].message.content
    except Exception as groq_err:
        print(f"Groq API Failed ({groq_err}). Switching to Mistral AI Backup...")
        if not MISTRAL_API_KEY:
            return "SYSTEM ERROR: AI explanation providers are not configured or are unavailable."
        try:
            headers = {
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "mistral-small-latest",
                "messages": messages,
                "temperature": temp
            }
            res = requests.post("https://api.mistral.ai/v1/chat/completions", headers=headers, json=payload, timeout=12)
            res.raise_for_status()
            return res.json()["choices"][0]["message"]["content"]
        except Exception as mistral_err:
            print(f"Mistral Fallback Failed: {mistral_err}")
            return "SYSTEM ERROR: Both primary (Groq) and backup (Mistral) AI engines are currently unreachable."

app = FastAPI(title="VYOM Unified Multi-Domain Backend")

LANG_CODES = {
    "English": "en", "Hindi": "hi", "Bengali": "bn", "Telugu": "te",
    "Tamil": "ta", "Marathi": "mr", "Gujarati": "gu", "Kannada": "kn",
    "Malayalam": "ml", "Punjabi": "pa", "Odia": "or"
}

MODEL_MAPPING = {
    "best": "best_match", "ukmo": "ukmo_seamless", "ecmwf": "ecmwf_ifs025",
    "gfs": "gfs_seamless", "icon": "icon_seamless", "graphcast": "gfs_graphcast025",  
    "aifs": "ecmwf_aifs025"
}

PERSONA_GUIDELINES = {
    "Farmer": "Focus on crop-water balance, heat/cold stress, waterlogging, crop-stage impacts, disease/pest scouting signals, and robust irrigation timing. Do not provide chemical-application instructions.",
    "Marine/Fisherman": "Use VYOM-MARINE/Ocean Guardian-X structured risk: multi-model wind/wave uncertainty, vessel response, steep/crossing seas, currents, rapid deterioration, extreme-wave environment, traffic CPA/TCPA, UKC/grounding when trusted inputs exist, cyclone/ice/coastal context when supplied, confidence/OOD screening, and official-warning overrides. Never claim a navigation or sailing clearance.",
    "Aviation": "Use VYOM-AVIATION structured screening: METAR/TAF/SIGMET context, ceiling/visibility, runway winds, density altitude, convection, turbulence, icing, LLWS, mountain wave and route exposure. Never issue flight, dispatch, takeoff or landing clearance.",
    "Disaster Management": "Use VYOM-RAKSHA structured multi-hazard decision support: flood/flash-flood, landslide, cyclone, severe convection, wind, heat/cold, wildfire/smoke, observed earthquakes, official tsunami context, drought/coastal context when available, exposure/vulnerability, compound hazards, confidence and official-warning overrides. Never invent evacuation, shelter, rescue-dispatch or all-clear orders.",
    "Smart City Planner": "Focus on urban heat island effects, municipal storm drainage pressure, and outdoor labor safety.",
    "Adventurer": "Use VYOM-ADVENTURE structured outdoor-safety screening: thermal load, altitude exposure, lightning, wind, visibility, surface conditions, snow/ice weather context, flood context, wildfire/smoke, air quality, route/terrain exposure, confidence and official-warning overrides. Never certify a route, technical climb, river crossing, snow slope or wildfire-area journey as safe.",
    "General Citizen": "Focus on daily commute comfort, clothing requirements, umbrella necessity, and transit plans."
}

# ==========================================
# USER SYNC ENDPOINTS (MongoDB)
# ==========================================
class UserProfileSync(BaseModel):
    clerk_id: Optional[str] = None
    email: str = ""
    phone: str = ""
    language: str = "English"
    persona: str = "General Citizen"
    health_profile: str = "None"
    lat: Optional[float] = Field(None, ge=-90, le=90, allow_inf_nan=False)
    lon: Optional[float] = Field(None, ge=-180, le=180, allow_inf_nan=False)

@app.post("/api/user/sync")
def sync_user_profile(payload: UserProfileSync, user: AuthenticatedUser = Depends(require_user)):
    user_id = owned_user_id(payload.clerk_id, user)
    primary_email, _ = verified_email_addresses(user_id)
    if users_collection is None:
        return {"status": "error", "message": "MongoDB not initialized"}
    try:
        update_data = payload.model_dump(exclude_none=True)
        update_data.update(clerk_id=user_id, email=primary_email)
        update_data["updated_at"] = datetime.utcnow().isoformat()
        
        users_collection.update_one(
            {"clerk_id": user_id},
            {"$set": update_data},
            upsert=True
        )
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/user/profile")
def get_user_profile(clerk_id: Optional[str] = None, user: AuthenticatedUser = Depends(require_user)):
    clerk_id = owned_user_id(clerk_id, user)
    if users_collection is None:
        return {"status": "error", "message": "MongoDB not initialized"}
    try:
        user = users_collection.find_one({"clerk_id": clerk_id}, {"_id": 0})
        if user:
            if user.get("alert_consent_version") != CONSENT_VERSION:
                user.update(alert_requires_renewal=bool(user.get("alert_enabled")),
                            alert_enabled=False, push_subscription=None)
            return {"status": "success", "profile": user}
        return {"status": "not_found", "profile": None}
    except Exception as e:
        return {"status": "error", "message": str(e)}

class SavedLocation(BaseModel):
    name: str
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)

class UpdateLocationsRequest(BaseModel):
    clerk_id: Optional[str] = None
    locations: list[SavedLocation]

@app.post("/api/user/locations")
def update_user_locations(payload: UpdateLocationsRequest, user: AuthenticatedUser = Depends(require_user)):
    user_id = owned_user_id(payload.clerk_id, user)
    if users_collection is None:
        return {"status": "error", "message": "MongoDB not initialized"}
    try:
        users_collection.update_one(
            {"clerk_id": user_id},
            {"$set": {"saved_locations": [loc.dict() for loc in payload.locations]}},
            upsert=True
        )
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

class AlertConfigUpdate(BaseModel):
    clerk_id: Optional[str] = None
    alert_email: str = ""
    alert_lat: Optional[float] = Field(None, ge=-90, le=90, allow_inf_nan=False)
    alert_lon: Optional[float] = Field(None, ge=-180, le=180, allow_inf_nan=False)
    alert_rain_threshold: float = Field(50.0, gt=0, le=1000, allow_inf_nan=False)
    alert_heat_threshold: float = Field(42.0, ge=-80, le=80, allow_inf_nan=False)
    alert_wind_threshold: float = Field(60.0, gt=0, le=500, allow_inf_nan=False)
    alert_enabled: bool = False
    push_subscription: Optional[Dict[str, Any]] = None

class LiveLocationUpdate(BaseModel):
    clerk_id: Optional[str] = None
    alert_lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    alert_lon: float = Field(ge=-180, le=180, allow_inf_nan=False)

@app.post("/api/alerts/live-location")
def update_live_location(payload: LiveLocationUpdate, user: AuthenticatedUser = Depends(require_user)):
    user_id = owned_user_id(payload.clerk_id, user)
    if users_collection is None:
        return {"status": "error", "message": "MongoDB not initialized"}
    try:
        users_collection.update_one(
            {"clerk_id": user_id},
            {"$set": {"alert_lat": payload.alert_lat, "alert_lon": payload.alert_lon}}
        )
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/alerts/config")
def update_alert_config(payload: AlertConfigUpdate, background_tasks: BackgroundTasks, user: AuthenticatedUser = Depends(require_user)):
    user_id = owned_user_id(payload.clerk_id, user)
    if payload.alert_enabled and (payload.alert_lat is None or payload.alert_lon is None):
        raise HTTPException(400, "Alert coordinates are required")
    recipient = verified_alert_email(user_id, payload.alert_email) if payload.alert_enabled else ""
    try:
        subscription = validate_push_subscription(payload.push_subscription) if payload.alert_enabled else None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    update_data = payload.model_dump()
    update_data.update(clerk_id=user_id, alert_email=recipient, push_subscription=subscription,
                       push_vapid_public_key=public_push_key() if subscription else None,
                       alert_consent_version=CONSENT_VERSION, alert_config_revision=uuid.uuid4().hex)
    if users_collection is None:
        return {"status": "error", "message": "MongoDB not initialized"}
    try:
        users_collection.update_one(
            {"clerk_id": user_id},
            {"$set": update_data},
            upsert=True
        )
        if payload.alert_enabled:
            background_tasks.add_task(scan_and_alert_users, user_id)
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ==========================================
# WEBSOCKET MANAGER FOR REAL-TIME ALERTS
# ==========================================
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, set[WebSocket]] = {}

    def connect(self, websocket: WebSocket, user_id: str):
        self.active_connections.setdefault(user_id, set()).add(websocket)

    def disconnect(self, websocket: WebSocket, user_id: str):
        sockets = self.active_connections.get(user_id, set())
        sockets.discard(websocket)
        if not sockets:
            self.active_connections.pop(user_id, None)

    async def send_personal_message(self, message: str, user_id: str):
        sent = False
        for websocket in tuple(self.active_connections.get(user_id, set())):
            try:
                await websocket.send_text(message)
                sent = True
            except Exception:
                self.disconnect(websocket, user_id)
        return sent

manager = ConnectionManager()

@app.websocket("/ws/{clerk_id}")
async def websocket_endpoint(websocket: WebSocket, clerk_id: str):
    # Browsers cannot set Authorization headers on WebSocket handshakes. The first
    # frame authenticates the transport; it is never registered for alerts before verification.
    if websocket.headers.get("origin") not in allowed_origins():
        await websocket.close(code=4403)
        return
    await websocket.accept()
    user = None
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=5)
        if len(raw) > 17000:
            raise HTTPException(401, "Invalid authentication frame")
        token = json.loads(raw).get("token")
        user = await run_in_threadpool(authenticate_token, token)
        owned_user_id(clerk_id, user)
        manager.connect(websocket, user.user_id)
        await websocket.send_json({"type": "authenticated"})
        while True:
            remaining = user.expires_at - time.time()
            if remaining <= 0:
                break
            await asyncio.wait_for(websocket.receive_text(), timeout=remaining)
        await websocket.close(code=4401)
    except HTTPException as exc:
        await websocket.close(code=4403 if exc.status_code == 403 else 4401)
    except (asyncio.TimeoutError, ValueError, TypeError, AttributeError):
        await websocket.close(code=4401)
    except WebSocketDisconnect:
        pass
    finally:
        if user is not None:
            manager.disconnect(websocket, user.user_id)

# ==========================================
# AUTOMATED DISASTER ALERT ENGINE
# ==========================================
def send_email_alert(to_email, subject, body):
    if not to_email or not os.getenv("SMTP_EMAIL") or not os.getenv("SMTP_APP_PASSWORD"):
        return False
    try:
        msg = MIMEText(body, 'html', 'utf-8')
        msg['Subject'] = subject
        msg['From'] = os.environ["SMTP_EMAIL"]
        msg['To'] = to_email
        with smtplib.SMTP(os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "587")), timeout=10) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(os.environ["SMTP_EMAIL"], os.environ["SMTP_APP_PASSWORD"])
            server.send_message(msg)
        return True
    except Exception:
        logger.warning("Email delivery failed")
        return False


def send_web_push(subscription_info, title, body):
    if not subscription_info or not public_push_key():
        return False
    try:
        subscription = validate_push_subscription(subscription_info)
        from pywebpush import webpush
        webpush(subscription_info=subscription,
                data=json.dumps({"title": title, "body": body[:2000], "url": "/"}),
                vapid_private_key=os.environ["VAPID_PRIVATE_KEY_PATH"],
                vapid_claims={"sub": os.environ["VAPID_SUBJECT"]}, timeout=10)
        return True
    except Exception:
        logger.warning("Web push delivery failed")
        return False


def _send_live_alert(user_id, event):
    if global_loop is None or not global_loop.is_running():
        return False
    message = json.dumps({"type": "disaster_alert", "calamity": event.title,
                          "message": event.text, "event_id": event.event_id, "source": event.source})
    future = asyncio.run_coroutine_threadsafe(manager.send_personal_message(message, user_id), global_loop)
    try:
        return future.result(timeout=10)
    except Exception:
        future.cancel()
        return False


def scan_and_alert_users(user_id: Optional[str] = None):
    scan_users(users_collection, alert_deliveries_collection, send_email_alert,
               send_web_push, _send_live_alert, public_push_key(), user_id=user_id)


@app.post("/api/alerts/trigger", dependencies=[Depends(require_admin)])
def trigger_alert_scan(background_tasks: BackgroundTasks):
    background_tasks.add_task(scan_and_alert_users)
    return {"status": "success", "message": "Opted-in user alert scan initiated."}

# ==========================================
# STANDARD ROUTES
# ==========================================
@app.get("/")
def home():
    return FileResponse("index.html", headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})

@app.get("/api/push/public-key")
def get_push_public_key():
    return {"public_key": public_push_key()}


@app.get("/manifest.json")
def get_manifest():
    return FileResponse("manifest.json", media_type="application/json")

@app.get("/sw.js")
def get_service_worker():
    return FileResponse("sw.js", media_type="application/javascript")

@app.get("/news")
def get_weather_news(response: Response):
    try:
        rss_url = f'https://news.google.com/rss/search?q=extreme+weather+OR+climate+change+OR+flood+OR+cyclone+OR+hurricane+OR+wildfire+OR+heatwave+when:1d&hl=en-US&gl=US&ceid=US:en&_={int(time.time())}'
        res = requests.get(rss_url, timeout=5)
        root = ET.fromstring(res.content)
        items = root.findall('.//item')
        
        parsed_items = []
        for item in items:
            title = item.find('title').text
            link = item.find('link').text
            pub_date_str = item.find('pubDate').text if item.find('pubDate') is not None else ''
            
            try:
                dt = parsedate_to_datetime(pub_date_str)
                if dt.tzinfo is None:
                    from datetime import timezone
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                from datetime import timezone
                dt = datetime.min.replace(tzinfo=timezone.utc)
                
            clean_title = title.rsplit(" - ", 1)[0] if " - " in title else title
            parsed_items.append({
                "title": clean_title, 
                "link": link,
                "dt": dt
            })
            
        parsed_items.sort(key=lambda x: x["dt"], reverse=True)
        news_items = [{"title": x["title"], "link": x["link"]} for x in parsed_items[:15]]
        
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return {"news": news_items}
    except Exception as e:
        return {"news": [{"title": "Live global news currently unavailable.", "link": "#"}]}

def fallback_web_search(original_query: str, history: List[Dict[str, str]] = None) -> str:
    history = history or []
    try:
        chat_context_str = "\n".join([f"{msg['role'].capitalize()}: {msg['content']}" for msg in history[-3:]]) if history else "None"
        intent_prompt = f"Recent Chat History:\n{chat_context_str}\n\nUser Query: '{original_query}'. Rewrite the user's query into a standalone web search query for current weather or climate, incorporating the implied location or subject from the context. Reply with ONLY the search query string and nothing else."
        search_query = safe_ai_completion([{"role": "user", "content": intent_prompt}], temp=0.1).strip()
        
        results = DDGS().text(search_query, max_results=3)
        if not results:
            return f"I couldn't find a direct answer. Please refine your query."

        compiled_text = f"**Based on real-time data analysis:**\n\n"
        for i, r in enumerate(results):
            compiled_text += f"- {r.get('body', '')}\n"

        ai_prompt = f"Recent Chat Context:\n{chat_context_str}\n\nThe user asked about: '{original_query}'. Here is real-time data from search: {compiled_text}. Synthesize a helpful, conversational, and direct meteorological response. DO NOT provide cultural, political, or geographic trivia. ONLY discuss weather, climate, and meteorological data (including historical climate if asked). DO NOT mention that you searched the web or used real-time data."
        ai_summary = safe_ai_completion([{"role": "user", "content": ai_prompt}], temp=0.3)

        if ai_summary and not ai_summary.startswith("SYSTEM ERROR"):
            return ai_summary

        return compiled_text
    except Exception as e:
        return f"I couldn't process this complex query: {str(e)}"

from typing import List, Optional, Dict, Any

class AgroFieldRequest(BaseModel):
    # Identity / persistence
    field_id: Optional[str] = None

    # Crop context
    crop: str = "generic"
    variety: Optional[str] = None
    planting_date: Optional[str] = None       # YYYY-MM-DD
    crop_stage: Optional[str] = None
    cumulative_gdd: Optional[float] = None
    disease: Optional[str] = None             # weather-screening profile key
    pest: Optional[str] = None                # thermal-development profile key
    pest_degree_days_accumulated: Optional[float] = None

    # Soil / field context
    soil_type: str = "loam"
    field_area_acres: float = 1.0
    irrigation_efficiency: float = 0.75
    max_single_irrigation_mm: float = 35.0
    root_zone_vwc: Optional[float] = None
    initial_depletion_fraction: Optional[float] = None
    surface_vwc: Optional[float] = None
    root_depth_m: Optional[float] = None
    slope_percent: float = 1.0
    irrigation_method: str = "unknown"
    curve_number: Optional[float] = None
    drainage_factor: float = 1.0
    canopy_cover_fraction: Optional[float] = None

    # Yield/biomass calibration hooks
    potential_yield_t_ha: Optional[float] = None
    harvest_index: Optional[float] = None
    normalized_water_productivity_g_m2: Optional[float] = None
    yield_parameters_calibrated: bool = False

    # Calibration flags - set true only after local validation
    crop_profile_calibrated: bool = False
    soil_profile_calibrated: bool = False
    root_zone_sensor: bool = False

    # Weather-only application-window limits. No chemical product/dose logic lives here.
    spray_min_wind_kmh: float = 3.0
    spray_max_wind_kmh: float = 15.0
    spray_max_gust_kmh: float = 25.0
    spray_max_temp_c: float = 30.0
    spray_min_rh_pct: float = 40.0
    spray_max_rh_pct: float = 90.0
    spray_rain_free_hours: int = 6


class MarineVesselRequest(BaseModel):
    # Identity / persistence
    vessel_id: Optional[str] = None
    vessel_type: str = "generic"

    # Vessel geometry / operating state
    length_m: Optional[float] = None
    beam_m: Optional[float] = None
    draft_m: Optional[float] = None
    freeboard_m: Optional[float] = None
    displacement_t: Optional[float] = None
    service_speed_kn: Optional[float] = None
    heading_deg: Optional[float] = None

    # Optional stability / motion information. The engine treats these only as
    # screening inputs; approved stability documentation always supersedes it.
    roll_period_s: Optional[float] = None
    gm_m: Optional[float] = None
    hull_depth_m: Optional[float] = None
    propulsion_power_kw: Optional[float] = None
    maneuverability_index: Optional[float] = None
    cargo_state: Optional[str] = None
    cargo_shift_susceptibility: Optional[float] = None
    ballast_state: Optional[str] = None
    free_surface_correction_m: Optional[float] = None
    polar_capable: bool = False
    polar_class: Optional[str] = None

    navigation_zone: str = "offshore"  # harbour/coastal/offshore
    water_depth_m: Optional[float] = None

    # Trusted navigation/hydrographic inputs. The engine will abstain from UKC
    # calculations when these are incomplete rather than substituting generic
    # forecast sea-level fields.
    safety_margin_m: Optional[float] = None
    squat_m: Optional[float] = None
    dynamic_motion_allowance_m: Optional[float] = None
    depth_uncertainty_m: Optional[float] = None
    tide_uncertainty_m: Optional[float] = None
    draft_uncertainty_m: Optional[float] = None
    squat_uncertainty_m: Optional[float] = None
    position_uncertainty_m: Optional[float] = None

    # Ocean Guardian-X optional external feeds/context. These remain caller-
    # supplied unless authoritative connectors are added later; missing data is
    # never fabricated by the marine engine.
    external_context: Dict[str, Any] = Field(default_factory=dict)
    traffic_contacts: List[Dict[str, Any]] = Field(default_factory=list)
    cyclone_ensemble: List[Dict[str, Any]] = Field(default_factory=list)
    route_candidates: List[Dict[str, Any]] = Field(default_factory=list)
    rao_data: Dict[str, Any] = Field(default_factory=dict)
    validation_domain: Dict[str, Any] = Field(default_factory=dict)
    sensor_observations: Dict[str, Any] = Field(default_factory=dict)
    fishing_context: Dict[str, Any] = Field(default_factory=dict)
    stock_context: Dict[str, Any] = Field(default_factory=dict)

    # Owner/operator screening limits when known.
    max_operating_hs_m: Optional[float] = None
    max_operating_wind_kn: Optional[float] = None
    max_operating_gust_kn: Optional[float] = None

    # Official-warning information should come from an authoritative external
    # source; VYOM-MARINE will never downgrade it.
    official_warning_level: str = "none"
    official_warning_text: Optional[str] = None
    official_warning_source: Optional[str] = None
    official_warning_issued_at: Optional[str] = None

    vessel_profile_calibrated: bool = False
    local_marine_calibrated: bool = False


class MarineAdvisoryRequest(BaseModel):
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    vessel: Optional[MarineVesselRequest] = None
    horizon_hours: int = 120
    scenario_count: int = 80
    clerk_id: Optional[str] = None
    persist_state: bool = True


class MarineDataCollectRequest(BaseModel):
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    vessel: Optional[MarineVesselRequest] = None
    run_calibration: bool = True


class VesselCalibrationCaseRequest(BaseModel):
    case_id: str
    vessel_id: str
    vessel_class: str
    occurred_at: str
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    event_type: str
    outcome: int
    predicted_probability: float
    loading_state: str = ""
    sea_state_band: str = ""
    encounter_band: str = ""
    source: str = "operator_verified"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AviationAircraftRequest(BaseModel):
    aircraft_id: Optional[str] = None
    aircraft_class: str = "generic"
    aircraft_type_icao: Optional[str] = None
    departure_icao: Optional[str] = None
    arrival_icao: Optional[str] = None
    alternate_icao: Optional[str] = None
    heading_deg: Optional[float] = None
    true_airspeed_kt: Optional[float] = None
    cruise_altitude_ft: Optional[float] = None
    airport_elevation_ft: Optional[float] = None
    max_crosswind_kt: Optional[float] = None
    max_tailwind_kt: Optional[float] = None
    max_density_altitude_ft: Optional[float] = None
    minimum_ceiling_ft: Optional[float] = None
    minimum_visibility_m: Optional[float] = None
    icing_approved: bool = False
    runways: List[Dict[str, Any]] = Field(default_factory=list)
    terrain_context: Dict[str, Any] = Field(default_factory=dict)
    route_weather_points: List[Dict[str, Any]] = Field(default_factory=list)
    metar_context: Dict[str, Any] = Field(default_factory=dict)
    taf_context: List[Dict[str, Any]] = Field(default_factory=list)
    pirep_context: List[Dict[str, Any]] = Field(default_factory=list)
    official_warnings: List[Dict[str, Any]] = Field(default_factory=list)
    official_warning_level: str = "none"
    official_warning_text: Optional[str] = None
    official_warning_source: Optional[str] = None
    official_warning_feed_status: Optional[str] = None
    source_status: Dict[str, Any] = Field(default_factory=dict)


class AviationAdvisoryRequest(BaseModel):
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    aircraft: Optional[AviationAircraftRequest] = None
    horizon_hours: int = 72
    scenario_count: int = 120


class AdventureActivityRequest(BaseModel):
    activity_id: Optional[str] = None
    activity_type: str = "hiking"
    country_code: Optional[str] = None
    route_points: List[Dict[str, Any]] = Field(default_factory=list)
    terrain_context: Dict[str, Any] = Field(default_factory=dict)
    official_warnings: List[Dict[str, Any]] = Field(default_factory=list)
    official_avalanche_bulletin: Optional[Dict[str, Any]] = None
    pm2_5: Optional[float] = None
    us_aqi: Optional[float] = None
    dust: Optional[float] = None
    fire_detections: List[Dict[str, Any]] = Field(default_factory=list)
    flood_context: Dict[str, Any] = Field(default_factory=dict)


class AdventureAdvisoryRequest(BaseModel):
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    activity: Optional[AdventureActivityRequest] = None
    horizon_hours: int = 72
    scenario_count: int = 120


class DisasterContextRequest(BaseModel):
    incident_id: Optional[str] = None
    country_code: Optional[str] = None
    terrain_context: Dict[str, Any] = Field(default_factory=dict)
    exposure_context: Dict[str, Any] = Field(default_factory=dict)
    flood_context: Dict[str, Any] = Field(default_factory=dict)
    coastal_context: Dict[str, Any] = Field(default_factory=dict)
    official_warnings: List[Dict[str, Any]] = Field(default_factory=list)
    official_tsunami_warnings: List[Dict[str, Any]] = Field(default_factory=list)
    earthquake_events: List[Dict[str, Any]] = Field(default_factory=list)
    cyclone_context: List[Dict[str, Any]] = Field(default_factory=list)
    fire_detections: List[Dict[str, Any]] = Field(default_factory=list)
    pm2_5: Optional[float] = None
    us_aqi: Optional[float] = None
    dust: Optional[float] = None
    lightning_density: Optional[float] = None
    antecedent_rain_7d_mm: Optional[float] = None
    drainage_capacity_index: Optional[float] = None
    impervious_fraction: Optional[float] = None
    heat_threshold_c: Optional[float] = None
    cold_threshold_c: Optional[float] = None
    spi_3: Optional[float] = None
    spi_6: Optional[float] = None
    spei_3: Optional[float] = None
    wind_exposure_index: Optional[float] = None


class DisasterAdvisoryRequest(BaseModel):
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    context: Optional[DisasterContextRequest] = None
    horizon_hours: int = 120
    scenario_count: int = 160
    collect_live_context: bool = True


class WeatherRequest(BaseModel):
    query: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    language: str = "English"
    persona: str = "General Citizen"
    health_profile: str = "None"
    model: str = "best"
    history: List[Dict[str, str]] = []
    clerk_id: Optional[str] = None
    agro: Optional[AgroFieldRequest] = None
    marine: Optional[MarineVesselRequest] = None
    aviation: Optional[AviationAircraftRequest] = None
    adventure: Optional[AdventureActivityRequest] = None
    disaster: Optional[DisasterContextRequest] = None


class RouterPreviewRequest(BaseModel):
    query: str = ""
    persona: str = "General Citizen"


@app.post("/api/router/preview")
def preview_router(payload: RouterPreviewRequest):
    """Development endpoint: show which domain the deterministic router selects."""
    return route_query(payload.persona, payload.query)


class AgroAdvisoryRequest(BaseModel):
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    field: Optional[AgroFieldRequest] = None
    horizon_hours: int = 120
    scenario_count: int = 64
    clerk_id: Optional[str] = None
    persist_state: bool = True


def load_persisted_agro_state(clerk_id: Optional[str], field_id: Optional[str]) -> Dict[str, Any]:
    """Load the latest VYOM-AGRO checkpoint for optional stateful advisories."""
    if not clerk_id or not field_id or agro_states_collection is None:
        return {}
    try:
        doc = agro_states_collection.find_one(
            {"clerk_id": clerk_id, "field_id": field_id},
            {"_id": 0, "state": 1},
        ) or {}
        state = doc.get("state") or {}
        if not isinstance(state, dict):
            return {}
        return {
            "prior_state_timestamp": state.get("timestamp"),
            "prior_state_depletion_mm": state.get("depletion_mm"),
            "prior_state_surface_depletion_mm": state.get("surface_depletion_mm"),
            "prior_state_excess_water_mm": state.get("excess_water_mm"),
            "prior_state_cumulative_gdd": state.get("cumulative_gdd"),
            "prior_state_root_depth_m": state.get("root_depth_m"),
        }
    except Exception as e:
        print(f"Agro state load warning: {e}")
        return {}


def save_persisted_agro_state(clerk_id: Optional[str], field_id: Optional[str], result: Dict[str, Any]) -> bool:
    if not clerk_id or not field_id or agro_states_collection is None:
        return False
    checkpoint = result.get("state_checkpoint") if isinstance(result, dict) else None
    if not isinstance(checkpoint, dict):
        return False
    try:
        agro_states_collection.update_one(
            {"clerk_id": clerk_id, "field_id": field_id},
            {"$set": {
                "state": checkpoint,
                "updated_at": datetime.utcnow().isoformat() + "Z",
                "engine_version": result.get("version"),
                "crop": result.get("crop", {}).get("profile_used"),
                "soil": result.get("soil", {}).get("profile_used"),
            }},
            upsert=True,
        )
        return True
    except Exception as e:
        print(f"Agro state save warning: {e}")
        return False


@app.post("/agro/advisory")
async def standalone_agro_advisory(req: AgroAdvisoryRequest, user: Optional[AuthenticatedUser] = Depends(optional_user)):
    user_id = owned_user_id(req.clerk_id, user)
    req = req.model_copy(update={"clerk_id": user_id})
    field_cfg = req.field.dict(exclude_none=True) if req.field else {}
    field_id = field_cfg.get("field_id")
    # Database state is only a prior. A fresh root-zone observation supplied in
    # this request remains stronger and is assimilated by the engine.
    field_cfg.update(load_persisted_agro_state(req.clerk_id, field_id))
    try:
        result = await run_in_threadpool(
            build_agro_advisory,
            req.lat,
            req.lon,
            field_cfg,
            req.horizon_hours,
            req.scenario_count,
        )
        persisted = False
        if req.persist_state and result.get("status") == "ok":
            persisted = save_persisted_agro_state(req.clerk_id, field_id, result)
        result["state_persistence"] = {
            "requested": bool(req.persist_state),
            "saved": persisted,
            "field_id": field_id,
            "requires": "clerk_id + field.field_id + MongoDB" if not persisted else None,
        }
        return result
    except Exception as e:
        return {
            "engine": "VYOM-AGRO",
            "version": "2.0.0",
            "status": "error",
            "reason": str(e),
            "risk": {},
            "confidence": {"overall": 0.0},
        }


def load_persisted_marine_state(clerk_id: Optional[str], vessel_id: Optional[str]) -> Dict[str, Any]:
    if not clerk_id or not vessel_id or marine_states_collection is None:
        return {}
    try:
        doc = marine_states_collection.find_one(
            {"clerk_id": clerk_id, "vessel_id": vessel_id},
            {"_id": 0, "state": 1},
        ) or {}
        state = doc.get("state") or {}
        if not isinstance(state, dict):
            return {}
        return {
            "prior_state_timestamp": state.get("timestamp"),
            "prior_cumulative_rough_hours": state.get("cumulative_rough_hours", 0.0),
            "prior_motion_exposure_index": state.get("motion_exposure_index", 0.0),
        }
    except Exception as e:
        print(f"Marine state load warning: {e}")
        return {}


def save_persisted_marine_state(clerk_id: Optional[str], vessel_id: Optional[str], result: Dict[str, Any]) -> bool:
    if not clerk_id or not vessel_id or marine_states_collection is None:
        return False
    checkpoint = result.get("state_checkpoint") if isinstance(result, dict) else None
    if not isinstance(checkpoint, dict):
        return False
    try:
        marine_states_collection.update_one(
            {"clerk_id": clerk_id, "vessel_id": vessel_id},
            {"$set": {
                "state": checkpoint,
                "updated_at": datetime.utcnow().isoformat() + "Z",
                "engine_version": result.get("version"),
                "vessel_profile": result.get("vessel", {}).get("profile_used"),
            }},
            upsert=True,
        )
        return True
    except Exception as e:
        print(f"Marine state save warning: {e}")
        return False


@app.post("/marine/advisory")
async def standalone_marine_advisory(req: MarineAdvisoryRequest, user: Optional[AuthenticatedUser] = Depends(optional_user)):
    user_id = owned_user_id(req.clerk_id, user)
    req = req.model_copy(update={"clerk_id": user_id})
    vessel_cfg = req.vessel.dict(exclude_none=True) if req.vessel else {}
    vessel_id = vessel_cfg.get("vessel_id")
    vessel_cfg.update(load_persisted_marine_state(req.clerk_id, vessel_id))
    try:
        result = await run_in_threadpool(
            build_marine_advisory,
            req.lat,
            req.lon,
            vessel_cfg,
            req.horizon_hours,
            req.scenario_count,
        )
        persisted = False
        if req.persist_state and result.get("status") == "ok":
            persisted = save_persisted_marine_state(req.clerk_id, vessel_id, result)
        result["state_persistence"] = {
            "requested": bool(req.persist_state),
            "saved": persisted,
            "vessel_id": vessel_id,
            "requires": "clerk_id + vessel.vessel_id + MongoDB" if not persisted else None,
        }
        return result
    except Exception as e:
        return {
            "engine": "VYOM-MARINE", "version": "4.0.0", "status": "error",
            "reason": str(e), "risk": {}, "confidence": {"overall": 0.0},
        }


@app.get("/marine/data/health", dependencies=[Depends(require_admin)])
async def marine_data_health():
    """Real-feed/data-fabric health and record counts."""
    try:
        return await run_in_threadpool(MarineRealtimeService().health)
    except Exception as e:
        return {"status":"error","reason":str(e)}


@app.post("/marine/data/collect", dependencies=[Depends(require_admin)])
async def marine_data_collect(req: MarineDataCollectRequest):
    """Collect configured authoritative/observational feeds for one ocean point."""
    svc = MarineRealtimeService()
    vessel = req.vessel.dict(exclude_none=True) if req.vessel else {}
    try:
        result = await run_in_threadpool(svc.collect_live_context, req.lat, req.lon, vessel)
        if req.run_calibration:
            result["verification"] = await run_in_threadpool(svc.verify_and_calibrate, int(os.getenv("CAL_MATCH_LOOKBACK_DAYS","10")))
        return result
    except Exception as e:
        return {"status":"error","reason":str(e)}


@app.post("/marine/data/calibrate", dependencies=[Depends(require_admin)])
async def marine_data_calibrate(lookback_days: int = 10):
    """Match stored forecasts to observations and recompute calibration cells."""
    try:
        return await run_in_threadpool(MarineRealtimeService().verify_and_calibrate, max(1,min(365,lookback_days)))
    except Exception as e:
        return {"status":"error","reason":str(e)}

@app.post("/marine/data/vessel-case", dependencies=[Depends(require_admin)])
async def marine_vessel_calibration_case(req: VesselCalibrationCaseRequest):
    """Store an explicitly verified event/control label for vessel-response calibration.

    This endpoint is admin-gated because false labels can corrupt safety calibration.
    Set VYOM_ADMIN_TOKEN in .env and send it as X-VYOM-Admin-Token.
    """
    svc = MarineRealtimeService()
    ok = svc.fabric.log_vessel_labeled_case(
        req.case_id, req.vessel_id, req.vessel_class, req.occurred_at, req.lat, req.lon,
        req.event_type, req.outcome, req.predicted_probability, req.loading_state,
        req.sea_state_band, req.encounter_band, req.source, True, req.metadata
    )
    cells = svc.fabric.recompute_vessel_calibration(min_samples=int(os.getenv("VESSEL_CAL_MIN_SAMPLES", "12"))) if ok else 0
    return {"status":"ok" if ok else "error", "vessel_calibration_cells_recomputed": cells}


@app.post("/aviation/advisory")
async def standalone_aviation_advisory(req: AviationAdvisoryRequest):
    cfg = req.aircraft.dict(exclude_none=True) if req.aircraft else {}
    try:
        return await run_in_threadpool(build_aviation_advisory, req.lat, req.lon, cfg, req.horizon_hours, req.scenario_count)
    except Exception as e:
        return {"engine":"VYOM-AVIATION","version":"1.1.0","status":"error","reason":str(e),"risk":{},"confidence":{"overall":0.0},"dispatch_clearance":None}

@app.get("/aviation/data/health", dependencies=[Depends(require_admin)])
async def aviation_data_health():
    try:
        return await run_in_threadpool(AviationRealtimeService().health)
    except Exception as e:
        return {"status":"error","reason":str(e)}

@app.post("/aviation/data/calibrate", dependencies=[Depends(require_admin)])
async def aviation_data_calibrate(lookback_days: int = 30):
    try:
        return await run_in_threadpool(AviationRealtimeService().verify_and_calibrate, max(1,min(365,lookback_days)))
    except Exception as e:
        return {"status":"error","reason":str(e)}


@app.post("/adventure/advisory")
async def standalone_adventure_advisory(req: AdventureAdvisoryRequest):
    cfg = req.activity.dict(exclude_none=True) if req.activity else {}
    try:
        return await run_in_threadpool(build_adventure_advisory, req.lat, req.lon, cfg, req.horizon_hours, req.scenario_count)
    except Exception as e:
        return {"engine":"VYOM-ADVENTURE","version":"1.0.0","status":"error","reason":str(e),"risk":{},"confidence":{"overall":0.0},"safe_to_proceed":None}

@app.get("/adventure/data/health", dependencies=[Depends(require_admin)])
async def adventure_data_health():
    try:
        return await run_in_threadpool(AdventureRealtimeService().health)
    except Exception as e:
        return {"status":"error","reason":str(e)}

@app.post("/adventure/data/calibrate", dependencies=[Depends(require_admin)])
async def adventure_data_calibrate(lookback_days: int = 30):
    try:
        return await run_in_threadpool(AdventureRealtimeService().verify_and_calibrate, max(1,min(365,lookback_days)))
    except Exception as e:
        return {"status":"error","reason":str(e)}


def _merge_context_patch(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base or {})
    for k, v in (patch or {}).items():
        if isinstance(v, list):
            out.setdefault(k, [])
            if isinstance(out[k], list): out[k].extend(v)
            else: out[k] = list(v)
        elif isinstance(v, dict):
            if isinstance(out.get(k), dict):
                x = dict(out[k]); x.update(v); out[k] = x
            elif k not in out or out.get(k) is None:
                out[k] = dict(v)
        elif out.get(k) is None:
            out[k] = v
    return out


@app.post("/disaster/advisory")
async def standalone_disaster_advisory(req: DisasterAdvisoryRequest):
    cfg = req.context.dict(exclude_none=True) if req.context else {}
    live_meta = None
    if req.collect_live_context:
        try:
            live = await run_in_threadpool(DisasterRealtimeService().collect_live_context, req.lat, req.lon, cfg)
            cfg = _merge_context_patch(cfg, live.get("patch") or {})
            if live.get("dynamic_calibration_path"):
                cfg["calibration_path"] = live["dynamic_calibration_path"]
            live_meta = {"status":live.get("status"),"sources":live.get("sources",[]),"elapsed_ms":live.get("elapsed_ms")}
        except Exception as e:
            live_meta = {"status":"error","reason":str(e)}
    try:
        result = await run_in_threadpool(build_disaster_advisory, req.lat, req.lon, cfg, req.horizon_hours, req.scenario_count)
        result["live_data"] = live_meta
        return result
    except Exception as e:
        return {"engine":"VYOM-RAKSHA","version":"1.0.0","status":"error","reason":str(e),"risk":{},"confidence":{"overall":0.0},"evacuation_order":None,"all_clear":None}


@app.get("/disaster/data/health", dependencies=[Depends(require_admin)])
async def disaster_data_health():
    try:
        return await run_in_threadpool(DisasterRealtimeService().health)
    except Exception as e:
        return {"status":"error","reason":str(e)}


@app.post("/disaster/data/calibrate", dependencies=[Depends(require_admin)])
async def disaster_data_calibrate(lookback_days: int = 30):
    try:
        return await run_in_threadpool(DisasterRealtimeService().verify_and_calibrate, max(1,min(365,lookback_days)))
    except Exception as e:
        return {"status":"error","reason":str(e)}


import math

def haversine_dist(lon1, lat1, lon2, lat2):
    lon1, lat1, lon2, lat2 = map(math.radians, [lon1, lat1, lon2, lat2])
    dlon = lon2 - lon1 
    dlat = lat2 - lat1 
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a)) 
    r = 6371 
    return c * r


def _gc_interpolate(lat1, lon1, lat2, lon2, count=5):
    """Great-circle screening points. These are not operational flight-plan waypoints."""
    count = max(3, min(9, int(count)))
    phi1, lam1 = math.radians(lat1), math.radians(lon1)
    phi2, lam2 = math.radians(lat2), math.radians(lon2)
    v1 = (math.cos(phi1)*math.cos(lam1), math.cos(phi1)*math.sin(lam1), math.sin(phi1))
    v2 = (math.cos(phi2)*math.cos(lam2), math.cos(phi2)*math.sin(lam2), math.sin(phi2))
    dot = max(-1.0, min(1.0, sum(a*b for a,b in zip(v1,v2))))
    omega = math.acos(dot)
    out=[]
    for i in range(count):
        f=i/(count-1)
        if omega < 1e-9:
            x,y,z=v1
        else:
            so=math.sin(omega)
            a=math.sin((1-f)*omega)/so
            b=math.sin(f*omega)/so
            x=a*v1[0]+b*v2[0]; y=a*v1[1]+b*v2[1]; z=a*v1[2]+b*v2[2]
        lat=math.degrees(math.atan2(z, math.hypot(x,y)))
        lon=math.degrees(math.atan2(y,x))
        out.append((lat,lon))
    return out


def _aviation_enroute_config(cfg):
    out=dict(cfg or {})
    for key in (
        "runways","airport_elevation_ft","minimum_ceiling_ft","minimum_visibility_m",
        "departure_icao","arrival_icao","alternate_icao","metar_context","taf_context",
        "pirep_context","official_warnings","official_warning_level","official_warning_text",
        "official_warning_source","official_warning_feed_status","source_status",
    ):
        out.pop(key, None)
    return out


async def process_aviation_route_weather(origin_str, dest_str, req, query, language, headers, routing=None):
    """Aviation-specific route screening; never uses road routing or road-segment colors."""
    try:
        geo_url = "https://nominatim.openstreetmap.org/search"
        res_orig = requests.get(geo_url, headers=headers, params={"q": origin_str, "format": "json", "limit": 1}, timeout=8).json()
        res_dest = requests.get(geo_url, headers=headers, params={"q": dest_str, "format": "json", "limit": 1}, timeout=8).json()
        if not res_orig or not res_dest:
            return {"weather_report": f"VYOM VAYU could not resolve the requested aviation corridor from {origin_str} to {dest_str}.", "weather_code": 0, "telemetry": {}, "engine_diagnostics": {"engine":"VYOM VAYU","executed":False,"reason":"route geocoding failed"}}
        lat1, lon1 = float(res_orig[0]["lat"]), float(res_orig[0]["lon"])
        lat2, lon2 = float(res_dest[0]["lat"]), float(res_dest[0]["lon"])
        origin={"name":origin_str,"lat":lat1,"lon":lon1}
        destination={"name":dest_str,"lat":lat2,"lon":lon2}
        cfg = req.aviation.dict(exclude_none=True) if req.aviation else {}
        coords=_gc_interpolate(lat1,lon1,lat2,lon2,5)

        async def run_point(idx, lat, lon):
            point_cfg = dict(cfg) if idx in {0, len(coords)-1} else _aviation_enroute_config(cfg)
            result = await run_in_threadpool(build_aviation_advisory, lat, lon, point_cfg, 48, 80)
            risk=(result.get("risk") or {}) if isinstance(result,dict) else {}
            hazards=risk.get("hazards") or {}
            top=[]
            if isinstance(hazards,dict):
                rows=[]
                for k,v in hazards.items():
                    if isinstance(v,dict) and v.get("max_probability") is not None:
                        rows.append((float(v.get("max_probability")),k,v.get("level")))
                rows.sort(reverse=True)
                top=[{"hazard":k,"probability":round(p,4),"level":lvl} for p,k,lvl in rows[:4]]
            official=result.get("official_weather") or {}
            return {
                "index":idx,
                "lat":round(lat,5),"lon":round(lon,5),
                "status":result.get("status"),
                "risk_probability":risk.get("integrated_peak_probability"),
                "risk_level":risk.get("level"),
                "confidence":(result.get("confidence") or {}).get("overall"),
                "data_quality":(result.get("data_quality") or {}).get("overall"),
                "top_hazards":top,
                "official_warning_present":official.get("present"),
                "official_warning_verified_none":official.get("verified_none"),
                "official_warning_feed_status":official.get("feed_status"),
                "source_status":result.get("source_status") or {},
                "_result":result,
            }

        sampled=await asyncio.gather(*(run_point(i,a,b) for i,(a,b) in enumerate(coords)))
        dep=sampled[0].pop("_result") if sampled else {}
        arr=sampled[-1].pop("_result") if sampled else {}
        for row in sampled[1:-1]:
            row.pop("_result",None)
        payload=build_route_narration_payload(query,origin,destination,sampled,dep,arr)
        messages=build_aviation_narrator_messages(payload,language)
        text=safe_ai_completion(messages,temp=0.1)
        valid,problems=validate_aviation_narration(text,payload)
        if (not text) or text.startswith("SYSTEM ERROR") or not valid:
            text=deterministic_aviation_route_fallback(payload)
        return {
            "weather_report":text,
            "weather_code":0,
            "telemetry":{"aviation_route":payload},
            "location_name":f"Aviation corridor: {origin_str} to {dest_str}",
            "lat":lat1,"lon":lon1,"is_route":True,
            "route_geometry":[[lon,lat] for lat,lon in coords],
            "engine_diagnostics":{
                "engine":"VYOM VAYU","executed":True,"mode":"aviation_route_screening",
                "sample_count":len(sampled),"narration_validation_passed":valid,"validation_problems":problems,
                "road_router_used":False,
                "routing": routing or {},
            },
        }
    except Exception as e:
        return {"weather_report":f"VYOM VAYU route screening could not be completed: {e}","weather_code":0,"telemetry":{},"engine_diagnostics":{"engine":"VYOM VAYU","executed":False,"reason":str(e),"routing":routing or {}}}

async def process_specialized_corridor_weather(domain, origin_str, dest_str, req, query, language, headers, routing):
    """Great-circle environmental corridor screening for specialized non-road engines.

    This deliberately does NOT use OSRM/driving geometry. It samples a geodesic
    corridor for environmental screening only; it is not a navigation, hiking,
    evacuation, or operational route clearance.
    """
    try:
        geo_url = "https://nominatim.openstreetmap.org/search"
        res_orig = requests.get(geo_url, headers=headers, params={"q": origin_str, "format": "json", "limit": 1}, timeout=8).json()
        res_dest = requests.get(geo_url, headers=headers, params={"q": dest_str, "format": "json", "limit": 1}, timeout=8).json()
        if not res_orig or not res_dest:
            public = {"marine":"VYOM SAGAR","adventure":"VYOM ADVENTURE","disaster":"VYOM RAKSHA"}.get(domain,"VYOM")
            return {"weather_report": f"{public} could not resolve the requested corridor from {origin_str} to {dest_str}.", "weather_code": 0, "telemetry": {}, "engine_diagnostics": {"engine":public,"engine_executed":False,"reason":"corridor geocoding failed","routing":routing}}
        lat1, lon1 = float(res_orig[0]["lat"]), float(res_orig[0]["lon"])
        lat2, lon2 = float(res_dest[0]["lat"]), float(res_dest[0]["lon"])
        coords = _gc_interpolate(lat1, lon1, lat2, lon2, 5)
        extra_sources = []

        async def run_point(idx, lat, lon):
            nonlocal extra_sources
            if domain == "marine":
                cfg = req.marine.dict(exclude_none=True) if req.marine else {}
                result = await run_in_threadpool(build_marine_advisory, lat, lon, cfg, 72, 48)
                ogx = result.get("ocean_guardian_x") or {}
                risk = ((ogx.get("dependency_aware_global_risk") or {}).get("risk"))
                level = ogx.get("user_facing_safety_level") or ((result.get("risk") or {}).get("level"))
                confidence = (result.get("confidence") or {}).get("overall")
            elif domain == "adventure":
                cfg = req.adventure.dict(exclude_none=True) if req.adventure else {}
                result = await run_in_threadpool(build_adventure_advisory, lat, lon, cfg, 48, 64)
                risk = (result.get("risk") or {}).get("integrated_peak_probability")
                level = (result.get("risk") or {}).get("level")
                confidence = (result.get("confidence") or {}).get("overall")
            elif domain == "disaster":
                cfg = req.disaster.dict(exclude_none=True) if req.disaster else {}
                live_sources = []
                if os.getenv("DISASTER_LIVE_CONTEXT_ON_QUERY", "1") == "1":
                    live = await run_in_threadpool(DisasterRealtimeService().collect_live_context, lat, lon, cfg)
                    cfg = _merge_context_patch(cfg, live.get("patch") or {})
                    live_sources = live.get("sources") or []
                result = await run_in_threadpool(build_disaster_advisory, lat, lon, cfg, 72, 80)
                if live_sources:
                    result["realtime"] = {"enabled":True,"sources":live_sources,"status":"ok"}
                risk = (result.get("risk") or {}).get("integrated")
                level = (result.get("risk") or {}).get("level")
                confidence = (result.get("confidence") or {}).get("overall")
            else:
                raise ValueError(f"Unsupported corridor domain: {domain}")
            return {"index":idx,"lat":round(lat,5),"lon":round(lon,5),"risk":risk,"level":level,"confidence":confidence,"result":result}

        rows = await asyncio.gather(*(run_point(i,a,b) for i,(a,b) in enumerate(coords)))
        valid_rows = [r for r in rows if isinstance(r.get("risk"),(int,float))]
        max_row = max(valid_rows, key=lambda r: r["risk"]) if valid_rows else (rows[0] if rows else {})
        peak_result = dict(max_row.get("result") or {})
        corridor_summary = [{k:v for k,v in r.items() if k != "result"} for r in rows]

        # Attach corridor evidence without changing the engine's own risk math.
        peak_result["corridor_screening"] = {
            "origin": {"name":origin_str,"lat":lat1,"lon":lon1},
            "destination": {"name":dest_str,"lat":lat2,"lon":lon2},
            "sample_count": len(rows),
            "samples": corridor_summary,
            "screening_only": True,
            "not_operational_route_clearance": True,
        }
        payload = build_engine_narration_payload(domain, peak_result, query, f"{origin_str} to {dest_str}", routing=routing)
        payload["corridor_screening"] = peak_result["corridor_screening"]
        messages = build_engine_narrator_messages(domain, payload, language)
        text = safe_ai_completion(messages, temp=0.1)
        valid, problems = validate_engine_narration(domain, text, payload)
        if (not text) or text.startswith("SYSTEM ERROR") or not valid:
            text = deterministic_engine_fallback(domain, payload)
        diagnostics = build_engine_diagnostics(domain, peak_result, payload, valid, problems, routing)
        diagnostics.update({"mode":"great_circle_environmental_corridor_screening","sample_count":len(rows),"road_router_used":False})
        return {
            "weather_report": text,
            "weather_code": 0,
            "telemetry": {f"{domain}_corridor": payload},
            "location_name": f"{origin_str} to {dest_str}",
            "lat": lat1, "lon": lon1, "is_route": True,
            "route_geometry": [[lon,lat] for lat,lon in coords],
            "engine_diagnostics": diagnostics,
        }
    except Exception as e:
        return {"weather_report": f"Specialized corridor screening could not be completed: {e}", "weather_code": 0, "telemetry": {}, "engine_diagnostics": {"engine_executed":False,"reason":str(e),"routing":routing}}


async def process_route_weather(origin_str, dest_str, query, language, persona, history, headers):
    try:
        # 1. Geocode Origin and Destination
        geo_url = "https://nominatim.openstreetmap.org/search"
        res_orig = requests.get(geo_url, headers=headers, params={"q": origin_str, "format": "json", "limit": 1}).json()
        res_dest = requests.get(geo_url, headers=headers, params={"q": dest_str, "format": "json", "limit": 1}).json()
        
        if not res_orig or not res_dest:
            return {"weather_report": f"Could not find coordinates for route: {origin_str} to {dest_str}.", "weather_code": 0, "telemetry": {}}
            
        lat1, lon1 = float(res_orig[0]['lat']), float(res_orig[0]['lon'])
        lat2, lon2 = float(res_dest[0]['lat']), float(res_dest[0]['lon'])
        
        # 2. Get Route from OSRM
        osrm_url = f"http://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=full&geometries=geojson"
        route_res = requests.get(osrm_url, timeout=10).json()
        
        if route_res.get("code") != "Ok":
            return {"weather_report": "Could not calculate driving route.", "weather_code": 0, "telemetry": {}}
            
        coords = route_res['routes'][0]['geometry']['coordinates'] # [lon, lat] list
        duration = route_res['routes'][0]['duration'] # in seconds
        distance = route_res['routes'][0]['distance'] # in meters
        
        # 3. Sample Waypoints (every ~50km)
        waypoints = []
        last_coord = coords[0]
        waypoints.append(last_coord)
        dist_accum = 0
        for c in coords[1:]:
            d = haversine_dist(last_coord[0], last_coord[1], c[0], c[1])
            dist_accum += d
            last_coord = c
            if dist_accum >= 50:
                waypoints.append(c)
                dist_accum = 0
        waypoints.append(coords[-1])
        
        # 4. Fetch Weather for Waypoints
        # Open-Meteo allows multiple coordinates if we pass comma separated lats/lons
        lats = ",".join([str(c[1]) for c in waypoints])
        lons = ",".join([str(c[0]) for c in waypoints])
        
        wm_url = f"https://api.open-meteo.com/v1/forecast?latitude={lats}&longitude={lons}&current=temperature_2m,precipitation,weather_code,wind_speed_10m"
        weather_data = requests.get(wm_url, timeout=10).json()
        
        # Determine safety colors for segments
        weather_waypoints = []
        is_array = isinstance(weather_data, list)
        items = weather_data if is_array else [weather_data]
        
        for i, c in enumerate(waypoints):
            # Sometimes Open-Meteo returns a list for multi-coords, sometimes a single object if 1 coord
            w = items[i] if len(items) > i else items[0]
            curr = w.get("current", {})
            temp = curr.get("temperature_2m", 0)
            precip = curr.get("precipitation", 0)
            wind = curr.get("wind_speed_10m", 0)
            code = curr.get("weather_code", 0)
            
            # Determine color: green = clear, yellow = rain/fog/wind, red = severe
            color = "green"
            if precip > 0 or code in [45, 48]: color = "yellow"
            if precip > 10 or wind > 50 or code in [95, 96, 99]: color = "red"
            
            weather_waypoints.append({
                "lat": c[1], "lon": c[0],
                "temp": temp, "precip": precip, "wind": wind,
                "color": color, "code": code
            })
            
        # 5. Generate AI Summary for the Route
        chat_context_str = "\n".join([f"{msg['role'].capitalize()}: {msg['content']}" for msg in history[-3:]]) if history else "None"
        route_prompt = f"Recent Chat History:\n{chat_context_str}\n\nUser asked for a route forecast: '{query}'.\nRoute: {origin_str} to {dest_str}.\nDistance: {round(distance/1000)} km.\nEst Duration: {round(duration/3600, 1)} hours.\nWaypoints show conditions ranging from {min([w['temp'] for w in weather_waypoints])}°C to {max([w['temp'] for w in weather_waypoints])}°C.\nRed segments (severe): {sum([1 for w in weather_waypoints if w['color'] == 'red'])}. Yellow: {sum([1 for w in weather_waypoints if w['color'] == 'yellow'])}.\nWrite an authoritative meteorological advisory for this travel route in {language}. Provide actionable safety tips for the {persona} persona. RULE: Provide a natural, conversational response. NEVER output markdown tables or long bulleted lists unless explicitly asked."
        
        ai_response_text = safe_ai_completion([{"role": "user", "content": route_prompt}], temp=0.2)
        
        return {
            "weather_report": ai_response_text,
            "weather_code": weather_waypoints[0]["code"] if weather_waypoints else 0,
            "telemetry": {"temperature_2m": weather_waypoints[0]["temp"] if weather_waypoints else 0},
            "location_name": f"Route: {origin_str.split(',')[0]} to {dest_str.split(',')[0]}",
            "lat": lat1,
            "lon": lon1,
            "is_route": True,
            "route_geometry": coords,
            "route_waypoints": weather_waypoints
        }
        
    except Exception as e:
        return {"weather_report": f"Error calculating route weather: {str(e)}", "weather_code": 0, "telemetry": {}}

@app.post("/weather")
async def post_live_weather(req: WeatherRequest, user: Optional[AuthenticatedUser] = Depends(optional_user)):
    user_id = owned_user_id(req.clerk_id, user)
    req = req.model_copy(update={"clerk_id": user_id})
    query = req.query
    lat = req.lat
    lon = req.lon
    language = req.language
    original_persona = req.persona
    routing = route_query(original_persona, query)
    domain = routing.get("domain", "general")
    persona = routing.get("persona", original_persona)
    health_profile = req.health_profile
    model = req.model
    history = req.history

    try:
        headers = {'User-Agent': 'VYOM/1.0'}
        city_name = "the selected location"

        # Specialized corridor intent must override an automatically attached current
        # map/location. This prevents flight/voyage/trek/disaster-corridor questions
        # from being incorrectly answered at the user's current coordinates or sent
        # through the road router.
        if domain in {"aviation", "marine", "adventure", "disaster"} and routing.get("route_intent") and query:
            parsed_route = extract_route_endpoints(query)
            if parsed_route:
                origin_str, dest_str = parsed_route
            else:
                route_extract_prompt = f"""Extract an origin and destination from this specialized environmental-corridor query: {query!r}.
Return exactly ROUTE: Origin | Destination if both are explicit. Otherwise return NONE. Do not add commentary."""
                route_raw = safe_ai_completion([{"role":"user","content":route_extract_prompt}], temp=0.0).strip().strip('"').strip("'")
                origin_str = dest_str = None
                if route_raw.upper().startswith("ROUTE:"):
                    route_parts = route_raw[6:].split("|")
                    if len(route_parts) >= 2:
                        origin_str, dest_str = route_parts[0].strip(), route_parts[1].strip()
            if origin_str and dest_str:
                if domain == "aviation":
                    return await process_aviation_route_weather(origin_str, dest_str, req, query, language, headers, routing)
                return await process_specialized_corridor_weather(domain, origin_str, dest_str, req, query, language, headers, routing)

        if lat is not None and lon is not None:
            try:
                headers['Accept-Language'] = 'en-US,en;q=0.9'
                rev_url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json&zoom=18"
                rev_res = requests.get(rev_url, headers=headers, timeout=5).json()
                feat_name = rev_res.get("name")
                addr = rev_res.get("address", {})
                if feat_name:
                    city_name = feat_name
                else:
                    city_name = (
                        addr.get("natural") or addr.get("peak") or addr.get("waterway") or
                        addr.get("tourism") or addr.get("city") or addr.get("town") or 
                        addr.get("village") or addr.get("county") or "the selected location"
                    )
            except Exception:
                city_name = "the selected location"
        else:
            if not query:
                return {"weather_report": "Please enter a location.", "weather_code": 0, "telemetry": {}}

            chat_context_str = "\n".join([f"{msg['role'].capitalize()}: {msg['content']}" for msg in history[-3:]]) if history else "None"
            extraction_prompt = f"Recent Chat History:\n{chat_context_str}\n\nAnalyze the following weather query: '{query}'. If this is a request for weather (current or forecast) at a SINGLE, specific location (inferring the location from context if needed), return ONLY a highly specific, fully-qualified GIS search string formatted as 'Location, State/Region, Country'. If the query involves a travel route or corridor (e.g., 'driving from A to B' or 'flight from X to Y'), return exactly 'ROUTE: Origin | Destination'. If the query is ambiguous, past climate, general meteorology, or complex without a specific location, return exactly 'COMPLEX'. If no location is found at all, return 'UNKNOWN'. Reply with nothing but the qualified location string, 'ROUTE: A | B', 'COMPLEX', or 'UNKNOWN'."
            
            raw_location = safe_ai_completion([{"role": "user", "content": extraction_prompt}], temp=0.1)
            extracted_location = raw_location.strip().strip('"').strip("'")

            if extracted_location.upper() in ["UNKNOWN", "COMPLEX"] or not extracted_location:
                fallback_response = fallback_web_search(query, history)
                return {"weather_report": fallback_response, "weather_code": 0, "telemetry": {}}

            if extracted_location.upper().startswith("ROUTE:"):
                route_parts = extracted_location[6:].split("|")
                if len(route_parts) >= 2:
                    origin_str = route_parts[0].strip()
                    dest_str = route_parts[1].strip()
                    if domain == "aviation":
                        return await process_aviation_route_weather(origin_str, dest_str, req, query, language, headers, routing)
                    if domain in {"marine", "adventure", "disaster"}:
                        return await process_specialized_corridor_weather(domain, origin_str, dest_str, req, query, language, headers, routing)
                    return await process_route_weather(origin_str, dest_str, query, language, persona, history, headers)
                else:
                    return {"weather_report": "Could not determine origin and destination for the route.", "weather_code": 0, "telemetry": {}}

            geo_url = "https://nominatim.openstreetmap.org/search"
            geo_params = {"q": extracted_location, "format": "json", "limit": 1}
            geo_res = requests.get(geo_url, headers=headers, params=geo_params).json()

            if not geo_res:
                simplified_query = extracted_location.split(',')[0].strip()
                geo_params = {"q": simplified_query, "format": "json", "limit": 1}
                geo_res = requests.get(geo_url, headers=headers, params=geo_params).json()

            if not geo_res:
                fallback_response = fallback_web_search(query, history)
                return {"weather_report": fallback_response, "weather_code": 0, "telemetry": {}}

            lat = float(geo_res[0]["lat"])
            lon = float(geo_res[0]["lon"])
            city_name = geo_res[0].get("name", extracted_location)

        active_health_profile = health_profile if persona == "General Citizen" else "None"
        selected_model_param = MODEL_MAPPING.get(model.lower(), "best_match")

        consensus_models = ["ecmwf_ifs025", "gfs_seamless", "ukmo_seamless", "icon_seamless"]
        model_temps = []
        for m_name in consensus_models:
            m_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m&models={m_name}"
            try:
                m_res = requests.get(m_url, timeout=3).json()
                t_val = m_res.get("current", {}).get("temperature_2m")
                if t_val is not None: model_temps.append(float(t_val))
            except Exception: pass

        temp_spread = round(statistics.stdev(model_temps), 2) if len(model_temps) > 1 else 0.0

        # Seasonally normalized day-of-year temperature anomaly.
        anomaly_index = 0.0
        anomaly_z = None
        try:
            clim = fetch_day_of_year_climatology(lat, lon)
            current_temp_sample = model_temps[0] if model_temps else None
            if clim.get("status") == "ok" and current_temp_sample is not None:
                anomaly = standardized_anomaly(current_temp_sample, clim.get("temperature_c", {}))
                anomaly_index = round(float(anomaly.get("anomaly") or 0.0), 1)
                anomaly_z = anomaly.get("z_score")
        except Exception:
            anomaly_index = 0.0
            anomaly_z = None

        weather_url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,surface_pressure,wind_speed_10m,precipitation,weather_code"
            f"&hourly=temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation,weather_code"
            f"&forecast_days=3&timezone=auto&models={selected_model_param}"
        )
        
        weather_res = requests.get(weather_url)
        
        if weather_res.status_code != 200 or not weather_res.json().get("current", {}).get("temperature_2m"):
            fallback_url = weather_url.replace(selected_model_param, "best_match")
            weather_res = requests.get(fallback_url)

        if weather_res.status_code != 200:
            return {"weather_report": "Sensors unreachable.", "weather_code": 0, "telemetry": {}, "location_name": city_name}
             
        data = weather_res.json()
        current_data = data.get("current")
        hourly_data = data.get("hourly", {})
        
        if not current_data:
            return {"weather_report": "No verified data available.", "weather_code": 0, "telemetry": {}, "location_name": city_name}

        # AQI Fetch
        aqi_data = {}
        try:
            aqi_url = f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lon}&current=pm10,pm2_5,uv_index"
            aqi_res = requests.get(aqi_url, timeout=3).json()
            if "current" in aqi_res:
                aqi_data = aqi_res["current"]
        except Exception as e:
            print("AQI Fetch Error:", e)
            
        # Advanced VYOM-MARINE Decision Engine
        # Heavy calculations live in vyom_marine_engine.py. The LLM only explains
        # its structured output and is never allowed to invent a sailing clearance.
        marine_data = {}
        if persona == "Marine/Fisherman":
            try:
                vessel_cfg = req.marine.dict(exclude_none=True) if req.marine else {}
                vessel_id = vessel_cfg.get("vessel_id")
                vessel_cfg.update(load_persisted_marine_state(req.clerk_id, vessel_id))
                marine_data = await run_in_threadpool(
                    build_marine_advisory,
                    lat,
                    lon,
                    vessel_cfg,
                    120,   # 5-day marine decision horizon
                    80,    # probabilistic coupled atmosphere-wave scenarios
                )
                if marine_data.get("status") == "ok":
                    saved = save_persisted_marine_state(req.clerk_id, vessel_id, marine_data)
                    marine_data["state_persistence"] = {
                        "saved": saved,
                        "vessel_id": vessel_id,
                        "requires": "clerk_id + marine.vessel_id + MongoDB" if not saved else None,
                    }
            except Exception as e:
                print("VYOM-MARINE Engine Error:", e)
                marine_data = {
                    "engine": "VYOM-MARINE", "version": "4.0.0", "status": "error",
                    "reason": str(e), "risk": {}, "confidence": {"overall": 0.0},
                }

        # Advanced VYOM-AVIATION Decision Engine
        aviation_data = {}
        if persona == "Aviation":
            try:
                aviation_cfg = req.aviation.dict(exclude_none=True) if req.aviation else {}
                aviation_data = await run_in_threadpool(build_aviation_advisory, lat, lon, aviation_cfg, 72, 120)
            except Exception as e:
                print("VYOM-AVIATION Engine Error:", e)
                aviation_data = {"engine":"VYOM-AVIATION","version":"1.1.0","status":"error","reason":str(e),"risk":{},"confidence":{"overall":0.0},"dispatch_clearance":None}

        # Aviation narration is strictly isolated from generic raw-weather narration.
        # The LLM receives only a reduced, validated VYOM VAYU payload.
        if persona == "Aviation":
            aviation_payload = build_aviation_narration_payload(aviation_data, query, city_name)
            aviation_messages = build_aviation_narrator_messages(aviation_payload, language)
            aviation_text = safe_ai_completion(aviation_messages, temp=0.1)
            valid_narration, narration_problems = validate_aviation_narration(aviation_text, aviation_payload)
            if (not aviation_text) or aviation_text.startswith("SYSTEM ERROR") or not valid_narration:
                aviation_text = deterministic_aviation_fallback(aviation_payload)
            return {
                "weather_report": aviation_text,
                "weather_code": current_data.get("weather_code", 0),
                "telemetry": {
                    "surface_weather": current_data,
                    "aviation": aviation_data,
                },
                "location_name": city_name,
                "alert": None,
                "lat": lat,
                "lon": lon,
                "engine_diagnostics": {
                    "engine": "VYOM VAYU",
                    "engine_version": aviation_data.get("version"),
                    "engine_executed": aviation_data.get("status") == "ok",
                    "modules_executed": (aviation_data.get("engine_diagnostics") or {}).get("modules_executed", []),
                    "sources": aviation_data.get("source_status") or {},
                    "engine_runtime_ms": aviation_data.get("elapsed_ms"),
                    "narration_validation_passed": valid_narration,
                    "validation_problems": narration_problems,
                    "strict_narration_guard": True,
                    "routing": routing,
                },
            }

        # Advanced VYOM-ADVENTURE Decision Engine
        adventure_data = {}
        if persona == "Adventurer":
            try:
                adventure_cfg = req.adventure.dict(exclude_none=True) if req.adventure else {}
                adventure_data = await run_in_threadpool(build_adventure_advisory, lat, lon, adventure_cfg, 72, 120)
            except Exception as e:
                print("VYOM-ADVENTURE Engine Error:", e)
                adventure_data = {"engine":"VYOM-ADVENTURE","version":"1.0.0","status":"error","reason":str(e),"risk":{},"confidence":{"overall":0.0},"safe_to_proceed":None}

        # Advanced VYOM-RAKSHA Disaster Management Engine
        disaster_data = {}
        disaster_live_sources = []
        if persona == "Disaster Management":
            try:
                disaster_cfg = req.disaster.dict(exclude_none=True) if req.disaster else {}
                if os.getenv("DISASTER_LIVE_CONTEXT_ON_QUERY", "1") == "1":
                    live = await run_in_threadpool(DisasterRealtimeService().collect_live_context, lat, lon, disaster_cfg)
                    disaster_live_sources = live.get("sources") or []
                    disaster_cfg = _merge_context_patch(disaster_cfg, live.get("patch") or {})
                    if live.get("dynamic_calibration_path"):
                        disaster_cfg["calibration_path"] = live["dynamic_calibration_path"]
                disaster_data = await run_in_threadpool(build_disaster_advisory, lat, lon, disaster_cfg, 120, 160)
                if disaster_live_sources:
                    disaster_data["realtime"] = {"enabled": True, "status": "ok", "sources": disaster_live_sources}
            except Exception as e:
                print("VYOM-RAKSHA Engine Error:", e)
                disaster_data = {"engine":"VYOM-RAKSHA","version":"1.0.0","status":"error","reason":str(e),"risk":{},"confidence":{"overall":0.0},"evacuation_order":None,"all_clear":None}

        # Advanced VYOM-AGRO Decision Engine
        # The LLM is NOT allowed to calculate agronomy. It only explains structured output.
        agro_data = {}
        if persona == "Farmer":
            try:
                field_cfg = req.agro.dict(exclude_none=True) if req.agro else {}
                field_id = field_cfg.get("field_id")
                field_cfg.update(load_persisted_agro_state(req.clerk_id, field_id))
                agro_data = await run_in_threadpool(
                    build_agro_advisory,
                    lat,
                    lon,
                    field_cfg,
                    120,   # 5-day decision horizon
                    64,    # probabilistic scenarios
                )
                if agro_data.get("status") == "ok":
                    saved = save_persisted_agro_state(req.clerk_id, field_id, agro_data)
                    agro_data["state_persistence"] = {
                        "saved": saved,
                        "field_id": field_id,
                        "requires": "clerk_id + agro.field_id + MongoDB" if not saved else None,
                    }
            except Exception as e:
                print("VYOM-AGRO Engine Error:", e)
                agro_data = {
                    "engine": "VYOM-AGRO",
                    "version": "2.0.0",
                    "status": "error",
                    "reason": str(e),
                    "risk": {},
                    "confidence": {"overall": 0.0},
                }

        # Strict narration guards for every specialized engine. The generic LLM path
        # below is now reserved for general/smart-city weather only.
        specialized_result = None
        if domain == "agro":
            specialized_result = agro_data
        elif domain == "marine":
            specialized_result = marine_data
        elif domain == "adventure":
            specialized_result = adventure_data
        elif domain == "disaster":
            specialized_result = disaster_data

        if domain in {"agro", "marine", "adventure", "disaster"} and isinstance(specialized_result, dict):
            extra_sources = disaster_live_sources if domain == "disaster" else None
            guarded_payload = build_engine_narration_payload(
                domain, specialized_result, query, city_name,
                extra_sources=extra_sources, routing=routing,
            )
            guarded_messages = build_engine_narrator_messages(domain, guarded_payload, language)
            guarded_text = safe_ai_completion(guarded_messages, temp=0.1)
            guarded_valid, guarded_problems = validate_engine_narration(domain, guarded_text, guarded_payload)
            if (not guarded_text) or guarded_text.startswith("SYSTEM ERROR") or not guarded_valid:
                guarded_text = deterministic_engine_fallback(domain, guarded_payload)
            return {
                "weather_report": guarded_text,
                "weather_code": current_data.get("weather_code", 0),
                "telemetry": {
                    "surface_weather": current_data,
                    domain: specialized_result,
                },
                "location_name": city_name,
                "alert": None,
                "lat": lat,
                "lon": lon,
                "engine_diagnostics": build_engine_diagnostics(
                    domain, specialized_result, guarded_payload,
                    guarded_valid, guarded_problems, routing,
                ),
            }

        elevation = data.get("elevation", 0)
        raw_pressure = current_data.get("surface_pressure", 1013.25)
        effective_oxygen = round(20.9 * (float(raw_pressure) / 1013.25), 1)
        wind_speed = float(current_data.get("wind_speed_10m", 0.0))
        precipitation = float(current_data.get("precipitation", 0.0))
        temperature = float(current_data.get("temperature_2m", 0.0))

        infra_risk = "Low"
        if wind_speed > 50 or precipitation > 20: infra_risk = "Critical"
        elif wind_speed > 30 or precipitation > 10: infra_risk = "High"

        travel_risk = "Low"
        if precipitation > 15 or temperature < -10 or elevation > 4000: travel_risk = "Critical"
        elif precipitation > 5 or temperature < 0: travel_risk = "High"

        health_risk = "Low"
        if elevation > 3500 or temperature < -15: health_risk = "Critical"
        elif elevation > 2000 or temperature > 40 or temperature < -5: health_risk = "High"
        
        # Beaufort scale calculation
        beaufort = 0
        if wind_speed >= 118: beaufort = 12
        elif wind_speed >= 103: beaufort = 11
        elif wind_speed >= 89: beaufort = 10
        elif wind_speed >= 75: beaufort = 9
        elif wind_speed >= 62: beaufort = 8
        elif wind_speed >= 50: beaufort = 7
        elif wind_speed >= 39: beaufort = 6
        elif wind_speed >= 29: beaufort = 5
        elif wind_speed >= 20: beaufort = 4
        elif wind_speed >= 12: beaufort = 3
        elif wind_speed >= 6: beaufort = 2
        elif wind_speed >= 1: beaufort = 1

        current_data.update({
            "elevation": elevation, "effective_oxygen": effective_oxygen, 
            "model_spread": temp_spread, "climate_anomaly": anomaly_index, "climate_anomaly_z": anomaly_z,
            "risk_matrix": {"infrastructure": infra_risk, "travel": travel_risk, "health": health_risk},
            "hourly": hourly_data,
            "aqi": aqi_data,
            "marine": marine_data,
            "agro": agro_data,
            "aviation": aviation_data,
            "adventure": adventure_data,
            "disaster": disaster_data
        })

        active_persona = persona
        active_guideline = PERSONA_GUIDELINES.get(persona, "General overview.")
        if elevation > 3500:
            active_persona = "High-Altitude Outdoor Safety"
            active_guideline = "CRITICAL: High-altitude environmental exposure. Use conservative cold, wind, weather and altitude-risk screening; do not encourage technical or hazardous travel."

        messages = [
            {"role": "system", "content": f"You are VYOM's explanation layer for weather decision support. Distinguish model forecasts from observations and official authority bulletins.\n\nMANDATORY RULES:\n1. STRICT LANGUAGE ENFORCEMENT: Answer in {language}.\n2. PHYSICS OVERRIDE: If temp < 0°C, interpret precipitation as snow/ice.\n3. SEVERE WEATHER ALERT: If severe, start with [ALERT: <Urgent warning>]\n4. Direct Answer: Mention {city_name} in sentence 1.\n5. WEATHER ONLY: NEVER provide history, political data, or general geographic facts about the place. ONLY discuss the current and forecasted weather conditions, telemetry, and actionable advice.\n6. AIR QUALITY WARNINGS: If PM2.5, PM10, or UV Index in Current Telemetry's aqi field is high, and the user's Biometric Profile is 'Asthma / Respiratory', prioritize targeted severe health warnings for their airways. Include AQI context if relevant to the profile or persona (e.g. urban environments).\n7. MARINE DECISION CONTRACT: If Target Stakeholder is 'Marine/Fisherman', use ONLY structured VYOM-MARINE/Ocean Guardian-X telemetry for marine risk. Never recompute vessel safety from raw wave height alone. Keep risk and confidence separate. NEVER say a vessel is 'safe to sail', never invent a navigation clearance, never downgrade an official warning, and never convert generic model sea-level/current fields into coastal navigation or under-keel-clearance advice. Discuss CPA/TCPA, UKC, cyclone, sea-ice, route, RAO or extreme-wave outputs only when the engine marks that module available and the required trusted inputs were supplied. Explain screening risks rather than issuing helm/course commands.\n8. AGRO DECISION CONTRACT: If Target Stakeholder is 'Farmer', use ONLY the structured VYOM-AGRO telemetry for crop/soil risk and irrigation decisions. NEVER recompute agronomy from raw ET0/rain. Keep risk and confidence separate. If irrigation.liters_per_acre_authoritative is null, DO NOT present any exact irrigation volume as authoritative; explain the returned planning range and what field data is missing. Never diagnose a crop disease or invent a pesticide/product/dose from generic disease-conducive weather.\n9. AVIATION DECISION CONTRACT: Aviation responses are handled by the dedicated VYOM VAYU guarded narrator and must not be regenerated from generic telemetry. Never issue or imply flight clearance.\n10. ADVENTURE DECISION CONTRACT: If Target Stakeholder is 'Adventurer', use ONLY structured VYOM-ADVENTURE telemetry for outdoor environmental risk. Never certify a route or activity as safe, and never provide operational instructions for technical climbing, avalanche travel, river crossings, wildfire escape, or other hazardous maneuvers. Treat official local warnings and avalanche/land-management bulletins as higher priority than model output. Keep risk, confidence and data quality separate.\n11. DISASTER DECISION CONTRACT: If Target Stakeholder is 'Disaster Management', use ONLY structured VYOM-RAKSHA telemetry for multi-hazard assessment. Official CAP/agency warnings and evacuation/all-clear orders always outrank model output. Never invent an evacuation order, shelter selection, rescue dispatch, all-clear, earthquake prediction or tsunami evacuation decision. Treat USGS/GDACS/FIRMS/GloFAS/model outputs as context with their documented limitations, keep hazard probability, impact risk, confidence and data quality separate, and state when local exposure, terrain, gauge or authority information is missing.\n12. NO CLUTTER: Provide a natural, conversational paragraph. NEVER output large Markdown tables, hour-by-hour forecast lists, or verbose bullet structures unless explicitly requested by the user."}
        ]
        
        for msg in history[-4:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
            
        wc = current_data.get('weather_code', 0)
        conditions_map = {
            0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
            45: "Foggy", 48: "Depositing rime fog", 51: "Light drizzle", 53: "Moderate drizzle",
            55: "Dense drizzle", 61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
            71: "Slight snowfall", 73: "Moderate snowfall", 75: "Heavy snowfall",
            77: "Snow grains", 80: "Slight rain showers", 81: "Moderate rain showers",
            82: "Violent rain showers", 85: "Slight snow showers", 86: "Heavy snow showers",
            95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail"
        }
        condition_str = conditions_map.get(wc, "Variable conditions")
            
        current_context = f"""Natural Language Query: "{query}"
Detected Location: {city_name} (Coordinates: {lat}, {lon}). Elevation: {elevation} meters.
Model Spread: ±{temp_spread}°C. Climate Baseline Anomaly: {anomaly_index}°C.
Current Condition: {condition_str} (WMO Code: {wc})
Current Telemetry: {current_data}.
Target Stakeholder: {active_persona} (Guideline: {active_guideline}).
Biometric Profile: {active_health_profile}."""
        messages.append({"role": "user", "content": current_context})

        ai_response_text = safe_ai_completion(messages, temp=0.2)

        # Fallback if AI response is empty or an error message
        if not ai_response_text or ai_response_text.strip() == "" or ai_response_text.startswith("SYSTEM ERROR"):
            # First try web search fallback for complex queries
            web_fallback = fallback_web_search(query)
            if web_fallback and not web_fallback.startswith("I searched the web") and not web_fallback.startswith("I couldn't process"):
                ai_response_text = web_fallback + f"\n\n*Live Telemetry Context: {city_name} is currently at {temperature}°C.*"
            else:
                wc = current_data.get('weather_code', 0)
                conditions_map = {
                    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
                    45: "Foggy", 48: "Depositing rime fog", 51: "Light drizzle", 53: "Moderate drizzle",
                    55: "Dense drizzle", 61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
                    71: "Slight snowfall", 73: "Moderate snowfall", 75: "Heavy snowfall",
                    77: "Snow grains", 80: "Slight rain showers", 81: "Moderate rain showers",
                    82: "Violent rain showers", 85: "Slight snow showers", 86: "Heavy snow showers",
                    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail"
                }
                condition = conditions_map.get(wc, "Variable conditions")
                ai_response_text = (
                    f"**Current Weather in {city_name}:**\n\n"
                    f"**Conditions:** {condition}\n"
                    f"**Temperature:** {temperature}°C\n"
                    f"**Wind Speed:** {wind_speed} km/h\n"
                    f"**Humidity:** {current_data.get('relative_humidity_2m', 'N/A')}%\n"
                    f"**Pressure:** {raw_pressure} hPa\n"
                    f"**Elevation:** {elevation}m\n\n"
                    f"*AI analysis is temporarily unavailable. The telemetry data above reflects live conditions.*"
                )

        alert_text = None
        if "[ALERT:" in ai_response_text:
            try:
                alert_text = ai_response_text.split("[ALERT:")[1].split("]")[0].strip()
                ai_response_text = ai_response_text.replace(f"[ALERT: {alert_text}]", "").replace(f"[ALERT:{alert_text}]", "").strip()
            except: pass

        return {
            "weather_report": ai_response_text,
            "weather_code": current_data.get("weather_code", 0),
            "telemetry": current_data,
            "location_name": city_name,
            "alert": alert_text,
            "lat": lat, # Return coords so frontend can sync them to Mongo
            "lon": lon
        }
    except Exception as e:
        return {"weather_report": f"Error processing telemetry: {str(e)}", "weather_code": 0, "telemetry": {}}

@app.get("/pdf-brief")
def generate_executive_pdf(
    location: str = "Unknown", temp: float = 0.0, humidity: float = 0.0, wind: float = 0.0,
    pressure: float = 1013.0, elevation: float = 0.0, oxygen: float = 20.9, spread: float = 0.0,
    anomaly: float = 0.0, infra_risk: str = "Low", travel_risk: str = "Low", health_risk: str = "Low",
    model: str = "Best", persona: str = "General Citizen", report: str = "No report generated.",
    pm25: float = None, pm10: float = None, uv: float = None
):
    pdf_buffer = io.BytesIO()
    doc = SimpleDocTemplate(pdf_buffer, pagesize=letter, rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40)
    story = []
    styles = getSampleStyleSheet()

    # Try to load Nirmala UI font for Indian languages support
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase.pdfmetrics import registerFontFamily
    import os
    
    font_regular = 'Helvetica'
    font_bold = 'Helvetica-Bold'
    
    try:
        if os.path.exists('C:/Windows/Fonts/Nirmala.ttc'):
            pdfmetrics.registerFont(TTFont('Nirmala', 'C:/Windows/Fonts/Nirmala.ttc', subfontIndex=0))
            pdfmetrics.registerFont(TTFont('Nirmala-Bold', 'C:/Windows/Fonts/Nirmala.ttc', subfontIndex=1))
            registerFontFamily('Nirmala', normal='Nirmala', bold='Nirmala-Bold', italic='Nirmala', boldItalic='Nirmala-Bold')
            font_regular = 'Nirmala'
            font_bold = 'Nirmala-Bold'
    except Exception as e:
        print("Font registration error:", e)

    title_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontSize=15, textColor=colors.HexColor('#0f172a'), spaceAfter=4, fontName=font_bold)
    subtitle_style = ParagraphStyle('SubTitleStyle', parent=styles['Normal'], fontSize=8.5, textColor=colors.HexColor('#64748b'), spaceAfter=12, fontName=font_regular)
    heading_style = ParagraphStyle('HeadingStyle', parent=styles['Heading2'], fontSize=10.5, textColor=colors.HexColor('#0d9488'), spaceBefore=10, spaceAfter=4, fontName=font_bold)
    body_style = ParagraphStyle('BodyStyle', parent=styles['Normal'], fontSize=8.5, textColor=colors.HexColor('#334155'), spaceAfter=6, leading=12, fontName=font_regular)

    story.append(Paragraph("VYOM — WEATHER DECISION-SUPPORT BRIEF", title_style))
    story.append(Paragraph(f"Timestamp: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')} | VYOM decision support", subtitle_style))
    story.append(Spacer(1, 2))

    story.append(Paragraph("1. TARGET LOCATION & TOPOGRAPHY", heading_style))
    loc_data = [[Paragraph("<b>Location:</b>", body_style), Paragraph(location, body_style), Paragraph("<b>Elevation:</b>", body_style), Paragraph(f"{elevation} m", body_style)]]
    t1 = Table(loc_data, colWidths=[90, 180, 90, 180])
    t1.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#f8fafc')), ('PADDING', (0,0), (-1,-1), 5), ('BOX', (0,0), (-1,-1), 0.5, colors.HexColor('#cbd5e1'))]))
    story.append(t1)
    story.append(Spacer(1, 6))

    story.append(Paragraph("2. ATMOSPHERIC TELEMETRY", heading_style))
    tel_data = [
        [Paragraph("<b>Temperature:</b>", body_style), Paragraph(f"{temp}°C", body_style), Paragraph("<b>Infrastructure Risk:</b>", body_style), Paragraph(infra_risk, body_style)],
        [Paragraph("<b>Wind Speed:</b>", body_style), Paragraph(f"{wind} km/h", body_style), Paragraph("<b>Climate Anomaly:</b>", body_style), Paragraph(f"{anomaly}°C", body_style)]
    ]
    if pm25 is not None and pm10 is not None:
        tel_data.append([Paragraph("<b>PM2.5:</b>", body_style), Paragraph(f"{pm25} µg/m³", body_style), Paragraph("<b>PM10:</b>", body_style), Paragraph(f"{pm10} µg/m³", body_style)])
    if uv is not None:
        tel_data.append([Paragraph("<b>UV Index:</b>", body_style), Paragraph(f"{uv}", body_style), Paragraph("", body_style), Paragraph("", body_style)])
    t2 = Table(tel_data, colWidths=[90, 180, 90, 180])
    t2.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#f8fafc')), ('PADDING', (0,0), (-1,-1), 5), ('BOX', (0,0), (-1,-1), 0.5, colors.HexColor('#cbd5e1'))]))
    story.append(t2)
    story.append(Spacer(1, 6))

    story.append(Paragraph(f"3. VYOM STAKEHOLDER ADVISORY", heading_style))
    
    # Clean markdown and problematic characters
    clean_report = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', report) # Bold
    clean_report = re.sub(r'\*(.*?)\*', r'<i>\1</i>', clean_report) # Italic
    clean_report = clean_report.replace('•', '-') # Fix black dots from bullets
    clean_report = re.sub(r'[#`~]', '', clean_report) # Remove leftover markdown
    clean_report = clean_report.replace('\n', '<br/>')
    
    story.append(Paragraph(clean_report, body_style))

    doc.build(story)
    pdf_buffer.seek(0)
    return StreamingResponse(pdf_buffer, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=VYOM_Briefing_{location.replace(' ', '_')}.pdf"})

VOICE_MAPPING = {
    "English": "en-IN-NeerjaExpressiveNeural", 
    "Hindi": "hi-IN-SwaraNeural",
    "Bengali": "bn-IN-TanishaaNeural",
    "Telugu": "te-IN-ShrutiNeural",
    "Tamil": "ta-IN-PallaviNeural",
    "Marathi": "mr-IN-AarohiNeural",
    "Gujarati": "gu-IN-DhwaniNeural",
    "Kannada": "kn-IN-SapnaNeural",
    "Malayalam": "ml-IN-SobhanaNeural",
    "Punjabi": "hi-IN-SwaraNeural", 
    "Odia": "hi-IN-SwaraNeural"     
}

@app.get("/speak")
async def text_to_speech(text: str, language: str = "English"):
    try:
        # Prompt AI to summarize into a short, natural voice response in the correct language
        summary_prompt = f"Summarize the following weather report into 2-3 natural spoken sentences for a voice assistant. Keep the summary and the very important parts only. You MUST respond strictly in the {language} language. Do NOT include markdown, lists, or symbols. Text: {text}"
        
        # Run synchronous Groq/Mistral AI function in threadpool
        summary_text = await run_in_threadpool(
            safe_ai_completion, 
            [{"role": "user", "content": summary_prompt}], 
            0.3
        )
        
        if not summary_text or summary_text.startswith("SYSTEM ERROR"):
            summary_text = text # fallback
            
        clean_text = re.sub(r'[*#_`~>-]', '', summary_text)
        clean_text = re.sub(r'\s+', ' ', clean_text).strip()
        
        voice = VOICE_MAPPING.get(language, "en-IN-NeerjaExpressiveNeural")
        communicate = edge_tts.Communicate(clean_text, voice)
        
        async def audio_stream():
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    yield chunk["data"]
                    
        return StreamingResponse(audio_stream(), media_type="audio/mpeg")
    except Exception as e:
        print(f"TTS Error: {e}")
        return {"error": "Audio synthesis failed."}

import asyncio

global_loop = None

@app.on_event("startup")
async def start_periodic_scan():
    global global_loop
    global_loop = asyncio.get_running_loop()
    async def periodic_task():
        while True:
            await asyncio.sleep(600)  # wait 10 mins (600 seconds)
            try:
                await asyncio.to_thread(scan_and_alert_users)
            except Exception as e:
                print(f"Periodic scan error: {e}")
                
    asyncio.create_task(periodic_task())

    if os.getenv("VYOM_MARINE_BACKGROUND_WORKER", "0") == "1":
        async def marine_data_task():
            svc = MarineRealtimeService()
            interval = max(900, int(os.getenv("MARINE_WORKER_INTERVAL_SEC", "3600")))
            while True:
                await asyncio.sleep(interval)
                try:
                    locations_raw = os.getenv("MARINE_WORKER_LOCATIONS_JSON", "[]")
                    import json as _json
                    if os.path.exists(locations_raw):
                        with open(locations_raw, "r", encoding="utf-8") as _f:
                            locations = _json.load(_f)
                    else:
                        locations = _json.loads(locations_raw) if locations_raw.strip().startswith("[") else []
                    for loc in locations:
                        await asyncio.to_thread(svc.collect_live_context, float(loc["lat"]), float(loc["lon"]), {})
                    await asyncio.to_thread(svc.verify_and_calibrate, int(os.getenv("CAL_MATCH_LOOKBACK_DAYS", "10")))
                except Exception as e:
                    print(f"Marine data worker warning: {e}")
        asyncio.create_task(marine_data_task())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
