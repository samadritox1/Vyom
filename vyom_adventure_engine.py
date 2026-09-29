"""
VYOM-ADVENTURE — probabilistic outdoor-weather and terrain exposure engine.

Decision-support only. This module does not certify that an outdoor route or
activity is safe. It screens environmental hazards for hiking, trekking,
camping and other non-motorized outdoor travel using numerical weather,
terrain/route context, air quality, hydrology, fire detections, official alerts,
and explicit uncertainty.

Safety principles
-----------------
1. Official warnings/bulletins outrank model convenience guidance.
2. Risk and confidence are independent quantities.
3. Missing route, terrain, snowpack or local-hazard information causes
   abstention/downgraded precision rather than invented certainty.
4. The engine avoids operational instructions for technical climbing,
   avalanche travel, river crossings or wildfire escape tactics.
5. The LLM may explain the structured output but should not recompute or
   override it.
"""
from __future__ import annotations

import json
import math
import os
import random
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import requests

ENGINE_NAME = "VYOM-ADVENTURE"
ENGINE_VERSION = "1.0.0"
G = 9.80665
R_D = 287.05287
KELVIN = 273.15
P0_HPA = 1013.25
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
    return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, x))))


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2.0 * math.asin(math.sqrt(a))


def circular_diff_deg(a: float, b: float) -> float:
    return (float(a) - float(b) + 180.0) % 360.0 - 180.0


def season_key(dt: datetime, lat: float = 0.0) -> str:
    m = dt.month
    if m in (12, 1, 2): s = "DJF"
    elif m in (3, 4, 5): s = "MAM"
    elif m in (6, 7, 8): s = "JJA"
    else: s = "SON"
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


@dataclass
class CalibrationRecord:
    bias: float = 0.0
    rmse: float = 1.0
    reliability: float = 0.72
    samples: int = 0

    @property
    def quality(self) -> float:
        n = 0.5 + 0.5 * min(1.0, self.samples / 150.0)
        return clamp(self.reliability * n * math.exp(-0.06 * max(0.0, self.rmse - 1.0)), 0.15, 0.98)


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
            "temperature_2m": 1.8, "apparent_temperature": 2.3, "relative_humidity_2m": 8.0,
            "precipitation": 2.0, "precipitation_probability": 18.0, "wind_speed_10m": 5.0,
            "wind_gusts_10m": 8.0, "visibility": 2500.0, "cape": 300.0,
            "freezing_level_height": 350.0, "snow_depth": 0.08, "snowfall": 1.2,
            "shortwave_radiation": 120.0, "surface_pressure": 2.5,
        }
        for k in keys:
            row = self.payload.get(k)
            if isinstance(row, Mapping):
                return CalibrationRecord(
                    safe_float(row.get("bias"), 0.0) or 0.0,
                    max(0.01, safe_float(row.get("rmse"), defaults.get(variable, 2.0)) or defaults.get(variable, 2.0)),
                    clamp(safe_float(row.get("reliability"), 0.72) or 0.72),
                    max(0, int(safe_float(row.get("samples"), 0) or 0)),
                )
        return CalibrationRecord(0.0, defaults.get(variable, 2.0), 0.68, 0)


@dataclass
class ForecastMember:
    model: str
    quality: float
    hours: List[Dict[str, Any]]


HOURLY_VARS = [
    "temperature_2m", "relative_humidity_2m", "apparent_temperature", "dew_point_2m",
    "precipitation", "rain", "showers", "snowfall", "snow_depth", "precipitation_probability",
    "weather_code", "surface_pressure", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
    "visibility", "cape", "freezing_level_height", "shortwave_radiation", "is_day",
    "soil_moisture_0_to_1cm", "soil_moisture_1_to_3cm", "soil_temperature_0cm",
]


def fetch_model_member(lat: float, lon: float, model: str, horizon: int, calibration: CalibrationStore) -> Optional[ForecastMember]:
    params = {
        "latitude": lat, "longitude": lon, "models": model, "timezone": "UTC",
        "forecast_hours": horizon, "hourly": ",".join(HOURLY_VARS),
    }
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        h = data.get("hourly") or {}
        times = h.get("time") or []
        if not times: return None
        season = season_key(datetime.now(timezone.utc), lat)
        rows = []
        qualities = []
        for i, t in enumerate(times[:horizon]):
            row = {"time": t, "lead_hours": i, "model": model}
            q_parts = []
            for var in HOURLY_VARS:
                arr = h.get(var) or []
                val = arr[i] if i < len(arr) else None
                if var in {"weather_code", "is_day", "wind_direction_10m"}:
                    row[var] = val
                    continue
                x = safe_float(val)
                if x is None:
                    row[var] = None
                    continue
                cal = calibration.get(model, var, i, season)
                row[var] = x - cal.bias
                row[f"{var}_sigma"] = cal.rmse
                q_parts.append(cal.quality)
            row["elevation"] = safe_float(data.get("elevation"), 0.0) or 0.0
            row["quality"] = statistics.mean(q_parts) if q_parts else MODEL_PRIOR_QUALITY.get(model, 0.7)
            rows.append(row)
            qualities.append(row["quality"])
        return ForecastMember(model, statistics.mean(qualities) if qualities else 0.65, rows)
    except Exception:
        return None


def fuse_members(members: Sequence[ForecastMember], horizon: int) -> List[Dict[str, Any]]:
    out = []
    if not members: return out
    variables = [v for v in HOURLY_VARS if v not in {"weather_code", "is_day", "wind_direction_10m"}]
    for i in range(min(horizon, min(len(m.hours) for m in members))):
        weights = [max(0.05, m.quality) for m in members]
        row: Dict[str, Any] = {"time": members[0].hours[i]["time"], "lead_hours": i}
        for var in variables:
            vals = [safe_float(m.hours[i].get(var)) for m in members]
            mu = weighted_mean(vals, weights)
            spread = weighted_std(vals, weights)
            sigmas = [safe_float(m.hours[i].get(f"{var}_sigma"), 0.0) or 0.0 for m in members]
            cal_unc = weighted_mean(sigmas, weights) or 0.0
            row[var] = mu
            row[f"{var}_sigma"] = math.sqrt(spread ** 2 + cal_unc ** 2)
            row[f"{var}_spread"] = spread
        # circular direction
        dirs = [safe_float(m.hours[i].get("wind_direction_10m")) for m in members]
        pairs = [(d, w) for d, w in zip(dirs, weights) if d is not None]
        if pairs:
            sx = sum(w * math.sin(math.radians(d)) for d, w in pairs)
            cx = sum(w * math.cos(math.radians(d)) for d, w in pairs)
            row["wind_direction_10m"] = math.degrees(math.atan2(sx, cx)) % 360.0
        else: row["wind_direction_10m"] = None
        # categorical codes choose most severe code from valid members when disagreement
        codes = [int(c) for c in (m.hours[i].get("weather_code") for m in members) if c is not None]
        row["weather_code"] = max(codes, key=lambda c: weather_code_severity(c), default=None)
        is_days = [safe_float(m.hours[i].get("is_day")) for m in members]
        row["is_day"] = round(weighted_mean(is_days, weights) or 0)
        row["elevation"] = weighted_mean([safe_float(m.hours[i].get("elevation"), 0) for m in members], weights) or 0.0
        row["model_count"] = len(members)
        row["source_quality"] = statistics.mean(m.quality for m in members)
        out.append(row)
    return out


def weather_code_severity(code: int) -> int:
    table = {95: 9, 96: 10, 99: 10, 82: 8, 86: 8, 65: 7, 75: 7, 81: 6, 85: 6,
             63: 5, 73: 5, 55: 4, 53: 3, 61: 3, 71: 3, 45: 4, 48: 5, 51: 2,
             80: 4, 77: 3, 3: 1, 2: 1, 1: 0, 0: 0}
    return table.get(int(code), 1)


def saturation_vapor_pressure_hpa(t_c: float) -> float:
    return 6.112 * math.exp((17.67 * t_c) / (t_c + 243.5))


def dew_point_c(t_c: float, rh: float) -> float:
    rh = max(1e-3, min(100.0, rh))
    a, b = 17.625, 243.04
    alpha = math.log(rh / 100.0) + a * t_c / (b + t_c)
    return b * alpha / (a - alpha)


def wet_bulb_stull_c(t_c: float, rh: float) -> float:
    """Stull approximation; environmental screening only."""
    rh = max(1.0, min(100.0, rh))
    return (t_c * math.atan(0.151977 * math.sqrt(rh + 8.313659)) +
            math.atan(t_c + rh) - math.atan(rh - 1.676331) +
            0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh) - 4.686035)


def wind_chill_c(t_c: float, wind_kmh: float) -> float:
    if t_c > 10 or wind_kmh < 4.8: return t_c
    v = max(4.8, wind_kmh) ** 0.16
    return 13.12 + 0.6215 * t_c - 11.37 * v + 0.3965 * t_c * v


def heat_index_c(t_c: float, rh: float) -> float:
    # NOAA regression in Fahrenheit; only meaningful in warm/humid conditions.
    if t_c < 26.0 or rh < 40.0: return t_c
    f = t_c * 9/5 + 32
    hi = (-42.379 + 2.04901523*f + 10.14333127*rh - 0.22475541*f*rh
          - 0.00683783*f*f - 0.05481717*rh*rh + 0.00122874*f*f*rh
          + 0.00085282*f*rh*rh - 0.00000199*f*f*rh*rh)
    return (hi - 32) * 5/9


def pressure_at_altitude_hpa(alt_m: float, sea_level_hpa: float = 1013.25) -> float:
    return sea_level_hpa * max(0.05, (1.0 - 2.25577e-5 * max(-500.0, alt_m))) ** 5.25588


def altitude_exposure_screen(altitude_m: float, surface_pressure_hpa: Optional[float] = None) -> Dict[str, Any]:
    p = surface_pressure_hpa if surface_pressure_hpa and surface_pressure_hpa > 0 else pressure_at_altitude_hpa(altitude_m)
    pressure_ratio = p / P0_HPA
    # This is a physical oxygen-pressure proxy, not a medical diagnosis.
    oxygen_partial_proxy = 0.2095 * p
    r = logistic((altitude_m - 2500.0) / 550.0) * 0.72 + logistic((3000.0 - p) / 500.0) * 0.0
    # pressure term based on fraction rather than raw hPa
    r = clamp(0.75 * logistic((0.76 - pressure_ratio) / 0.06) + 0.25 * logistic((altitude_m - 3000) / 500))
    return {"risk": r, "altitude_m": altitude_m, "pressure_hpa": round(p,1),
            "pressure_ratio": round(pressure_ratio,3), "oxygen_partial_pressure_proxy_hpa": round(oxygen_partial_proxy,1),
            "medical_diagnosis": None}


def thermal_screen(t: float, rh: float, wind_kmh: float, radiation_wm2: float = 0.0) -> Dict[str, Any]:
    wb = wet_bulb_stull_c(t, rh)
    hi = heat_index_c(t, rh)
    wc = wind_chill_c(t, wind_kmh)
    # Solar load is intentionally bounded; this is not a certified WBGT instrument estimate.
    solar_term = clamp(radiation_wm2 / 900.0) * 5.0
    wbgt_proxy = 0.7 * wb + 0.3 * t + 0.25 * solar_term
    heat = clamp(logistic((wbgt_proxy - 27.0) / 2.4) * 0.75 + logistic((hi - 35.0) / 3.5) * 0.25)
    cold = clamp(logistic((-wc - 5.0) / 5.5))
    return {"wet_bulb_c": round(wb,1), "heat_index_c": round(hi,1), "wind_chill_c": round(wc,1),
            "wbgt_proxy_c": round(wbgt_proxy,1), "heat_risk": heat, "cold_risk": cold,
            "wbgt_is_screening_proxy": True}


def lightning_screen(cape: float, weather_code: Optional[int], precipitation_mm: float, gust_kmh: float,
                     lightning_density: Optional[float] = None) -> Dict[str, Any]:
    code_r = 1.0 if weather_code in (95,96,99) else 0.0
    cape_r = logistic((cape - 700.0) / 350.0)
    precip_r = logistic((precipitation_mm - 3.0) / 2.0)
    gust_r = logistic((gust_kmh - 45.0) / 10.0)
    dens_r = 0.0 if lightning_density is None else clamp(logistic((lightning_density - 0.02) / 0.02))
    r = clamp(0.50*code_r + 0.22*cape_r + 0.10*precip_r + 0.08*gust_r + 0.10*dens_r)
    return {"risk": r, "cape_jkg": cape, "thunderstorm_code": bool(code_r), "lightning_density": lightning_density}


def wind_exposure_screen(wind_kmh: float, gust_kmh: float, terrain: Mapping[str, Any]) -> Dict[str, Any]:
    exposure = str(terrain.get("exposure", "unknown")).lower()
    factor = {"sheltered":0.75,"forest":0.80,"open":1.0,"ridge":1.25,"summit":1.30}.get(exposure,1.0)
    effective_gust = gust_kmh * factor
    r = clamp(0.55*logistic((effective_gust-45)/10) + 0.45*logistic((wind_kmh*factor-30)/9))
    return {"risk":r,"effective_gust_kmh":round(effective_gust,1),"terrain_exposure":exposure,
            "terrain_factor":factor}


def visibility_screen(visibility_m: float, rh: float, precip_mm: float, weather_code: Optional[int]) -> Dict[str, Any]:
    fog = 1.0 if weather_code in (45,48) else 0.0
    vis = clamp(logistic((3000.0 - visibility_m) / 900.0))
    rain = logistic((precip_mm - 2.0)/1.5)
    humid = logistic((rh - 96.0)/2.0)
    return {"risk":clamp(0.55*vis+0.20*fog+0.15*rain+0.10*humid),"visibility_m":visibility_m}


def surface_condition_screen(row: Mapping[str, Any]) -> Dict[str, Any]:
    rain = safe_float(row.get("rain"), 0) or 0
    snow = safe_float(row.get("snowfall"), 0) or 0
    t = safe_float(row.get("temperature_2m"), 0) or 0
    sm = safe_float(row.get("soil_moisture_0_to_1cm"), 0.25) or 0.25
    frozen = logistic((1.0 - t)/1.7)
    wet = logistic((rain-1.5)/1.2)
    muddy = logistic((sm-0.34)/0.045)
    snow_r = logistic((snow-0.5)/0.7)
    r = clamp(0.32*frozen + 0.28*wet + 0.22*muddy + 0.18*snow_r)
    return {"risk":r,"wet_surface":wet,"freeze_ice_proxy":frozen,"mud_proxy":muddy,"fresh_snow_proxy":snow_r}


def snow_ice_screen(row: Mapping[str, Any], terrain: Mapping[str, Any]) -> Dict[str, Any]:
    t = safe_float(row.get("temperature_2m"), 0) or 0
    snowfall = safe_float(row.get("snowfall"), 0) or 0
    snow_depth_m = safe_float(row.get("snow_depth"), 0) or 0
    freeze = safe_float(row.get("freezing_level_height"), 99999) or 99999
    route_alt = safe_float(terrain.get("max_elevation_m"), safe_float(row.get("elevation"),0)) or 0
    fresh = logistic((snowfall-1.0)/1.0)
    below_freeze = logistic((-t)/2.0)
    freezing_overlap = logistic((route_alt-freeze)/250.0)
    snowpack = logistic((snow_depth_m-0.10)/0.08)
    snow_ice = clamp(0.30*fresh+0.25*below_freeze+0.25*freezing_overlap+0.20*snowpack)
    # Not an avalanche forecast: weather context only. Official avalanche bulletin must dominate.
    snow_instability_weather = clamp(0.45*fresh + 0.20*logistic((safe_float(row.get("wind_gusts_10m"),0)-35)/10)
                                       +0.20*freezing_overlap+0.15*snowpack)
    return {"risk":snow_ice,"snow_instability_weather_context":snow_instability_weather,
            "snow_depth_m":snow_depth_m,"freezing_level_m":freeze,"route_altitude_m":route_alt,
            "avalanche_forecast":None}


def uv_screen(uv_index: Optional[float], is_day: float) -> Dict[str, Any]:
    if uv_index is None: return {"risk":None,"uv_index":None}
    return {"risk":clamp(logistic((uv_index-6.0)/1.5))*clamp(is_day),"uv_index":uv_index}


def air_quality_screen(pm25: Optional[float], us_aqi: Optional[float], dust: Optional[float]) -> Dict[str, Any]:
    if pm25 is None and us_aqi is None and dust is None: return {"risk":None}
    p = 0.0 if pm25 is None else logistic((pm25-35)/15)
    a = 0.0 if us_aqi is None else logistic((us_aqi-100)/35)
    d = 0.0 if dust is None else logistic((dust-100)/70)
    return {"risk":clamp(max(p,a,d)),"pm2_5":pm25,"us_aqi":us_aqi,"dust":dust}


def fire_smoke_screen(context: Mapping[str, Any]) -> Dict[str, Any]:
    fires = context.get("fire_detections") or []
    nearest = None
    max_frp = 0.0
    for f in fires:
        d = safe_float(f.get("distance_km"))
        frp = safe_float(f.get("frp"),0) or 0
        if d is not None: nearest = d if nearest is None else min(nearest,d)
        max_frp=max(max_frp,frp)
    if nearest is None:
        return {"risk":0.0,"nearest_active_fire_km":None,"detections":0,"satellite_detection_only":True}
    distance_r = logistic((25-nearest)/8)
    intensity_r = logistic((max_frp-20)/20)
    return {"risk":clamp(0.70*distance_r+0.30*intensity_r),"nearest_active_fire_km":round(nearest,1),
            "detections":len(fires),"max_frp":max_frp,"satellite_detection_only":True}


def flood_screen(context: Mapping[str, Any], row: Mapping[str, Any]) -> Dict[str, Any]:
    flood = context.get("flood_context") or {}
    q = safe_float(flood.get("river_discharge"))
    qmed = safe_float(flood.get("river_discharge_median"))
    q75 = safe_float(flood.get("river_discharge_75th_percentile"))
    rain = safe_float(row.get("precipitation"),0) or 0
    terrain = context.get("terrain_context") or {}
    channel = bool(terrain.get("near_channel_or_gorge", False))
    q_r = 0.0
    if q is not None and qmed and qmed>0: q_r=max(q_r,logistic((q/qmed-1.7)/0.4))
    if q is not None and q75 and q75>0: q_r=max(q_r,logistic((q/q75-1.2)/0.25))
    rain_r=logistic((rain-8.0)/4.0)
    context_factor=1.2 if channel else 0.85
    r=clamp((0.65*q_r+0.35*rain_r)*context_factor)
    return {"risk":r,"river_discharge_m3s":q,"river_discharge_median_m3s":qmed,
            "near_channel_or_gorge":channel,"crossing_recommendation":None}


def route_terrain_summary(route_points: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    pts=[]
    for p in route_points or []:
        lat=safe_float(p.get("lat")); lon=safe_float(p.get("lon")); elev=safe_float(p.get("elevation_m"))
        if lat is not None and lon is not None:
            pts.append((lat,lon,elev))
    if not pts: return {"available":False}
    distance=0.0; ascent=0.0; descent=0.0; slopes=[]; elevs=[p[2] for p in pts if p[2] is not None]
    for a,b in zip(pts,pts[1:]):
        d=max(0.001,haversine_km(a[0],a[1],b[0],b[1]))
        distance+=d
        if a[2] is not None and b[2] is not None:
            dz=b[2]-a[2]
            if dz>0: ascent+=dz
            else: descent+=-dz
            slopes.append(math.degrees(math.atan2(abs(dz),d*1000.0)))
    return {"available":True,"points":len(pts),"distance_km":round(distance,2),"ascent_m":round(ascent,0),
            "descent_m":round(descent,0),"min_elevation_m":min(elevs) if elevs else None,
            "max_elevation_m":max(elevs) if elevs else None,"max_segment_slope_deg":max(slopes) if slopes else None,
            "slope_is_route_sampling_proxy":True}


def official_warning_assessment(context: Mapping[str, Any]) -> Dict[str, Any]:
    warnings = list(context.get("official_warnings") or [])
    avalanche = context.get("official_avalanche_bulletin")
    if isinstance(avalanche, Mapping): warnings.append(dict(avalanche, category="avalanche"))
    critical=False; severe=False; levels=[]
    for w in warnings:
        text=(str(w.get("severity", ""))+" "+str(w.get("level", ""))+" "+str(w.get("event", ""))).lower()
        if any(x in text for x in ("extreme","critical","emergency","warning","red")): critical=True
        if any(x in text for x in ("severe","high","watch","orange")): severe=True
        levels.append(text.strip())
    risk=0.97 if critical else 0.82 if severe else 0.55 if warnings else 0.0
    return {"present":bool(warnings),"critical":critical,"severe":severe,"risk_floor":risk,"count":len(warnings),
            "warnings":warnings,"highest_priority_input":True}


def analyze_hour(row: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    t=safe_float(row.get("temperature_2m"),0) or 0; rh=safe_float(row.get("relative_humidity_2m"),50) or 50
    wind=safe_float(row.get("wind_speed_10m"),0) or 0; gust=safe_float(row.get("wind_gusts_10m"),wind) or wind
    rain=safe_float(row.get("precipitation"),0) or 0; vis=safe_float(row.get("visibility"),10000) or 10000
    cape=safe_float(row.get("cape"),0) or 0; rad=safe_float(row.get("shortwave_radiation"),0) or 0
    terrain=context.get("terrain_context") or {}
    alt=safe_float(terrain.get("max_elevation_m"), safe_float(row.get("elevation"),0)) or 0
    thermal=thermal_screen(t,rh,wind,rad)
    altitude=altitude_exposure_screen(alt,safe_float(row.get("surface_pressure")))
    lightning=lightning_screen(cape,row.get("weather_code"),rain,gust,safe_float(row.get("lightning_density")))
    wind_r=wind_exposure_screen(wind,gust,terrain)
    visibility=visibility_screen(vis,rh,rain,row.get("weather_code"))
    surface=surface_condition_screen(row)
    snow=snow_ice_screen(row,terrain)
    flood=flood_screen(context,row)
    hazards={
        "heat":thermal["heat_risk"],"cold":thermal["cold_risk"],"altitude":altitude["risk"],
        "lightning":lightning["risk"],"wind":wind_r["risk"],"visibility":visibility["risk"],
        "surface":surface["risk"],"snow_ice":snow["risk"],"flood":flood["risk"],
    }
    # dependency-aware aggregation: max dominates, secondary hazards add less.
    vals=sorted(hazards.values(),reverse=True)
    integrated=clamp((vals[0] if vals else 0)+(0.22*vals[1] if len(vals)>1 else 0)+(0.10*vals[2] if len(vals)>2 else 0))
    return {"time":row.get("time"),"integrated_risk":integrated,"risk_level":risk_label(integrated),
            "hazards":hazards,"thermal":thermal,"altitude":altitude,"lightning":lightning,"wind":wind_r,
            "visibility":visibility,"surface":surface,"snow_ice":snow,"flood":flood,"raw":dict(row)}


def risk_label(r: float) -> str:
    if r >= 0.88: return "CRITICAL"
    if r >= 0.70: return "HIGH RISK"
    if r >= 0.45: return "CAUTION"
    return "NORMAL"


def summarize_hazards(hours: Sequence[Mapping[str, Any]], extra: Mapping[str, Any]) -> Dict[str, Any]:
    names=("heat","cold","altitude","lightning","wind","visibility","surface","snow_ice","flood")
    out={}
    for n in names:
        vals=[safe_float(h.get("hazards",{}).get(n),0) or 0 for h in hours]
        if vals:
            mx=max(vals); idx=vals.index(mx)
            out[n]={"max_probability":round(mx,4),"peak_time":hours[idx].get("time")}
    for n in ("air_quality","wildfire_smoke"):
        v=safe_float(extra.get(n,{}).get("risk"))
        if v is not None: out[n]={"max_probability":round(v,4),"peak_time":None}
    return out


def monte_carlo_risk(fused: Sequence[Mapping[str, Any]], context: Mapping[str, Any], scenarios: int = 120) -> Dict[str, Any]:
    if not fused: return {"scenarios":0}
    rng=random.Random(1771)
    peaks=[]
    sample_hours=list(fused[:min(72,len(fused))])
    for _ in range(max(20,min(500,scenarios))):
        peak=0.0
        for row in sample_hours:
            x=dict(row)
            for var in ("temperature_2m","relative_humidity_2m","precipitation","wind_speed_10m","wind_gusts_10m","visibility","cape","freezing_level_height","snowfall"):
                mu=safe_float(row.get(var)); sigma=safe_float(row.get(f"{var}_sigma"),0) or 0
                if mu is not None and sigma>0:
                    x[var]=rng.gauss(mu,sigma)
            peak=max(peak,analyze_hour(x,context)["integrated_risk"])
        peaks.append(peak)
    peaks.sort()
    def q(p): return peaks[min(len(peaks)-1,max(0,int(round((len(peaks)-1)*p))))]
    return {"scenarios":len(peaks),"median_peak_risk":round(q(.5),4),"p10":round(q(.1),4),"p90":round(q(.9),4),
            "p_risk_ge_0_70":round(sum(x>=.70 for x in peaks)/len(peaks),4),
            "p_risk_ge_0_88":round(sum(x>=.88 for x in peaks)/len(peaks),4)}


def confidence_score(fused: Sequence[Mapping[str, Any]], members: Sequence[ForecastMember], context: Mapping[str, Any], realtime: Mapping[str, Any]) -> Dict[str, Any]:
    model_q=statistics.mean(m.quality for m in members) if members else 0.0
    agreement=0.0
    if fused:
        spreads=[]
        for r in fused[:24]:
            t=safe_float(r.get("temperature_2m_spread"),0) or 0
            w=safe_float(r.get("wind_gusts_10m_spread"),0) or 0
            p=safe_float(r.get("precipitation_spread"),0) or 0
            spreads.append(math.exp(-0.12*t-0.03*w-0.08*p))
        agreement=statistics.mean(spreads) if spreads else 0.5
    route=0.15 if context.get("route_points") else 0.0
    official=0.10 if context.get("official_warnings") or context.get("official_avalanche_bulletin") else 0.0
    live=0.10 if realtime.get("status") == "ok" else 0.03 if realtime.get("enabled") else 0.0
    overall=clamp(0.48*model_q+0.32*agreement+route+official+live,0.05,0.97)
    return {"overall":round(overall,4),"model_quality":round(model_q,4),"model_agreement":round(agreement,4),
            "route_context_bonus":route,"official_context_bonus":official,"realtime_bonus":live}


def precision_level(context: Mapping[str, Any], members: int, realtime: Mapping[str, Any]) -> Tuple[str,List[str]]:
    missing=[]
    if members < 2: missing.append("multi-model consensus")
    if not context.get("route_points"): missing.append("route elevation profile")
    if not context.get("terrain_context"): missing.append("terrain exposure context")
    if not context.get("official_warnings"): missing.append("jurisdiction-specific official warning feed")
    if not realtime.get("enabled"): missing.append("realtime environmental feeds")
    if not missing: return "LOCALLY_CONTEXTUALIZED", missing
    if len(missing)<=2: return "ROUTE_AWARE", missing
    if members>=2: return "WEATHER_AWARE", missing
    return "SCREENING_ONLY", missing


def build_action_flags(summary: Mapping[str, Any], official: Mapping[str, Any], context: Mapping[str, Any]) -> List[Dict[str,Any]]:
    flags=[]
    if official.get("critical"):
        flags.append({"priority":1,"type":"official_warning","action":"Do not rely on VYOM to override the official warning; follow the issuing authority.","probability":official.get("risk_floor")})
    mapping={
        "lightning":"Avoid exposed outdoor terrain while thunderstorm/lightning risk is elevated.",
        "flood":"Avoid entering flood-prone channels, gorges or crossings while flood risk is elevated.",
        "wildfire_smoke":"Avoid wildfire-affected areas and follow local emergency/land-management guidance.",
        "snow_ice":"Treat snow/ice conditions conservatively and use official local mountain/snow bulletins where available.",
        "visibility":"Avoid route-finding dependence on visibility when modeled visibility risk is high.",
        "wind":"Avoid exposed ridges/summits when modeled wind exposure is high.",
        "heat":"Reduce outdoor exposure during severe heat load and seek cooler/sheltered conditions.",
        "cold":"Avoid prolonged exposure during severe cold/wind-chill conditions.",
        "air_quality":"Reduce outdoor exposure when smoke/air-quality risk is high.",
    }
    for k,msg in mapping.items():
        p=safe_float((summary.get(k) or {}).get("max_probability"))
        if p is not None and p>=0.65:
            flags.append({"priority":2 if p>=0.85 else 3,"type":k,"action":msg,"probability":round(p,4)})
    flags.sort(key=lambda x:x["priority"])
    return flags


def _merge_live_patch(base: Dict[str,Any], patch: Mapping[str,Any]) -> Dict[str,Any]:
    out=dict(base)
    for k,v in (patch or {}).items():
        if v is None: continue
        if isinstance(v,list):
            if not out.get(k): out[k]=v
        elif isinstance(v,Mapping):
            existing=out.get(k)
            if not isinstance(existing,Mapping): out[k]=dict(v)
            else:
                merged=dict(v); merged.update(existing); out[k]=merged
        elif out.get(k) is None:
            out[k]=v
    return out


def build_adventure_advisory(
    lat: float,
    lon: float,
    adventure_config: Optional[Mapping[str,Any]] = None,
    horizon_hours: int = 72,
    scenario_count: int = 120,
    models: Sequence[str] = DEFAULT_MODELS,
    calibration_path: Optional[str] = None,
) -> Dict[str,Any]:
    started=time.time(); horizon=max(6,min(168,int(horizon_hours)))
    context=dict(adventure_config or {})
    realtime={"enabled":False}; service=None
    if os.getenv("VYOM_ADVENTURE_REAL_FEEDS_ENABLED","1") == "1":
        try:
            from adventure_realtime_service import AdventureRealtimeService
            service=AdventureRealtimeService()
            live=service.collect_live_context(lat,lon,context)
            context=_merge_live_patch(context,live.get("patch") or {})
            realtime={"enabled":True,"status":live.get("status","ok"),"sources":live.get("sources",[]),"elapsed_ms":live.get("elapsed_ms")}
            if calibration_path is None and live.get("dynamic_calibration_path"): calibration_path=live.get("dynamic_calibration_path")
        except Exception as exc:
            realtime={"enabled":True,"status":"degraded","error":str(exc)}
    calibration_path=calibration_path or os.getenv("VYOM_ADVENTURE_CALIBRATION_PATH")
    cal=CalibrationStore.from_json(calibration_path)
    members=[]
    for m in models:
        fm=fetch_model_member(lat,lon,m,horizon,cal)
        if fm: members.append(fm)
    if not members:
        return {"engine":ENGINE_NAME,"version":ENGINE_VERSION,"status":"error","reason":"No numerical outdoor-weather forecast members were available.","safe_to_proceed":None,"confidence":{"overall":0.0},"realtime":realtime}
    fused=fuse_members(members,horizon)
    # Live environmental extras
    aq=air_quality_screen(safe_float(context.get("pm2_5")),safe_float(context.get("us_aqi")),safe_float(context.get("dust")))
    fire=fire_smoke_screen(context)
    official=official_warning_assessment(context)
    route=route_terrain_summary(context.get("route_points") or [])
    if route.get("available"):
        tc=dict(context.get("terrain_context") or {})
        if tc.get("max_elevation_m") is None: tc["max_elevation_m"]=route.get("max_elevation_m")
        context["terrain_context"]=tc
    hours=[analyze_hour(r,context) for r in fused]
    extra={"air_quality":aq,"wildfire_smoke":fire}
    summary=summarize_hazards(hours,extra)
    deterministic=max((h["integrated_risk"] for h in hours),default=0.0)
    deterministic=max(deterministic, safe_float(aq.get("risk"),0) or 0, safe_float(fire.get("risk"),0) or 0, safe_float(official.get("risk_floor"),0) or 0)
    probabilistic=monte_carlo_risk(fused,context,scenario_count)
    confidence=confidence_score(fused,members,context,realtime)
    precision,missing=precision_level(context,len(members),realtime)
    abstain=[]
    if confidence["overall"]<0.45: abstain.append("Low overall confidence")
    if len(members)<2: abstain.append("No multi-model consensus")
    if route.get("available") is False: abstain.append("No route elevation profile")
    if not official.get("present"): abstain.append("No jurisdiction-specific official warning/bulletin context")
    # Official avalanche bulletin is required before any avalanche-related interpretation.
    if (context.get("terrain_context") or {}).get("snow_terrain") and not context.get("official_avalanche_bulletin"):
        abstain.append("No official avalanche bulletin; snow-instability output is weather context only")
    flags=build_action_flags(summary,official,context)
    timeline=[{"time":h["time"],"risk":round(h["integrated_risk"],4),"level":h["risk_level"]} for h in hours]
    peaks=sorted(hours,key=lambda x:x["integrated_risk"],reverse=True)[:6]
    out={
        "engine":ENGINE_NAME,"version":ENGINE_VERSION,"status":"ok","generated_at":datetime.now(timezone.utc).isoformat(),
        "lat":lat,"lon":lon,"horizon_hours":horizon,
        "activity_profile":{k:v for k,v in context.items() if k not in {"official_warnings","fire_detections"}},
        "forecast_models":[{"model":m.model,"quality":round(m.quality,4),"hours":len(m.hours)} for m in members],
        "risk":{"integrated_peak_probability":round(deterministic,4),"level":risk_label(deterministic),"hazards":summary,"probabilistic":probabilistic},
        "timeline":timeline,"peak_hours":peaks,"route_terrain":route,
        "air_quality":aq,"wildfire_smoke":fire,"official_warnings":official,
        "confidence":confidence,"precision":{"level":precision,"missing_or_unverified":missing,"abstention_reasons":abstain},
        "action_flags":flags,
        "safe_to_proceed":None,"technical_climbing_clearance":None,"river_crossing_clearance":None,"avalanche_clearance":None,
        "field_semantics":{
            "risk":"Modeled outdoor environmental exposure screening; risk is not confidence.",
            "snow":"Snow/ice instability context is not an avalanche forecast unless an official bulletin is supplied.",
            "flood":"Regional discharge/flood context is not a local river-crossing measurement.",
            "wildfire":"Satellite fire detections are context, not an official incident perimeter or escape route."
        },
        "engine_diagnostics":{
            "engine_executed":True,
            "validation_contract":"risk_confidence_precision_authority_separated",
            "guarded_narration_expected":True
        },
        "safety_contract":{
            "decision_support_only":True,
            "official_warnings_take_precedence":True,
            "local_land_manager_and_rescue_guidance_take_precedence":True,
            "not_a_route_clearance":True,
            "message":"VYOM-ADVENTURE screens environmental exposure. It does not certify a route, crossing, climb, snow slope or wildfire-area journey as safe."
        },
        "realtime":realtime,"elapsed_ms":round((time.time()-started)*1000,1),
    }
    if service is not None:
        try:
            service.record_model_forecast(lat,lon,fused,members)
            out["realtime"]["forecast_recorded_for_calibration"]=True
        except Exception as exc:
            out["realtime"]["forecast_record_error"]=str(exc)
    return out


__all__=[
    "ENGINE_NAME","ENGINE_VERSION","CalibrationStore","ForecastMember","build_adventure_advisory",
    "wet_bulb_stull_c","wind_chill_c","heat_index_c","altitude_exposure_screen","thermal_screen",
    "lightning_screen","wind_exposure_screen","visibility_screen","surface_condition_screen",
    "snow_ice_screen","flood_screen","route_terrain_summary","monte_carlo_risk"
]
