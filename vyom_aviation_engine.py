"""
VYOM-AVIATION — probabilistic aviation-weather decision-support engine.

This module is intentionally NOT a flight-dispatch, ATC, airworthiness, or
operational-clearance system. It converts numerical weather, aviation weather
observations/advisories, airport/runway context, and optional aircraft limits
into structured screening diagnostics with explicit uncertainty and abstention.

Design principles
-----------------
1. Official SIGMET/TAF/METAR/airport/operator information outranks derived model
   convenience guidance.
2. Risk and confidence are separate quantities.
3. Aircraft-specific limits are never invented. If a required approved limit is
   missing, the corresponding output is screening-only or withheld.
4. Numerical weather-model uncertainty is propagated through multi-model fusion
   and Monte-Carlo scenarios.
5. The LLM may explain this engine's structured output, but should not recompute
   or override its calculations.
"""
from __future__ import annotations

import json
import math
import os
import random
import statistics
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import requests

ENGINE_NAME = "VYOM-AVIATION"
ENGINE_VERSION = "1.1.0"
G = 9.80665
R_D = 287.05287
KELVIN = 273.15
MPS_PER_KT = 0.514444
FT_PER_M = 3.280839895
M_PER_FT = 1.0 / FT_PER_M
HPA_STD = 1013.25
T0_K = 288.15
P0_PA = 101325.0
LAPSE_K_M = 0.0065

DEFAULT_MODELS = (
    "ecmwf_ifs025",
    "gfs_seamless",
    "icon_seamless",
    "gem_seamless",
)

# Base skill priors are intentionally modest; learned calibration should replace
# them when verified forecast/observation pairs become available.
MODEL_PRIOR_QUALITY = {
    "ecmwf_ifs025": 0.87,
    "gfs_seamless": 0.82,
    "icon_seamless": 0.80,
    "gem_seamless": 0.77,
    "best_match": 0.78,
}

PRESSURE_LEVELS = (1000, 925, 850, 700, 600, 500, 400, 300, 250, 200)


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return default
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def logistic(x: float) -> float:
    x = max(-60.0, min(60.0, x))
    return 1.0 / (1.0 + math.exp(-x))


def circular_diff_deg(a: float, b: float) -> float:
    return (float(a) - float(b) + 180.0) % 360.0 - 180.0


def circular_mean_deg(values: Sequence[float], weights: Optional[Sequence[float]] = None) -> Optional[float]:
    vals = [safe_float(v) for v in values]
    pairs = [(v, 1.0 if weights is None else safe_float(weights[i], 0.0) or 0.0) for i, v in enumerate(vals) if v is not None]
    if not pairs:
        return None
    sx = sum(w * math.sin(math.radians(v)) for v, w in pairs)
    cx = sum(w * math.cos(math.radians(v)) for v, w in pairs)
    if abs(sx) + abs(cx) < 1e-12:
        return float(pairs[0][0]) % 360.0
    return math.degrees(math.atan2(sx, cx)) % 360.0


def weighted_mean(values: Sequence[Optional[float]], weights: Sequence[float]) -> Optional[float]:
    pairs = [(float(v), max(0.0, float(w))) for v, w in zip(values, weights) if v is not None and math.isfinite(float(v))]
    sw = sum(w for _, w in pairs)
    if not pairs or sw <= 0:
        return None
    return sum(v * w for v, w in pairs) / sw


def weighted_std(values: Sequence[Optional[float]], weights: Sequence[float]) -> float:
    mu = weighted_mean(values, weights)
    if mu is None:
        return 0.0
    pairs = [(float(v), max(0.0, float(w))) for v, w in zip(values, weights) if v is not None and math.isfinite(float(v))]
    sw = sum(w for _, w in pairs)
    if sw <= 0:
        return 0.0
    return math.sqrt(sum(w * (v - mu) ** 2 for v, w in pairs) / sw)


def vector_from_wind(speed_kt: float, direction_from_deg: float) -> Tuple[float, float]:
    """Meteorological wind (direction FROM) -> east/north m/s components."""
    s = float(speed_kt) * MPS_PER_KT
    r = math.radians(float(direction_from_deg))
    return -s * math.sin(r), -s * math.cos(r)


def vector_difference_kt(speed1: float, dir1: float, speed2: float, dir2: float) -> float:
    u1, v1 = vector_from_wind(speed1, dir1)
    u2, v2 = vector_from_wind(speed2, dir2)
    return math.hypot(u2 - u1, v2 - v1) / MPS_PER_KT


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2.0 * math.asin(math.sqrt(a))


def season_key(dt: datetime, lat: float = 0.0) -> str:
    m = dt.month
    if m in (12, 1, 2): s = "DJF"
    elif m in (3, 4, 5): s = "MAM"
    elif m in (6, 7, 8): s = "JJA"
    else: s = "SON"
    if lat < 0:
        s = {"DJF": "JJA", "JJA": "DJF", "MAM": "SON", "SON": "MAM"}[s]
    return s


def lead_bin(hours: int) -> str:
    h = int(hours)
    if h <= 6: return "0_6"
    if h <= 24: return "7_24"
    if h <= 72: return "25_72"
    if h <= 120: return "73_120"
    return "121_plus"


@dataclass
class CalibrationRecord:
    bias: float = 0.0
    rmse: float = 1.0
    reliability: float = 0.72
    samples: int = 0

    @property
    def quality(self) -> float:
        n_factor = 0.5 + 0.5 * min(1.0, self.samples / 150.0)
        return clamp(self.reliability * n_factor * math.exp(-0.08 * max(0.0, self.rmse - 1.0)), 0.15, 0.98)


class CalibrationStore:
    """Hierarchical JSON calibration lookup.

    Accepts keys with progressively simpler specificity, for example:
      model|variable|SON|25_72
      model|variable|SON|*
      model|variable|*|*
      model|*|*|*
    """
    def __init__(self, payload: Optional[Mapping[str, Any]] = None):
        self.payload = dict(payload or {})

    @classmethod
    def from_json(cls, path: Optional[str]) -> "CalibrationStore":
        if not path:
            return cls()
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
        for key in keys:
            row = self.payload.get(key)
            if isinstance(row, Mapping):
                return CalibrationRecord(
                    bias=safe_float(row.get("bias"), 0.0) or 0.0,
                    rmse=max(0.01, safe_float(row.get("rmse"), 1.0) or 1.0),
                    reliability=clamp(safe_float(row.get("reliability"), 0.72) or 0.72),
                    samples=max(0, int(safe_float(row.get("samples"), 0) or 0)),
                )
        # variable-aware fallback uncertainty scales
        defaults = {
            "temperature_2m": 1.8, "visibility_m": 2500.0, "wind_speed_10m_kt": 5.0,
            "wind_gust_10m_kt": 8.0, "surface_pressure_hpa": 2.5, "cape_jkg": 300.0,
            "freezing_level_ft": 1600.0, "cloud_base_ft": 1200.0,
            "precipitation_mm": 1.2,
        }
        return CalibrationRecord(rmse=defaults.get(variable, 2.0), reliability=0.65, samples=0)


@dataclass
class AviationHour:
    time: str
    temperature_c: Optional[float] = None
    dewpoint_c: Optional[float] = None
    rh_pct: Optional[float] = None
    surface_pressure_hpa: Optional[float] = None
    pressure_msl_hpa: Optional[float] = None
    visibility_m: Optional[float] = None
    cloud_cover_pct: Optional[float] = None
    cloud_cover_low_pct: Optional[float] = None
    cloud_cover_mid_pct: Optional[float] = None
    cloud_cover_high_pct: Optional[float] = None
    precipitation_mm: Optional[float] = None
    snowfall_cm: Optional[float] = None
    weather_code: Optional[float] = None
    wind_speed_10m_kt: Optional[float] = None
    wind_direction_10m_deg: Optional[float] = None
    wind_gust_10m_kt: Optional[float] = None
    cape_jkg: Optional[float] = None
    cin_jkg: Optional[float] = None
    lifted_index_c: Optional[float] = None
    freezing_level_ft: Optional[float] = None
    boundary_layer_height_m: Optional[float] = None
    pressure_levels: Dict[str, Dict[str, Optional[float]]] = None

    def __post_init__(self):
        if self.pressure_levels is None:
            self.pressure_levels = {}


@dataclass
class ForecastMember:
    model: str
    hours: List[AviationHour]
    quality: float


def _hourly_at(hourly: Mapping[str, Any], key: str, i: int) -> Optional[float]:
    arr = hourly.get(key) or []
    if i >= len(arr):
        return None
    return safe_float(arr[i])


def _build_open_meteo_url(lat: float, lon: float, model: str, horizon: int) -> Tuple[str, str]:
    surf = [
        "temperature_2m", "relative_humidity_2m", "dew_point_2m", "surface_pressure", "pressure_msl",
        "visibility", "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
        "precipitation", "snowfall", "weather_code", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
        "cape", "convective_inhibition", "lifted_index", "freezing_level_height", "boundary_layer_height",
    ]
    upper = []
    for p in PRESSURE_LEVELS:
        upper += [
            f"temperature_{p}hPa", f"relative_humidity_{p}hPa", f"geopotential_height_{p}hPa",
            f"wind_speed_{p}hPa", f"wind_direction_{p}hPa", f"vertical_velocity_{p}hPa",
        ]
    params = ",".join(surf + upper)
    base = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        f"&hourly={params}&forecast_hours={horizon}&timezone=UTC&wind_speed_unit=kn&models={model}"
    )
    # Reduced fallback for models which do not expose every diagnostic.
    fallback = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        f"&hourly={','.join(surf)}&forecast_hours={horizon}&timezone=UTC&wind_speed_unit=kn&models={model}"
    )
    return base, fallback


def fetch_model_member(lat: float, lon: float, model: str, horizon: int, calibration: CalibrationStore, timeout: int = 12) -> Optional[ForecastMember]:
    urls = _build_open_meteo_url(lat, lon, model, horizon)
    payload = None
    for url in urls:
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "VYOM-Aviation/1.0"})
            if r.status_code == 200:
                candidate = r.json()
                if candidate.get("hourly", {}).get("time"):
                    payload = candidate
                    break
        except Exception:
            continue
    if not payload:
        return None
    h = payload.get("hourly") or {}
    times = h.get("time") or []
    now = datetime.now(timezone.utc)
    season = season_key(now, lat)
    out: List[AviationHour] = []
    qualities: List[float] = []
    for i, ts in enumerate(times[:horizon]):
        lead = i + 1
        def corrected(key: str, calvar: str, scale: float = 1.0) -> Optional[float]:
            v = _hourly_at(h, key, i)
            if v is None: return None
            rec = calibration.get(model, calvar, lead, season)
            qualities.append(rec.quality)
            return (v * scale) - rec.bias
        levels: Dict[str, Dict[str, Optional[float]]] = {}
        for p in PRESSURE_LEVELS:
            levels[str(p)] = {
                "temperature_c": _hourly_at(h, f"temperature_{p}hPa", i),
                "rh_pct": _hourly_at(h, f"relative_humidity_{p}hPa", i),
                "height_m": _hourly_at(h, f"geopotential_height_{p}hPa", i),
                "wind_speed_kt": _hourly_at(h, f"wind_speed_{p}hPa", i),
                "wind_direction_deg": _hourly_at(h, f"wind_direction_{p}hPa", i),
                "vertical_velocity": _hourly_at(h, f"vertical_velocity_{p}hPa", i),
            }
        out.append(AviationHour(
            time=str(ts),
            temperature_c=corrected("temperature_2m", "temperature_2m"),
            dewpoint_c=_hourly_at(h, "dew_point_2m", i),
            rh_pct=_hourly_at(h, "relative_humidity_2m", i),
            surface_pressure_hpa=corrected("surface_pressure", "surface_pressure_hpa"),
            pressure_msl_hpa=_hourly_at(h, "pressure_msl", i),
            visibility_m=max(0.0, corrected("visibility", "visibility_m") or 0.0) if _hourly_at(h, "visibility", i) is not None else None,
            cloud_cover_pct=_hourly_at(h, "cloud_cover", i),
            cloud_cover_low_pct=_hourly_at(h, "cloud_cover_low", i),
            cloud_cover_mid_pct=_hourly_at(h, "cloud_cover_mid", i),
            cloud_cover_high_pct=_hourly_at(h, "cloud_cover_high", i),
            precipitation_mm=max(0.0, corrected("precipitation", "precipitation_mm") or 0.0) if _hourly_at(h, "precipitation", i) is not None else None,
            snowfall_cm=_hourly_at(h, "snowfall", i),
            weather_code=_hourly_at(h, "weather_code", i),
            wind_speed_10m_kt=max(0.0, corrected("wind_speed_10m", "wind_speed_10m_kt") or 0.0) if _hourly_at(h, "wind_speed_10m", i) is not None else None,
            wind_direction_10m_deg=_hourly_at(h, "wind_direction_10m", i),
            wind_gust_10m_kt=max(0.0, corrected("wind_gusts_10m", "wind_gust_10m_kt") or 0.0) if _hourly_at(h, "wind_gusts_10m", i) is not None else None,
            cape_jkg=max(0.0, _hourly_at(h, "cape", i) or 0.0) if _hourly_at(h, "cape", i) is not None else None,
            cin_jkg=_hourly_at(h, "convective_inhibition", i),
            lifted_index_c=_hourly_at(h, "lifted_index", i),
            freezing_level_ft=(_hourly_at(h, "freezing_level_height", i) or 0.0) * FT_PER_M if _hourly_at(h, "freezing_level_height", i) is not None else None,
            boundary_layer_height_m=_hourly_at(h, "boundary_layer_height", i),
            pressure_levels=levels,
        ))
    if not out:
        return None
    q = statistics.mean(qualities) if qualities else MODEL_PRIOR_QUALITY.get(model, 0.68)
    q = 0.65 * q + 0.35 * MODEL_PRIOR_QUALITY.get(model, 0.68)
    return ForecastMember(model=model, hours=out, quality=clamp(q, 0.2, 0.98))


def fuse_members(members: Sequence[ForecastMember], horizon: int) -> List[Dict[str, Any]]:
    if not members:
        return []
    result = []
    numeric_fields = [
        "temperature_c", "dewpoint_c", "rh_pct", "surface_pressure_hpa", "pressure_msl_hpa", "visibility_m",
        "cloud_cover_pct", "cloud_cover_low_pct", "cloud_cover_mid_pct", "cloud_cover_high_pct", "precipitation_mm",
        "snowfall_cm", "weather_code", "wind_speed_10m_kt", "wind_gust_10m_kt", "cape_jkg", "cin_jkg",
        "lifted_index_c", "freezing_level_ft", "boundary_layer_height_m",
    ]
    for i in range(min(horizon, max(len(m.hours) for m in members))):
        valid = [m for m in members if i < len(m.hours)]
        weights = [m.quality for m in valid]
        row: Dict[str, Any] = {"time": valid[0].hours[i].time, "model_count": len(valid)}
        for field in numeric_fields:
            vals = [getattr(m.hours[i], field) for m in valid]
            row[field] = weighted_mean(vals, weights)
            row[field + "_spread"] = weighted_std(vals, weights)
        dirs = [m.hours[i].wind_direction_10m_deg for m in valid]
        dvals, dweights = [], []
        for d, w in zip(dirs, weights):
            if d is not None:
                dvals.append(d); dweights.append(w)
        row["wind_direction_10m_deg"] = circular_mean_deg(dvals, dweights) if dvals else None
        # Pressure-level fusion
        levels: Dict[str, Dict[str, Optional[float]]] = {}
        for p in PRESSURE_LEVELS:
            key = str(p)
            level_rows = [m.hours[i].pressure_levels.get(key, {}) for m in valid]
            lr: Dict[str, Optional[float]] = {}
            for f in ("temperature_c", "rh_pct", "height_m", "wind_speed_kt", "vertical_velocity"):
                lr[f] = weighted_mean([x.get(f) for x in level_rows], weights)
            dv, dw = [], []
            for x, w in zip(level_rows, weights):
                if x.get("wind_direction_deg") is not None:
                    dv.append(x["wind_direction_deg"]); dw.append(w)
            lr["wind_direction_deg"] = circular_mean_deg(dv, dw) if dv else None
            levels[key] = lr
        row["pressure_levels"] = levels
        row["model_quality"] = weighted_mean([m.quality for m in valid], [1.0]*len(valid)) or 0.0
        result.append(row)
    return result


# ---------------------------------------------------------------------------
# Atmosphere / aircraft performance screening physics
# ---------------------------------------------------------------------------
def isa_temperature_c(altitude_ft: float) -> float:
    h = max(0.0, float(altitude_ft)) * M_PER_FT
    if h <= 11000.0:
        return 15.0 - 0.0065 * h
    return -56.5


def isa_pressure_hpa(altitude_ft: float) -> float:
    h = max(0.0, float(altitude_ft)) * M_PER_FT
    if h <= 11000.0:
        return HPA_STD * (1.0 - LAPSE_K_M * h / T0_K) ** 5.2558797
    p11 = HPA_STD * (1.0 - LAPSE_K_M * 11000.0 / T0_K) ** 5.2558797
    return p11 * math.exp(-G * (h - 11000.0) / (R_D * 216.65))


def pressure_altitude_ft(field_elevation_ft: float, altimeter_hpa: float) -> float:
    # Operational approximation: approximately 27 ft per hPa near standard pressure.
    return float(field_elevation_ft) + (HPA_STD - float(altimeter_hpa)) * 27.0


def density_altitude_ft(field_elevation_ft: float, altimeter_hpa: float, oat_c: float) -> Dict[str, float]:
    pa = pressure_altitude_ft(field_elevation_ft, altimeter_hpa)
    isa_c = isa_temperature_c(pa)
    da = pa + 120.0 * (float(oat_c) - isa_c)
    return {"pressure_altitude_ft": round(pa, 1), "isa_temperature_c": round(isa_c, 1), "density_altitude_ft": round(da, 1)}


def runway_wind_components(wind_speed_kt: float, wind_from_deg: float, runway_heading_deg: float) -> Dict[str, float]:
    a = math.radians(circular_diff_deg(wind_from_deg, runway_heading_deg))
    head = float(wind_speed_kt) * math.cos(a)
    cross = float(wind_speed_kt) * math.sin(a)
    return {
        "headwind_kt": round(max(0.0, head), 2),
        "tailwind_kt": round(max(0.0, -head), 2),
        "crosswind_kt": round(abs(cross), 2),
        "crosswind_signed_kt": round(cross, 2),
        "relative_angle_deg": round(abs(circular_diff_deg(wind_from_deg, runway_heading_deg)), 1),
    }


def estimate_lcl_cloud_base_ft(temp_c: Optional[float], dewpoint_c: Optional[float]) -> Optional[float]:
    if temp_c is None or dewpoint_c is None:
        return None
    spread = max(0.0, float(temp_c) - float(dewpoint_c))
    # Approximate convective cloud base only, not a substitute for observed ceiling.
    return spread * 410.0


def flight_category(ceiling_ft_agl: Optional[float], visibility_m: Optional[float]) -> Optional[str]:
    if ceiling_ft_agl is None and visibility_m is None:
        return None
    vis_sm = None if visibility_m is None else visibility_m / 1609.344
    # Common U.S. aviation-weather categories. Ceiling uses BKN/OVC/VV when
    # observed; estimated LCL must be tagged separately by the caller.
    if (ceiling_ft_agl is not None and ceiling_ft_agl < 500) or (vis_sm is not None and vis_sm < 1):
        return "LIFR"
    if (ceiling_ft_agl is not None and ceiling_ft_agl < 1000) or (vis_sm is not None and vis_sm < 3):
        return "IFR"
    if (ceiling_ft_agl is not None and ceiling_ft_agl <= 3000) or (vis_sm is not None and vis_sm <= 5):
        return "MVFR"
    return "VFR"


def potential_temperature_k(temp_c: float, pressure_hpa: float) -> float:
    return (float(temp_c) + KELVIN) * (1000.0 / float(pressure_hpa)) ** 0.2854


def gradient_richardson_number(lower: Mapping[str, Any], upper: Mapping[str, Any], p_lower: float, p_upper: float) -> Optional[float]:
    z1, z2 = safe_float(lower.get("height_m")), safe_float(upper.get("height_m"))
    t1, t2 = safe_float(lower.get("temperature_c")), safe_float(upper.get("temperature_c"))
    s1, d1 = safe_float(lower.get("wind_speed_kt")), safe_float(lower.get("wind_direction_deg"))
    s2, d2 = safe_float(upper.get("wind_speed_kt")), safe_float(upper.get("wind_direction_deg"))
    if None in (z1, z2, t1, t2, s1, d1, s2, d2) or abs(z2-z1) < 20:
        return None
    dz = z2 - z1
    th1, th2 = potential_temperature_k(t1, p_lower), potential_temperature_k(t2, p_upper)
    thbar = 0.5 * (th1 + th2)
    dth_dz = (th2 - th1) / dz
    u1, v1 = vector_from_wind(s1, d1); u2, v2 = vector_from_wind(s2, d2)
    shear2 = ((u2-u1)/dz)**2 + ((v2-v1)/dz)**2
    if shear2 < 1e-10:
        return None
    return (G / thbar) * dth_dz / shear2


def vertical_shear_s1(lower: Mapping[str, Any], upper: Mapping[str, Any]) -> Optional[float]:
    z1, z2 = safe_float(lower.get("height_m")), safe_float(upper.get("height_m"))
    s1, d1 = safe_float(lower.get("wind_speed_kt")), safe_float(lower.get("wind_direction_deg"))
    s2, d2 = safe_float(upper.get("wind_speed_kt")), safe_float(upper.get("wind_direction_deg"))
    if None in (z1, z2, s1, d1, s2, d2) or abs(z2-z1) < 20:
        return None
    u1, v1 = vector_from_wind(s1, d1); u2, v2 = vector_from_wind(s2, d2)
    return math.hypot(u2-u1, v2-v1) / abs(z2-z1)


def ellrod_turbulence_index(vertical_shear: Optional[float], deformation_s1: Optional[float], convergence_s1: float = 0.0) -> Optional[float]:
    if vertical_shear is None or deformation_s1 is None:
        return None
    # Diagnostic proxy in scaled units. Horizontal deformation must be supplied
    # from a surrounding grid; a single-point forecast cannot estimate it.
    return max(0.0, vertical_shear * max(0.0, deformation_s1 - convergence_s1) * 1e6)


def turbulence_screen(row: Mapping[str, Any], aircraft: Mapping[str, Any]) -> Dict[str, Any]:
    levels = row.get("pressure_levels") or {}
    pair_metrics = []
    for p1, p2 in zip(PRESSURE_LEVELS[:-1], PRESSURE_LEVELS[1:]):
        l, u = levels.get(str(p1), {}), levels.get(str(p2), {})
        ri = gradient_richardson_number(l, u, p1, p2)
        sh = vertical_shear_s1(l, u)
        if ri is not None or sh is not None:
            pair_metrics.append({"layer_hpa": f"{p1}-{p2}", "ri": ri, "shear_s1": sh})
    valid_ri = [x["ri"] for x in pair_metrics if x["ri"] is not None]
    valid_sh = [x["shear_s1"] for x in pair_metrics if x["shear_s1"] is not None]
    min_ri = min(valid_ri) if valid_ri else None
    max_shear = max(valid_sh) if valid_sh else None
    cape = safe_float(row.get("cape_jkg"), 0.0) or 0.0
    cruise_wind = 0.0
    for p in (300, 250, 200):
        cruise_wind = max(cruise_wind, safe_float(levels.get(str(p), {}).get("wind_speed_kt"), 0.0) or 0.0)
    x = -2.7
    if min_ri is not None:
        x += 2.5 * max(0.0, (0.5 - min_ri) / 0.5)
    if max_shear is not None:
        x += 1.5 * clamp((max_shear - 0.006) / 0.018)
    x += 0.65 * clamp((cruise_wind - 60.0) / 80.0)
    x += 0.55 * clamp((cape - 500.0) / 2500.0)
    pirep = aircraft.get("pirep_context") or []
    pirep_boost = 0.0
    for r in pirep:
        txt = json.dumps(r).upper()
        if "SEV" in txt or "EXTRM" in txt: pirep_boost = max(pirep_boost, 1.6)
        elif "MOD" in txt: pirep_boost = max(pirep_boost, 0.9)
        elif "LGT" in txt: pirep_boost = max(pirep_boost, 0.4)
    risk = logistic(x + pirep_boost)
    return {
        "probability": round(risk, 4),
        "minimum_gradient_richardson_number": None if min_ri is None else round(min_ri, 3),
        "maximum_vertical_shear_s1": None if max_shear is None else round(max_shear, 5),
        "upper_wind_max_kt": round(cruise_wind, 1),
        "layers": pair_metrics,
        "pirep_adjustment": pirep_boost,
        "edr": None,
        "edr_note": "EDR is withheld unless an authoritative/model EDR field is supplied; this is a physics screening proxy.",
    }


def icing_screen(row: Mapping[str, Any], aircraft: Mapping[str, Any]) -> Dict[str, Any]:
    levels = row.get("pressure_levels") or {}
    candidates = []
    for p in PRESSURE_LEVELS:
        lv = levels.get(str(p), {})
        t, rh = safe_float(lv.get("temperature_c")), safe_float(lv.get("rh_pct"))
        z = safe_float(lv.get("height_m"))
        if t is None or rh is None or z is None:
            continue
        thermal = math.exp(-((t + 8.0) / 10.0) ** 2) if -25 <= t <= 3 else 0.0
        moisture = clamp((rh - 70.0) / 30.0)
        score = thermal * moisture
        candidates.append({"pressure_hpa": p, "altitude_ft": z*FT_PER_M, "temperature_c": t, "rh_pct": rh, "score": score})
    max_score = max((x["score"] for x in candidates), default=0.0)
    precip = safe_float(row.get("precipitation_mm"), 0.0) or 0.0
    surface_t = safe_float(row.get("temperature_c"))
    freezing = safe_float(row.get("freezing_level_ft"))
    icing_pirep = 0.0
    for r in aircraft.get("pirep_context") or []:
        txt = json.dumps(r).upper()
        if "IC" in txt or "ICING" in txt:
            if "SEV" in txt or "HVY" in txt: icing_pirep = max(icing_pirep, 1.8)
            elif "MOD" in txt: icing_pirep = max(icing_pirep, 1.1)
            else: icing_pirep = max(icing_pirep, 0.55)
    x = -3.0 + 4.0*max_score + 0.55*clamp(precip/3.0) + icing_pirep
    risk = logistic(x)
    icing_approved = bool(aircraft.get("icing_approved", False))
    return {
        "probability": round(risk, 4),
        "candidate_layers": sorted(candidates, key=lambda z: z["score"], reverse=True)[:5],
        "freezing_level_ft": freezing,
        "aircraft_icing_approved_reported": icing_approved,
        "operational_clearance": None,
        "note": "Meteorological icing-conduciveness screening only; certification/AFM/operator limitations and official icing products take precedence.",
    }


def convective_screen(row: Mapping[str, Any], aircraft: Mapping[str, Any]) -> Dict[str, Any]:
    cape = safe_float(row.get("cape_jkg"), 0.0) or 0.0
    cin = safe_float(row.get("cin_jkg"), 0.0) or 0.0
    li = safe_float(row.get("lifted_index_c"), 1.0)
    precip = safe_float(row.get("precipitation_mm"), 0.0) or 0.0
    gust = safe_float(row.get("wind_gust_10m_kt"), 0.0) or 0.0
    code = int(safe_float(row.get("weather_code"), 0) or 0)
    x = -3.1 + 1.7*clamp(cape/1800.0) + 0.75*clamp(max(0.0, -float(li or 0))/6.0)
    x += 0.55*clamp(precip/8.0) + 0.4*clamp((gust-20.0)/30.0)
    if code in (95, 96, 99): x += 2.2
    # Strong CIN suppresses initiation but does not cancel existing storms.
    if cin < -100 and code not in (95,96,99): x -= 0.35
    sigmet_boost = 0.0
    for w in aircraft.get("official_warnings") or []:
        txt = json.dumps(w).upper()
        if "TS" in txt or "THUNDER" in txt or "CONVECT" in txt or "CB" in txt:
            sigmet_boost = max(sigmet_boost, 2.0)
    return {
        "probability": round(logistic(x + sigmet_boost), 4),
        "cape_jkg": cape, "cin_jkg": cin, "lifted_index_c": li,
        "official_warning_adjustment": sigmet_boost,
    }


def microburst_downdraft_proxy(row: Mapping[str, Any]) -> Dict[str, Any]:
    # DCAPE is preferred but often unavailable through simple APIs. This proxy
    # deliberately reports lower confidence.
    cape = safe_float(row.get("cape_jkg"), 0.0) or 0.0
    precip = safe_float(row.get("precipitation_mm"), 0.0) or 0.0
    gust = safe_float(row.get("wind_gust_10m_kt"), 0.0) or 0.0
    t = safe_float(row.get("temperature_c")); td = safe_float(row.get("dewpoint_c"))
    spread = max(0.0, (t-td)) if t is not None and td is not None else 0.0
    p = logistic(-4.0 + 1.1*clamp(cape/2000.0) + 1.1*clamp(precip/10.0) + 1.2*clamp((gust-20)/30) + 0.5*clamp(spread/15.0))
    return {"probability": round(p,4), "confidence": 0.42, "note": "Proxy only; use authoritative LLWS/microburst detection and airport sensors when available."}


def low_level_wind_shear(row: Mapping[str, Any]) -> Dict[str, Any]:
    levels = row.get("pressure_levels") or {}
    s0, d0 = safe_float(row.get("wind_speed_10m_kt")), safe_float(row.get("wind_direction_10m_deg"))
    best = None
    # 925 hPa or 850 hPa as a rough low-level layer depending available geopotential height.
    for p in (925, 850):
        lv = levels.get(str(p), {})
        z, s, d = safe_float(lv.get("height_m")), safe_float(lv.get("wind_speed_kt")), safe_float(lv.get("wind_direction_deg"))
        if None not in (s0,d0,z,s,d) and z <= 2500:
            diff = vector_difference_kt(s0,d0,s,d)
            shear = diff*MPS_PER_KT/max(100.0,z)
            candidate = {"top_pressure_hpa": p, "top_height_ft": z*FT_PER_M, "vector_change_kt": diff, "shear_s1": shear}
            if best is None or candidate["shear_s1"] > best["shear_s1"]:
                best = candidate
    if best is None:
        return {"probability": None, "available": False, "reason": "Insufficient low-level wind-profile data"}
    p = logistic(-3.0 + 3.2*clamp(best["vector_change_kt"]/30.0) + 1.0*clamp(best["shear_s1"]/0.02))
    best.update({"probability": round(p,4), "available": True})
    return best


def mountain_wave_screen(row: Mapping[str, Any], aircraft: Mapping[str, Any]) -> Dict[str, Any]:
    terrain = aircraft.get("terrain_context") or {}
    ridge_orientation = safe_float(terrain.get("ridge_orientation_deg"))
    relief_m = safe_float(terrain.get("relief_m"), 0.0) or 0.0
    if ridge_orientation is None or relief_m < 250:
        return {"probability": None, "available": False, "reason": "Terrain ridge orientation/relief not supplied"}
    levels = row.get("pressure_levels") or {}
    lv = levels.get("700", {})
    ws, wd = safe_float(lv.get("wind_speed_kt")), safe_float(lv.get("wind_direction_deg"))
    if ws is None or wd is None:
        return {"probability": None, "available": False, "reason": "700-hPa wind unavailable"}
    # Component normal to ridge axis.
    normal = (ridge_orientation + 90.0) % 360.0
    comp = abs(ws * math.cos(math.radians(circular_diff_deg(wd, normal))))
    # Stable-layer support using Ri between 850 and 700 where possible.
    ri = gradient_richardson_number(levels.get("850",{}), levels.get("700",{}), 850,700)
    stability = 0.5 if ri is None else clamp((ri - 0.1)/0.8)
    p = logistic(-3.2 + 2.0*clamp((comp-15)/35.0) + 1.2*clamp(relief_m/2000.0) + 0.8*stability)
    return {"probability": round(p,4), "available": True, "cross_barrier_wind_kt": round(comp,1), "relief_m": relief_m, "ri_850_700": ri}


def runway_analysis(row: Mapping[str, Any], aircraft: Mapping[str, Any]) -> Dict[str, Any]:
    runways = aircraft.get("runways") or []
    wind = safe_float(row.get("wind_speed_10m_kt")); wdir = safe_float(row.get("wind_direction_10m_deg")); gust=safe_float(row.get("wind_gust_10m_kt"))
    if not runways or wind is None or wdir is None:
        return {"available": False, "reason": "Runway headings or surface wind unavailable", "runways": []}
    max_cross = safe_float(aircraft.get("max_crosswind_kt"))
    max_tail = safe_float(aircraft.get("max_tailwind_kt"))
    rows = []
    for rw in runways:
        hdg = safe_float(rw.get("heading_deg"))
        if hdg is None: continue
        c = runway_wind_components(wind, wdir, hdg)
        cg = runway_wind_components(gust if gust is not None else wind, wdir, hdg)
        cross_exceed = None if max_cross is None else max(0.0, cg["crosswind_kt"]-max_cross)
        tail_exceed = None if max_tail is None else max(0.0, c["tailwind_kt"]-max_tail)
        risk = 0.15 + 0.35*clamp(cg["crosswind_kt"]/max(10.0,max_cross or 25.0)) + 0.35*clamp(c["tailwind_kt"]/max(5.0,max_tail or 10.0))
        if cross_exceed and cross_exceed > 0: risk = max(risk,0.92)
        if tail_exceed and tail_exceed > 0: risk = max(risk,0.92)
        rows.append({
            "runway": rw.get("id") or rw.get("name") or str(round(hdg)), "heading_deg": hdg,
            **c, "gust_crosswind_kt": cg["crosswind_kt"], "risk": round(clamp(risk),4),
            "crosswind_limit_kt": max_cross, "tailwind_limit_kt": max_tail,
            "crosswind_limit_exceeded": None if cross_exceed is None else cross_exceed > 0,
            "tailwind_limit_exceeded": None if tail_exceed is None else tail_exceed > 0,
        })
    rows.sort(key=lambda x: x["risk"])
    return {"available": bool(rows), "runways": rows, "best_screening_runway": rows[0]["runway"] if rows else None,
            "note": "Screening only. Runway assignment, actual limits, NOTAMs, contamination, ATC and AFM/operator procedures control operations."}


def density_altitude_screen(row: Mapping[str, Any], aircraft: Mapping[str, Any]) -> Dict[str, Any]:
    elev = safe_float(aircraft.get("airport_elevation_ft"))
    temp = safe_float(row.get("temperature_c")); qnh=safe_float(row.get("pressure_msl_hpa")) or safe_float(row.get("surface_pressure_hpa"))
    if None in (elev,temp,qnh):
        return {"available": False, "reason": "Airport elevation, temperature or altimeter pressure unavailable"}
    diag = density_altitude_ft(elev,qnh,temp)
    da = diag["density_altitude_ft"]
    excess = da-elev
    # Impact is qualitative only unless certified aircraft performance tables supplied.
    risk = logistic(-2.4 + 1.5*clamp(excess/5000.0) + 0.8*clamp((da-5000)/7000.0))
    max_da = safe_float(aircraft.get("max_density_altitude_ft"))
    if max_da is not None and da > max_da: risk=max(risk,0.95)
    return {"available":True, **diag, "density_altitude_excess_ft":round(excess,1), "performance_risk":round(risk,4),
            "reported_max_density_altitude_ft":max_da, "limit_exceeded":None if max_da is None else da>max_da,
            "takeoff_distance":None, "note":"Use approved AFM/POH/operator performance data for actual takeoff/landing performance."}


def runway_contamination_proxy(row: Mapping[str, Any]) -> Dict[str, Any]:
    temp=safe_float(row.get("temperature_c")); precip=safe_float(row.get("precipitation_mm"),0.0) or 0.0; snow=safe_float(row.get("snowfall_cm"),0.0) or 0.0
    if temp is None: return {"probability":None,"available":False}
    x=-4.0 + 1.0*clamp(precip/5.0)
    if snow>0: x+=2.0*clamp(snow/2.0)
    if -3<=temp<=2 and precip>0: x+=1.2
    return {"probability":round(logistic(x),4),"available":True,"note":"Weather-based contamination proxy only; airport runway condition reports/NOTAMs are authoritative."}


def ceiling_visibility_screen(row: Mapping[str, Any], aircraft: Mapping[str, Any]) -> Dict[str, Any]:
    metar=aircraft.get("metar_context") or {}
    obs_ceiling=safe_float(metar.get("ceiling_ft")) or safe_float(metar.get("ceiling"))
    obs_vis_m=safe_float(metar.get("visibility_m"))
    estimated=False
    ceiling=obs_ceiling
    if ceiling is None:
        ceiling=estimate_lcl_cloud_base_ft(safe_float(row.get("temperature_c")),safe_float(row.get("dewpoint_c")))
        estimated=ceiling is not None
        low_cloud=safe_float(row.get("cloud_cover_low_pct"),0.0) or 0.0
        if low_cloud < 50: ceiling=None  # LCL is not a ceiling if low cloud cover is sparse.
    vis=obs_vis_m if obs_vis_m is not None else safe_float(row.get("visibility_m"))
    cat=flight_category(ceiling,vis)
    min_ceiling=safe_float(aircraft.get("minimum_ceiling_ft")); min_vis=safe_float(aircraft.get("minimum_visibility_m"))
    limit_exceeded=None
    if min_ceiling is not None and ceiling is not None: limit_exceeded=ceiling<min_ceiling
    if min_vis is not None and vis is not None: limit_exceeded=bool(limit_exceeded) or vis<min_vis
    risk={None:0.35,"VFR":0.1,"MVFR":0.4,"IFR":0.72,"LIFR":0.92}.get(cat,0.35)
    if limit_exceeded: risk=max(risk,0.95)
    return {"ceiling_ft_agl":None if ceiling is None else round(ceiling,0),"ceiling_estimated_from_lcl":estimated,"visibility_m":vis,"flight_category":cat,"probability":round(risk,4),
            "user_minimums_exceeded":limit_exceeded,"note":"Observed METAR ceiling is preferred. Estimated LCL is not equivalent to an observed ceiling."}


def official_warning_assessment(aircraft: Mapping[str, Any]) -> Dict[str, Any]:
    warnings = aircraft.get("official_warnings") or []
    level = str(aircraft.get("official_warning_level") or "none").lower()
    text = str(aircraft.get("official_warning_text") or "")
    source_status = aircraft.get("source_status") if isinstance(aircraft.get("source_status"), Mapping) else {}
    feed_status = str(
        aircraft.get("official_warning_feed_status")
        or source_status.get("AWC_SIGMET")
        or "not_requested"
    ).lower()
    all_text = (text + " " + json.dumps(warnings)).upper()
    categories = []
    for key, tag in [
        ("TURB", "turbulence"), ("ICE", "icing"), ("ICING", "icing"),
        ("TS", "convection"), ("THUNDER", "convection"),
        ("VA", "volcanic_ash"), ("VOLCAN", "volcanic_ash"),
        ("TC", "tropical_cyclone"), ("CYCLONE", "tropical_cyclone"),
    ]:
        if key in all_text and tag not in categories:
            categories.append(tag)
    present = bool(warnings or text or level not in {"none", ""})
    critical = level in {"critical", "severe", "sigmet"} or bool(categories)
    verified_none = (not present) and feed_status == "success_none"
    status_unknown = (not present) and feed_status in {"error", "degraded", "unavailable", "stale", "not_requested", "unknown", ""}
    return {
        "present": present,
        "critical": critical,
        "level": level,
        "categories": categories,
        "source": aircraft.get("official_warning_source"),
        "text": text or None,
        "warnings": warnings,
        "feed_status": feed_status,
        "verified_none": verified_none,
        "status_unknown": status_unknown,
        "note": (
            "A successfully checked empty warning feed is distinct from an unavailable or failed feed. "
            "VYOM never converts feed failure into 'no warning'."
        ),
    }


def _risk_label(p: Optional[float]) -> str:
    if p is None: return "UNKNOWN"
    if p >= 0.85: return "CRITICAL"
    if p >= 0.65: return "HIGH"
    if p >= 0.35: return "CAUTION"
    return "LOW"


def analyze_hour(row: Mapping[str, Any], aircraft: Mapping[str, Any]) -> Dict[str, Any]:
    cv=ceiling_visibility_screen(row,aircraft)
    conv=convective_screen(row,aircraft)
    icing=icing_screen(row,aircraft)
    turb=turbulence_screen(row,aircraft)
    llws=low_level_wind_shear(row)
    micro=microburst_downdraft_proxy(row)
    mountain=mountain_wave_screen(row,aircraft)
    runway=runway_analysis(row,aircraft)
    da=density_altitude_screen(row,aircraft)
    contam=runway_contamination_proxy(row)
    risk_components={
        "ceiling_visibility":cv.get("probability"),"convection":conv.get("probability"),"icing":icing.get("probability"),"turbulence":turb.get("probability"),
        "low_level_wind_shear":llws.get("probability"),"microburst_proxy":micro.get("probability"),"mountain_wave":mountain.get("probability"),
        "runway_wind":max((r.get("risk",0) for r in runway.get("runways",[])),default=None),"density_altitude":da.get("performance_risk"),"runway_contamination":contam.get("probability")
    }
    vals=[v for v in risk_components.values() if v is not None]
    # Dependency-aware soft-max style fusion. Avoid naive noisy-OR inflation.
    if vals:
        s=sorted(vals,reverse=True)
        integrated=clamp(s[0]+0.28*(s[1] if len(s)>1 else 0)+0.12*(s[2] if len(s)>2 else 0))
    else: integrated=None
    return {"time":row.get("time"),"risk":risk_components,"integrated_risk":None if integrated is None else round(integrated,4),"risk_level":_risk_label(integrated),
            "ceiling_visibility":cv,"convection":conv,"icing":icing,"turbulence":turb,"low_level_wind_shear":llws,"microburst":micro,"mountain_wave":mountain,
            "runway":runway,"density_altitude":da,"runway_contamination":contam}


def monte_carlo_risk(fused: Sequence[Mapping[str, Any]], aircraft: Mapping[str, Any], n: int = 120, seed: int = 1701) -> Dict[str, Any]:
    if not fused: return {"available":False}
    rng=random.Random(seed)
    outcomes=[]
    for _ in range(max(20,min(1000,int(n)))):
        max_r=0.0
        for row0 in fused[:min(48,len(fused))]:
            row=dict(row0)
            # Perturb key variables by inter-model spread. Preserve physically linked
            # row structure by perturbing one row together, rather than independently
            # selecting unrelated models for each field.
            for key in ("temperature_c","visibility_m","wind_speed_10m_kt","wind_gust_10m_kt","cape_jkg","freezing_level_ft","precipitation_mm"):
                v=safe_float(row.get(key)); sp=safe_float(row.get(key+"_spread"),0.0) or 0.0
                if v is not None and sp>0:
                    row[key]=v+rng.gauss(0,sp)
            a=analyze_hour(row,aircraft)
            max_r=max(max_r,a.get("integrated_risk") or 0.0)
        outcomes.append(max_r)
    outcomes.sort()
    def q(frac): return outcomes[min(len(outcomes)-1,max(0,int(frac*(len(outcomes)-1))))]
    return {"available":True,"scenarios":len(outcomes),"median":round(q(0.5),4),"p10":round(q(0.1),4),"p90":round(q(0.9),4),
            "p_risk_ge_0_65":round(sum(x>=0.65 for x in outcomes)/len(outcomes),4),"p_risk_ge_0_85":round(sum(x>=0.85 for x in outcomes)/len(outcomes),4)}


def route_exposure(aircraft: Mapping[str, Any]) -> Dict[str, Any]:
    points=aircraft.get("route_weather_points") or []
    if not points:
        return {"available":False,"reason":"No route_weather_points supplied"}
    analyzed=[]
    for p in points:
        if not isinstance(p,Mapping): continue
        a=analyze_hour(p,aircraft)
        analyzed.append({"lat":p.get("lat"),"lon":p.get("lon"),"altitude_ft":p.get("altitude_ft"),"time":p.get("time"),"risk":a["integrated_risk"],"level":a["risk_level"]})
    vals=[x["risk"] for x in analyzed if x["risk"] is not None]
    return {"available":bool(vals),"maximum_risk":round(max(vals),4) if vals else None,"mean_risk":round(statistics.mean(vals),4) if vals else None,"points":analyzed,
            "route_clearance":None,"note":"Route weather exposure screening only; certified dispatch/ATC/NOTAM/airspace constraints are outside this result."}


def precision_level(aircraft: Mapping[str, Any], official: Mapping[str, Any], model_count: int) -> Tuple[str,List[str]]:
    missing=[]
    if not aircraft.get("aircraft_class"): missing.append("aircraft_class")
    if safe_float(aircraft.get("heading_deg")) is None: missing.append("heading_deg")
    if safe_float(aircraft.get("cruise_altitude_ft")) is None: missing.append("cruise_altitude_ft")
    if not aircraft.get("runways"): missing.append("runway_geometry")
    if safe_float(aircraft.get("max_crosswind_kt")) is None: missing.append("approved/operator crosswind limit")
    if not aircraft.get("departure_icao") and not aircraft.get("arrival_icao"): missing.append("aerodrome ICAO identifiers")
    if model_count < 2: missing.append("multi-model agreement")
    if len(missing)>=5: level="WEATHER_ONLY"
    elif len(missing)>=3: level="AIRCRAFT_AWARE"
    elif len(missing)>=1: level="OPERATION_AWARE"
    else: level="CONTEXT_RICH"
    if official.get("present") and level=="CONTEXT_RICH": level="AUTHORITY_CROSSCHECKED"
    return level,missing


def _source_status_quality(aircraft: Mapping[str, Any]) -> Dict[str, Any]:
    status_map = aircraft.get("source_status") if isinstance(aircraft.get("source_status"), Mapping) else {}
    score_map = {
        "success": 1.0,
        "success_none": 0.95,
        "ok": 0.95,
        "stale": 0.45,
        "degraded": 0.40,
        "unavailable": 0.25,
        "error": 0.15,
        "not_requested": 0.40,
        "unknown": 0.30,
    }
    scores = [score_map.get(str(v).lower(), 0.30) for v in status_map.values()]
    return {
        "overall": round(statistics.mean(scores), 4) if scores else 0.45,
        "sources": dict(status_map),
        "source_count": len(status_map),
    }


def confidence_score(fused: Sequence[Mapping[str,Any]], members: Sequence[ForecastMember], aircraft: Mapping[str,Any], official: Mapping[str,Any]) -> Dict[str,Any]:
    if not fused:
        return {"overall":0.0,"forecast":0.0,"aircraft_profile":0.0,"observational_authority_context":0.0,"model_agreement":0.0}
    model_q = statistics.mean([m.quality for m in members]) if members else 0.0
    spreads = []
    for r in fused[:24]:
        ws = safe_float(r.get("wind_speed_10m_kt_spread")); temp = safe_float(r.get("temperature_c_spread"))
        if ws is not None: spreads.append(clamp(1-ws/20.0))
        if temp is not None: spreads.append(clamp(1-temp/6.0))
    agreement = statistics.mean(spreads) if spreads else 0.55
    forecast = clamp(0.55*model_q + 0.45*agreement)
    fields = ["aircraft_class","heading_deg","cruise_altitude_ft","true_airspeed_kt","max_crosswind_kt","max_tailwind_kt"]
    aircraft_q = sum(1 for f in fields if aircraft.get(f) is not None)/len(fields)
    obs = 0.20
    if aircraft.get("metar_context"): obs += 0.25
    if aircraft.get("taf_context"): obs += 0.15
    if aircraft.get("pirep_context"): obs += 0.15
    if official.get("present"): obs += 0.20
    elif official.get("verified_none"): obs += 0.12
    obs = clamp(obs)
    overall = clamp(0.5*forecast + 0.3*aircraft_q + 0.2*obs)
    return {
        "overall":round(overall,4),
        "forecast":round(forecast,4),
        "aircraft_profile":round(aircraft_q,4),
        "observational_authority_context":round(obs,4),
        "model_agreement":round(agreement,4),
    }


def summarize_hazards(hours: Sequence[Mapping[str,Any]], official: Mapping[str,Any]) -> Dict[str,Any]:
    keys=["ceiling_visibility","convection","icing","turbulence","low_level_wind_shear","microburst_proxy","mountain_wave","runway_wind","density_altitude","runway_contamination"]
    out={}
    for k in keys:
        vals=[h.get("risk",{}).get(k) for h in hours if h.get("risk",{}).get(k) is not None]
        out[k]={"max_probability":round(max(vals),4) if vals else None,"level":_risk_label(max(vals) if vals else None)}
    if official.get("critical"):
        out["official_warning"]={"max_probability":1.0,"level":"CRITICAL","categories":official.get("categories")}
    return out


def build_action_flags(summary: Mapping[str,Any], official: Mapping[str,Any], aircraft: Mapping[str,Any]) -> List[Dict[str,Any]]:
    flags=[]
    if official.get("present"):
        flags.append({"priority":1,"type":"official_aviation_weather","action":"Review and comply with the current authoritative aviation weather advisory/warning before operational decisions.","source":official.get("source")})
    map_actions={
        "convection":"Convective weather exposure is elevated; use official SIGMET/TAF/radar/ATC information and approved operator procedures.",
        "icing":"Icing-conducive conditions are elevated; compare with aircraft certification/limitations and official icing products.",
        "turbulence":"Turbulence screening is elevated; cross-check official turbulence products, PIREPs/AIREPs and operator procedures.",
        "low_level_wind_shear":"Low-level wind-shear screening is elevated; prioritize airport LLWS alerts and current ATC/aerodrome information.",
        "ceiling_visibility":"Ceiling/visibility constraints are elevated; compare observed/forecast values with applicable operational minima.",
        "runway_wind":"Runway wind exposure is elevated; compare actual wind/gusts with approved aircraft/operator limits and assigned runway.",
        "density_altitude":"Density-altitude performance impact is elevated; calculate actual performance using approved AFM/POH/operator data.",
    }
    for k,msg in map_actions.items():
        p=(summary.get(k) or {}).get("max_probability")
        if p is not None and p>=0.65:
            flags.append({"priority":2 if p>=0.85 else 3,"type":k,"action":msg,"probability":p})
    flags.sort(key=lambda x:x["priority"])
    return flags


def _merge_live_patch(base: Dict[str,Any], patch: Mapping[str,Any]) -> Dict[str,Any]:
    out=dict(base)
    for k,v in (patch or {}).items():
        if v is None: continue
        if isinstance(v,list):
            existing=out.get(k)
            if not existing: out[k]=v
        elif isinstance(v,Mapping):
            existing=out.get(k)
            if not isinstance(existing,Mapping): out[k]=dict(v)
            else:
                m=dict(v); m.update(existing); out[k]=m
        elif out.get(k) is None:
            out[k]=v
    return out


def build_aviation_advisory(
    lat: float,
    lon: float,
    aircraft_config: Optional[Mapping[str,Any]] = None,
    horizon_hours: int = 72,
    scenario_count: int = 120,
    models: Sequence[str] = DEFAULT_MODELS,
    calibration_path: Optional[str] = None,
) -> Dict[str,Any]:
    """Build a probabilistic aviation-weather screening advisory.

    Optional real aviation-weather context is gathered through
    aviation_realtime_service when VYOM_AVIATION_REAL_FEEDS_ENABLED=1. The
    engine never fabricates missing official data or approved aircraft limits.
    """
    started=time.time()
    horizon=max(6,min(168,int(horizon_hours)))
    aircraft=dict(aircraft_config or {})
    realtime={"enabled":False}
    service=None
    if os.getenv("VYOM_AVIATION_REAL_FEEDS_ENABLED","1")=="1":
        try:
            from aviation_realtime_service import AviationRealtimeService
            service=AviationRealtimeService()
            live=service.collect_live_context(lat,lon,aircraft)
            aircraft=_merge_live_patch(aircraft,live.get("patch") or {})
            realtime={"enabled":True,"sources":live.get("sources",[]),"elapsed_ms":live.get("elapsed_ms"),"status":live.get("status","ok")}
            if calibration_path is None:
                cp=live.get("dynamic_calibration_path")
                if cp: calibration_path=cp
        except Exception as exc:
            realtime={"enabled":True,"status":"degraded","error":str(exc)}
    calibration_path=calibration_path or os.getenv("VYOM_AVIATION_CALIBRATION_PATH")
    calibration=CalibrationStore.from_json(calibration_path)
    members=[]
    for model in models:
        m=fetch_model_member(lat,lon,model,horizon,calibration)
        if m: members.append(m)
    if not members:
        return {"engine":ENGINE_NAME,"version":ENGINE_VERSION,"status":"error","reason":"No numerical aviation-weather forecast members were available.","dispatch_clearance":None,"takeoff_landing_clearance":None,"confidence":{"overall":0.0},"realtime":realtime}
    fused=fuse_members(members,horizon)
    hours=[analyze_hour(row,aircraft) for row in fused]
    official=official_warning_assessment(aircraft)
    summary=summarize_hazards(hours,official)
    deterministic=max((h.get("integrated_risk") or 0.0 for h in hours),default=0.0)
    if official.get("critical"): deterministic=max(deterministic,0.95)
    probabilistic=monte_carlo_risk(fused,aircraft,scenario_count)
    confidence=confidence_score(fused,members,aircraft,official)
    source_quality=_source_status_quality(aircraft)
    data_quality={
        "overall":round(clamp(0.65*confidence.get("forecast",0.0)+0.35*source_quality.get("overall",0.45)),4),
        "forecast_model_quality":round(statistics.mean([m.quality for m in members]),4) if members else 0.0,
        "source_health_quality":source_quality.get("overall"),
        "source_count":source_quality.get("source_count"),
    }
    precision,missing=precision_level(aircraft,official,len(members))
    route=route_exposure(aircraft)
    timeline=[{"time":h["time"],"risk":h["integrated_risk"],"level":h["risk_level"]} for h in hours[:72]]
    peak=sorted(hours,key=lambda x:x.get("integrated_risk") or -1,reverse=True)[:6]
    flags=build_action_flags(summary,official,aircraft)
    abstain=[]
    if confidence["overall"]<0.45: abstain.append("Low overall confidence")
    if len(members)<2: abstain.append("No multi-model consensus")
    if not aircraft.get("metar_context") and (aircraft.get("departure_icao") or aircraft.get("arrival_icao")): abstain.append("Current METAR context unavailable")
    if not aircraft.get("taf_context") and (aircraft.get("departure_icao") or aircraft.get("arrival_icao")): abstain.append("TAF context unavailable")
    if safe_float(aircraft.get("max_crosswind_kt")) is None: abstain.append("Approved/operator crosswind limit not supplied")
    if official.get("status_unknown"): abstain.append("Official SIGMET/warning status not verified")
    output={
        "engine":ENGINE_NAME,"version":ENGINE_VERSION,"status":"ok","lat":lat,"lon":lon,
        "generated_at":datetime.now(timezone.utc).isoformat(),"horizon_hours":horizon,
        "aircraft_profile":{k:v for k,v in aircraft.items() if k not in {"pirep_context","official_warnings","route_weather_points"}},
        "forecast_models":[{"model":m.model,"quality":round(m.quality,4),"hours":len(m.hours)} for m in members],
        "risk":{"integrated_peak_probability":round(deterministic,4),"level":_risk_label(deterministic),"hazards":summary,"probabilistic":probabilistic},
        "timeline":timeline,"peak_hours":peak,"route":route,"official_weather":official,"confidence":confidence,
        "data_quality":data_quality,
        "source_status":dict(aircraft.get("source_status") or {}),
        "precision":{"level":precision,"missing_or_unverified":missing,"abstention_reasons":abstain},
        "action_flags":flags,
        "field_semantics":{
            "temperature":"Outside/ambient atmospheric temperature. It is never aircraft cabin/interior temperature.",
            "turbulence":"Modeled turbulence exposure screening unless an explicit observed/authoritative product is attached.",
            "icing":"Meteorological icing-conduciveness screening, not aircraft certification or flight clearance.",
            "route":"Weather-exposure screening only; not an ATC, dispatch, navigation, or operational route clearance."
        },
        "engine_diagnostics":{
            "engine_executed":True,
            "modules_executed":["ceiling_visibility","convection","icing","turbulence","low_level_wind_shear","microburst_proxy","mountain_wave","runway_wind","density_altitude","runway_contamination","monte_carlo","official_warning_gate"],
            "source_status":dict(aircraft.get("source_status") or {}),
            "forecast_models":[m.model for m in members],
            "validation_contract":"risk_confidence_data_quality_separated"
        },
        "dispatch_clearance":None,"takeoff_landing_clearance":None,"airworthiness_determination":None,
        "safety_contract":{
            "decision_support_only":True,
            "official_products_take_precedence":True,
            "approved_aircraft_limits_take_precedence":True,
            "notam_atc_dispatch_required_for_operations":True,
            "message":"VYOM-AVIATION screens weather-related exposure; it does not authorize, dispatch, release, clear, or certify a flight."
        },
        "realtime":realtime,"elapsed_ms":round((time.time()-started)*1000,1),
    }
    # Persist forecast samples for later verification if a real-data service exists.
    if service is not None:
        try:
            service.record_model_forecast(lat,lon,fused,members)
            output["realtime"]["forecast_recorded_for_calibration"]=True
        except Exception as exc:
            output["realtime"]["forecast_record_error"]=str(exc)
    return output


__all__=[
    "ENGINE_NAME","ENGINE_VERSION","AviationHour","ForecastMember","CalibrationStore","build_aviation_advisory",
    "density_altitude_ft","pressure_altitude_ft","runway_wind_components","flight_category",
    "gradient_richardson_number","vertical_shear_s1","turbulence_screen","icing_screen","convective_screen",
    "low_level_wind_shear","mountain_wave_screen","analyze_hour","monte_carlo_risk",
]
