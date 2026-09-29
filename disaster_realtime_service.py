"""Realtime feed orchestration and continuous calibration for VYOM-RAKSHA."""
from __future__ import annotations
import os, time
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from disaster_connectors import (
    USGSEarthquakeConnector, GDACSConnector, OpenMeteoHazardConnector,
    NASAFIRMSConnector, IMDIndiaConnector, NWSAlertsConnector, CAPFeedConnector,
    ConfiguredTsunamiWarningConnector, ConfiguredLocalImpactConnector, ConfiguredObservationConnector,
)
from disaster_data_fabric import DisasterDataFabric


class DisasterRealtimeService:
    def __init__(self):
        self.fabric = DisasterDataFabric()
        self.usgs = USGSEarthquakeConnector(); self.gdacs = GDACSConnector(); self.env = OpenMeteoHazardConnector()
        self.firms = NASAFIRMSConnector(); self.imd = IMDIndiaConnector(); self.nws = NWSAlertsConnector()
        self.cap = CAPFeedConnector(); self.tsunami = ConfiguredTsunamiWarningConnector(); self.local = ConfiguredLocalImpactConnector(); self.observations = ConfiguredObservationConnector()
        self.calibration_path = os.getenv("VYOM_DISASTER_DYNAMIC_CALIBRATION", "data/disaster/calibration/dynamic_calibration.json")

    def _merge(self, out: Dict[str, Any], result: Dict[str, Any], source_name: str):
        patch = result.get("patch") or {}; outpatch = out.setdefault("patch", {})
        for k, v in patch.items():
            if isinstance(v, list):
                outpatch.setdefault(k, []); outpatch[k].extend(v)
            elif isinstance(v, dict):
                if k not in outpatch: outpatch[k] = v
                elif isinstance(outpatch[k], dict): outpatch[k].update(v)
            elif outpatch.get(k) is None:
                outpatch[k] = v
        srcs = result.get("sources") or []; out.setdefault("sources", []).extend(srcs)
        status = "ok" if any(s.get("status") == "ok" for s in srcs) else "degraded"
        err = "; ".join(str(s.get("error")) for s in srcs if s.get("error"))
        records = sum(int(s.get("records", 0) or 0) for s in srcs)
        self.fabric.log_source_status(source_name, status, result.get("elapsed_ms", 0), records, err)

    def _persist_events(self, patch: Mapping[str, Any]):
        for e in patch.get("earthquake_events") or []:
            key = f'USGS:{e.get("id") or e.get("time") or e.get("place")}'
            self.fabric.upsert_hazard_event(key, "USGS", "earthquake", str(e.get("alert") or ""), e.get("lat"), e.get("lon"), e.get("time"), None, e.get("status") or "observed", e)
        for e in patch.get("gdacs_events") or []:
            key = f'GDACS:{e.get("event_type")}:{e.get("event_id") or e.get("name")}'
            self.fabric.upsert_hazard_event(key, "GDACS", str(e.get("event_type") or "hazard"), str(e.get("alertlevel") or ""), e.get("lat"), e.get("lon"), e.get("fromdate"), e.get("todate"), "active", e)
        for w in (patch.get("official_warnings") or []) + (patch.get("official_tsunami_warnings") or []):
            key = str(w.get("identifier") or w.get("id") or f'{w.get("source")}:{w.get("event")}:{w.get("effective")}')
            self.fabric.upsert_official_warning(key, str(w.get("source") or w.get("authority") or "unknown"), str(w.get("event") or w.get("headline") or "warning"), str(w.get("severity") or ""), str(w.get("certainty") or ""), str(w.get("urgency") or ""), w.get("sent") or w.get("issued_at"), w.get("effective"), w.get("expires"), w.get("geometry") if isinstance(w.get("geometry"), dict) else {}, w)

    def collect_live_context(self, lat: float, lon: float, context: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        started = time.time(); out = {"patch": {}, "sources": [], "status": "ok"}; c = dict(context or {})
        connectors = [self.env, self.usgs, self.gdacs, self.firms, self.cap, self.tsunami, self.local]
        country = str(c.get("country_code") or "").upper()
        if os.getenv("DISASTER_ENABLE_IMD", "1") == "1" and (country in {"", "IN"}): connectors.append(self.imd)
        if os.getenv("DISASTER_ENABLE_NWS", "0") == "1" or country == "US": connectors.append(self.nws)
        for conn in connectors:
            try: self._merge(out, conn.fetch(lat, lon), conn.name)
            except Exception as exc:
                out["sources"].append({"source": conn.name, "status": "error", "error": str(exc)})
                self.fabric.log_source_status(conn.name, "error", error=str(exc))
        try:
            obs_result = self.observations.fetch(lat, lon)
            out["sources"].extend(obs_result.get("sources") or [])
            self.ingest_observation_batch(obs_result.get("observations") or [])
            status = "ok" if any(x.get("status")=="ok" for x in obs_result.get("sources") or []) else "degraded"
            self.fabric.log_source_status(self.observations.name, status, obs_result.get("elapsed_ms",0), len(obs_result.get("observations") or []), "")
        except Exception as exc:
            out["sources"].append({"source":"Configured observations","status":"error","error":str(exc)})
        if any(s.get("status") == "error" for s in out["sources"]): out["status"] = "degraded"
        out["patch"]["source_health_ok"] = out["status"] == "ok"
        self._persist_events(out["patch"])
        out["elapsed_ms"] = round((time.time()-started)*1000, 1)
        out["dynamic_calibration_path"] = self.calibration_path if os.path.exists(self.calibration_path) else None
        return out

    def record_model_forecast(self, lat: float, lon: float, members):
        now = datetime.now(timezone.utc); count = 0
        vars_ = ("temperature_2m", "relative_humidity_2m", "precipitation", "wind_speed_10m", "wind_gusts_10m", "surface_pressure", "soil_moisture_0_to_1cm")
        for m in members:
            for row in m.hours[:168]:
                lead = int(row.get("lead_hours", 0)); valid = row.get("time")
                for var in vars_:
                    val = row.get(var)
                    if val is None: continue
                    self.fabric.add_forecast(m.model, now.isoformat(), str(valid), lead, lat, lon, var, float(val), quality=m.quality)
                    count += 1
        return count

    def ingest_observation_batch(self, observations):
        count = 0
        for o in observations or []:
            try:
                ok = self.fabric.add_observation(str(o.get("source") or "local"), str(o.get("station_id") or "unknown"), float(o["lat"]), float(o["lon"]), str(o["observed_at"]), str(o["variable"]), float(o["value"]), str(o.get("unit") or ""), float(o.get("quality", .8)), o.get("metadata") or {})
                count += int(bool(ok))
            except Exception: pass
        return count

    def collect_forecasts_for_calibration(self, lat: float, lon: float, horizon_hours: int = 120):
        from vyom_disaster_engine import CalibrationStore, fetch_model_member, DEFAULT_MODELS
        cal = CalibrationStore.from_json(self.calibration_path if os.path.exists(self.calibration_path) else None)
        members = []
        for model in DEFAULT_MODELS:
            m = fetch_model_member(lat, lon, model, horizon_hours, cal)
            if m: members.append(m)
        return {"models": [m.model for m in members], "forecast_records": self.record_model_forecast(lat, lon, members)}

    def verify_and_calibrate(self, lookback_days: int = 30):
        matches = self.fabric.match_forecasts(lookback_days=lookback_days)
        cells = self.fabric.recompute_calibration(min_samples=int(os.getenv("DISASTER_CAL_MIN_SAMPLES", "5")))
        path = self.fabric.export_calibration_json(self.calibration_path)
        return {"status":"ok", "matches_created":matches, "calibration_cells":cells, "calibration_path":path}

    def health(self): return self.fabric.health()


__all__ = ["DisasterRealtimeService"]
