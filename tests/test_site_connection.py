import ast
import os
from pathlib import Path
import sys
import types
import unittest
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load the actual GitHub request models without starting Mongo/LLM/feed services.
tree = ast.parse((ROOT / "main.py").read_text())
names = {"AgroFieldRequest", "MarineVesselRequest", "AviationAircraftRequest", "AdventureActivityRequest", "DisasterContextRequest", "WeatherRequest"}
nodes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name in names]
namespace = dict(BaseModel=BaseModel, Field=Field, Any=Any, Dict=Dict, List=List, Optional=Optional)
exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py request models", "exec"), namespace)
fake_main = types.ModuleType("main")
fake_main.WeatherRequest = namespace["WeatherRequest"]
calls = []

async def advisory(req):
    calls.append(req.model_dump())
    return {"weather_report": "Supplied engine report", "lat": req.lat, "lon": req.lon, "telemetry": {"engine": {"status": "ok"}}}

fake_main.post_live_weather = advisory
sys.modules["main"] = fake_main
import site_api
from site_forecast import chart_forecast

async def forecast(lat, lon, include_marine):
    return {"hourly": {"time": ["2026-10-10T18:00"], "temperature_2m": [30]}, "utc_offset_seconds": 0}

site_api.collect_chart_forecast = forecast

class ConnectionTests(unittest.TestCase):
    def setUp(self):
        os.environ["VYOM_SITE_SERVICE_TOKEN"] = "test-only-credential-with-at-least-32-characters"
        calls.clear()
        self.client = TestClient(site_api.app)
        self.body = {"query": "Inspect conditions", "persona": "Farmer", "lat": 22.57, "lon": 88.36}
        self.headers = {"Authorization": "Bearer " + os.environ["VYOM_SITE_SERVICE_TOKEN"]}

    def test_protected_weather_reuses_actual_models_and_preserves_report(self):
        for persona in site_api.PERSONAS:
            res = self.client.post("/weather", json={**self.body, "persona": persona, "agro": {"crop": "rice"}, "clerk_id": "another-user", "health_profile": "Asthma", "history": [{"role": "user", "content": "private"}]}, headers=self.headers)
            self.assertEqual(res.status_code, 200, res.text)
            self.assertEqual(res.json()["weather_report"], "Supplied engine report")
            self.assertEqual(res.json()["frontend_forecast"]["utc_offset_seconds"], 0)
            self.assertEqual(calls[-1]["persona"], persona)
            self.assertEqual(calls[-1]["agro"]["crop"], "rice")
            self.assertIsNone(calls[-1]["clerk_id"])
            self.assertEqual(calls[-1]["history"], [])
            self.assertEqual(calls[-1]["health_profile"], "None")

    def test_no_token_or_wrong_token_cannot_invoke_engines(self):
        for headers in [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic token"}]:
            res = self.client.post("/weather", json=self.body, headers=headers)
            self.assertEqual(res.status_code, 401)
        self.assertEqual(calls, [])

    def test_missing_configuration_and_invalid_requests(self):
        os.environ["VYOM_SITE_SERVICE_TOKEN"] = ""
        self.assertEqual(self.client.get("/health").status_code, 503)
        os.environ["VYOM_SITE_SERVICE_TOKEN"] = self.headers["Authorization"][7:]
        self.assertEqual(self.client.get("/health").json()["status"], "ok")
        for bad in [{"lat": 91}, {"lon": None}, {"persona": "Road"}, {"query": ""}]:
            self.assertEqual(self.client.post("/weather", json={**self.body, **bad}, headers=self.headers).status_code, 422)

    def test_original_user_alert_and_admin_surfaces_are_not_exposed(self):
        for path in ["/api/user/profile", "/api/alerts/config", "/api/alerts/trigger", "/docs", "/openapi.json"]:
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_wave_alignment_and_missingness(self):
        surface = {"hourly": {"time": ["a", "b", "c"], "temperature_2m": [10, None, 12]}}
        marine = {"hourly": {"time": ["b", "c"], "wave_height": [1.2, None]}}
        result = chart_forecast(surface, marine)
        self.assertEqual(result["hourly"]["wave_height"], [None, 1.2, None])
        self.assertEqual(result["hourly"]["temperature_2m"], [10, None, 12])
        self.assertNotIn("visibility", result["hourly"])
        self.assertEqual(chart_forecast({})["status"], "unavailable")

if __name__ == "__main__":
    unittest.main()
