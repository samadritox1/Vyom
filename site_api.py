"""Protected weather-only entry point for the private VYOM website.

Run uvicorn site_api:app. This reuses main.py's engines and guarded narrator,
without exposing profile/alert/admin routes or starting its notification loop.
"""
import asyncio
import hmac
import math
import os
from fastapi import FastAPI, Header, HTTPException
from main import WeatherRequest, post_live_weather
from site_forecast import collect_chart_forecast

app = FastAPI(title="VYOM Private Site API", docs_url=None, redoc_url=None, openapi_url=None)
PERSONAS = {"Farmer", "Marine/Fisherman", "Aviation", "Disaster Management", "Smart City Planner", "Adventurer"}


@app.get("/health")
def health():
    configured = len(os.getenv("VYOM_SITE_SERVICE_TOKEN", "")) >= 32
    if not configured:
        raise HTTPException(status_code=503, detail="Weather service credential is not configured")
    return {"status": "ok", "service": "VYOM"}


def authenticate(authorization):
    token = os.getenv("VYOM_SITE_SERVICE_TOKEN", "")
    if len(token) < 32:
        raise HTTPException(status_code=503, detail="Weather service credential is not configured")
    supplied = (authorization or "").removeprefix("Bearer ")
    if not authorization or not authorization.startswith("Bearer ") or not hmac.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(status_code=401, detail="Service authentication required")


@app.post("/weather")
async def weather(req: WeatherRequest, authorization: str = Header(default="")):
    authenticate(authorization)
    if req.lat is None or req.lon is None or not math.isfinite(req.lat) or not math.isfinite(req.lon) or abs(req.lat) > 90 or abs(req.lon) > 180:
        raise HTTPException(status_code=422, detail="Valid latitude and longitude are required")
    if req.persona not in PERSONAS or not req.query.strip() or len(req.query) > 4000:
        raise HTTPException(status_code=422, detail="Valid perspective and question are required")
    # This surface never reads or updates another user's state or sends body measurements.
    req.clerk_id = None
    req.history = []
    req.health_profile = "None"
    chart_task = asyncio.create_task(collect_chart_forecast(req.lat, req.lon, req.persona == "Marine/Fisherman"))
    try:
        result = await post_live_weather(req)
        if not isinstance(result, dict):
            raise HTTPException(status_code=502, detail="Invalid weather response")
        if result.get("is_route"):
            raise HTTPException(status_code=422, detail="Select a single location for this website")
        result["frontend_forecast"] = await chart_task
        return result
    finally:
        if not chart_task.done():
            chart_task.cancel()
