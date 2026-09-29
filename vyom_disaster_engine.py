"""
VYOM-RAKSHA — probabilistic multi-hazard disaster-management decision engine.

Research / decision-support only. This module does not replace official emergency
management authorities, warning centres, evacuation orders, incident command,
certified engineering assessments, or emergency services.

Core design rules
-----------------
1. Authoritative warnings and orders outrank model-derived convenience guidance.
2. Risk, confidence, data quality and uncertainty are separate quantities.
3. Missing exposure/vulnerability/local-observation data downgrades precision.
4. Earthquakes are observed, not predicted by this engine.
5. Tsunami evacuation/all-clear decisions are never inferred from a generic
   earthquake magnitude; official tsunami-warning information is required.
6. Satellite fire detections and coarse global flood models are context, not
   life-safety clearance.
7. The LLM may explain structured outputs but must not recompute or override them.
"""
from __future__ import annotations

import json
import math
import os
import random
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import requests

ENGINE_NAME = "VYOM-RAKSHA"
ENGINE_VERSION = "1.0.0"
DEFAULT_MODELS = ("ecmwf_ifs025", "gfs_seamless", "icon_seamless", "gem_seamless")
MODEL_PRIOR_QUALITY = {
    "ecmwf_ifs025": 0.87,
    "gfs_seamless": 0.82,
    "icon_seamless": 0.80,
    "gem_seamless": 0.77,
    "best_match": 0.78,
}


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


def safe_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if v is None or isinstance(v, bool):
            return default
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def logistic(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, float(x)))))


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2.0 * math.asin(math.sqrt(a))


def season_key(dt: datetime, lat: float = 0.0) -> str:
    m = dt.month
    if m in (12, 1, 2):
        s = "DJF"
    elif m in (3, 4, 5):
        s = "MAM"
    elif m in (6, 7, 8):
        s = "JJA"
    else:
        s = "SON"
    if lat < 0:
        s = {"DJF": "JJA", "JJA": "DJF", "MAM": "SON", "SON": "MAM"}[s]
    return s


def lead_bin(h: int) -> str:
    h = int(h)
    if h <= 6: return "0_6"
    if h <= 24: return "7_24"
    if h <= 72: return "25_72"
    if h <= 120: return "73_120"
    return "121_plus"


def weighted_mean(values: Sequence[Optional[float]], weights: Sequence[float]) -> Optional[float]:
    pairs = [(float(v), max(0.0, float(w))) for v, w in zip(values, weights) if v is not None]
    sw = sum(w for _, w in pairs)
    if not pairs or sw <= 0: return None
    return sum(v * w for v, w in pairs) / sw


def weighted_std(values: Sequence[Optional[float]], weights: Sequence[float]) -> float:
    mu = weighted_mean(values, weights)
    if mu is None: return 0.0
    pairs = [(float(v), max(0.0, float(w))) for v, w in zip(values, weights) if v is not None]
    sw = sum(w for _, w in pairs)
    if sw <= 0: return 0.0
    return math.sqrt(sum(w * (v - mu) ** 2 for v, w in pairs) / sw)


def circular_diff_deg(a: float, b: float) -> float:
    return (float(a) - float(b) + 180.0) % 360.0 - 180.0


def rolling_sum(rows: Sequence[Mapping[str, Any]], key: str, end_idx: int, hours: int) -> float:
    start = max(0, end_idx - hours + 1)
    return sum(max(0.0, safe_float(rows[j].get(key), 0.0) or 0.0) for j in range(start, end_idx + 1))


def wet_bulb_stull_c(t_c: float, rh: float) -> float:
    rh = clamp(rh, 1.0, 100.0)
    return (
        t_c * math.atan(0.151977 * math.sqrt(rh + 8.313659))
        + math.atan(t_c + rh)
        - math.atan(rh - 1.676331)
        + 0.00391838 * (rh ** 1.5) * math.atan(0.023101 * rh)
        - 4.686035
    )


def wind_chill_c(t_c: float, wind_kmh: float) -> float:
    if t_c > 10 or wind_kmh < 4.8:
        return t_c
    v = wind_kmh ** 0.16
    return 13.12 + 0.6215 * t_c - 11.37 * v + 0.3965 * t_c * v


@dataclass
class CalibrationRecord:
    bias: float = 0.0
    rmse: float = 1.0
    reliability: float = 0.70
    samples: int = 0

    @property
    def quality(self) -> float:
        n_factor = 0.45 + 0.55 * min(1.0, self.samples / 150.0)
        return clamp(self.reliability * n_factor * math.exp(-0.04 * max(0.0, self.rmse - 1.0)), 0.15, 0.98)


class CalibrationStore:
    def __init__(self, payload: Optional[Mapping[str, Any]] = None):
        self.payload = dict(payload or {})

    @classmethod
    def from_json(cls, path: Optional[str]) -> "CalibrationStore":
        if not path: return cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                return cls(json.load(f))
        except Exception:
            return cls()

    def get(self, model: str, variable: str, lead_hours: int, season: str) -> CalibrationRecord:
        lb = lead_bin(lead_hours)
        keys = [
            f"{model}|{variable}|{season}|{lb}",
            f"{model}|{variable}|{season}|*",
            f"{model}|{variable}|*|{lb}",
            f"{model}|{variable}|*|*",
            f"{model}|*|*|*",
        ]
        defaults = {
            "temperature_2m": 1.8,
            "relative_humidity_2m": 8.0,
            "dew_point_2m": 2.0,
            "precipitation": 2.5,
            "rain": 2.5,
            "showers": 2.5,
            "snowfall": 1.3,
            "snow_depth": 0.08,
            "surface_pressure": 3.0,
            "wind_speed_10m": 5.5,
            "wind_gusts_10m": 9.0,
            "visibility": 3000.0,
            "cape": 350.0,
            "soil_moisture_0_to_1cm": 0.05,
            "soil_moisture_1_to_3cm": 0.05,
            "shortwave_radiation": 130.0,
        }
        for k in keys:
            row = self.payload.get(k)
            if isinstance(row, Mapping):
                return CalibrationRecord(
                    safe_float(row.get("bias"), 0.0) or 0.0,
                    max(0.01, safe_float(row.get("rmse"), defaults.get(variable, 2.0)) or defaults.get(variable, 2.0)),
                    clamp(safe_float(row.get("reliability"), 0.70) or 0.70),
                    max(0, int(safe_float(row.get("samples"), 0) or 0)),
                )
        return CalibrationRecord(0.0, defaults.get(variable, 2.0), 0.66, 0)


@dataclass
class ForecastMember:
    model: str
    quality: float
    hours: List[Dict[str, Any]]


HOURLY_VARS = [
    "temperature_2m", "relative_humidity_2m", "dew_point_2m",
    "precipitation", "rain", "showers", "snowfall", "snow_depth",
    "weather_code", "surface_pressure", "wind_speed_10m", "wind_direction_10m",
    "wind_gusts_10m", "visibility", "cape", "shortwave_radiation",
    "soil_moisture_0_to_1cm", "soil_moisture_1_to_3cm",
]


def fetch_model_member(lat: float, lon: float, model: str, horizon: int, calibration: CalibrationStore) -> Optional[ForecastMember]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "models": model,
        "timezone": "UTC",
        "forecast_hours": horizon,
        "hourly": ",".join(HOURLY_VARS),
    }
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=12)
        r.raise_for_status()
        data = r.json()
        h = data.get("hourly") or {}
        times = h.get("time") or []
        if not times: return None
        seas = season_key(datetime.now(timezone.utc), lat)
        rows, qualities = [], []
        for i, t in enumerate(times[:horizon]):
            row: Dict[str, Any] = {"time": t, "lead_hours": i, "model": model}
            q_parts = []
            for var in HOURLY_VARS:
                arr = h.get(var) or []
                val = arr[i] if i < len(arr) else None
                if var in {"weather_code", "wind_direction_10m"}:
                    row[var] = val
                    continue
                x = safe_float(val)
                if x is None:
                    row[var] = None
                    continue
                cal = calibration.get(model, var, i, seas)
                row[var] = x - cal.bias
                row[f"{var}_sigma"] = cal.rmse
                q_parts.append(cal.quality)
            row["quality"] = statistics.mean(q_parts) if q_parts else MODEL_PRIOR_QUALITY.get(model, 0.70)
            row["elevation"] = safe_float(data.get("elevation"), 0.0) or 0.0
            rows.append(row)
            qualities.append(row["quality"])
        return ForecastMember(model, statistics.mean(qualities) if qualities else 0.65, rows)
    except Exception:
        return None


def fuse_members(members: Sequence[ForecastMember], horizon: int) -> List[Dict[str, Any]]:
    if not members: return []
    n = min(horizon, min(len(m.hours) for m in members))
    continuous = [v for v in HOURLY_VARS if v not in {"weather_code", "wind_direction_10m"}]
    out: List[Dict[str, Any]] = []
    for i in range(n):
        weights = [max(0.05, m.quality) for m in members]
        row: Dict[str, Any] = {"time": members[0].hours[i]["time"], "lead_hours": i}
        for var in continuous:
            vals = [safe_float(m.hours[i].get(var)) for m in members]
            mu = weighted_mean(vals, weights)
            spread = weighted_std(vals, weights)
            sigmas = [safe_float(m.hours[i].get(f"{var}_sigma"), 0.0) or 0.0 for m in members]
            cal_unc = weighted_mean(sigmas, weights) or 0.0
            row[var] = mu
            row[f"{var}_spread"] = spread
            row[f"{var}_sigma"] = math.sqrt(spread ** 2 + cal_unc ** 2)
        # Use modal/weighted representative weather code.
        codes = [m.hours[i].get("weather_code") for m in members if m.hours[i].get("weather_code") is not None]
        row["weather_code"] = int(round(statistics.median(codes))) if codes else None
        # Circular mean for wind direction.
        dirs = [safe_float(m.hours[i].get("wind_direction_10m")) for m in members]
        pairs = [(d, w) for d, w in zip(dirs, weights) if d is not None]
        if pairs:
            x = sum(math.cos(math.radians(d)) * w for d, w in pairs)
            y = sum(math.sin(math.radians(d)) * w for d, w in pairs)
            row["wind_direction_10m"] = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
        else:
            row["wind_direction_10m"] = None
        row["quality"] = statistics.mean(m.quality for m in members)
        row["member_count"] = len(members)
        out.append(row)
    return out


def generate_scenarios(fused: Sequence[Mapping[str, Any]], count: int = 120, seed: int = 47) -> List[List[Dict[str, Any]]]:
    rng = random.Random(seed)
    count = max(16, min(1000, int(count)))
    vars_ = ["temperature_2m", "relative_humidity_2m", "precipitation", "wind_speed_10m", "wind_gusts_10m", "cape", "surface_pressure", "soil_moisture_0_to_1cm"]
    scenarios = []
    for _ in range(count):
        # scenario-level factors preserve temporal coherence better than independent hourly noise
        common = {v: rng.gauss(0, 0.65) for v in vars_}
        rows = []
        for row in fused:
            r = dict(row)
            for v in vars_:
                mu = safe_float(row.get(v))
                if mu is None: continue
                sig = max(1e-6, safe_float(row.get(f"{v}_sigma"), 0.0) or 0.0)
                local = 0.35 * rng.gauss(0, 1)
                x = mu + sig * (common[v] + local)
                if v in {"precipitation", "wind_speed_10m", "wind_gusts_10m", "cape"}: x = max(0.0, x)
                if v.startswith("relative_humidity"): x = max(0.0, min(100.0, x))
                if v.startswith("soil_moisture"): x = max(0.0, min(0.7, x))
                r[v] = x
            rows.append(r)
        scenarios.append(rows)
    return scenarios


def _soil_saturation(row: Mapping[str, Any], field_capacity: float = 0.32, saturation: float = 0.46) -> float:
    th = safe_float(row.get("soil_moisture_0_to_1cm"))
    if th is None: return 0.45
    return clamp((th - 0.08) / max(0.05, saturation - 0.08))


def _warning_level(warnings: Sequence[Mapping[str, Any]]) -> Tuple[float, Optional[Dict[str, Any]]]:
    sev_map = {"extreme": 1.0, "severe": 0.88, "moderate": 0.62, "minor": 0.32, "unknown": 0.45,
               "red": 1.0, "orange": 0.78, "yellow": 0.56, "green": 0.24, "critical": 1.0, "high": 0.82}
    urgency = {"immediate": 1.0, "expected": 0.85, "future": 0.65, "past": 0.05, "unknown": 0.55}
    certainty = {"observed": 1.0, "likely": 0.88, "possible": 0.62, "unlikely": 0.18, "unknown": 0.55}
    best, best_w = 0.0, None
    for w in warnings or []:
        s = sev_map.get(str(w.get("severity") or w.get("alertlevel") or w.get("level") or "unknown").lower(), 0.45)
        u = urgency.get(str(w.get("urgency") or "unknown").lower(), 0.55)
        c = certainty.get(str(w.get("certainty") or "unknown").lower(), 0.55)
        score = clamp(0.55 * s + 0.25 * u + 0.20 * c)
        if score > best:
            best, best_w = score, dict(w)
    return best, best_w


def rainfall_metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, float]:
    p = [max(0.0, safe_float(r.get("precipitation"), 0.0) or 0.0) for r in rows]
    def s(n: int): return sum(p[:min(len(p), n)])
    return {
        "max_1h_mm": max(p) if p else 0.0,
        "max_3h_mm": max((sum(p[i:i+3]) for i in range(max(1, len(p)-2))), default=0.0),
        "max_6h_mm": max((sum(p[i:i+6]) for i in range(max(1, len(p)-5))), default=0.0),
        "next_24h_mm": s(24), "next_72h_mm": s(72),
    }


def flood_risk(rows: Sequence[Mapping[str, Any]], context: Mapping[str, Any]) -> Dict[str, Any]:
    rain = rainfall_metrics(rows)
    soil = statistics.mean(_soil_saturation(r) for r in rows[:min(24, len(rows))]) if rows else 0.45
    flood = context.get("flood_context") or {}
    q = safe_float(flood.get("river_discharge"))
    qmed = safe_float(flood.get("river_discharge_median")) or safe_float(flood.get("river_discharge_mean"))
    q75 = safe_float(flood.get("river_discharge_75th_percentile"))
    qratio = q / max(0.01, qmed) if q is not None and qmed is not None else 1.0
    q75ratio = q / max(0.01, q75) if q is not None and q75 is not None else None
    terrain = context.get("terrain_context") or {}
    impervious = clamp(safe_float(context.get("impervious_fraction"), safe_float(terrain.get("impervious_fraction"), 0.15)) or 0.15)
    drainage = clamp(safe_float(context.get("drainage_capacity_index"), 0.55) or 0.55)
    slope = max(0.0, safe_float(terrain.get("slope_deg"), 2.0) or 2.0)
    x = -4.0 + 0.045 * rain["max_6h_mm"] + 0.018 * rain["next_24h_mm"] + 1.7 * soil + 0.65 * math.log1p(max(0.0, qratio - 1.0)) + 0.9 * impervious - 0.9 * drainage
    river = logistic(x)
    flash_x = -4.4 + 0.16 * rain["max_1h_mm"] + 0.07 * rain["max_3h_mm"] + 1.8 * soil + 0.035 * slope + 0.85 * impervious - 0.6 * drainage
    flash = logistic(flash_x)
    return {
        "river_flood": clamp(river), "flash_flood": clamp(flash), "rainfall": rain,
        "soil_saturation_proxy": round(soil, 3), "discharge_ratio_to_median": round(qratio, 3) if qratio is not None else None,
        "discharge_ratio_to_q75": round(q75ratio, 3) if q75ratio is not None else None,
        "flood_model_resolution_note": flood.get("resolution_note") or ("coarse/global context" if flood else "no river-model context"),
    }


def landslide_risk(rows: Sequence[Mapping[str, Any]], context: Mapping[str, Any]) -> Dict[str, Any]:
    rain = rainfall_metrics(rows)
    terrain = context.get("terrain_context") or {}
    slope = max(0.0, safe_float(terrain.get("slope_deg"), 0.0) or 0.0)
    susceptibility = clamp(safe_float(terrain.get("landslide_susceptibility"), 0.35) or 0.35)
    soil = statistics.mean(_soil_saturation(r) for r in rows[:min(48, len(rows))]) if rows else 0.45
    antecedent = max(0.0, safe_float(context.get("antecedent_rain_7d_mm"), 0.0) or 0.0)
    x = -5.0 + 0.024 * rain["next_24h_mm"] + 0.010 * rain["next_72h_mm"] + 0.004 * antecedent + 2.0 * soil + 0.055 * min(60.0, slope) + 1.1 * susceptibility
    return {
        "risk": clamp(logistic(x)), "slope_deg": slope, "susceptibility_input": susceptibility,
        "antecedent_rain_7d_mm": antecedent,
        "note": "Weather/terrain susceptibility screening only; not a geotechnical slope-stability assessment.",
    }


def convection_risk(rows: Sequence[Mapping[str, Any]], context: Mapping[str, Any]) -> Dict[str, Any]:
    if not rows: return {"risk": 0.0}
    max_cape = max(safe_float(r.get("cape"), 0.0) or 0.0 for r in rows)
    max_gust = max(safe_float(r.get("wind_gusts_10m"), 0.0) or 0.0 for r in rows)
    thunder = any(int(safe_float(r.get("weather_code"), 0) or 0) in {95, 96, 99} for r in rows)
    lightning = max(0.0, safe_float(context.get("lightning_density"), 0.0) or 0.0)
    x = -4.2 + max_cape / 850.0 + max_gust / 35.0 + (1.6 if thunder else 0.0) + 0.25 * math.log1p(lightning)
    return {"risk": clamp(logistic(x)), "max_cape_jkg": max_cape, "max_gust_kmh": max_gust, "thunderstorm_code_present": thunder, "lightning_density_input": lightning or None}


def wind_risk(rows: Sequence[Mapping[str, Any]], context: Mapping[str, Any]) -> Dict[str, Any]:
    gust = max((safe_float(r.get("wind_gusts_10m"), 0.0) or 0.0 for r in rows), default=0.0)
    sustained = max((safe_float(r.get("wind_speed_10m"), 0.0) or 0.0 for r in rows), default=0.0)
    exposure = clamp(safe_float(context.get("wind_exposure_index"), 0.5) or 0.5)
    risk = logistic((gust - 55.0) / 13.0 + 0.7 * exposure)
    return {"risk": clamp(risk), "max_gust_kmh": gust, "max_sustained_kmh": sustained, "exposure_index": exposure}


def heat_cold_risk(rows: Sequence[Mapping[str, Any]], context: Mapping[str, Any]) -> Dict[str, Any]:
    if not rows: return {"heat": 0.0, "cold": 0.0}
    heat_thr = safe_float(context.get("heat_threshold_c"), 40.0) or 40.0
    cold_thr = safe_float(context.get("cold_threshold_c"), 5.0) or 5.0
    heat_dose, cold_dose, max_tw, min_wc = 0.0, 0.0, -99.0, 99.0
    for r in rows[:min(72, len(rows))]:
        t = safe_float(r.get("temperature_2m"), 20.0) or 20.0
        rh = safe_float(r.get("relative_humidity_2m"), 50.0) or 50.0
        wind = safe_float(r.get("wind_speed_10m"), 5.0) or 5.0
        tw = wet_bulb_stull_c(t, rh)
        wc = wind_chill_c(t, wind)
        max_tw, min_wc = max(max_tw, tw), min(min_wc, wc)
        heat_dose += max(0.0, t - heat_thr) + 1.2 * max(0.0, tw - 28.0)
        cold_dose += max(0.0, cold_thr - wc)
    heat = 1.0 - math.exp(-heat_dose / 18.0)
    cold = 1.0 - math.exp(-cold_dose / 20.0)
    return {"heat": clamp(heat), "cold": clamp(cold), "max_wet_bulb_c": round(max_tw, 2), "min_wind_chill_c": round(min_wc, 2), "heat_threshold_c": heat_thr}


def wildfire_risk(rows: Sequence[Mapping[str, Any]], context: Mapping[str, Any]) -> Dict[str, Any]:
    if not rows: return {"risk": 0.0}
    t = max(safe_float(r.get("temperature_2m"), 20.0) or 20.0 for r in rows[:48])
    rh = min(safe_float(r.get("relative_humidity_2m"), 70.0) or 70.0 for r in rows[:48])
    gust = max(safe_float(r.get("wind_gusts_10m"), 0.0) or 0.0 for r in rows[:48])
    rain24 = sum(max(0.0, safe_float(r.get("precipitation"), 0.0) or 0.0) for r in rows[:24])
    fires = context.get("fire_detections") or []
    nearest = min((safe_float(f.get("distance_km")) for f in fires if safe_float(f.get("distance_km")) is not None), default=None)
    max_frp = max((safe_float(f.get("frp"), 0.0) or 0.0 for f in fires), default=0.0)
    pm25 = max(0.0, safe_float(context.get("pm2_5"), 0.0) or 0.0)
    meteo = logistic(-5.0 + 0.08 * max(0.0, t - 20.0) + 0.035 * max(0.0, 45.0 - rh) + 0.025 * gust - 0.10 * min(30.0, rain24))
    observed_fire = 0.0 if nearest is None else clamp(math.exp(-nearest / 35.0) * (0.45 + 0.08 * math.log1p(max_frp)))
    smoke = clamp(logistic((pm25 - 35.0) / 18.0)) if pm25 > 0 else 0.0
    risk = 1.0 - (1.0 - meteo) * (1.0 - observed_fire) * (1.0 - 0.55 * smoke)
    return {"risk": clamp(risk), "fire_weather": clamp(meteo), "nearest_firms_detection_km": round(nearest, 1) if nearest is not None else None, "firms_records": len(fires), "max_frp": max_frp or None, "smoke_pm25": pm25 or None, "satellite_fire_context_only": True}


def earthquake_risk(lat: float, lon: float, context: Mapping[str, Any]) -> Dict[str, Any]:
    events = context.get("earthquake_events") or []
    normalized = []
    peak = 0.0
    for ev in events:
        mag = safe_float(ev.get("magnitude") or ev.get("mag"))
        ela = safe_float(ev.get("lat") or ev.get("latitude"))
        elo = safe_float(ev.get("lon") or ev.get("longitude"))
        if mag is None or ela is None or elo is None: continue
        d = safe_float(ev.get("distance_km"))
        if d is None: d = haversine_km(lat, lon, ela, elo)
        # Screening intensity proxy only; not USGS ShakeMap/MMI/PGA.
        intensity_proxy = clamp((1.45 * mag - 2.8 * math.log10(max(10.0, d)) - 0.3) / 8.0)
        r = clamp(logistic((intensity_proxy - 0.38) * 7.0))
        peak = max(peak, r)
        normalized.append({"magnitude": mag, "distance_km": round(d, 1), "risk_proxy": round(r, 3), "tsunami_flag": ev.get("tsunami"), "time": ev.get("time"), "source": ev.get("source") or "earthquake feed"})
    normalized.sort(key=lambda e: e["risk_proxy"], reverse=True)
    return {"risk": peak, "events_considered": normalized[:10], "prediction": False, "note": "Observed-event shaking context only; VYOM does not predict earthquakes or replace ShakeMap/engineering assessment."}


def tsunami_context(context: Mapping[str, Any], eq: Mapping[str, Any]) -> Dict[str, Any]:
    warnings = context.get("official_tsunami_warnings") or []
    official_score, best = _warning_level(warnings)
    eq_flag = any(bool(e.get("tsunami_flag")) for e in eq.get("events_considered", []))
    # An earthquake feed flag alone is not treated as an evacuation decision.
    screening = max(official_score, 0.38 if eq_flag and not warnings else 0.0)
    return {"risk_context": clamp(screening), "official_warning_present": bool(warnings), "official_warning": best, "earthquake_tsunami_flag_present": eq_flag, "evacuation_decision": None, "all_clear": None, "note": "Actionable tsunami evacuation/all-clear must come from an official tsunami warning authority."}


def cyclone_context(lat: float, lon: float, context: Mapping[str, Any]) -> Dict[str, Any]:
    tracks = context.get("cyclone_context") or []
    if isinstance(tracks, Mapping): tracks = tracks.get("points") or tracks.get("track") or [tracks]
    best, peak = None, 0.0
    for p in tracks:
        pla = safe_float(p.get("lat") or p.get("latitude")); plo = safe_float(p.get("lon") or p.get("longitude"))
        wind = safe_float(p.get("max_wind_kmh") or p.get("wind_kmh") or p.get("mean_msw_kmph") or p.get("Mean MSW (kmph)"), 0.0) or 0.0
        d = safe_float(p.get("distance_km"))
        if d is None and pla is not None and plo is not None: d = haversine_km(lat, lon, pla, plo)
        if d is None: continue
        r = clamp(logistic((wind - 55.0) / 25.0 + (250.0 - min(500.0, d)) / 130.0))
        if r > peak:
            peak, best = r, {"distance_km": round(d, 1), "max_wind_kmh": wind or None, "category": p.get("category") or p.get("Category"), "valid_time": p.get("valid_time") or p.get("Date/Time"), "source": p.get("source")}
    return {"risk": peak, "nearest_or_highest_impact_track_point": best, "track_points": len(tracks)}


def drought_context(context: Mapping[str, Any]) -> Dict[str, Any]:
    spi3 = safe_float(context.get("spi_3")); spi6 = safe_float(context.get("spi_6")); spei3 = safe_float(context.get("spei_3"))
    vals = [v for v in (spi3, spi6, spei3) if v is not None]
    if not vals:
        return {"available": False, "risk": None, "reason": "No SPI/SPEI or validated local drought index supplied."}
    worst = min(vals)
    risk = clamp(logistic(-(worst + 0.5) * 2.0))
    return {"available": True, "risk": risk, "spi_3": spi3, "spi_6": spi6, "spei_3": spei3}


def coastal_inundation_context(context: Mapping[str, Any]) -> Dict[str, Any]:
    c = context.get("coastal_context") or {}
    surge = safe_float(c.get("storm_surge_m")); tide = safe_float(c.get("tide_m")); wave_setup = safe_float(c.get("wave_setup_m"), 0.0) or 0.0
    ground = safe_float(c.get("ground_elevation_m")); uncertainty = max(0.05, safe_float(c.get("water_level_uncertainty_m"), 0.35) or 0.35)
    if surge is None or tide is None or ground is None:
        return {"available": False, "risk": None, "reason": "Need trusted storm-surge/tide and local ground-elevation context."}
    total = surge + tide + wave_setup
    margin = total - ground
    risk = clamp(logistic(margin / uncertainty))
    return {"available": True, "risk": risk, "total_water_level_proxy_m": round(total, 3), "ground_elevation_m": ground, "margin_m": round(margin, 3), "navigation_or_evacuation_grade": False}


def exposure_vulnerability(context: Mapping[str, Any]) -> Dict[str, Any]:
    exp = context.get("exposure_context") or {}
    pop_density = max(0.0, safe_float(exp.get("population_density_km2"), 0.0) or 0.0)
    population = max(0.0, safe_float(exp.get("population"), 0.0) or 0.0)
    critical = max(0.0, safe_float(exp.get("critical_facilities_count"), 0.0) or 0.0)
    infra = clamp(safe_float(exp.get("infrastructure_exposure_index"), 0.4) or 0.4)
    vuln = clamp(safe_float(exp.get("vulnerability_index"), 0.45) or 0.45)
    preparedness = clamp(safe_float(exp.get("preparedness_index"), 0.55) or 0.55)
    exposure = clamp(0.28 * (1 - math.exp(-pop_density / 1500.0)) + 0.22 * (1 - math.exp(-population / 250000.0)) + 0.22 * (1 - math.exp(-critical / 20.0)) + 0.28 * infra)
    vulnerability = clamp(0.75 * vuln + 0.25 * (1.0 - preparedness))
    return {"exposure": exposure, "vulnerability": vulnerability, "inputs_present": bool(exp), "population": population or None, "population_density_km2": pop_density or None, "critical_facilities_count": critical or None, "preparedness_index": preparedness}


def impact_score(hazard: float, exposure: float, vulnerability: float) -> float:
    return clamp(hazard * (0.55 + 0.45 * exposure) * (0.55 + 0.45 * vulnerability))


def dependency_fusion(hazards: Mapping[str, Optional[float]], official_level: float = 0.0) -> Dict[str, Any]:
    vals = {k: clamp(v) for k, v in hazards.items() if v is not None}
    if not vals: return {"risk": official_level, "contributors": []}
    # Base noisy-OR with capped hazard contributions, then add compound dependencies.
    prod = 1.0
    for v in vals.values(): prod *= (1.0 - 0.60 * v)
    base = 1.0 - prod
    synergy = 0.0
    def pair(a,b,w):
        nonlocal synergy
        synergy += w * vals.get(a,0.0) * vals.get(b,0.0)
    pair("river_flood","landslide",0.18)
    pair("flash_flood","landslide",0.20)
    pair("wildfire","wind",0.17)
    pair("cyclone","river_flood",0.16)
    pair("cyclone","coastal_inundation",0.24)
    pair("earthquake","tsunami_context",0.20)
    pair("convection","wind",0.12)
    fused = clamp(base + synergy)
    final = max(fused, clamp(official_level))
    contrib = sorted(({"hazard": k, "risk": round(v, 3)} for k, v in vals.items()), key=lambda x: x["risk"], reverse=True)
    return {"risk": final, "model_fusion_risk": fused, "official_warning_floor": official_level, "contributors": contrib[:10]}


def safety_level(risk: float, official_level: float = 0.0) -> str:
    x = max(risk, official_level)
    if official_level >= 0.88 or x >= 0.90: return "CRITICAL"
    if x >= 0.72: return "HIGH RISK"
    if x >= 0.46: return "CAUTION"
    return "NORMAL"


def scenario_peak_risk(rows: Sequence[Mapping[str, Any]], context: Mapping[str, Any]) -> float:
    f = flood_risk(rows, context)
    l = landslide_risk(rows, context)["risk"]
    c = convection_risk(rows, context)["risk"]
    w = wind_risk(rows, context)["risk"]
    hc = heat_cold_risk(rows, context)
    wf = wildfire_risk(rows, context)["risk"]
    h = [f["river_flood"], f["flash_flood"], l, c, w, hc["heat"], hc["cold"], wf]
    return max(h) if h else 0.0


def scenario_summary(scenarios: Sequence[Sequence[Mapping[str, Any]]], context: Mapping[str, Any]) -> Dict[str, Any]:
    vals = sorted(scenario_peak_risk(s, context) for s in scenarios)
    if not vals: return {"scenarios": 0}
    def q(p: float):
        idx = max(0, min(len(vals)-1, int(round((len(vals)-1)*p))))
        return vals[idx]
    return {
        "scenarios": len(vals), "median_peak_weather_hazard": round(q(0.5),3),
        "p10": round(q(0.1),3), "p90": round(q(0.9),3),
        "p_peak_ge_0_70": round(sum(v>=0.70 for v in vals)/len(vals),3),
        "p_peak_ge_0_90": round(sum(v>=0.90 for v in vals)/len(vals),3),
    }


def build_timeline(rows: Sequence[Mapping[str, Any]], context: Mapping[str, Any], hours_step: int = 6) -> List[Dict[str, Any]]:
    out = []
    for start in range(0, min(72, len(rows)), hours_step):
        subset = rows[start:min(len(rows), start+max(12, hours_step))]
        if not subset: continue
        f = flood_risk(subset, context)
        l = landslide_risk(subset, context)["risk"]
        c = convection_risk(subset, context)["risk"]
        w = wind_risk(subset, context)["risk"]
        hc = heat_cold_risk(subset, context)
        wf = wildfire_risk(subset, context)["risk"]
        peak = max(f["river_flood"], f["flash_flood"], l, c, w, hc["heat"], hc["cold"], wf)
        out.append({"lead_hours": start, "valid_time": subset[0].get("time"), "weather_hazard_peak": round(peak,3), "level": safety_level(peak)})
    return out


def precision_mode(context: Mapping[str, Any], members: int, calibration: CalibrationStore) -> str:
    if context.get("official_warnings") or context.get("official_tsunami_warnings"):
        if context.get("exposure_context") and context.get("terrain_context"): return "OFFICIAL_IMPACT_AWARE"
        return "OFFICIAL_AWARE"
    if context.get("exposure_context") and context.get("terrain_context"): return "IMPACT_AWARE"
    if context.get("flood_context") or context.get("earthquake_events") or context.get("fire_detections"): return "HAZARD_CONTEXT"
    return "WEATHER_ONLY"


def confidence_score(fused: Sequence[Mapping[str, Any]], members: Sequence[ForecastMember], context: Mapping[str, Any], precision: str) -> Dict[str, Any]:
    if not fused: return {"overall": 0.0, "forecast": 0.0, "context": 0.0, "authority": 0.0}
    forecast_q = clamp(statistics.mean(safe_float(r.get("quality"), 0.5) or 0.5 for r in fused))
    spread_scores = []
    for r in fused[:min(48, len(fused))]:
        ts = safe_float(r.get("temperature_2m_spread"), 0.0) or 0.0
        ps = safe_float(r.get("precipitation_spread"), 0.0) or 0.0
        ws = safe_float(r.get("wind_gusts_10m_spread"), 0.0) or 0.0
        spread_scores.append(math.exp(-0.12*ts - 0.05*ps - 0.03*ws))
    agreement = statistics.mean(spread_scores) if spread_scores else 0.6
    context_parts = [
        1.0 if context.get("flood_context") else 0.45,
        1.0 if context.get("terrain_context") else 0.45,
        1.0 if context.get("exposure_context") else 0.40,
        1.0 if context.get("source_health_ok", True) else 0.45,
    ]
    context_q = statistics.mean(context_parts)
    authority_q = 0.95 if (context.get("official_warnings") or context.get("official_tsunami_warnings")) else 0.60
    overall = clamp(0.42*forecast_q + 0.22*agreement + 0.24*context_q + 0.12*authority_q)
    return {"overall": round(overall,3), "forecast": round(forecast_q,3), "model_agreement": round(agreement,3), "context": round(context_q,3), "authority": round(authority_q,3), "member_count": len(members), "precision_mode": precision}


def management_priorities(impact: Mapping[str, float], warnings: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    items = []
    for h, r in impact.items():
        if r >= 0.70: cls = "PRIORITY_1"
        elif r >= 0.48: cls = "PRIORITY_2"
        elif r >= 0.28: cls = "PRIORITY_3"
        else: continue
        items.append({"hazard": h, "impact_risk": round(r,3), "priority": cls, "action_class": "Follow incident-command/official SOP; verify exposed populations, critical facilities, communications and resource readiness."})
    if warnings:
        items.insert(0, {"hazard": "official_warning", "impact_risk": 1.0, "priority": "PRIORITY_1", "action_class": "Implement the competent authority's warning/order and established emergency plan."})
    return items[:12]


def build_disaster_advisory(lat: float, lon: float, context: Optional[Mapping[str, Any]] = None, horizon_hours: int = 120, scenario_count: int = 160) -> Dict[str, Any]:
    context = dict(context or {})
    horizon_hours = max(12, min(168, int(horizon_hours)))
    cal_path = context.get("calibration_path") or os.getenv("VYOM_DISASTER_CALIBRATION") or "data/disaster/calibration/dynamic_calibration.json"
    calibration = CalibrationStore.from_json(cal_path if os.path.exists(cal_path) else None)

    models = context.get("models") or list(DEFAULT_MODELS)
    members = [m for m in (fetch_model_member(lat, lon, model, horizon_hours, calibration) for model in models) if m]
    if not members:
        return {"engine": ENGINE_NAME, "version": ENGINE_VERSION, "status": "error", "reason": "No numerical weather forecast members available.", "risk": {}, "confidence": {"overall": 0.0}, "evacuation_order": None, "all_clear": None}

    fused = fuse_members(members, horizon_hours)
    if not fused:
        return {"engine": ENGINE_NAME, "version": ENGINE_VERSION, "status": "error", "reason": "Unable to fuse forecast members.", "risk": {}, "confidence": {"overall": 0.0}, "evacuation_order": None, "all_clear": None}

    flood = flood_risk(fused, context)
    landslide = landslide_risk(fused, context)
    conv = convection_risk(fused, context)
    wind = wind_risk(fused, context)
    thermal = heat_cold_risk(fused, context)
    wildfire = wildfire_risk(fused, context)
    eq = earthquake_risk(lat, lon, context)
    tsunami = tsunami_context(context, eq)
    cyclone = cyclone_context(lat, lon, context)
    drought = drought_context(context)
    coastal = coastal_inundation_context(context)
    exposure = exposure_vulnerability(context)
    warnings = list(context.get("official_warnings") or []) + list(context.get("official_tsunami_warnings") or [])
    official_level, strongest_warning = _warning_level(warnings)

    hazard = {
        "river_flood": flood["river_flood"],
        "flash_flood": flood["flash_flood"],
        "landslide": landslide["risk"],
        "convection": conv["risk"],
        "wind": wind["risk"],
        "heat": thermal["heat"],
        "cold": thermal["cold"],
        "wildfire": wildfire["risk"],
        "earthquake": eq["risk"],
        "tsunami_context": tsunami["risk_context"],
        "cyclone": cyclone["risk"],
        "drought": drought.get("risk"),
        "coastal_inundation": coastal.get("risk"),
    }
    impact = {k: impact_score(v, exposure["exposure"], exposure["vulnerability"]) for k, v in hazard.items() if v is not None}
    fused_risk = dependency_fusion(impact, official_level)
    level = safety_level(fused_risk["risk"], official_level)
    scenarios = generate_scenarios(fused, scenario_count)
    scen = scenario_summary(scenarios, context)
    precision = precision_mode(context, len(members), calibration)
    confidence = confidence_score(fused, members, context, precision)

    # Abstention gates: low confidence and missing local impact inputs mean no precise operational claim.
    missing = []
    if not context.get("exposure_context"): missing.append("local exposure/vulnerability inventory")
    if not context.get("terrain_context"): missing.append("local terrain/susceptibility data")
    if not context.get("official_warnings"): missing.append("configured local official all-hazard warning feed")
    if flood.get("flood_model_resolution_note") == "no river-model context": missing.append("river/discharge context")

    return {
        "engine": ENGINE_NAME, "version": ENGINE_VERSION, "status": "ok",
        "location": {"lat": lat, "lon": lon}, "generated_at": datetime.now(timezone.utc).isoformat(),
        "horizon_hours": horizon_hours,
        "forecast": {
            "models_used": [m.model for m in members],
            "model_qualities": {m.model: round(m.quality,3) for m in members},
            "first_hour": fused[0],
            "timeline": build_timeline(fused, context),
        },
        "hazards": {
            "flood": flood, "landslide": landslide, "severe_convection": conv, "wind": wind,
            "thermal": thermal, "wildfire": wildfire, "earthquake": eq, "tsunami": tsunami,
            "cyclone": cyclone, "drought": drought, "coastal_inundation": coastal,
        },
        "exposure_vulnerability": exposure,
        "risk": {
            "hazard_probabilities": {k: (round(v,3) if v is not None else None) for k,v in hazard.items()},
            "impact_risk": {k: round(v,3) for k,v in impact.items()},
            "integrated": round(fused_risk["risk"],3),
            "model_fusion": round(fused_risk["model_fusion_risk"],3),
            "official_warning_floor": round(official_level,3),
            "level": level,
            "contributors": fused_risk["contributors"],
        },
        "scenario_uncertainty": scen,
        "official_warning_gate": {
            "warnings_present": bool(warnings), "strongest_warning": strongest_warning,
            "warning_floor": round(official_level,3), "model_cannot_downgrade_official_warning": True,
        },
        "management_priorities": management_priorities(impact, warnings),
        "confidence": confidence,
        "precision": {"mode": precision, "missing_for_higher_precision": missing},
        "evacuation_order": None,
        "shelter_selection": None,
        "rescue_dispatch": None,
        "all_clear": None,
        "field_semantics": {
            "hazard_probability": "Modeled hazard likelihood/context, distinct from impact risk.",
            "impact_risk": "Hazard combined with available exposure/vulnerability context; missing local inventories reduce precision.",
            "earthquake": "Observed-event context only; no earthquake prediction.",
            "tsunami": "Official tsunami authority is required for evacuation/all-clear decisions."
        },
        "engine_diagnostics": {
            "engine_executed": True,
            "validation_contract": "hazard_impact_confidence_authority_separated",
            "guarded_narration_expected": True
        },
        "decision_contract": {
            "authority": "Decision-support only; official emergency-management orders and incident command take precedence.",
            "earthquakes": "Observed events only; no earthquake prediction.",
            "tsunami": "No inferred evacuation/all-clear from magnitude alone; official tsunami authority required.",
            "fires": "Satellite thermal detections are contextual and may be incomplete or ambiguous.",
            "flood": "Global discharge models are coarse and do not replace local gauges/hydraulic modelling.",
        },
    }


__all__ = [
    "build_disaster_advisory", "CalibrationStore", "CalibrationRecord", "ForecastMember",
    "fetch_model_member", "fuse_members", "generate_scenarios", "flood_risk",
    "landslide_risk", "convection_risk", "wind_risk", "heat_cold_risk",
    "wildfire_risk", "earthquake_risk", "tsunami_context", "cyclone_context",
    "drought_context", "coastal_inundation_context", "dependency_fusion",
]
