"""
VYOM-MARINE v3.1.0
==================
Probabilistic marine weather / vessel-operability decision engine.

This module is intentionally separate from the FastAPI application layer.
It performs deterministic and probabilistic marine calculations and returns
structured telemetry for an NLP layer to explain.  It is NOT a certified
navigation, stability, routing, or departure-clearance system.

Scientific design goals
-----------------------
* Multi-model atmosphere + wave fusion with variable/lead/season calibration.
* Coupled scenario generation (preserves each model member's internal structure).
* Significant-wave-height, period, direction, wind-sea/swell and current metrics.
* Vessel-relative encounter geometry and conservative motion/seakeeping proxies.
* Steep-wave, crossing-sea, wind/gust, squall, visibility, cold-exposure,
  slamming, deck-wetness, roll-resonance, surf-riding/broaching screening.
* Persistent voyage exposure checkpoint hooks.
* Optional Ocean Guardian-X extensions: traffic CPA/TCPA, traffic-complexity,
  under-keel-clearance/grounding, cyclone-ensemble exposure, storm-surge/coastal
  water level, sea-ice capability, rapidly-developing-sea detection, EVT tail
  screening, rogue-wave environment screening, optional RAO spectral response,
  dependency-aware hazard fusion, OOD/abstention, route-candidate optimization,
  Bayesian sensor/model assimilation, explainability, forecast safety timelines,
  validation metrics, and optional fisheries/stock-dynamics utilities.
* Risk and confidence kept separate.
* Precision/abstention gates: no "safe to sail" output and no certified stability
  result without the vessel's approved stability information.
* Official maritime warnings are allowed to OVERRIDE model-derived screening;
  this engine never downgrades an official warning.

The mathematical proxies are decision-support screens, not substitutes for
SOLAS/IMO requirements, a vessel's stability booklet, nautical publications,
VTS/port instructions, coastguard/navy warnings, or a qualified master.
"""

from __future__ import annotations

import json
import math
import os
import random
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import requests

# ---------------------------------------------------------------------------
# Constants / models
# ---------------------------------------------------------------------------

ENGINE_NAME = "VYOM-MARINE"
ENGINE_VERSION = "4.0.0"
G = 9.80665
RHO_SEAWATER = 1025.0
KNOT_TO_MS = 0.514444
KMH_TO_MS = 1.0 / 3.6
MS_TO_KNOT = 1.943844
EARTH_RADIUS_KM = 6371.0088

DEFAULT_ATMOS_MODELS: Tuple[str, ...] = (
    "ecmwf_ifs025",
    "gfs_seamless",
    "icon_seamless",
    "ukmo_seamless",
)

# Exact Open-Meteo marine model identifiers documented by its OpenAPI spec.
DEFAULT_WAVE_MODELS: Tuple[str, ...] = (
    "ecmwf_wam",
    "ecmwf_wam025",
    "ncep_gfswave025",
    "meteofrance_wave",
    "dwd_gwam",
)

ATMOS_VARIABLES = (
    "temperature_2m",
    "relative_humidity_2m",
    "surface_pressure",
    "wind_speed_10m",
    "wind_gusts_10m",
    "wind_direction_10m",
    "precipitation",
    "visibility",
    "weather_code",
    "cape",
    "cloud_cover",
)

MARINE_VARIABLES = (
    "wave_height",
    "wave_direction",
    "wave_period",
    "wave_peak_period",
    "wind_wave_height",
    "wind_wave_direction",
    "wind_wave_period",
    "wind_wave_peak_period",
    "swell_wave_height",
    "swell_wave_direction",
    "swell_wave_period",
    "swell_wave_peak_period",
    "secondary_swell_wave_height",
    "secondary_swell_wave_direction",
    "secondary_swell_wave_period",
    "tertiary_swell_wave_height",
    "tertiary_swell_wave_direction",
    "tertiary_swell_wave_period",
    "sea_level_height_msl",
    "sea_surface_temperature",
    "ocean_current_velocity",
    "ocean_current_direction",
)

# Conservative screening defaults only.  They are NOT regulatory limits.
# Values are deliberately broad and are overridden by actual vessel operating
# manuals / company limits supplied through VesselConfig.
VESSEL_PROFILES: Dict[str, Dict[str, float]] = {
    "small_fishing_boat": {
        "length_m": 9.0, "beam_m": 2.8, "freeboard_m": 0.7,
        "service_speed_kn": 8.0, "screen_hs_m": 1.5,
        "screen_wind_kn": 25.0, "screen_gust_kn": 32.0,
    },
    "fishing_trawler": {
        "length_m": 24.0, "beam_m": 7.0, "freeboard_m": 1.8,
        "service_speed_kn": 10.0, "screen_hs_m": 3.0,
        "screen_wind_kn": 32.0, "screen_gust_kn": 40.0,
    },
    "coastal_cargo": {
        "length_m": 70.0, "beam_m": 13.0, "freeboard_m": 3.0,
        "service_speed_kn": 12.0, "screen_hs_m": 5.0,
        "screen_wind_kn": 40.0, "screen_gust_kn": 50.0,
    },
    "container_ship": {
        "length_m": 250.0, "beam_m": 40.0, "freeboard_m": 8.0,
        "service_speed_kn": 18.0, "screen_hs_m": 8.0,
        "screen_wind_kn": 50.0, "screen_gust_kn": 60.0,
    },
    "bulk_carrier": {
        "length_m": 220.0, "beam_m": 32.0, "freeboard_m": 7.0,
        "service_speed_kn": 14.0, "screen_hs_m": 8.0,
        "screen_wind_kn": 50.0, "screen_gust_kn": 60.0,
    },
    "tanker": {
        "length_m": 240.0, "beam_m": 42.0, "freeboard_m": 7.0,
        "service_speed_kn": 14.0, "screen_hs_m": 8.0,
        "screen_wind_kn": 50.0, "screen_gust_kn": 60.0,
    },
    "passenger_ship": {
        "length_m": 180.0, "beam_m": 28.0, "freeboard_m": 6.0,
        "service_speed_kn": 18.0, "screen_hs_m": 6.0,
        "screen_wind_kn": 45.0, "screen_gust_kn": 55.0,
    },
    "sailboat": {
        "length_m": 11.0, "beam_m": 3.6, "freeboard_m": 0.9,
        "service_speed_kn": 6.0, "screen_hs_m": 2.0,
        "screen_wind_kn": 28.0, "screen_gust_kn": 35.0,
    },
    "generic": {
        "length_m": 30.0, "beam_m": 8.0, "freeboard_m": 2.0,
        "service_speed_kn": 10.0, "screen_hs_m": 3.0,
        "screen_wind_kn": 30.0, "screen_gust_kn": 40.0,
    },
}

OFFICIAL_WARNING_RANK = {
    "none": 0,
    "statement": 1,
    "advisory": 2,
    "watch": 3,
    "warning": 4,
    "emergency": 5,
}

# ---------------------------------------------------------------------------
# Utility math
# ---------------------------------------------------------------------------


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def safe_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if v is None:
            return default
        x = float(v)
        if not math.isfinite(x):
            return default
        return x
    except (TypeError, ValueError):
        return default


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    vals = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    q = clamp(p, 0.0, 100.0) / 100.0 * (len(vals) - 1)
    lo = int(math.floor(q))
    hi = int(math.ceil(q))
    if lo == hi:
        return vals[lo]
    f = q - lo
    return vals[lo] * (1.0 - f) + vals[hi] * f


def sigmoid(x: float) -> float:
    x = clamp(x, -60.0, 60.0)
    return 1.0 / (1.0 + math.exp(-x))


def angular_difference_deg(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    return abs(((float(a) - float(b) + 180.0) % 360.0) - 180.0)


def circular_mean_deg(values: Sequence[float], weights: Optional[Sequence[float]] = None) -> Optional[float]:
    vals = [safe_float(v) for v in values]
    pairs: List[Tuple[float, float]] = []
    for i, v in enumerate(vals):
        if v is None:
            continue
        w = 1.0 if weights is None else max(0.0, float(weights[i]))
        pairs.append((v, w))
    if not pairs:
        return None
    sx = sum(w * math.cos(math.radians(v)) for v, w in pairs)
    sy = sum(w * math.sin(math.radians(v)) for v, w in pairs)
    if abs(sx) + abs(sy) < 1e-12:
        return None
    return math.degrees(math.atan2(sy, sx)) % 360.0


def weighted_mean(values: Sequence[Optional[float]], weights: Sequence[float]) -> Optional[float]:
    num = 0.0
    den = 0.0
    for v, w in zip(values, weights):
        if v is None:
            continue
        num += float(v) * max(0.0, float(w))
        den += max(0.0, float(w))
    return num / den if den > 0 else None


def normalize_weights(weights: Sequence[float]) -> List[float]:
    w = [max(0.0, float(x)) for x in weights]
    s = sum(w)
    if s <= 0:
        return [1.0 / len(w)] * len(w) if w else []
    return [x / s for x in w]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def season_key(dt: datetime, lat: float) -> str:
    month = dt.month
    north = lat >= 0
    if month in (12, 1, 2): s = "winter" if north else "summer"
    elif month in (3, 4, 5): s = "spring" if north else "autumn"
    elif month in (6, 7, 8): s = "summer" if north else "winter"
    else: s = "autumn" if north else "spring"
    return s


def lead_band(hour: int) -> str:
    if hour <= 24: return "0_24h"
    if hour <= 72: return "25_72h"
    if hour <= 120: return "73_120h"
    return "121_192h"


def beaufort_from_ms(ms: float) -> int:
    # WMO Beaufort thresholds in m/s, rounded operationally.
    t = [0.3,1.6,3.4,5.5,8.0,10.8,13.9,17.2,20.8,24.5,28.5,32.7]
    for i, x in enumerate(t):
        if ms < x:
            return i
    return 12


def deepwater_wavelength_m(period_s: Optional[float]) -> Optional[float]:
    if period_s is None or period_s <= 0:
        return None
    return G * period_s * period_s / (2.0 * math.pi)


def deepwater_phase_speed_ms(period_s: Optional[float]) -> Optional[float]:
    if period_s is None or period_s <= 0:
        return None
    return G * period_s / (2.0 * math.pi)


def deepwater_group_speed_ms(period_s: Optional[float]) -> Optional[float]:
    c = deepwater_phase_speed_ms(period_s)
    return c / 2.0 if c is not None else None


def wave_steepness(hs_m: Optional[float], period_s: Optional[float]) -> Optional[float]:
    if hs_m is None:
        return None
    L = deepwater_wavelength_m(period_s)
    if not L or L <= 0:
        return None
    return max(0.0, hs_m / L)


def wave_energy_density_j_m2(hs_m: Optional[float]) -> Optional[float]:
    if hs_m is None or hs_m < 0:
        return None
    return RHO_SEAWATER * G * hs_m * hs_m / 16.0


def wave_power_kw_m(hs_m: Optional[float], energy_period_s: Optional[float]) -> Optional[float]:
    if hs_m is None or energy_period_s is None or energy_period_s <= 0:
        return None
    p = RHO_SEAWATER * G * G * hs_m * hs_m * energy_period_s / (64.0 * math.pi)
    return p / 1000.0


def relative_wave_geometry(heading_deg: Optional[float], wave_from_deg: Optional[float]) -> Dict[str, Optional[float]]:
    if heading_deg is None or wave_from_deg is None:
        return {"wave_propagation_deg": None, "encounter_angle_deg": None}
    propagation = (wave_from_deg + 180.0) % 360.0
    # 0 = following, 180 = head sea, 90 = beam sea.
    angle = angular_difference_deg(heading_deg, propagation)
    return {"wave_propagation_deg": propagation, "encounter_angle_deg": angle}


def encounter_period_s(period_s: Optional[float], heading_deg: Optional[float], wave_from_deg: Optional[float], speed_kn: Optional[float]) -> Optional[float]:
    if period_s is None or period_s <= 0 or heading_deg is None or wave_from_deg is None or speed_kn is None:
        return None
    L = deepwater_wavelength_m(period_s)
    if not L or L <= 0:
        return None
    omega = 2.0 * math.pi / period_s
    k = 2.0 * math.pi / L
    beta = math.radians(relative_wave_geometry(heading_deg, wave_from_deg)["encounter_angle_deg"] or 0.0)
    U = max(0.0, speed_kn * KNOT_TO_MS)
    omega_e = abs(omega - k * U * math.cos(beta))
    if omega_e < 1e-4:
        return 999.0
    return 2.0 * math.pi / omega_e


def estimated_roll_period_s(beam_m: Optional[float], gm_m: Optional[float]) -> Optional[float]:
    # Simple screening approximation only; approved ship stability data supersedes it.
    if beam_m is None or gm_m is None or beam_m <= 0 or gm_m <= 0:
        return None
    radius_gyration = 0.35 * beam_m
    return 2.0 * math.pi * radius_gyration / math.sqrt(G * gm_m)


def ar1_noise(n: int, sigma: float, rho: float, rng: random.Random) -> List[float]:
    if n <= 0 or sigma <= 0:
        return [0.0] * max(0, n)
    rho = clamp(rho, 0.0, 0.98)
    out = [rng.gauss(0.0, sigma)]
    innovation = sigma * math.sqrt(max(1e-9, 1.0 - rho * rho))
    for _ in range(1, n):
        out.append(rho * out[-1] + rng.gauss(0.0, innovation))
    return out


# ---------------------------------------------------------------------------
# Ocean Guardian-X extended mathematical utilities
# ---------------------------------------------------------------------------

def vector_from_speed_heading(speed_ms: float, heading_deg: float) -> Tuple[float,float]:
    """East/North velocity components for navigation heading (0=N, 90=E)."""
    th=math.radians(float(heading_deg)%360.0)
    return float(speed_ms)*math.sin(th), float(speed_ms)*math.cos(th)


def local_enu_m(lat0: float, lon0: float, lat: float, lon: float) -> Tuple[float,float]:
    """Small-area easting/northing approximation adequate for CPA screening."""
    dy=math.radians(lat-lat0)*EARTH_RADIUS_KM*1000.0
    dx=math.radians(lon-lon0)*EARTH_RADIUS_KM*1000.0*math.cos(math.radians((lat+lat0)/2.0))
    return dx,dy


def tcpa_dcpa(own_lat: float, own_lon: float, own_speed_kn: float, own_heading_deg: float,
              target_lat: float, target_lon: float, target_speed_kn: float, target_heading_deg: float) -> Dict[str,Optional[float]]:
    """Closest-point-of-approach geometry; awareness aid only, never COLREG advice."""
    px,py=local_enu_m(own_lat,own_lon,target_lat,target_lon)
    ovx,ovy=vector_from_speed_heading(max(0.0,own_speed_kn)*KNOT_TO_MS,own_heading_deg)
    tvx,tvy=vector_from_speed_heading(max(0.0,target_speed_kn)*KNOT_TO_MS,target_heading_deg)
    vrx,vry=tvx-ovx,tvy-ovy
    vv=vrx*vrx+vry*vry
    if vv < 1e-9:
        return {"tcpa_min":None,"dcpa_nm":math.hypot(px,py)/1852.0,"closing_speed_kn":0.0}
    tcpa_s=-(px*vrx+py*vry)/vv
    cx,cy=px+tcpa_s*vrx,py+tcpa_s*vry
    return {
        "tcpa_min":tcpa_s/60.0,
        "dcpa_nm":math.hypot(cx,cy)/1852.0,
        "closing_speed_kn":math.sqrt(vv)*MS_TO_KNOT,
    }


def collision_risk_from_cpa(dcpa_nm: Optional[float], tcpa_min: Optional[float], visibility_m: Optional[float],
                            maneuverability_index: Optional[float]=None) -> float:
    if dcpa_nm is None or tcpa_min is None or tcpa_min < -5.0:
        return 0.0
    d=math.exp(-max(0.0,dcpa_nm)/0.6)
    t=math.exp(-max(0.0,tcpa_min)/18.0) if tcpa_min>=0 else 0.15
    vis=visibility_risk(visibility_m)
    man=0.5 if maneuverability_index is None else clamp(maneuverability_index,0.0,1.0)
    return clamp((0.60*d*t + 0.25*d + 0.15*vis)*(1.15-0.3*man),0.0,1.0)


def traffic_complexity_index(contacts: Sequence[Mapping[str,Any]], own_lat: float, own_lon: float,
                             own_speed_kn: float, own_heading_deg: float, visibility_m: Optional[float]=None) -> Dict[str,Any]:
    details=[]; total=0.0; max_collision=0.0
    for c in contacts or []:
        try:
            geom=tcpa_dcpa(own_lat,own_lon,own_speed_kn,own_heading_deg,float(c["lat"]),float(c["lon"]),
                           float(c.get("speed_kn",0.0)),float(c.get("heading_deg",0.0)))
        except Exception:
            continue
        r=collision_risk_from_cpa(geom["dcpa_nm"],geom["tcpa_min"],visibility_m,c.get("maneuverability_index"))
        dist_nm=haversine_km(own_lat,own_lon,float(c["lat"]),float(c["lon"]))/1.852
        weight=math.exp(-dist_nm/6.0)
        tcpa_factor=1.0 if geom["tcpa_min"] is None else math.exp(-max(0.0,geom["tcpa_min"])/30.0)
        contrib=weight*(0.4+0.6*r)*tcpa_factor
        total+=contrib; max_collision=max(max_collision,r)
        details.append({"id":c.get("id") or c.get("mmsi"),**geom,"collision_screen":round(r,4),"distance_nm":round(dist_nm,3)})
    return {"tci":round(total,4),"normalized":round(clamp(1.0-math.exp(-total/2.5),0,1),4),
            "max_collision_risk":round(max_collision,4),"contacts":details}


def rapid_development_metrics(hourly: Sequence[Mapping[str,Any]]) -> Dict[str,Any]:
    """Implements dHs/dt, dWind/dt, dPressure/dt and dTp/dt screening."""
    out=[]; peak=0.0
    for i in range(3,len(hourly)):
        cur=hourly[i]; prev=hourly[i-3]
        dh=(safe_float(cur.get("wave_height_m"),0)-safe_float(prev.get("wave_height_m"),0))/3.0
        dw=(safe_float(cur.get("wind_kn"),0)-safe_float(prev.get("wind_kn"),0))/3.0
        dp=(safe_float(cur.get("pressure_hpa"),1013)-safe_float(prev.get("pressure_hpa"),1013))/3.0
        dtp=(safe_float(cur.get("wave_peak_period_s"),0)-safe_float(prev.get("wave_peak_period_s"),0))/3.0
        x=-2.8+1.25*max(0.0,dh)+0.055*max(0.0,dw)+0.22*max(0.0,-dp)+0.10*abs(dtp)
        r=clamp(sigmoid(x),0,1); peak=max(peak,r)
        out.append({"time":cur.get("time"),"dHs_dt_mph":round(dh,3),"dWind_dt_knph":round(dw,3),
                    "dPressure_dt_hPa_ph":round(dp,3),"dTp_dt_sph":round(dtp,3),"risk":round(r,4)})
    return {"peak_risk":round(peak,4),"series":out}


def gpd_exceedance_probability(y: float, xi: float, sigma: float) -> Optional[float]:
    """Generalized Pareto survival P(Y>y) for threshold exceedance Y=H-u."""
    if y < 0 or sigma <= 0: return None
    if abs(xi) < 1e-9: return math.exp(-y/sigma)
    z=1.0+xi*y/sigma
    if z<=0: return 0.0
    return clamp(z**(-1.0/xi),0.0,1.0)


def statistical_hmax_quantile(hs_m: float, period_s: float, duration_h: float=3.0, probability: float=0.95) -> Optional[float]:
    """Rayleigh-sea screening estimate for maximum individual wave over a window, not rogue prediction."""
    if hs_m<=0 or period_s<=0: return None
    n=max(1.0,duration_h*3600.0/period_s)
    p=clamp(probability,0.01,0.999999)
    # P(max<=h)=[1-exp(-2(h/Hs)^2)]^N
    q=max(1e-12,1.0-p**(1.0/n))
    return hs_m*math.sqrt(max(0.0,-0.5*math.log(q)))


def extreme_wave_screen(hs_m: float, period_s: float, context: Mapping[str,Any]) -> Dict[str,Any]:
    ext=dict((context or {}).get("evt",{}) or {})
    hmax_obs=safe_float((context or {}).get("hmax_m"))
    hmax_est=statistical_hmax_quantile(hs_m,period_s,3.0,0.95)
    threshold=safe_float(ext.get("threshold_u_m")); xi=safe_float(ext.get("shape_xi")); sigma=safe_float(ext.get("scale_sigma_m"))
    critical=safe_float(ext.get("critical_height_m"))
    evt_p=None
    if threshold is not None and xi is not None and sigma is not None and critical is not None and critical>threshold:
        evt_p=gpd_exceedance_probability(critical-threshold,xi,sigma)
    hmax=hmax_obs if hmax_obs is not None else hmax_est
    ai=None if hs_m<=0 or hmax is None else hmax/hs_m
    return {"hmax_m":hmax,"hmax_source":"observed/supplied" if hmax_obs is not None else "rayleigh_screening",
            "abnormality_index_hmax_hs":ai,"evt_exceedance_probability":evt_p}


def rogue_environment_score(hs_m: float, period_s: float, opposition: Optional[float], context: Mapping[str,Any], hmax: Optional[float]=None) -> Dict[str,Any]:
    ds=safe_float((context or {}).get("directional_spread_deg"))
    bw=safe_float((context or {}).get("spectral_bandwidth"))
    steep=wave_steepness(hs_m,period_s) or 0.0
    steep_f=clamp((steep-1/40)/(1/12-1/40),0,1)
    dir_f=0.35 if ds is None else clamp((35.0-ds)/30.0,0,1)
    narrow=0.35 if bw is None else clamp((0.55-bw)/0.45,0,1)
    opp=opposition or 0.0
    ai=None if hmax is None or hs_m<=0 else hmax/hs_m
    abnormal=0.0 if ai is None else clamp((ai-1.6)/0.8,0,1)
    score=clamp(0.30*steep_f+0.22*dir_f+0.18*narrow+0.20*opp+0.10*abnormal,0,1)
    return {"risk_environment":score,"directional_spread_deg":ds,"spectral_bandwidth":bw,"abnormality_index":ai,
            "note":"Environmental screening only; exact rogue-wave time/location is not predicted."}


def interp1(x: Sequence[float], y: Sequence[float], q: float) -> Optional[float]:
    if len(x)!=len(y) or not x: return None
    pts=sorted((float(a),float(b)) for a,b in zip(x,y))
    if q<=pts[0][0]: return pts[0][1]
    if q>=pts[-1][0]: return pts[-1][1]
    for (x0,y0),(x1,y1) in zip(pts,pts[1:]):
        if x0<=q<=x1:
            f=(q-x0)/(x1-x0) if x1!=x0 else 0
            return y0+f*(y1-y0)
    return None


def jonswap_like_spectrum(freq_hz: Sequence[float], hs_m: float, tp_s: float) -> List[float]:
    """Normalized JONSWAP-like spectrum scaled to m0=(Hs/4)^2."""
    if hs_m<=0 or tp_s<=0: return [0.0 for _ in freq_hz]
    fp=1.0/tp_s; raw=[]
    for f in freq_hz:
        f=max(1e-4,float(f)); sig=0.07 if f<=fp else 0.09
        r=math.exp(-((f-fp)**2)/(2*sig*sig*fp*fp))
        val=(f**-5)*math.exp(-1.25*(fp/f)**4)*(3.3**r)
        raw.append(val)
    integ=0.0
    for i in range(1,len(freq_hz)):
        integ+=0.5*(raw[i]+raw[i-1])*(float(freq_hz[i])-float(freq_hz[i-1]))
    target=(hs_m/4.0)**2
    scale=0.0 if integ<=0 else target/integ
    return [v*scale for v in raw]


def rao_motion_response(hs_m: float, tp_s: float, rao_data: Mapping[str,Any], encounter_angle_deg: Optional[float]=None) -> Dict[str,Any]:
    """Implements S_j=|RAO|²S_eta; m0=∫S_j dω; sigma=sqrt(m0) using supplied RAOs."""
    if not rao_data: return {"available":False,"reason":"No vessel-specific RAO data supplied."}
    freq=rao_data.get("frequency_hz") or []
    if len(freq)<3: return {"available":False,"reason":"RAO frequency grid is insufficient."}
    spec=jonswap_like_spectrum(freq,hs_m,tp_s)
    motions={}
    for dof in ("roll","pitch","heave","yaw"):
        amp=rao_data.get(dof)
        if not isinstance(amp,list) or len(amp)!=len(freq): continue
        sj=[max(0.0,float(a))**2*s for a,s in zip(amp,spec)]
        m0=0.0
        for i in range(1,len(freq)):
            m0+=0.5*(sj[i]+sj[i-1])*(float(freq[i])-float(freq[i-1]))
        motions[dof]={"variance_m0":m0,"sigma":math.sqrt(max(0.0,m0))}
    return {"available":bool(motions),"motions":motions,"encounter_angle_deg":encounter_angle_deg,
            "note":"Uses user-supplied vessel RAOs; not a substitute for approved seakeeping/stability calculations."}


def effective_gm_screen(gm_m: Optional[float], free_surface_correction_m: Optional[float]) -> Optional[float]:
    if gm_m is None: return None
    return gm_m-max(0.0,free_surface_correction_m or 0.0)


def dynamic_stability_screen(vessel: Mapping[str,Any], cfg: Any, metrics: Mapping[str,Any]) -> Dict[str,Any]:
    gm_eff=effective_gm_screen(vessel.get("gm_m"),cfg.free_surface_correction_m)
    cargo=0.0 if vessel.get("cargo_shift_susceptibility") is None else vessel.get("cargo_shift_susceptibility")
    sync=safe_float((metrics.get("risk_scores") or {}).get("synchronous_roll_proxy"),0) or 0
    param=safe_float((metrics.get("risk_scores") or {}).get("parametric_roll_proxy"),0) or 0
    accel=safe_float((metrics.get("risk_scores") or {}).get("excessive_motion_acceleration"),0) or 0
    green=safe_float((metrics.get("risk_scores") or {}).get("deck_wetness_green_water"),0) or 0
    gm_pen=0.25 if gm_eff is None else clamp((0.5-gm_eff)/0.5,0,1)
    score=clamp(0.28*max(sync,param)+0.20*accel+0.18*green+0.18*cargo+0.16*gm_pen,0,1)
    return {"risk":score,"effective_gm_m":gm_eff,"cargo_shift_susceptibility":cargo,
            "certification":None,"note":"Screening only; approved stability information supersedes this output."}


def probabilistic_ukc(cfg: Any, vessel: Mapping[str,Any], context: Mapping[str,Any]) -> Dict[str,Any]:
    depth=safe_float((context or {}).get("charted_depth_m"),cfg.water_depth_m)
    tide=safe_float((context or {}).get("trusted_tide_m"))
    surge=safe_float((context or {}).get("trusted_surge_m"),0.0) or 0.0
    setup=safe_float((context or {}).get("wave_setup_m"),0.0) or 0.0
    draft=safe_float(vessel.get("draft_m")); squat=safe_float(cfg.squat_m); dyn=safe_float(cfg.dynamic_motion_allowance_m)
    margin=safe_float(cfg.safety_margin_m)
    needed=[depth,tide,draft,squat,dyn,margin]
    if any(v is None for v in needed):
        return {"available":False,"ukc_m":None,"p_below_required":None,
                "missing":[name for name,val in zip(("trusted depth","trusted tide","draft","squat","dynamic motion allowance","safety margin"),needed) if val is None]}
    ukc=depth+tide+surge+setup-draft-squat-dyn-margin
    sig=math.sqrt(sum(max(0.0,x or 0.0)**2 for x in (cfg.depth_uncertainty_m,cfg.tide_uncertainty_m,cfg.draft_uncertainty_m,cfg.squat_uncertainty_m)))
    p=None
    if sig>1e-9:
        z=(0.0-ukc)/sig
        p=0.5*(1.0+math.erf(z/math.sqrt(2.0)))
    else:
        p=1.0 if ukc<0 else 0.0
    return {"available":True,"ukc_m":ukc,"p_below_required":clamp(p,0,1),"combined_uncertainty_m":sig,
            "components":{"depth_m":depth,"tide_m":tide,"surge_m":surge,"wave_setup_m":setup,"draft_m":draft,"squat_m":squat,"dynamic_motion_m":dyn,"safety_margin_m":margin},
            "note":"Requires trusted hydrographic/tidal/vessel inputs; forecast sea level is not substituted."}


def coastal_water_level_risk(context: Mapping[str,Any], wave_height_m: Optional[float]=None) -> Dict[str,Any]:
    tide=safe_float((context or {}).get("trusted_tide_m")); surge=safe_float((context or {}).get("trusted_surge_m")); setup=safe_float((context or {}).get("wave_setup_m"))
    threshold=safe_float((context or {}).get("coastal_flood_threshold_m"))
    vals=[v for v in (tide,surge,setup) if v is not None]
    if not vals: return {"available":False,"eta_total_m":None,"risk":None}
    total=sum(vals)
    if threshold is None: risk=None
    else:
        base=sigmoid(4.0*(total-threshold)/max(0.25,abs(threshold)*0.15))
        slope=safe_float((context or {}).get("coastal_slope")); geometry=clamp(safe_float((context or {}).get("coast_geometry_amplification"),0) or 0,0,1)
        shallow=clamp((safe_float(wave_height_m,0) or 0)/max(1.0,safe_float((context or {}).get("nearshore_depth_m"),5) or 5),0,1)
        slope_factor=0.0 if slope is None else clamp((0.05-slope)/0.05,0,1)
        risk=clamp(base+0.12*geometry+0.08*shallow+0.06*slope_factor,0,1)
    return {"available":True,"eta_total_m":total,"risk":risk,"components":{"tide":tide,"surge":surge,"wave_setup":setup}}


def cyclone_route_exposure(lat: float, lon: float, ensemble: Sequence[Mapping[str,Any]], route_points: Optional[Sequence[Mapping[str,Any]]]=None) -> Dict[str,Any]:
    if not ensemble: return {"available":False,"intersection_probability":None,"members":0,"risk":None}
    pts=[{"lat":lat,"lon":lon}]+list(route_points or [])
    intersects=0; min_dist=[]; member_risks=[]
    for member in ensemble:
        track=member.get("track") or member.get("points") or []
        radius=float(member.get("hazard_radius_km",150.0)); md=1e9
        for tp in track:
            try:
                for rp in pts: md=min(md,haversine_km(float(rp["lat"]),float(rp["lon"]),float(tp["lat"]),float(tp["lon"])))
            except Exception: pass
        min_dist.append(md if md<1e9 else None)
        hit=md<=radius
        if hit: intersects+=1
        intensity=clamp((safe_float(member.get("max_wind_kn"),0) or 0)/100.0,0,1)
        surge=clamp((safe_float(member.get("surge_m"),0) or 0)/4.0,0,1)
        proximity=0.0 if md>=1e9 else math.exp(-md/max(20.0,radius))
        member_risks.append(clamp(0.5*proximity+0.3*intensity+0.2*surge,0,1))
    p=intersects/max(1,len(ensemble))
    return {"available":True,"intersection_probability":p,"members":len(ensemble),"minimum_distances_km":min_dist,
            "risk":clamp(0.65*p+0.35*(statistics.mean(member_risks) if member_risks else 0.0),0,1)}


def sea_ice_risk(context: Mapping[str,Any], vessel: Mapping[str,Any]) -> Dict[str,Any]:
    conc=safe_float((context or {}).get("ice_concentration_pct")); thick=safe_float((context or {}).get("ice_thickness_m")); drift=safe_float((context or {}).get("ice_drift_ms"))
    if conc is None and thick is None: return {"available":False,"risk":None}
    c=clamp((conc or 0.0)/80.0,0,1); t=clamp((thick or 0.0)/1.5,0,1); d=clamp((drift or 0.0)/1.0,0,1)
    capability=1.0 if vessel.get("polar_capable") else 0.0
    risk=clamp((0.5*c+0.35*t+0.15*d)*(1.0-0.65*capability),0,1)
    return {"available":True,"risk":risk,"ice_concentration_pct":conc,"ice_thickness_m":thick,"ice_drift_ms":drift,"polar_capable":bool(vessel.get("polar_capable"))}


def dependent_hazard_fusion(hazards: Mapping[str,Optional[float]], coefficients: Optional[Mapping[str,float]]=None) -> Dict[str,Any]:
    """Dependency-aware fusion approximation.

    Retains the PDF's independent noisy-OR as a diagnostic but does NOT use it as
    the final risk because noisy-OR can become overconfident when correlated
    hazards (wave/motion/stability) are counted separately.  The final score uses
    a calibrated-style top-hazard aggregation plus explicit interaction terms.
    Production deployments should replace coefficients with learned/calibrated
    values or a proper Bayesian graphical model.
    """
    vals={k:clamp(float(v),0,1) for k,v in hazards.items() if v is not None}
    if not vals: return {"risk":0.0,"independent_noisy_or":0.0,"interaction_uplift":0.0}
    prod=1.0
    for v in vals.values(): prod*=1.0-v
    noisy=1.0-prod
    weights=dict(coefficients or {})
    default_w={"wave":1.00,"wind":0.75,"motion":0.95,"stability":1.00,"traffic":0.90,"grounding":1.00,
               "storm":1.00,"ice":0.95,"visibility":0.65,"current":0.60,"coastal":0.90,"human":0.55}
    scored=sorted((clamp(v*float(weights.get(k,default_w.get(k,0.75))),0,1),k) for k,v in vals.items())[::-1]
    top=[x[0] for x in scored[:4]]+[0.0]*max(0,4-len(scored))
    base=clamp(0.52*top[0]+0.25*top[1]+0.15*top[2]+0.08*top[3],0,1)
    pairs=(("wave","motion",0.12),("motion","stability",0.14),("traffic","visibility",0.12),
           ("grounding","current",0.10),("storm","wave",0.11),("coastal","storm",0.10),
           ("ice","visibility",0.08),("human","traffic",0.08))
    interaction_raw=0.0
    for a,b,w in pairs:
        if a in vals and b in vals: interaction_raw+=w*vals[a]*vals[b]
    uplift=min(0.22*(1.0-base),interaction_raw*(1.0-base))
    return {"risk":clamp(base+uplift,0,1),"independent_noisy_or":noisy,"interaction_uplift":uplift,
            "top_hazard_aggregation":base,"hazards":vals,"method":"dependency-aware screening fusion; calibrate coefficients for production"}


def human_factor_risk(context: Mapping[str,Any]) -> Dict[str,Any]:
    h=(context or {}).get("human_factors") or {}
    if not h: return {"available":False,"risk":None}
    fatigue=clamp(safe_float(h.get("fatigue_index"),0) or 0,0,1)
    workload=clamp(safe_float(h.get("workload_index"),0) or 0,0,1)
    watch=clamp(safe_float(h.get("watchkeeping_degradation"),0) or 0,0,1)
    risk=clamp(0.45*fatigue+0.35*workload+0.20*watch,0,1)
    return {"available":True,"risk":risk,"fatigue_index":fatigue,"workload_index":workload,"watchkeeping_degradation":watch}


def navigation_interaction_risk(visibility_score: float, traffic_score: float, speed_kn: float, context: Mapping[str,Any]) -> float:
    night=1.0 if bool((context or {}).get("night",False)) else 0.0
    rain=clamp(safe_float((context or {}).get("rain_intensity_index"),0) or 0,0,1)
    speed=clamp(max(0.0,speed_kn)/25.0,0,1)
    # Explicit interaction: poor visibility + traffic can be disproportionate.
    return clamp(0.35*visibility_score+0.25*traffic_score+0.15*speed+0.10*night+0.08*rain+0.25*visibility_score*traffic_score,0,1)


def grounding_risk_screen(ukc: Mapping[str,Any], context: Mapping[str,Any], current_score: float,
                          wind_score: float, traffic_score: float, speed_kn: float, cfg: Any) -> Dict[str,Any]:
    if not ukc.get("available"):
        return {"available":False,"risk":None,"reason":"Trusted UKC inputs incomplete."}
    p_ukc=clamp(safe_float(ukc.get("p_below_required"),0) or 0,0,1)
    xte=max(0.0,safe_float((context or {}).get("cross_track_error_m"),0) or 0)
    pos=max(0.0,safe_float(cfg.position_uncertainty_m,0) or 0)
    corridor=max(25.0,safe_float((context or {}).get("safe_corridor_half_width_m"),100) or 100)
    nav_error=clamp((xte+pos)/corridor,0,1)
    speed=clamp(max(0.0,speed_kn)/20.0,0,1)
    risk=clamp(0.58*p_ukc+0.16*nav_error+0.08*current_score+0.06*wind_score+0.06*traffic_score+0.06*speed,0,1)
    return {"available":True,"risk":risk,"p_ukc_below_required":p_ukc,"navigation_error_index":nav_error}


def small_vessel_risk_index(vessel: Mapping[str,Any], peak: Mapping[str,Any]) -> Dict[str,Any]:
    L=max(1.0,safe_float(vessel.get("length_m"),1) or 1); B=max(0.5,safe_float(vessel.get("beam_m"),0.5) or 0.5)
    F=max(0.2,safe_float(vessel.get("freeboard_m"),0.2) or 0.2); hs=max(0.0,safe_float(peak.get("wave_height_m"),0) or 0)
    steep=safe_float(peak.get("wave_steepness"),0) or 0; cross=safe_float(peak.get("crossing_sea_index"),0) or 0
    wind=clamp((safe_float(peak.get("wind_kn"),0) or 0)/35.0,0,1)
    score=clamp(0.25*clamp(hs/L*12,0,1)+0.20*clamp(hs/F/2,0,1)+0.15*clamp(steep/(1/14),0,1)+0.15*cross+0.15*wind+0.10*clamp(3.0/B,0,1),0,1)
    return {"applicable":L<=40.0,"svri":score,"note":"Small-vessel screening index; not an approved operating limit."}


def large_ship_risk_index(vessel: Mapping[str,Any], peak: Mapping[str,Any]) -> Dict[str,Any]:
    L=max(1.0,safe_float(vessel.get("length_m"),1) or 1); draft=max(0.1,safe_float(vessel.get("draft_m"),1) or 1)
    rs=peak.get("risk_scores") or {}; hs=max(0.0,safe_float(peak.get("wave_height_m"),0) or 0)
    sl=safe_float(rs.get("slamming"),0) or 0; green=safe_float(rs.get("deck_wetness_green_water"),0) or 0
    prm=safe_float(rs.get("parametric_roll_proxy"),0) or 0; sync=safe_float(rs.get("synchronous_roll_proxy"),0) or 0
    cargo=clamp(safe_float(vessel.get("cargo_shift_susceptibility"),0) or 0,0,1)
    propeller=clamp((hs/(2.0*draft))-0.25,0,1) # screening only
    structural=clamp((safe_float(peak.get("wave_energy_kj_m2"),0) or 0)/80.0,0,1)
    score=clamp(0.20*max(prm,sync)+0.20*sl+0.16*green+0.12*propeller+0.14*cargo+0.18*structural,0,1)
    return {"applicable":L>40.0,"lsri":score,"propeller_emergence_proxy":propeller,"cargo_motion_proxy":cargo,
            "structural_fatigue_exposure_proxy":structural,"note":"Large-ship screening index; vessel-class/approved response data supersede it."}


def spatial_safety_field(lat: float, lon: float, time_value: Any, vessel_id: Any, risk: float, context: Mapping[str,Any]) -> List[Dict[str,Any]]:
    pts=[{"lat":lat,"lon":lon,"time":time_value,"vessel_id":vessel_id,"risk":clamp(risk,0,1),"source":"computed_current_point"}]
    for p in ((context or {}).get("spatial_risk_points") or []):
        try:
            pts.append({"lat":float(p["lat"]),"lon":float(p["lon"]),"time":p.get("time"),"vessel_id":vessel_id,
                        "risk":clamp(float(p["risk"]),0,1),"source":p.get("source","caller_supplied")})
        except Exception: pass
    return pts


def dynamic_reroute_advisory(route_result: Mapping[str,Any], context: Mapping[str,Any]) -> Dict[str,Any]:
    current=(context or {}).get("current_route_id"); selected=route_result.get("selected_candidate_id")
    materially_changed=bool(current and selected and current!=selected)
    return {"current_route_id":current,"lower_risk_candidate_id":selected,"material_change":materially_changed,
            "advisory_only":True,"note":"No automatic helm/course command is issued."}


def safety_level_from_risk(risk: float, official_rank: int=0) -> str:
    if official_rank>=4 or risk>=0.85: return "CRITICAL"
    if risk>=0.62: return "HIGH RISK"
    if risk>=0.30: return "CAUTION"
    return "NORMAL"


def explainable_contributions(hazards: Mapping[str,Optional[float]]) -> Dict[str,float]:
    vals={k:max(0.0,float(v)) for k,v in hazards.items() if v is not None and float(v)>0}
    if not vals: return {}
    # Model-derived normalized contribution proxy; not claimed as causal SHAP values.
    total=sum(v*v for v in vals.values())
    return {k:round(100.0*v*v/total,2) for k,v in sorted(vals.items(),key=lambda z:z[1],reverse=True)}


def _invert_matrix(a: Sequence[Sequence[float]]) -> Optional[List[List[float]]]:
    n=len(a)
    if n==0 or any(len(r)!=n for r in a): return None
    m=[[float(a[i][j]) for j in range(n)]+[1.0 if i==j else 0.0 for j in range(n)] for i in range(n)]
    for col in range(n):
        pivot=max(range(col,n),key=lambda r:abs(m[r][col]))
        if abs(m[pivot][col])<1e-12: return None
        m[col],m[pivot]=m[pivot],m[col]
        div=m[col][col]; m[col]=[v/div for v in m[col]]
        for r in range(n):
            if r==col: continue
            f=m[r][col]
            m[r]=[m[r][j]-f*m[col][j] for j in range(2*n)]
    return [row[n:] for row in m]


def mahalanobis_ood(features: Mapping[str,float], validation_domain: Mapping[str,Any]) -> Dict[str,Any]:
    vd=validation_domain or {}; names=vd.get("features") or []
    mean_vec=vd.get("mean_vector"); cov=vd.get("covariance")
    if names and isinstance(mean_vec,list) and isinstance(cov,list) and all(k in features for k in names):
        inv=_invert_matrix(cov)
        if inv is not None and len(mean_vec)==len(names):
            d=[float(features[k])-float(mean_vec[i]) for i,k in enumerate(names)]
            q=sum(d[i]*inv[i][j]*d[j] for i in range(len(d)) for j in range(len(d)))
            dist=math.sqrt(max(0.0,q)); threshold=safe_float(vd.get("threshold"),math.sqrt(len(d))*3.0) or math.sqrt(len(d))*3.0
            return {"available":True,"distance":dist,"threshold":threshold,"out_of_domain":dist>threshold,"method":"full_mahalanobis"}
    means=vd.get("mean") or {}; stds=vd.get("std") or {}; z2=0.0; n=0; used={}
    for k,v in features.items():
        if k in means and k in stds and safe_float(stds[k],0) and float(stds[k])>0:
            z=(float(v)-float(means[k]))/float(stds[k]); z2+=z*z; n+=1; used[k]=z
    if not n: return {"available":False,"distance":None,"out_of_domain":False}
    dist=math.sqrt(z2); threshold=safe_float(vd.get("threshold"),math.sqrt(n)*3.0) or math.sqrt(n)*3.0
    return {"available":True,"distance":dist,"threshold":threshold,"out_of_domain":dist>threshold,"z_scores":used,"method":"diagonal_mahalanobis"}


def bayesian_scalar_fusion(prior_mean: float, prior_sigma: float, observation: float, obs_sigma: float) -> Dict[str,float]:
    pv=max(1e-12,prior_sigma**2); ov=max(1e-12,obs_sigma**2)
    post_var=1.0/(1.0/pv+1.0/ov); post_mean=post_var*(prior_mean/pv+observation/ov)
    return {"posterior_mean":post_mean,"posterior_sigma":math.sqrt(post_var),"kalman_gain":pv/(pv+ov)}


def assimilate_consensus_observations(consensus: Sequence[Mapping[str,Any]], observations: Mapping[str,Any]) -> Dict[str,Any]:
    """Diagnostic scalar Bayesian updates for first-hour observed variables."""
    if not consensus or not observations: return {"available":False,"updates":{}}
    first=consensus[0]; updates={}
    mapping={"wave_height_m":0.5,"wind_kn":5.0,"pressure_hpa":2.0,"visibility_m":1500.0}
    for k,ps in mapping.items():
        obs=observations.get(k)
        if isinstance(obs,Mapping):
            val=safe_float(obs.get("value")); os=safe_float(obs.get("sigma"),ps)
        else:
            val=safe_float(obs); os=ps
        prior=safe_float(first.get(k))
        if prior is not None and val is not None and os and os>0:
            updates[k]=bayesian_scalar_fusion(prior,ps,val,os)
    return {"available":bool(updates),"updates":updates}


def safety_forecast_timeline(hourly: Sequence[Mapping[str,Any]], official_rank: int=0) -> List[Dict[str,Any]]:
    if not hourly: return []
    horizons=(0,3,6,12,24,48,72)
    out=[]
    for h in horizons:
        idx=min(h,len(hourly)-1)
        r=safe_float((hourly[idx].get("risk_scores") or {}).get("composite_operational_exposure"),0.0) or 0.0
        out.append({"lead_hours":h,"time":hourly[idx].get("time"),"risk":round(r,4),"level":safety_level_from_risk(r,official_rank)})
    return out


def route_candidate_optimizer(candidates: Sequence[Mapping[str,Any]], critical_risk: float=0.80,
                              weights: Optional[Mapping[str,float]]=None) -> Dict[str,Any]:
    """Safety-first C_e=alpha*T+beta*Fuel+gamma*Risk+delta*Motion+epsilon*Uncertainty."""
    w={"time":1.0,"fuel":0.15,"risk":8.0,"motion":3.0,"uncertainty":2.5}; w.update(dict(weights or {}))
    evaluated=[]
    for c in candidates or []:
        segs=c.get("segments") or []; rejected=False; total=0.0; reasons=[]
        for seg in segs:
            risk=clamp(safe_float(seg.get("risk"),0.0) or 0.0,0,1)
            if seg.get("closed") or seg.get("official_prohibited") or risk>=critical_risk:
                rejected=True; reasons.append("hard safety/closure constraint")
            if seg.get("ukc_ok") is False: rejected=True; reasons.append("UKC constraint")
            total += w["time"]*(safe_float(seg.get("time_h"),0) or 0) + w["fuel"]*(safe_float(seg.get("fuel_index"),0) or 0) + w["risk"]*risk + w["motion"]*(safe_float(seg.get("motion"),0) or 0) + w["uncertainty"]*(safe_float(seg.get("uncertainty"),0) or 0)
        evaluated.append({"id":c.get("id"),"cost":None if rejected else total,"utility":float("-inf") if rejected else -total,"rejected":rejected,"reasons":sorted(set(reasons))})
    feasible=[x for x in evaluated if not x["rejected"]]
    best=min(feasible,key=lambda x:x["cost"]) if feasible else None
    return {"available":bool(evaluated),"selected_candidate_id":None if best is None else best["id"],"evaluated":evaluated,"weights":w,
            "navigation_clearance":None,"note":"Candidate-route decision support only; hard constraints override fuel/time."}


def fisheries_habitat_probability(ctx: Mapping[str,Any]) -> Dict[str,Any]:
    if not ctx: return {"available":False,"probability":None}
    # Generic configurable logistic habitat model; coefficients must be species/local calibrated.
    beta0=safe_float(ctx.get("beta0"),-1.5) or -1.5
    coeff=ctx.get("coefficients") or {}
    features={k:safe_float(ctx.get(k)) for k in ("sst_c","chlorophyll","salinity_psu","depth_m","oxygen_mg_l","mld_m","current_ms","grad_temp","grad_chl")}
    if not any(v is not None for v in features.values()): return {"available":False,"probability":None}
    x=beta0
    for k,v in features.items():
        if v is not None: x += safe_float(coeff.get(k),0.0)*v
    return {"available":True,"probability":sigmoid(x),"features":features,"calibrated":bool(ctx.get("calibrated",False)),
            "safety_gate_required":True}


def sustainable_stock_step(ctx: Mapping[str,Any]) -> Dict[str,Any]:
    if not ctx: return {"available":False}
    ages=ctx.get("ages") or []
    if not ages: return {"available":False}
    next_n=[]; ssb=0.0
    for a in ages:
        n=max(0.0,safe_float(a.get("N"),0) or 0); M=max(0.0,safe_float(a.get("M"),0) or 0); F=max(0.0,safe_float(a.get("F"),0) or 0)
        nxt=n*math.exp(-(M+F)); next_n.append(nxt)
        ssb += n*max(0.0,safe_float(a.get("weight"),0) or 0)*clamp(safe_float(a.get("maturity"),0) or 0,0,1)
    alpha=safe_float(ctx.get("alpha")); beta=safe_float(ctx.get("beta")); recruitment=None
    if alpha is not None and beta is not None:
        env=math.exp((safe_float(ctx.get("gamma_temp"),0) or 0)*(safe_float(ctx.get("temp"),0) or 0)+(safe_float(ctx.get("gamma_chl"),0) or 0)*(safe_float(ctx.get("chl"),0) or 0)+(safe_float(ctx.get("gamma_mld"),0) or 0)*(safe_float(ctx.get("mld"),0) or 0))
        recruitment=(alpha*ssb/(1.0+beta*ssb))*env if (1.0+beta*ssb)>0 else None
    return {"available":True,"ssb":ssb,"next_abundance":next_n,"recruitment":recruitment}


def ensemble_crps(observation: float, ensemble: Sequence[float]) -> Optional[float]:
    ens=[float(x) for x in ensemble if x is not None]
    if not ens: return None
    a=statistics.mean(abs(x-observation) for x in ens)
    b=sum(abs(x-y) for x in ens for y in ens)/(2.0*len(ens)*len(ens))
    return a-b


def reliability_bins(probabilities: Sequence[float], events: Sequence[int], bins: int=10) -> List[Dict[str,Any]]:
    out=[]
    for bi in range(bins):
        lo=bi/bins; hi=(bi+1)/bins
        pts=[(clamp(float(p),0,1),int(e)) for p,e in zip(probabilities,events) if lo<=float(p)<hi or (bi==bins-1 and float(p)==1.0)]
        if pts: out.append({"bin_center":(lo+hi)/2,"count":len(pts),"mean_forecast":statistics.mean(p for p,_ in pts),"observed_frequency":statistics.mean(e for _,e in pts)})
    return out


def validation_metrics(observed: Sequence[float], predicted: Sequence[float], probabilities: Optional[Sequence[float]]=None,
                       events: Optional[Sequence[int]]=None, ensemble_predictions: Optional[Sequence[Sequence[float]]]=None) -> Dict[str,Any]:
    pairs=[(float(o),float(p)) for o,p in zip(observed,predicted) if o is not None and p is not None]
    if not pairs: return {"available":False}
    err=[p-o for o,p in pairs]
    out={"available":True,"n":len(err),"bias":statistics.mean(err),"mae":statistics.mean(abs(e) for e in err),"rmse":math.sqrt(statistics.mean(e*e for e in err))}
    if probabilities is not None and events is not None:
        pe=[(clamp(float(p),0,1),int(e)) for p,e in zip(probabilities,events)]
        if pe:
            out["brier_score"]=statistics.mean((p-e)**2 for p,e in pe)
            out["reliability_bins"]=reliability_bins([p for p,_ in pe],[e for _,e in pe])
    if ensemble_predictions is not None:
        cr=[ensemble_crps(float(o),ens) for o,ens in zip(observed,ensemble_predictions)]
        cr=[x for x in cr if x is not None]
        if cr: out["mean_crps"]=statistics.mean(cr)
    return out


def blocked_spatiotemporal_folds(records: Sequence[Mapping[str,Any]], spatial_key: str="area", temporal_key: str="season") -> List[Dict[str,Any]]:
    """Leave-area/season-out fold descriptors; avoids random split leakage."""
    groups={}
    for i,r in enumerate(records): groups.setdefault((r.get(spatial_key),r.get(temporal_key)),[]).append(i)
    all_idx=set(range(len(records))); out=[]
    for key,test in groups.items(): out.append({"held_out":key,"train_indices":sorted(all_idx-set(test)),"test_indices":test})
    return out


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

@dataclass
class CalibrationRecord:
    bias: float = 0.0
    rmse: float = 1.0
    crps: Optional[float] = None
    reliability: float = 0.65
    sample_size: int = 0

    @property
    def quality(self) -> float:
        n_factor = min(1.0, max(0.15, self.sample_size / 300.0)) if self.sample_size else 0.35
        rel = clamp(self.reliability, 0.05, 1.0)
        crps_factor = 1.0 if self.crps is None else 1.0 / (1.0 + max(0.0, self.crps))
        return clamp((0.55 * rel + 0.45 * crps_factor) * n_factor, 0.05, 1.0)


DEFAULT_RMSE: Dict[str, float] = {
    "wind_speed_ms": 2.8,
    "wind_gust_ms": 4.0,
    "wind_direction_deg": 20.0,
    "pressure_hpa": 2.5,
    "visibility_m": 1800.0,
    "cape_jkg": 300.0,
    "wave_height_m": 0.55,
    "wave_period_s": 1.2,
    "wave_direction_deg": 18.0,
    "swell_height_m": 0.45,
    "swell_period_s": 1.4,
    "current_speed_ms": 0.25,
    "current_direction_deg": 25.0,
}


class CalibrationStore:
    """Variable × model × lead-time × season calibration lookup."""

    def __init__(self, raw: Optional[Mapping[str, Any]] = None):
        self.raw = dict(raw or {})

    @classmethod
    def from_json(cls, path: Optional[str]) -> "CalibrationStore":
        if not path or not os.path.exists(path):
            return cls({})
        try:
            with open(path, "r", encoding="utf-8") as f:
                return cls(json.load(f))
        except Exception:
            return cls({})

    def get(self, family: str, model: str, variable: str, lead: int, season: str) -> CalibrationRecord:
        default_rmse = DEFAULT_RMSE.get(variable, 1.0)
        node: Any = self.raw.get(family, {}).get(model, {}).get(variable, {})
        candidates = [
            node.get(lead_band(lead), {}).get(season) if isinstance(node, dict) else None,
            node.get(lead_band(lead), {}).get("all") if isinstance(node, dict) else None,
            node.get("all", {}).get(season) if isinstance(node, dict) else None,
            node.get("all", {}).get("all") if isinstance(node, dict) else None,
        ]
        rec = next((x for x in candidates if isinstance(x, dict)), None)
        if not rec:
            return CalibrationRecord(rmse=default_rmse)
        return CalibrationRecord(
            bias=safe_float(rec.get("bias"), 0.0) or 0.0,
            rmse=max(1e-6, safe_float(rec.get("rmse"), default_rmse) or default_rmse),
            crps=safe_float(rec.get("crps")),
            reliability=clamp(safe_float(rec.get("reliability"), 0.65) or 0.65, 0.0, 1.0),
            sample_size=int(safe_float(rec.get("sample_size"), 0) or 0),
        )

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AtmosHour:
    time: str
    temperature_c: Optional[float] = None
    rh_pct: Optional[float] = None
    pressure_hpa: Optional[float] = None
    wind_speed_ms: Optional[float] = None
    wind_gust_ms: Optional[float] = None
    wind_direction_deg: Optional[float] = None
    precipitation_mm: Optional[float] = None
    visibility_m: Optional[float] = None
    weather_code: Optional[int] = None
    cape_jkg: Optional[float] = None
    cloud_cover_pct: Optional[float] = None


@dataclass
class MarineHour:
    time: str
    wave_height_m: Optional[float] = None
    wave_direction_deg: Optional[float] = None
    wave_period_s: Optional[float] = None
    wave_peak_period_s: Optional[float] = None
    wind_wave_height_m: Optional[float] = None
    wind_wave_direction_deg: Optional[float] = None
    wind_wave_period_s: Optional[float] = None
    wind_wave_peak_period_s: Optional[float] = None
    swell_height_m: Optional[float] = None
    swell_direction_deg: Optional[float] = None
    swell_period_s: Optional[float] = None
    swell_peak_period_s: Optional[float] = None
    secondary_swell_height_m: Optional[float] = None
    secondary_swell_direction_deg: Optional[float] = None
    secondary_swell_period_s: Optional[float] = None
    tertiary_swell_height_m: Optional[float] = None
    tertiary_swell_direction_deg: Optional[float] = None
    tertiary_swell_period_s: Optional[float] = None
    sea_level_msl_m: Optional[float] = None
    sea_surface_temperature_c: Optional[float] = None
    current_speed_ms: Optional[float] = None
    current_direction_deg: Optional[float] = None


@dataclass
class AtmosMember:
    model: str
    hours: List[AtmosHour]
    quality: float = 0.45


@dataclass
class WaveMember:
    model: str
    hours: List[MarineHour]
    quality: float = 0.45


@dataclass
class VesselConfig:
    vessel_id: Optional[str] = None
    vessel_type: str = "generic"
    length_m: Optional[float] = None
    beam_m: Optional[float] = None
    draft_m: Optional[float] = None
    freeboard_m: Optional[float] = None
    displacement_t: Optional[float] = None
    service_speed_kn: Optional[float] = None
    heading_deg: Optional[float] = None
    roll_period_s: Optional[float] = None
    gm_m: Optional[float] = None
    navigation_zone: str = "offshore"  # harbour/coastal/offshore

    # Optional owner/operator limits.  These are screening inputs, not certified limits.
    max_operating_hs_m: Optional[float] = None
    max_operating_wind_kn: Optional[float] = None
    max_operating_gust_kn: Optional[float] = None

    # Optional contextual data.  A model-derived sea level is never used as a
    # navigation-grade tide or under-keel-clearance calculation.
    water_depth_m: Optional[float] = None

    # Extended vessel digital twin (Ocean Guardian-X). Values should come from
    # approved vessel/loading information when they affect navigation or stability.
    hull_depth_m: Optional[float] = None
    propulsion_power_kw: Optional[float] = None
    maneuverability_index: Optional[float] = None  # normalized 0..1 if known
    cargo_state: Optional[str] = None
    cargo_shift_susceptibility: Optional[float] = None  # screening 0..1
    ballast_state: Optional[str] = None
    free_surface_correction_m: Optional[float] = None
    polar_capable: bool = False
    polar_class: Optional[str] = None

    # Optional trusted navigation/hydrographic inputs. These are never inferred
    # from generic forecast sea-level fields when absent.
    safety_margin_m: Optional[float] = None
    squat_m: Optional[float] = None
    dynamic_motion_allowance_m: Optional[float] = None
    depth_uncertainty_m: Optional[float] = None
    tide_uncertainty_m: Optional[float] = None
    draft_uncertainty_m: Optional[float] = None
    squat_uncertainty_m: Optional[float] = None
    position_uncertainty_m: Optional[float] = None

    # Optional external contexts. They allow the mathematical engine to support
    # the full Ocean Guardian-X concept without inventing unavailable feeds.
    external_context: Dict[str, Any] = field(default_factory=dict)
    traffic_contacts: List[Dict[str, Any]] = field(default_factory=list)
    cyclone_ensemble: List[Dict[str, Any]] = field(default_factory=list)
    route_candidates: List[Dict[str, Any]] = field(default_factory=list)
    rao_data: Dict[str, Any] = field(default_factory=dict)
    validation_domain: Dict[str, Any] = field(default_factory=dict)
    sensor_observations: Dict[str, Any] = field(default_factory=dict)
    fishing_context: Dict[str, Any] = field(default_factory=dict)
    stock_context: Dict[str, Any] = field(default_factory=dict)

    # Trusted official warning supplied by an external authoritative feed/user.
    official_warning_level: str = "none"
    official_warning_text: Optional[str] = None
    official_warning_source: Optional[str] = None
    official_warning_issued_at: Optional[str] = None

    # Persistence / voyage state hooks.
    prior_state_timestamp: Optional[str] = None
    prior_cumulative_rough_hours: float = 0.0
    prior_motion_exposure_index: float = 0.0

    # Calibration declaration.
    vessel_profile_calibrated: bool = False
    local_marine_calibrated: bool = False

    @classmethod
    def from_mapping(cls, m: Optional[Mapping[str, Any]]) -> "VesselConfig":
        data = dict(m or {})
        allowed = set(cls.__dataclass_fields__.keys())
        cleaned = {k: v for k, v in data.items() if k in allowed}
        for key in ("external_context","rao_data","validation_domain","sensor_observations","fishing_context","stock_context"):
            if cleaned.get(key) is None: cleaned[key] = {}
        for key in ("traffic_contacts","cyclone_ensemble","route_candidates"):
            if cleaned.get(key) is None: cleaned[key] = []
        return cls(**cleaned)

    def resolved(self) -> Dict[str, Any]:
        key = (self.vessel_type or "generic").strip().lower().replace(" ", "_")
        p = VESSEL_PROFILES.get(key, VESSEL_PROFILES["generic"])
        return {
            "profile_key": key if key in VESSEL_PROFILES else "generic",
            "profile_known": key in VESSEL_PROFILES and key != "generic",
            "length_m": self.length_m if self.length_m and self.length_m > 0 else p["length_m"],
            "beam_m": self.beam_m if self.beam_m and self.beam_m > 0 else p["beam_m"],
            "draft_m": self.draft_m if self.draft_m and self.draft_m > 0 else None,
            "freeboard_m": self.freeboard_m if self.freeboard_m and self.freeboard_m > 0 else p["freeboard_m"],
            "displacement_t": self.displacement_t if self.displacement_t and self.displacement_t > 0 else None,
            "service_speed_kn": self.service_speed_kn if self.service_speed_kn and self.service_speed_kn >= 0 else p["service_speed_kn"],
            "heading_deg": self.heading_deg % 360.0 if self.heading_deg is not None else None,
            "roll_period_s": self.roll_period_s if self.roll_period_s and self.roll_period_s > 0 else estimated_roll_period_s(
                self.beam_m if self.beam_m and self.beam_m > 0 else p["beam_m"], self.gm_m
            ),
            "roll_period_source": "user" if self.roll_period_s else ("estimated_from_GM" if self.gm_m else "unavailable"),
            "gm_m": self.gm_m,
            "hull_depth_m": self.hull_depth_m,
            "propulsion_power_kw": self.propulsion_power_kw,
            "maneuverability_index": None if self.maneuverability_index is None else clamp(self.maneuverability_index,0.0,1.0),
            "cargo_state": self.cargo_state,
            "cargo_shift_susceptibility": None if self.cargo_shift_susceptibility is None else clamp(self.cargo_shift_susceptibility,0.0,1.0),
            "ballast_state": self.ballast_state,
            "free_surface_correction_m": self.free_surface_correction_m,
            "polar_capable": bool(self.polar_capable),
            "polar_class": self.polar_class,
            "screen_hs_m": self.max_operating_hs_m if self.max_operating_hs_m and self.max_operating_hs_m > 0 else p["screen_hs_m"],
            "screen_wind_kn": self.max_operating_wind_kn if self.max_operating_wind_kn and self.max_operating_wind_kn > 0 else p["screen_wind_kn"],
            "screen_gust_kn": self.max_operating_gust_kn if self.max_operating_gust_kn and self.max_operating_gust_kn > 0 else p["screen_gust_kn"],
            "limits_source": "user/operator" if any([self.max_operating_hs_m, self.max_operating_wind_kn, self.max_operating_gust_kn]) else "generic_screening_profile",
        }

# ---------------------------------------------------------------------------
# Data acquisition / calibration
# ---------------------------------------------------------------------------


def _request_json(url: str, params: Mapping[str, Any], timeout: float = 10.0) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code != 200:
            return None
        data = r.json()
        return data if isinstance(data, dict) and not data.get("error") else None
    except Exception:
        return None


def _series_value(series: Mapping[str, Any], key: str, i: int) -> Optional[float]:
    arr = series.get(key) or []
    if i >= len(arr):
        return None
    return safe_float(arr[i])


def fetch_atmos_member(lat: float, lon: float, model: str, horizon_hours: int, calibration: CalibrationStore) -> Optional[AtmosMember]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(ATMOS_VARIABLES),
        "forecast_hours": horizon_hours,
        "timezone": "UTC",
        "wind_speed_unit": "ms",
        "models": model,
    }
    data = _request_json("https://api.open-meteo.com/v1/forecast", params)
    if not data or not isinstance(data.get("hourly"), dict):
        return None
    h = data["hourly"]
    times = h.get("time") or []
    hours: List[AtmosHour] = []
    qualities: List[float] = []
    now = datetime.now(timezone.utc)
    season = season_key(now, lat)
    for i, t in enumerate(times[:horizon_hours]):
        # Open-Meteo wind_speed_unit=ms guarantees m/s for these fields.
        lead = i + 1
        def cal(key: str, raw: Optional[float]) -> Optional[float]:
            if raw is None:
                return None
            rec = calibration.get("atmosphere", model, key, lead, season)
            qualities.append(clamp(rec.quality * math.sqrt(max(0.15, DEFAULT_RMSE.get(key, rec.rmse) / max(rec.rmse, 1e-6))), 0.05, 1.0))
            return raw - rec.bias
        hours.append(AtmosHour(
            time=str(t),
            temperature_c=_series_value(h, "temperature_2m", i),
            rh_pct=_series_value(h, "relative_humidity_2m", i),
            pressure_hpa=cal("pressure_hpa", _series_value(h, "surface_pressure", i)),
            wind_speed_ms=max(0.0, cal("wind_speed_ms", _series_value(h, "wind_speed_10m", i)) or 0.0),
            wind_gust_ms=max(0.0, cal("wind_gust_ms", _series_value(h, "wind_gusts_10m", i)) or 0.0),
            wind_direction_deg=_series_value(h, "wind_direction_10m", i),
            precipitation_mm=max(0.0, _series_value(h, "precipitation", i) or 0.0),
            visibility_m=max(0.0, cal("visibility_m", _series_value(h, "visibility", i)) or 0.0) if _series_value(h, "visibility", i) is not None else None,
            weather_code=int(_series_value(h, "weather_code", i) or 0),
            cape_jkg=max(0.0, cal("cape_jkg", _series_value(h, "cape", i)) or 0.0) if _series_value(h, "cape", i) is not None else None,
            cloud_cover_pct=_series_value(h, "cloud_cover", i),
        ))
    if len(hours) < min(12, horizon_hours):
        return None
    q = statistics.mean(qualities) if qualities else 0.40
    return AtmosMember(model=model, hours=hours, quality=clamp(q, 0.05, 1.0))


def fetch_wave_member(lat: float, lon: float, model: str, horizon_hours: int, calibration: CalibrationStore) -> Optional[WaveMember]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(MARINE_VARIABLES),
        "forecast_hours": horizon_hours,
        "timezone": "UTC",
        "length_unit": "metric",
        "models": model,
        "cell_selection": "sea",
    }
    data = _request_json("https://marine-api.open-meteo.com/v1/marine", params)
    if not data or not isinstance(data.get("hourly"), dict):
        return None
    h = data["hourly"]
    times = h.get("time") or []
    now = datetime.now(timezone.utc)
    season = season_key(now, lat)
    qualities: List[float] = []
    hours: List[MarineHour] = []
    for i, t in enumerate(times[:horizon_hours]):
        lead = i + 1
        def cal(key: str, raw: Optional[float], floor0: bool = False) -> Optional[float]:
            if raw is None:
                return None
            rec = calibration.get("waves", model, key, lead, season)
            qualities.append(clamp(rec.quality * math.sqrt(max(0.15, DEFAULT_RMSE.get(key, rec.rmse) / max(rec.rmse, 1e-6))), 0.05, 1.0))
            v = raw - rec.bias
            return max(0.0, v) if floor0 else v
        # Open-Meteo current velocity defaults km/h; convert to m/s.
        current_kmh = _series_value(h, "ocean_current_velocity", i)
        cur_ms = current_kmh * KMH_TO_MS if current_kmh is not None else None
        hours.append(MarineHour(
            time=str(t),
            wave_height_m=cal("wave_height_m", _series_value(h, "wave_height", i), True),
            wave_direction_deg=_series_value(h, "wave_direction", i),
            wave_period_s=cal("wave_period_s", _series_value(h, "wave_period", i), True),
            wave_peak_period_s=cal("wave_period_s", _series_value(h, "wave_peak_period", i), True),
            wind_wave_height_m=cal("wave_height_m", _series_value(h, "wind_wave_height", i), True),
            wind_wave_direction_deg=_series_value(h, "wind_wave_direction", i),
            wind_wave_period_s=cal("wave_period_s", _series_value(h, "wind_wave_period", i), True),
            wind_wave_peak_period_s=cal("wave_period_s", _series_value(h, "wind_wave_peak_period", i), True),
            swell_height_m=cal("swell_height_m", _series_value(h, "swell_wave_height", i), True),
            swell_direction_deg=_series_value(h, "swell_wave_direction", i),
            swell_period_s=cal("swell_period_s", _series_value(h, "swell_wave_period", i), True),
            swell_peak_period_s=cal("swell_period_s", _series_value(h, "swell_wave_peak_period", i), True),
            secondary_swell_height_m=cal("swell_height_m", _series_value(h, "secondary_swell_wave_height", i), True),
            secondary_swell_direction_deg=_series_value(h, "secondary_swell_wave_direction", i),
            secondary_swell_period_s=cal("swell_period_s", _series_value(h, "secondary_swell_wave_period", i), True),
            tertiary_swell_height_m=cal("swell_height_m", _series_value(h, "tertiary_swell_wave_height", i), True),
            tertiary_swell_direction_deg=_series_value(h, "tertiary_swell_wave_direction", i),
            tertiary_swell_period_s=cal("swell_period_s", _series_value(h, "tertiary_swell_wave_period", i), True),
            sea_level_msl_m=_series_value(h, "sea_level_height_msl", i),
            sea_surface_temperature_c=_series_value(h, "sea_surface_temperature", i),
            current_speed_ms=cal("current_speed_ms", cur_ms, True),
            current_direction_deg=_series_value(h, "ocean_current_direction", i),
        ))
    # Some models expose only a subset. Wave height is mandatory for use.
    valid = [x for x in hours if x.wave_height_m is not None]
    if len(valid) < min(12, horizon_hours):
        return None
    q = statistics.mean(qualities) if qualities else 0.40
    return WaveMember(model=model, hours=hours, quality=clamp(q, 0.05, 1.0))


def model_weight(quality: float, disagreement_penalty: float = 0.0) -> float:
    return math.exp(2.2 * clamp(quality, 0.0, 1.0) - 1.4 * max(0.0, disagreement_penalty))


def member_weights_atmos(members: Sequence[AtmosMember]) -> List[float]:
    if not members:
        return []
    # Global quality weighting; hour-specific error distributions enter scenarios.
    return normalize_weights([model_weight(m.quality) for m in members])


def member_weights_wave(members: Sequence[WaveMember]) -> List[float]:
    if not members:
        return []
    return normalize_weights([model_weight(m.quality) for m in members])


def weighted_choice(items: Sequence[Any], weights: Sequence[float], rng: random.Random) -> Any:
    r = rng.random()
    acc = 0.0
    for item, w in zip(items, normalize_weights(weights)):
        acc += w
        if r <= acc:
            return item
    return items[-1]

# ---------------------------------------------------------------------------
# Environmental consensus and uncertainty
# ---------------------------------------------------------------------------


def _atmos_consensus(members: Sequence[AtmosMember], horizon: int) -> List[AtmosHour]:
    weights = member_weights_atmos(members)
    n = min([len(m.hours) for m in members] + [horizon])
    out: List[AtmosHour] = []
    for i in range(n):
        hs = [m.hours[i] for m in members]
        out.append(AtmosHour(
            time=hs[0].time,
            temperature_c=weighted_mean([x.temperature_c for x in hs], weights),
            rh_pct=weighted_mean([x.rh_pct for x in hs], weights),
            pressure_hpa=weighted_mean([x.pressure_hpa for x in hs], weights),
            wind_speed_ms=weighted_mean([x.wind_speed_ms for x in hs], weights),
            wind_gust_ms=weighted_mean([x.wind_gust_ms for x in hs], weights),
            wind_direction_deg=circular_mean_deg([x.wind_direction_deg for x in hs if x.wind_direction_deg is not None], [weights[j] for j,x in enumerate(hs) if x.wind_direction_deg is not None]),
            precipitation_mm=weighted_mean([x.precipitation_mm for x in hs], weights),
            visibility_m=weighted_mean([x.visibility_m for x in hs], weights),
            weather_code=int(round(weighted_mean([float(x.weather_code or 0) for x in hs], weights) or 0)),
            cape_jkg=weighted_mean([x.cape_jkg for x in hs], weights),
            cloud_cover_pct=weighted_mean([x.cloud_cover_pct for x in hs], weights),
        ))
    return out


def _wave_consensus(members: Sequence[WaveMember], horizon: int) -> List[MarineHour]:
    weights = member_weights_wave(members)
    n = min([len(m.hours) for m in members] + [horizon])
    out: List[MarineHour] = []
    for i in range(n):
        hs = [m.hours[i] for m in members]
        dirs = lambda attr: circular_mean_deg(
            [getattr(x, attr) for x in hs if getattr(x, attr) is not None],
            [weights[j] for j,x in enumerate(hs) if getattr(x, attr) is not None],
        )
        vals = lambda attr: weighted_mean([getattr(x, attr) for x in hs], weights)
        out.append(MarineHour(
            time=hs[0].time,
            wave_height_m=vals("wave_height_m"), wave_direction_deg=dirs("wave_direction_deg"), wave_period_s=vals("wave_period_s"), wave_peak_period_s=vals("wave_peak_period_s"),
            wind_wave_height_m=vals("wind_wave_height_m"), wind_wave_direction_deg=dirs("wind_wave_direction_deg"), wind_wave_period_s=vals("wind_wave_period_s"), wind_wave_peak_period_s=vals("wind_wave_peak_period_s"),
            swell_height_m=vals("swell_height_m"), swell_direction_deg=dirs("swell_direction_deg"), swell_period_s=vals("swell_period_s"), swell_peak_period_s=vals("swell_peak_period_s"),
            secondary_swell_height_m=vals("secondary_swell_height_m"), secondary_swell_direction_deg=dirs("secondary_swell_direction_deg"), secondary_swell_period_s=vals("secondary_swell_period_s"),
            tertiary_swell_height_m=vals("tertiary_swell_height_m"), tertiary_swell_direction_deg=dirs("tertiary_swell_direction_deg"), tertiary_swell_period_s=vals("tertiary_swell_period_s"),
            sea_level_msl_m=vals("sea_level_msl_m"), sea_surface_temperature_c=vals("sea_surface_temperature_c"), current_speed_ms=vals("current_speed_ms"), current_direction_deg=dirs("current_direction_deg"),
        ))
    return out


def consensus_spread(members: Sequence[Any], attr: str, max_hours: int = 48) -> float:
    spreads: List[float] = []
    if not members:
        return 0.0
    n = min(min(len(m.hours) for m in members), max_hours)
    for i in range(n):
        vals = [safe_float(getattr(m.hours[i], attr, None)) for m in members]
        vv = [v for v in vals if v is not None]
        if len(vv) >= 2:
            spreads.append(statistics.pstdev(vv))
    return statistics.mean(spreads) if spreads else 0.0

# ---------------------------------------------------------------------------
# Per-hour hazard physics / screening
# ---------------------------------------------------------------------------


def crossing_sea_index(m: MarineHour) -> Optional[float]:
    pairs: List[Tuple[float, float, float]] = []
    if m.wind_wave_direction_deg is not None and m.swell_direction_deg is not None:
        d = angular_difference_deg(m.wind_wave_direction_deg, m.swell_direction_deg)
        if d is not None:
            pairs.append((d, m.wind_wave_height_m or 0.0, m.swell_height_m or 0.0))
    if m.swell_direction_deg is not None and m.secondary_swell_direction_deg is not None:
        d = angular_difference_deg(m.swell_direction_deg, m.secondary_swell_direction_deg)
        if d is not None:
            pairs.append((d, m.swell_height_m or 0.0, m.secondary_swell_height_m or 0.0))
    if not pairs:
        return None
    scores = []
    for d, h1, h2 in pairs:
        angle_factor = math.sin(math.radians(clamp(d, 0.0, 180.0))) ** 2
        energy_balance = 0.0 if max(h1, h2) <= 0 else min(h1, h2) / max(h1, h2)
        scores.append(angle_factor * math.sqrt(clamp(energy_balance, 0.0, 1.0)))
    return clamp(max(scores), 0.0, 1.0)


def current_opposition_index(m: MarineHour) -> Optional[float]:
    if m.current_direction_deg is None or m.wave_direction_deg is None or m.current_speed_ms is None:
        return None
    propagation = (m.wave_direction_deg + 180.0) % 360.0
    d = angular_difference_deg(propagation, m.current_direction_deg)
    if d is None:
        return None
    # 180° between current flow and wave propagation = strongest opposition.
    direction_factor = (1.0 - math.cos(math.radians(d))) / 2.0
    speed_factor = 1.0 - math.exp(-max(0.0, m.current_speed_ms) / 0.7)
    return clamp(direction_factor * speed_factor, 0.0, 1.0)


def steep_wave_score(hs_m: Optional[float], period_s: Optional[float]) -> float:
    s = wave_steepness(hs_m, period_s)
    if s is None:
        return 0.0
    # NOAA Coast Pilot notes waves appear menacing near H/L≈1/18 and begin
    # breaking near ≈1/10.  Convert that into a continuous screening score.
    return clamp((s - 1.0/30.0) / (1.0/10.0 - 1.0/30.0), 0.0, 1.0)


def shallow_breaking_flag(hs_m: Optional[float], water_depth_m: Optional[float]) -> Optional[float]:
    if hs_m is None or water_depth_m is None or hs_m <= 0 or water_depth_m <= 0:
        return None
    # NOAA Coast Pilot: waves moving into shallow water can break when depth is
    # around 1.3× wave height.  This is only a screening flag; charted depth and
    # local bathymetry are required for navigation.
    ratio = water_depth_m / hs_m
    return clamp((2.2 - ratio) / (2.2 - 1.3), 0.0, 1.0)


def squall_score(a: AtmosHour, pressure_fall_hpa_3h: float = 0.0) -> float:
    code = int(a.weather_code or 0)
    thunder = 1.0 if code in (95, 96, 99) else 0.0
    gust = max(0.0, a.wind_gust_ms or 0.0)
    wind = max(0.1, a.wind_speed_ms or 0.1)
    gust_factor = max(0.0, gust / wind - 1.15)
    cape = max(0.0, a.cape_jkg or 0.0)
    precip = max(0.0, a.precipitation_mm or 0.0)
    x = -3.6 + 4.2*thunder + 0.0020*cape + 1.6*gust_factor + 0.10*min(20.0, precip) + 0.10*max(0.0, pressure_fall_hpa_3h)
    return clamp(sigmoid(x), 0.0, 1.0)


def visibility_risk(visibility_m: Optional[float]) -> float:
    if visibility_m is None:
        return 0.15
    v = max(0.0, visibility_m)
    if v <= 500: return 1.0
    if v <= 1000: return 0.85
    if v <= 2000: return 0.65
    if v <= 5000: return 0.35
    if v <= 10000: return 0.15
    return 0.03


def cold_exposure_score(air_c: Optional[float], sst_c: Optional[float], wind_ms: Optional[float]) -> float:
    # Crew-exposure proxy only; intentionally no survival-time estimate.
    if air_c is None and sst_c is None:
        return 0.0
    a = 15.0 if air_c is None else air_c
    s = 15.0 if sst_c is None else sst_c
    w = 0.0 if wind_ms is None else max(0.0, wind_ms)
    thermal = max(0.0, 12.0 - min(a, s)) / 18.0
    wind_factor = min(0.4, w / 30.0)
    return clamp(thermal + wind_factor, 0.0, 1.0)


def vessel_hour_metrics(a: AtmosHour, m: MarineHour, vessel: Mapping[str, Any], cfg: VesselConfig, previous_pressure: Optional[float] = None) -> Dict[str, Any]:
    hs = max(0.0, m.wave_height_m or 0.0)
    tp = m.wave_peak_period_s or m.wave_period_s
    tm = m.wave_period_s or tp
    wind_ms = max(0.0, a.wind_speed_ms or 0.0)
    gust_ms = max(wind_ms, a.wind_gust_ms or wind_ms)
    length = max(1.0, float(vessel["length_m"]))
    beam = max(0.5, float(vessel["beam_m"]))
    freeboard = max(0.2, float(vessel["freeboard_m"]))
    speed_kn = max(0.0, float(vessel["service_speed_kn"]))
    heading = vessel.get("heading_deg")
    geom = relative_wave_geometry(heading, m.wave_direction_deg)
    encounter_angle = geom["encounter_angle_deg"]
    Te = encounter_period_s(tm, heading, m.wave_direction_deg, speed_kn)
    roll_period = vessel.get("roll_period_s")

    steepness = wave_steepness(hs, tm)
    steep_score = steep_wave_score(hs, tm)
    cross = crossing_sea_index(m)
    opposition = current_opposition_index(m)
    if opposition is not None:
        steep_score = clamp(steep_score * (1.0 + 0.45*opposition), 0.0, 1.0)

    relative_h = hs / length
    freeboard_ratio = hs / freeboard
    screening_hs = max(0.2, float(vessel["screen_hs_m"]))
    wave_exceed = sigmoid(5.0 * (hs / screening_hs - 1.0))

    # Empirical small-/medium-vessel rough-sea checks from NOAA Coast Pilot.
    noaa_short_steep = 0.0
    if length <= 31.0 and hs >= 1.5 and (tm or 99.0) < 6.0:
        noaa_short_steep = 0.9
    elif length <= 61.0 and hs >= 3.0 and 6.0 <= (tm or 0.0) <= 10.0:
        noaa_short_steep = 0.65
    elif length <= 61.0 and hs >= 6.1:
        noaa_short_steep = 1.0

    # Head-sea slamming proxy.
    head_factor = 0.0 if encounter_angle is None else clamp((encounter_angle - 90.0) / 90.0, 0.0, 1.0)
    short_encounter = 0.0 if Te is None else clamp((8.0 - min(8.0, Te)) / 6.0, 0.0, 1.0)
    slamming = clamp(0.45*head_factor + 3.8*relative_h + 0.30*short_encounter + 0.25*steep_score, 0.0, 1.0)

    # Deck-wetness / green-water proxy.  Freeboard alone is insufficient for
    # certification, but Hs/freeboard is a useful conservative screening ratio.
    deck_wet = clamp((freeboard_ratio - 0.35) / 1.25, 0.0, 1.0)

    # Beam-sea synchronous roll screening.
    beam_factor = 0.0 if encounter_angle is None else 1.0 - clamp(abs(encounter_angle - 90.0) / 55.0, 0.0, 1.0)
    sync_roll = None
    param_roll = None
    if roll_period and Te and roll_period > 0 and Te > 0:
        sync_match = math.exp(-((Te/roll_period - 1.0)/0.22)**2)
        sync_roll = clamp(sync_match * beam_factor * (0.35 + 0.65*steep_score), 0.0, 1.0)
        longitudinal = 1.0 if encounter_angle is None else max(
            clamp((30.0 - encounter_angle) / 30.0, 0.0, 1.0),
            clamp((encounter_angle - 150.0) / 30.0, 0.0, 1.0),
        )
        param_match = math.exp(-((Te/(roll_period/2.0) - 1.0)/0.25)**2)
        param_roll = clamp(param_match * longitudinal * (0.25 + 0.75*steep_score), 0.0, 1.0)

    # Following/quartering sea + phase-speed proximity proxy for surf-riding / broaching.
    surf_broach = None
    phase = deepwater_phase_speed_ms(tm)
    if encounter_angle is not None and phase and phase > 0:
        following = clamp((55.0 - encounter_angle) / 55.0, 0.0, 1.0)
        speed_ratio = (speed_kn*KNOT_TO_MS)/phase
        speed_match = math.exp(-((speed_ratio - 0.75)/0.35)**2)
        surf_broach = clamp(following * speed_match * (0.25 + 0.75*steep_score), 0.0, 1.0)

    # Excessive-acceleration proxy from encounter frequency and relative wave height.
    accel = 0.0
    if Te and Te > 0:
        accel = clamp(5.0*relative_h + 0.20*min(3.0, 6.0/Te) + 0.20*(cross or 0.0), 0.0, 1.0)

    wind_kn = wind_ms * MS_TO_KNOT
    gust_kn = gust_ms * MS_TO_KNOT
    wind_score = max(
        sigmoid(5.0*(wind_kn/max(1.0,float(vessel["screen_wind_kn"])) - 1.0)),
        sigmoid(5.0*(gust_kn/max(1.0,float(vessel["screen_gust_kn"])) - 1.0)),
    )
    pressure_fall = 0.0 if previous_pressure is None or a.pressure_hpa is None else max(0.0, previous_pressure - a.pressure_hpa)
    squall = squall_score(a, pressure_fall)
    vis = visibility_risk(a.visibility_m)
    cold = cold_exposure_score(a.temperature_c, m.sea_surface_temperature_c, wind_ms)
    shallow = shallow_breaking_flag(hs, cfg.water_depth_m)

    # Composite operational exposure score.  This is not a seaworthiness verdict.
    components = [wave_exceed, steep_score, noaa_short_steep, wind_score, squall, vis, slamming, deck_wet, accel]
    if cross is not None: components.append(0.8*cross)
    if opposition is not None: components.append(0.7*opposition)
    if sync_roll is not None: components.append(sync_roll)
    if param_roll is not None: components.append(param_roll)
    if surf_broach is not None: components.append(surf_broach)
    if shallow is not None: components.append(shallow)
    # Soft-max: emphasizes the worst hazard without becoming a simple max.
    composite = clamp(math.log(sum(math.exp(3.0*c) for c in components))/3.0 - math.log(len(components))/3.0, 0.0, 1.0)

    return {
        "time": a.time,
        "wave_height_m": hs,
        "wave_period_s": tm,
        "wave_peak_period_s": tp,
        "pressure_hpa": a.pressure_hpa,
        "wind_direction_deg": a.wind_direction_deg,
        "wave_direction_deg": m.wave_direction_deg,
        "current_speed_ms": m.current_speed_ms,
        "current_direction_deg": m.current_direction_deg,
        "sea_level_msl_m": m.sea_level_msl_m,
        "sea_surface_temperature_c": m.sea_surface_temperature_c,
        "wave_steepness": steepness,
        "wave_energy_kj_m2": None if wave_energy_density_j_m2(hs) is None else wave_energy_density_j_m2(hs)/1000.0,
        "wave_power_kw_m": wave_power_kw_m(hs, tm),
        "beaufort": beaufort_from_ms(wind_ms),
        "wind_kn": wind_kn,
        "gust_kn": gust_kn,
        "visibility_m": a.visibility_m,
        "encounter_angle_deg": encounter_angle,
        "encounter_period_s": Te,
        "crossing_sea_index": cross,
        "current_opposition_index": opposition,
        "relative_wave_height_hs_L": relative_h,
        "hs_freeboard_ratio": freeboard_ratio,
        "risk_scores": {
            "wave_limit_exceedance": wave_exceed,
            "steep_wave": max(steep_score, noaa_short_steep),
            "wind_gust": wind_score,
            "squall_thunderstorm": squall,
            "restricted_visibility": vis,
            "cold_exposure": cold,
            "slamming": slamming,
            "deck_wetness_green_water": deck_wet,
            "excessive_motion_acceleration": accel,
            "crossing_seas": cross,
            "wave_current_opposition": opposition,
            "synchronous_roll_proxy": sync_roll,
            "parametric_roll_proxy": param_roll,
            "surf_riding_broaching_proxy": surf_broach,
            "shallow_breaking_proxy": shallow,
            "composite_operational_exposure": composite,
        },
    }

# ---------------------------------------------------------------------------
# Probabilistic scenarios
# ---------------------------------------------------------------------------


def perturb_atmos_member(member: AtmosMember, calibration: CalibrationStore, lat: float, rng: random.Random, horizon: int) -> List[AtmosHour]:
    n = min(len(member.hours), horizon)
    season = season_key(datetime.now(timezone.utc), lat)
    # Time-correlated errors approximate unresolved forecast uncertainty.
    rec_w = calibration.get("atmosphere", member.model, "wind_speed_ms", 24, season)
    rec_g = calibration.get("atmosphere", member.model, "wind_gust_ms", 24, season)
    rec_p = calibration.get("atmosphere", member.model, "pressure_hpa", 24, season)
    rec_v = calibration.get("atmosphere", member.model, "visibility_m", 24, season)
    nw = ar1_noise(n, rec_w.rmse, 0.84, rng)
    ng = ar1_noise(n, rec_g.rmse, 0.80, rng)
    np = ar1_noise(n, rec_p.rmse, 0.92, rng)
    nv = ar1_noise(n, rec_v.rmse, 0.80, rng)
    nd = ar1_noise(n, DEFAULT_RMSE["wind_direction_deg"], 0.80, rng)
    out: List[AtmosHour] = []
    for i, x in enumerate(member.hours[:n]):
        out.append(AtmosHour(
            time=x.time,
            temperature_c=x.temperature_c,
            rh_pct=x.rh_pct,
            pressure_hpa=None if x.pressure_hpa is None else x.pressure_hpa + np[i],
            wind_speed_ms=None if x.wind_speed_ms is None else max(0.0, x.wind_speed_ms + nw[i]),
            wind_gust_ms=None if x.wind_gust_ms is None else max(0.0, x.wind_gust_ms + ng[i]),
            wind_direction_deg=None if x.wind_direction_deg is None else (x.wind_direction_deg + nd[i]) % 360.0,
            precipitation_mm=x.precipitation_mm,
            visibility_m=None if x.visibility_m is None else max(50.0, x.visibility_m + nv[i]),
            weather_code=x.weather_code,
            cape_jkg=x.cape_jkg,
            cloud_cover_pct=x.cloud_cover_pct,
        ))
    return out


def perturb_wave_member(member: WaveMember, calibration: CalibrationStore, lat: float, rng: random.Random, horizon: int) -> List[MarineHour]:
    n = min(len(member.hours), horizon)
    season = season_key(datetime.now(timezone.utc), lat)
    rh = calibration.get("waves", member.model, "wave_height_m", 24, season)
    rt = calibration.get("waves", member.model, "wave_period_s", 24, season)
    rc = calibration.get("waves", member.model, "current_speed_ms", 24, season)
    nh = ar1_noise(n, rh.rmse, 0.92, rng)
    nt = ar1_noise(n, rt.rmse, 0.90, rng)
    nc = ar1_noise(n, rc.rmse, 0.88, rng)
    nd = ar1_noise(n, DEFAULT_RMSE["wave_direction_deg"], 0.88, rng)
    out: List[MarineHour] = []
    for i, x in enumerate(member.hours[:n]):
        hs = None if x.wave_height_m is None else max(0.0, x.wave_height_m + nh[i])
        tp = None if x.wave_period_s is None else max(1.0, x.wave_period_s + nt[i])
        scale = 1.0 if not x.wave_height_m or x.wave_height_m <= 0 else hs/max(0.05, x.wave_height_m)
        out.append(MarineHour(
            time=x.time,
            wave_height_m=hs,
            wave_direction_deg=None if x.wave_direction_deg is None else (x.wave_direction_deg + nd[i]) % 360.0,
            wave_period_s=tp,
            wave_peak_period_s=None if x.wave_peak_period_s is None else max(1.0, x.wave_peak_period_s + nt[i]),
            wind_wave_height_m=None if x.wind_wave_height_m is None else max(0.0, x.wind_wave_height_m*scale),
            wind_wave_direction_deg=x.wind_wave_direction_deg,
            wind_wave_period_s=x.wind_wave_period_s,
            wind_wave_peak_period_s=x.wind_wave_peak_period_s,
            swell_height_m=None if x.swell_height_m is None else max(0.0, x.swell_height_m*scale),
            swell_direction_deg=x.swell_direction_deg,
            swell_period_s=x.swell_period_s,
            swell_peak_period_s=x.swell_peak_period_s,
            secondary_swell_height_m=None if x.secondary_swell_height_m is None else max(0.0, x.secondary_swell_height_m*scale),
            secondary_swell_direction_deg=x.secondary_swell_direction_deg,
            secondary_swell_period_s=x.secondary_swell_period_s,
            tertiary_swell_height_m=None if x.tertiary_swell_height_m is None else max(0.0, x.tertiary_swell_height_m*scale),
            tertiary_swell_direction_deg=x.tertiary_swell_direction_deg,
            tertiary_swell_period_s=x.tertiary_swell_period_s,
            sea_level_msl_m=x.sea_level_msl_m,
            sea_surface_temperature_c=x.sea_surface_temperature_c,
            current_speed_ms=None if x.current_speed_ms is None else max(0.0, x.current_speed_ms + nc[i]),
            current_direction_deg=x.current_direction_deg,
        ))
    return out


def pair_time_series(atmos: Sequence[AtmosHour], waves: Sequence[MarineHour]) -> List[Tuple[AtmosHour, MarineHour]]:
    # Open-Meteo requests use UTC and hourly interpolation.  Pair by index where
    # timestamps match; otherwise map nearest timestamp by string key first.
    amap = {x.time: x for x in atmos}
    out: List[Tuple[AtmosHour, MarineHour]] = []
    for i, m in enumerate(waves):
        a = amap.get(m.time)
        if a is None and i < len(atmos):
            a = atmos[i]
        if a is not None:
            out.append((a, m))
    return out


def simulate_scenario(
    atmos_member: AtmosMember,
    wave_member: WaveMember,
    calibration: CalibrationStore,
    lat: float,
    cfg: VesselConfig,
    vessel: Mapping[str, Any],
    horizon: int,
    rng: random.Random,
) -> Dict[str, Any]:
    atmos = perturb_atmos_member(atmos_member, calibration, lat, rng, horizon)
    waves = perturb_wave_member(wave_member, calibration, lat, rng, horizon)
    pairs = pair_time_series(atmos, waves)
    hourly: List[Dict[str, Any]] = []
    prev_p: Optional[float] = None
    for a, m in pairs:
        hourly.append(vessel_hour_metrics(a, m, vessel, cfg, prev_p))
        prev_p = a.pressure_hpa
    if not hourly:
        return {"hourly": [], "hazard_max": {}, "composite_max": 0.0, "rough_hours": 0.0, "motion_exposure": 0.0}

    hazard_names: set[str] = set()
    for x in hourly:
        hazard_names.update(x["risk_scores"].keys())
    hazard_max: Dict[str, float] = {}
    for name in hazard_names:
        vals = [x["risk_scores"].get(name) for x in hourly]
        vv = [float(v) for v in vals if v is not None]
        if vv:
            hazard_max[name] = max(vv)
    comp = [x["risk_scores"]["composite_operational_exposure"] for x in hourly]
    rough_hours = sum(1.0 for x in hourly if x["risk_scores"]["composite_operational_exposure"] >= 0.65)
    motion_exposure = sum(
        max(x["risk_scores"].get("slamming") or 0.0, x["risk_scores"].get("excessive_motion_acceleration") or 0.0)
        for x in hourly
    )
    peak = max(hourly, key=lambda x: x["risk_scores"]["composite_operational_exposure"])
    return {
        "hourly": hourly,
        "hazard_max": hazard_max,
        "composite_max": max(comp),
        "composite_mean": statistics.mean(comp),
        "rough_hours": rough_hours,
        "motion_exposure": motion_exposure,
        "peak": peak,
    }


def risk_label(score: float) -> str:
    s = clamp(score, 0.0, 1.0)
    if s >= 0.85: return "severe"
    if s >= 0.65: return "high"
    if s >= 0.40: return "moderate"
    if s >= 0.20: return "low"
    return "minimal"


def probability_summary(scenarios: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    if not scenarios:
        return {}
    hazards: set[str] = set()
    for s in scenarios:
        hazards.update((s.get("hazard_max") or {}).keys())
    out: Dict[str, Any] = {}
    for h in sorted(hazards):
        vals = [safe_float((s.get("hazard_max") or {}).get(h)) for s in scenarios]
        vv = [v for v in vals if v is not None]
        if not vv:
            continue
        p_high = sum(v >= 0.65 for v in vv)/len(vv)
        out[h] = {
            "probability_high_or_worse": round(p_high, 4),
            "p50_score": round(percentile(vv, 50) or 0.0, 4),
            "p90_score": round(percentile(vv, 90) or 0.0, 4),
            "label_from_p90": risk_label(percentile(vv, 90) or 0.0),
        }
    comps = [safe_float(s.get("composite_max"), 0.0) or 0.0 for s in scenarios]
    out["overall_operational_exposure"] = {
        "probability_high_or_worse": round(sum(v >= 0.65 for v in comps)/len(comps), 4),
        "probability_severe": round(sum(v >= 0.85 for v in comps)/len(comps), 4),
        "p50_score": round(percentile(comps,50) or 0.0,4),
        "p90_score": round(percentile(comps,90) or 0.0,4),
        "label_from_p90": risk_label(percentile(comps,90) or 0.0),
    }
    return out

# ---------------------------------------------------------------------------
# Deterministic windows / warnings / confidence / abstention
# ---------------------------------------------------------------------------


def best_operating_windows(hourly: Sequence[Mapping[str, Any]], max_windows: int = 5) -> List[Dict[str, Any]]:
    """Find low-exposure windows; this is NOT a departure clearance."""
    candidates: List[Dict[str, Any]] = []
    for x in hourly:
        score = safe_float((x.get("risk_scores") or {}).get("composite_operational_exposure"), 1.0) or 1.0
        if score <= 0.45:
            candidates.append({
                "time": x.get("time"),
                "exposure_score": round(score, 3),
                "wave_height_m": round(safe_float(x.get("wave_height_m"), 0.0) or 0.0, 2),
                "wind_kn": round(safe_float(x.get("wind_kn"), 0.0) or 0.0, 1),
                "visibility_m": None if x.get("visibility_m") is None else round(float(x["visibility_m"])),
                "note": "Lower modeled exposure only; verify official warnings and vessel limits before any operation.",
            })
    return sorted(candidates, key=lambda x: x["exposure_score"])[:max_windows]


def official_warning_override(cfg: VesselConfig, modeled_label: str) -> Dict[str, Any]:
    level = (cfg.official_warning_level or "none").strip().lower()
    rank = OFFICIAL_WARNING_RANK.get(level, 0)
    label_rank = {"minimal":0,"low":1,"moderate":2,"high":3,"severe":4}
    final = modeled_label
    if rank >= 4 and label_rank.get(final,0) < 4:
        final = "severe"
    elif rank >= 3 and label_rank.get(final,0) < 3:
        final = "high"
    elif rank >= 2 and label_rank.get(final,0) < 2:
        final = "moderate"
    return {
        "level": level,
        "text": cfg.official_warning_text,
        "source": cfg.official_warning_source,
        "issued_at": cfg.official_warning_issued_at,
        "override_applied": final != modeled_label,
        "final_risk_floor": final,
        "rule": "Official authoritative warnings are never downgraded by VYOM-MARINE.",
    }


def precision_level(cfg: VesselConfig, vessel: Mapping[str, Any], atmos_n: int, wave_n: int) -> Tuple[str, List[str]]:
    missing: List[str] = []
    if atmos_n == 0 or wave_n == 0:
        return "INSUFFICIENT_DATA", ["usable atmosphere and wave forecast members"]
    user_dims = cfg.length_m is not None and cfg.beam_m is not None
    user_ops = cfg.heading_deg is not None and cfg.service_speed_kn is not None
    motion = cfg.roll_period_s is not None or (cfg.gm_m is not None and user_dims)
    if not user_dims:
        missing.append("actual vessel length and beam")
    if not user_ops:
        missing.append("actual vessel heading and operating speed")
    if not motion:
        missing.append("approved/observed roll period or suitable stability inputs")
    if cfg.local_marine_calibrated and cfg.vessel_profile_calibrated and user_dims and user_ops and motion:
        return "LOCALLY_CALIBRATED_VESSEL_AWARE", missing
    if user_dims and user_ops and motion:
        return "MOTION_AWARE", missing
    if user_dims and user_ops:
        return "VESSEL_ENCOUNTER_AWARE", missing
    if user_dims:
        return "VESSEL_AWARE", missing
    return "ENVIRONMENT_AWARE", missing


def overall_confidence(
    atmos_members: Sequence[AtmosMember], wave_members: Sequence[WaveMember], cfg: VesselConfig,
    vessel: Mapping[str, Any], navigation_zone: str,
) -> Dict[str, float]:
    aq = statistics.mean([m.quality for m in atmos_members]) if atmos_members else 0.0
    wq = statistics.mean([m.quality for m in wave_members]) if wave_members else 0.0
    wind_spread = consensus_spread(atmos_members, "wind_speed_ms", 48)
    wave_spread = consensus_spread(wave_members, "wave_height_m", 48)
    consensus = math.exp(-0.12*wind_spread - 0.55*wave_spread)
    vessel_completeness = 0.35
    vessel_completeness += 0.12 if cfg.length_m else 0.0
    vessel_completeness += 0.10 if cfg.beam_m else 0.0
    vessel_completeness += 0.08 if cfg.freeboard_m else 0.0
    vessel_completeness += 0.12 if cfg.heading_deg is not None else 0.0
    vessel_completeness += 0.08 if cfg.service_speed_kn is not None else 0.0
    vessel_completeness += 0.10 if (cfg.roll_period_s or cfg.gm_m) else 0.0
    vessel_completeness = clamp(vessel_completeness, 0.1, 1.0)
    # Open-Meteo explicitly says tides/currents have limited coastal accuracy and
    # are not suitable for coastal navigation, so confidence is reduced there.
    nav = 0.72 if navigation_zone.strip().lower() in ("harbour","harbor","coastal") else 0.92
    local = 0.95 if cfg.local_marine_calibrated else 0.68
    vals = [max(0.05,aq),max(0.05,wq),max(0.05,consensus),vessel_completeness,nav,local]
    overall = math.exp(sum(math.log(v) for v in vals)/len(vals))
    return {
        "overall": round(clamp(overall,0.0,1.0),4),
        "atmosphere_calibration": round(aq,4),
        "wave_calibration": round(wq,4),
        "multi_model_consensus": round(clamp(consensus,0.0,1.0),4),
        "vessel_input_completeness": round(vessel_completeness,4),
        "navigation_domain_data_quality": round(nav,4),
        "local_validation": round(local,4),
    }


def decision_contract(risk_label_final: str, confidence: float, warning: Mapping[str, Any]) -> Dict[str, Any]:
    # We intentionally never emit "safe" / "go" because model screening is not
    # a legal or professional sailing clearance.
    if risk_label_final == "severe":
        action = "Severe modeled marine hazards are present. Use authoritative maritime warnings and a qualified professional operator for any real-world decision."
    elif risk_label_final == "high":
        action = "High modeled marine hazards are present. Real-world decisions require authoritative forecasts and a qualified professional operator."
    elif risk_label_final == "moderate":
        action = "Moderate modeled marine hazards are present. This output is informational and does not provide operating instructions."
    else:
        action = "No severe modeled signal dominates, but this remains informational only and is not a navigation or sailing clearance."
    if confidence < 0.45:
        action += " Forecast confidence is low."
    return {
        "modeled_risk_label": risk_label_final,
        "screening_action": action,
        "safe_to_sail": None,
        "navigation_clearance": None,
        "stability_certification": None,
        "official_warning_override": bool(warning.get("override_applied")),
    }

# ---------------------------------------------------------------------------
# Core analysis API for live and synthetic data
# ---------------------------------------------------------------------------


def analyze_marine_members(
    lat: float,
    lon: float,
    atmos_members: Sequence[AtmosMember],
    wave_members: Sequence[WaveMember],
    vessel_config: Optional[Mapping[str, Any]] = None,
    horizon_hours: int = 120,
    scenario_count: int = 80,
    calibration: Optional[CalibrationStore] = None,
    random_seed: int = 20260926,
) -> Dict[str, Any]:
    cfg = VesselConfig.from_mapping(vessel_config)
    vessel = cfg.resolved()
    calibration = calibration or CalibrationStore({})
    horizon = max(12, min(192, int(horizon_hours)))
    scenarios_n = max(20, min(1000, int(scenario_count)))
    if not atmos_members or not wave_members:
        return {
            "engine": ENGINE_NAME, "version": ENGINE_VERSION, "status": "unavailable",
            "reason": "Both atmospheric and marine wave forecast members are required.",
            "risk": {}, "confidence": {"overall":0.0},
        }

    acons = _atmos_consensus(atmos_members, horizon)
    wcons = _wave_consensus(wave_members, horizon)
    paired = pair_time_series(acons, wcons)
    deterministic: List[Dict[str, Any]] = []
    prev_p: Optional[float] = None
    for a,m in paired:
        deterministic.append(vessel_hour_metrics(a,m,vessel,cfg,prev_p))
        prev_p = a.pressure_hpa

    rng = random.Random(random_seed)
    aw = member_weights_atmos(atmos_members)
    ww = member_weights_wave(wave_members)
    scenarios: List[Dict[str, Any]] = []
    for _ in range(scenarios_n):
        am = weighted_choice(atmos_members,aw,rng)
        wm = weighted_choice(wave_members,ww,rng)
        s = simulate_scenario(am,wm,calibration,lat,cfg,vessel,horizon,rng)
        if s.get("hourly"):
            scenarios.append(s)
    if not scenarios:
        return {"engine":ENGINE_NAME,"version":ENGINE_VERSION,"status":"unavailable","reason":"Scenario generation failed.","risk":{},"confidence":{"overall":0.0}}

    risks = probability_summary(scenarios)
    overall_p90 = (risks.get("overall_operational_exposure") or {}).get("p90_score",0.0)
    modeled_label = risk_label(float(overall_p90 or 0.0))
    warning = official_warning_override(cfg,modeled_label)
    final_label = warning["final_risk_floor"]
    precision, missing = precision_level(cfg,vessel,len(atmos_members),len(wave_members))
    conf = overall_confidence(atmos_members,wave_members,cfg,vessel,cfg.navigation_zone)

    # Peak deterministic modeled hour for explanation.
    peak = max(deterministic,key=lambda x:(x.get("risk_scores") or {}).get("composite_operational_exposure",0.0)) if deterministic else {}
    rough_vals = [float(s.get("rough_hours",0.0)) for s in scenarios]
    motion_vals = [float(s.get("motion_exposure",0.0)) for s in scenarios]
    prev_rough = max(0.0,float(cfg.prior_cumulative_rough_hours or 0.0))
    prev_motion = max(0.0,float(cfg.prior_motion_exposure_index or 0.0))

    # Determine explicit abstentions.
    abstain: List[str] = []
    if vessel.get("roll_period_source") == "unavailable":
        abstain.append("Parametric/synchronous-roll resonance cannot be vessel-specific without an approved/observed roll period or suitable stability data.")
    if cfg.navigation_zone.strip().lower() in ("harbour","harbor","coastal"):
        abstain.append("Model sea-level/current fields are not navigation-grade coastal tide or under-keel-clearance data.")
    if cfg.water_depth_m is None:
        abstain.append("Shoaling/breaking-wave screening is withheld because trusted local water depth was not supplied.")
    if not cfg.official_warning_source and (cfg.official_warning_level or "none").lower()=="none":
        abstain.append("No authoritative maritime-warning feed was supplied to this engine; the caller must check official warnings separately.")
    abstain.append("VYOM-MARINE never provides a certified 'safe to sail' or stability approval.")

    # Explain model data coverage.
    wave_models = [m.model for m in wave_members]
    atmos_models = [m.model for m in atmos_members]
    # Ocean Guardian-X extended modules. These add capability without replacing
    # any V2 hazard calculation. Missing external feeds produce explicit nulls.
    ext = dict(cfg.external_context or {})
    peak_hs = safe_float(peak.get("wave_height_m"),0.0) or 0.0
    peak_tp = safe_float(peak.get("wave_peak_period_s"), safe_float(peak.get("wave_period_s"),8.0)) or 8.0
    extwave = extreme_wave_screen(peak_hs, peak_tp, ext)
    rogue = rogue_environment_score(peak_hs, peak_tp, safe_float(peak.get("current_opposition_index")), ext, safe_float(extwave.get("hmax_m")))
    rao = rao_motion_response(peak_hs, peak_tp, cfg.rao_data or {}, safe_float(peak.get("encounter_angle_deg")))
    stability = dynamic_stability_screen(vessel,cfg,peak)
    ukc = probabilistic_ukc(cfg,vessel,ext)
    coastal = coastal_water_level_risk(ext,peak_hs)
    traffic = traffic_complexity_index(cfg.traffic_contacts,lat,lon,float(vessel.get("service_speed_kn") or 0.0),float(vessel.get("heading_deg") or 0.0),safe_float(peak.get("visibility_m"))) if cfg.traffic_contacts and vessel.get("heading_deg") is not None else {"tci":0.0,"normalized":0.0,"max_collision_risk":0.0,"contacts":[],"available":False}
    cyclone = cyclone_route_exposure(lat,lon,cfg.cyclone_ensemble,ext.get("route_points"))
    ice = sea_ice_risk(ext,vessel)
    rapid = rapid_development_metrics(deterministic)
    assimil = assimilate_consensus_observations(deterministic,cfg.sensor_observations or {})
    ood_features={"wave_height_m":peak_hs,"wind_kn":safe_float(peak.get("wind_kn"),0.0) or 0.0,"wave_period_s":safe_float(peak.get("wave_period_s"),0.0) or 0.0}
    ood=mahalanobis_ood(ood_features,cfg.validation_domain or {})
    route_opt=route_candidate_optimizer(cfg.route_candidates,weights=ext.get("route_cost_weights"))
    reroute=dynamic_reroute_advisory(route_opt,ext)
    fisheries=fisheries_habitat_probability(cfg.fishing_context or {})
    stock=sustainable_stock_step(cfg.stock_context or {})
    human=human_factor_risk(ext)
    small_vessel=small_vessel_risk_index(vessel,peak)
    large_ship=large_ship_risk_index(vessel,peak)

    # Dependency-aware global fusion over separate hazard families from the PDF.
    peakrs=peak.get("risk_scores") or {}
    wave_h=max(safe_float(peakrs.get("wave_limit_exceedance"),0) or 0,safe_float(peakrs.get("steep_wave"),0) or 0,safe_float(rogue.get("risk_environment"),0) or 0)
    motion_h=max(safe_float(peakrs.get("slamming"),0) or 0,safe_float(peakrs.get("excessive_motion_acceleration"),0) or 0,safe_float(peakrs.get("synchronous_roll_proxy"),0) or 0,safe_float(peakrs.get("parametric_roll_proxy"),0) or 0)
    current_h=safe_float(peakrs.get("wave_current_opposition"),0) or 0
    wind_h=safe_float(peakrs.get("wind_gust"),0) or 0
    traffic_h=safe_float(traffic.get("max_collision_risk"),0) or 0
    vis_base=safe_float(peakrs.get("restricted_visibility"),0) or 0
    vis_interaction=navigation_interaction_risk(vis_base,safe_float(traffic.get("normalized"),0) or 0,float(vessel.get("service_speed_kn") or 0.0),ext)
    storm_h=max(safe_float(peakrs.get("squall_thunderstorm"),0) or 0,safe_float(cyclone.get("risk"),safe_float(cyclone.get("intersection_probability"),0)) or 0,safe_float(rapid.get("peak_risk"),0) or 0)
    grounding=grounding_risk_screen(ukc,ext,current_h,wind_h,safe_float(traffic.get("normalized"),0) or 0,float(vessel.get("service_speed_kn") or 0.0),cfg)
    ground_h=safe_float(grounding.get("risk")) if grounding.get("available") else None
    coastal_h=safe_float(coastal.get("risk")) if coastal.get("available") else None
    hazards={
        "wave":wave_h,"wind":wind_h,"motion":motion_h,"stability":safe_float(stability.get("risk"),0) or 0,
        "traffic":traffic_h,"grounding":ground_h,"storm":storm_h,"ice":safe_float(ice.get("risk")) if ice.get("available") else None,
        "visibility":vis_interaction,"current":current_h,"coastal":coastal_h,"human":safe_float(human.get("risk")) if human.get("available") else None,
    }
    global_fusion=dependent_hazard_fusion(hazards)
    safety_level=safety_level_from_risk(global_fusion["risk"],OFFICIAL_WARNING_RANK.get((cfg.official_warning_level or "none").lower(),0))
    contributor_shares=explainable_contributions(hazards)
    timeline=safety_forecast_timeline(deterministic,OFFICIAL_WARNING_RANK.get((cfg.official_warning_level or "none").lower(),0))
    spatial=spatial_safety_field(lat,lon,peak.get("time"),cfg.vessel_id,global_fusion["risk"],ext)
    if ood.get("out_of_domain"):
        abstain.append("Input state is outside the supplied validation domain (OOD screen); precise operational recommendation is withheld.")

    contract = decision_contract(final_label,conf["overall"],warning)
    return {
        "engine": ENGINE_NAME,
        "version": ENGINE_VERSION,
        "status": "ok",
        "location": {"lat":lat,"lon":lon},
        "precision": {"level":precision,"missing_for_next_level":missing,"abstentions":abstain},
        "vessel": {
            "vessel_id": cfg.vessel_id,
            "vessel_type_requested": cfg.vessel_type,
            "profile_used": vessel["profile_key"],
            "profile_known": vessel["profile_known"],
            "length_m": round(float(vessel["length_m"]),2),
            "beam_m": round(float(vessel["beam_m"]),2),
            "draft_m": vessel["draft_m"],
            "freeboard_m": round(float(vessel["freeboard_m"]),2),
            "service_speed_kn": round(float(vessel["service_speed_kn"]),2),
            "heading_deg": vessel["heading_deg"],
            "roll_period_s": vessel["roll_period_s"],
            "roll_period_source": vessel["roll_period_source"],
            "screening_limits": {"hs_m":vessel["screen_hs_m"],"wind_kn":vessel["screen_wind_kn"],"gust_kn":vessel["screen_gust_kn"],"source":vessel["limits_source"]},
        },
        "forecast_fusion": {
            "atmosphere_models_used": atmos_models,
            "wave_models_used": wave_models,
            "scenario_count": len(scenarios),
            "horizon_hours": min(horizon,len(deterministic)),
            "atmosphere_weights": {m.model:round(w,4) for m,w in zip(atmos_members,aw)},
            "wave_weights": {m.model:round(w,4) for m,w in zip(wave_members,ww)},
            "wind_model_spread_ms_48h": round(consensus_spread(atmos_members,"wind_speed_ms",48),3),
            "wave_height_model_spread_m_48h": round(consensus_spread(wave_members,"wave_height_m",48),3),
        },
        "risk": risks,
        "modeled_peak": peak,
        "official_warning": warning,
        "confidence": conf,
        "decision_contract": contract,
        "voyage_exposure": {
            "prior_rough_sea_exposure_h": round(prev_rough,2),
            "forecast_rough_sea_exposure_h_p50": round(percentile(rough_vals,50) or 0.0,2),
            "forecast_rough_sea_exposure_h_p90": round(percentile(rough_vals,90) or 0.0,2),
            "prior_motion_exposure_index": round(prev_motion,3),
            "forecast_motion_exposure_index_p50": round(percentile(motion_vals,50) or 0.0,3),
        },
        "state_checkpoint": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "last_lat": lat,
            "last_lon": lon,
            "cumulative_rough_hours": round(prev_rough + (percentile(rough_vals,50) or 0.0),3),
            "motion_exposure_index": round(prev_motion + (percentile(motion_vals,50) or 0.0),3),
            "last_risk_label": final_label,
            "last_peak_hs_m": peak.get("wave_height_m") if peak else None,
            "last_peak_wind_kn": peak.get("wind_kn") if peak else None,
        },
        "ocean_guardian_x": {
            "global_ocean_state": {
                "Hs_m": peak_hs, "Hmax_m": extwave.get("hmax_m"), "Tp_s": peak_tp,
                "Tm_s": peak.get("wave_period_s"), "wave_direction_deg": peak.get("wave_direction_deg"),
                "wind_kn": peak.get("wind_kn"), "current_speed_ms": peak.get("current_speed_ms"),
                "pressure_hpa": peak.get("pressure_hpa"), "sst_c": peak.get("sea_surface_temperature_c"),
                "salinity_psu": ext.get("salinity_psu"), "ssh_m": peak.get("sea_level_msl_m"),
                "visibility_m": peak.get("visibility_m"), "ice_concentration_pct": ext.get("ice_concentration_pct"),
                "depth_m": ext.get("charted_depth_m", cfg.water_depth_m), "tide_m": ext.get("trusted_tide_m")
            },
            "rapidly_developing_sea": rapid,
            "extreme_wave_tail": extwave,
            "rogue_wave_environment": rogue,
            "rao_motion_response": rao,
            "dynamic_stability": stability,
            "under_keel_clearance": ukc,
            "grounding_risk": grounding,
            "traffic_collision": traffic,
            "visibility_traffic_interaction_risk": vis_interaction,
            "cyclone_ensemble_exposure": cyclone,
            "coastal_water_level": coastal,
            "sea_ice": ice,
            "small_vessel_risk_index": small_vessel,
            "large_ship_risk_index": large_ship,
            "human_factor_risk": human,
            "bayesian_sensor_fusion": assimil,
            "ood_detection": ood,
            "dependency_aware_global_risk": global_fusion,
            "user_facing_safety_level": safety_level,
            "explainable_contributor_share_pct": contributor_shares,
            "safety_forecast_timeline": timeline,
            "vessel_specific_spatial_safety_field": spatial,
            "route_candidate_optimization": route_opt,
            "continuous_dynamic_rerouting": reroute,
            "optional_fishing_intelligence": fisheries,
            "optional_sustainable_fisheries": stock,
            "audit_record": {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "engine": ENGINE_NAME, "engine_version": ENGINE_VERSION,
                "vessel_id": cfg.vessel_id, "vessel_profile": vessel.get("profile_key"),
                "atmosphere_models": atmos_models, "wave_models": wave_models,
                "global_risk": global_fusion.get("risk"), "safety_level": safety_level,
                "official_warning_level": cfg.official_warning_level,
                "confidence": conf.get("overall"), "ood": ood.get("out_of_domain"),
                "abstention_count": len(abstain),
            },
            "navigation_clearance": None,
        },
        "field_semantics": {
            "risk": "Modeled maritime environmental/vessel-response screening; risk is not confidence.",
            "navigation": "No safe-to-sail, helm, course, speed, or navigation clearance is provided.",
            "ukc": "Under-keel clearance is valid only when the module reports trusted depth/tide/draft inputs available.",
            "traffic": "CPA/TCPA is awareness analytics only and does not replace COLREG-compliant watchkeeping."
        },
        "engine_diagnostics": {
            "engine_executed": True,
            "validation_contract": "risk_confidence_precision_authority_separated",
            "guarded_narration_expected": True
        },
        "data_caveats": [
            "Significant wave height describes the sea state statistically; individual waves can be higher or lower.",
            "Wave/current/tide model accuracy degrades near complex coasts and harbours; nautical publications and official local services supersede this output.",
            "Dynamic stability proxies are screening metrics, not IMO/SOLAS compliance calculations or a replacement for the vessel stability booklet.",
            "Generic vessel-profile thresholds are conservative screening defaults, not class/flag/operator limits.",
            "CPA/TCPA and traffic-complexity outputs are awareness analytics only and do not replace COLREG compliance, AIS/radar watchkeeping, or bridge-team judgement.",
            "UKC/grounding calculations are withheld unless trusted depth, tide, draft, squat, motion allowance and safety margin are supplied.",
            "Cyclone, ice, coastal-surge, route and fisheries extensions operate only on supplied/authoritative context and never fabricate missing external feeds.",
        ],
    }

# ---------------------------------------------------------------------------
# Live public API
# ---------------------------------------------------------------------------


def _merge_realtime_patch(base: Mapping[str, Any], patch: Mapping[str, Any]) -> Dict[str, Any]:
    """Merge connector context conservatively: explicit caller data always wins."""
    out = dict(base or {})
    for key, value in (patch or {}).items():
        if key in ("external_context", "sensor_observations"):
            merged = dict(value or {})
            merged.update(out.get(key) or {})  # explicit caller values win
            out[key] = merged
        elif key in ("traffic_contacts", "cyclone_ensemble"):
            caller = list(out.get(key) or [])
            auto = list(value or [])
            if caller:
                # Keep caller list and add only distinct auto IDs/members.
                seen = {str(x.get("id") or x.get("mmsi") or x.get("member") or x) for x in caller if isinstance(x, Mapping)}
                caller.extend(x for x in auto if not isinstance(x, Mapping) or str(x.get("id") or x.get("mmsi") or x.get("member") or x) not in seen)
                out[key] = caller
            else:
                out[key] = auto
        elif key.startswith("official_warning_"):
            # An explicitly supplied authoritative warning is never replaced by automation.
            if not out.get("official_warning_source"):
                out[key] = value
        elif key == "water_depth_m":
            # Caller/approved depth always wins over non-navigation-grade screening depth.
            if out.get(key) is None:
                out[key] = value
        elif out.get(key) is None:
            out[key] = value
    return out


def build_marine_advisory(
    lat: float,
    lon: float,
    vessel_config: Optional[Mapping[str, Any]] = None,
    horizon_hours: int = 120,
    scenario_count: int = 80,
    atmos_models: Sequence[str] = DEFAULT_ATMOS_MODELS,
    wave_models: Sequence[str] = DEFAULT_WAVE_MODELS,
    calibration_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch live forecasts, authoritative/observational context and build VYOM-MARINE v4.

    Real-feed ingestion is enabled with VYOM_REAL_FEEDS_ENABLED=1. Missing or
    credentialed feeds never cause fabricated values; they are reported as
    unavailable. Explicit caller data supersedes automatic connector data.
    """
    horizon = max(12, min(192, int(horizon_hours)))
    vessel_cfg: Dict[str, Any] = dict(vessel_config or {})
    realtime: Dict[str, Any] = {"enabled": False}
    realtime_service = None

    if os.getenv("VYOM_REAL_FEEDS_ENABLED", "1") == "1":
        try:
            from marine_realtime_service import MarineRealtimeService
            realtime_service = MarineRealtimeService()
            live = realtime_service.collect_live_context(lat, lon, vessel_cfg)
            vessel_cfg = _merge_realtime_patch(vessel_cfg, live.get("patch") or {})
            realtime = {
                "enabled": True,
                "sources": live.get("sources", []),
                "near_observations": live.get("near_observations", []),
                "fabric_stats_before_forecast": live.get("fabric_stats", {}),
                "collection_elapsed_ms": live.get("elapsed_ms"),
            }
            # Use learned calibration only after there are real verified cells.
            if calibration_path is None and (live.get("fabric_stats") or {}).get("calibration", 0) > 0:
                calibration_path = realtime_service.calibration_path_for(
                    lat, lon,
                    list(atmos_models) + list(wave_models),
                    list(DEFAULT_RMSE.keys()),
                )
                realtime["dynamic_calibration_path"] = calibration_path
        except Exception as exc:
            realtime = {"enabled": True, "status": "degraded", "error": str(exc)}

    calibration_path = calibration_path or os.getenv("VYOM_MARINE_CALIBRATION_PATH")
    calibration = CalibrationStore.from_json(calibration_path)

    atmos_members: List[AtmosMember] = []
    for model in atmos_models:
        m = fetch_atmos_member(lat, lon, model, horizon, calibration)
        if m:
            atmos_members.append(m)

    wave_members: List[WaveMember] = []
    for model in wave_models:
        m = fetch_wave_member(lat, lon, model, horizon, calibration)
        if m:
            wave_members.append(m)

    # Add direct external wave-model series (for example a configured INCOIS WW3 NetCDF)
    # without replacing the Open-Meteo gateway members. Missing variables remain None.
    if realtime_service is not None:
        try:
            payload = realtime_service.direct_forecast_payload(lat, lon, horizon)
            added = []
            existing = {m.model for m in wave_members}
            for model_name, rows in (payload or {}).items():
                if model_name in existing:
                    continue
                hours: List[MarineHour] = []
                quals: List[float] = []
                for row in rows[:horizon]:
                    vals = row.get("values") or {}
                    hs = safe_float(vals.get("wave_height_m"))
                    if hs is None:
                        continue
                    lead = int(safe_float(row.get("lead_hours"), len(hours)+1) or len(hours)+1)
                    rec = calibration.get("waves", model_name, "wave_height_m", lead, season_key(datetime.now(timezone.utc), lat))
                    quals.append(rec.quality)
                    hours.append(MarineHour(
                        time=str(row.get("valid_time")),
                        wave_height_m=max(0.0, hs - rec.bias),
                        wave_direction_deg=safe_float(vals.get("wave_direction_deg")),
                        wave_period_s=safe_float(vals.get("wave_period_s")),
                        wave_peak_period_s=safe_float(vals.get("wave_period_s")),
                        sea_surface_temperature_c=safe_float(vals.get("sea_surface_temperature_c")),
                        current_speed_ms=safe_float(vals.get("current_speed_ms")),
                        current_direction_deg=safe_float(vals.get("current_direction_deg")),
                        sea_level_msl_m=safe_float(vals.get("sea_level_msl_m")),
                    ))
                if len(hours) >= min(12, horizon):
                    q = statistics.mean(quals) if quals else 0.45
                    wave_members.append(WaveMember(model=model_name, hours=hours, quality=clamp(q,0.05,1.0)))
                    added.append(model_name)
            if added:
                realtime["direct_wave_models_added"] = added
        except Exception as exc:
            realtime["direct_wave_model_error"] = str(exc)

    if not wave_members:
        m = fetch_wave_member(lat, lon, "best_match", horizon, calibration)
        if m: wave_members.append(m)
    if not atmos_members:
        m = fetch_atmos_member(lat, lon, "best_match", horizon, calibration)
        if m: atmos_members.append(m)

    # Persist forecasts so future observations can verify the exact predictions.
    if realtime_service is not None:
        try:
            realtime["forecasts_persisted"] = realtime_service.persist_engine_forecasts(lat, lon, atmos_members, wave_members)
            if os.getenv("VYOM_CALIBRATE_ON_REQUEST", "0") == "1":
                realtime["verification"] = realtime_service.verify_and_calibrate(
                    lookback_days=int(os.getenv("CAL_MATCH_LOOKBACK_DAYS", "10"))
                )
            realtime["health"] = realtime_service.health()
        except Exception as exc:
            realtime["verification_error"] = str(exc)

    result = analyze_marine_members(
        lat, lon, atmos_members, wave_members, vessel_cfg, horizon, scenario_count, calibration
    )
    result.setdefault("source_attribution", {})
    result["source_attribution"].update({
        "forecast_provider_gateway": "Open-Meteo",
        "marine_models_requested": list(wave_models),
        "atmosphere_models_requested": list(atmos_models),
        "real_feed_layer": "VYOM Marine Data Fabric v4" if realtime.get("enabled") else "disabled",
        "note": "Production use must comply with each provider's licensing/attribution and official warnings supersede model screening.",
    })
    result["real_data_fabric"] = realtime
    if realtime_service is not None:
        try:
            result["real_data_fabric"]["risk_predictions_logged"] = realtime_service.log_engine_result(result, vessel_cfg, lat, lon)
        except Exception as exc:
            result["real_data_fabric"]["risk_prediction_log_error"] = str(exc)
    return result

__all__ = [
    "ENGINE_NAME","ENGINE_VERSION","VesselConfig","AtmosHour","MarineHour",
    "AtmosMember","WaveMember","CalibrationStore","build_marine_advisory",
    "analyze_marine_members","deepwater_wavelength_m",
    "wave_steepness","wave_energy_density_j_m2","wave_power_kw_m",
    "relative_wave_geometry","encounter_period_s","estimated_roll_period_s",
    "tcpa_dcpa","traffic_complexity_index","rapid_development_metrics",
    "gpd_exceedance_probability","extreme_wave_screen","rogue_environment_score",
    "rao_motion_response","probabilistic_ukc","cyclone_route_exposure",
    "sea_ice_risk","dependent_hazard_fusion","mahalanobis_ood",
    "route_candidate_optimizer","fisheries_habitat_probability",
    "sustainable_stock_step","validation_metrics","blocked_spatiotemporal_folds",
    "small_vessel_risk_index","large_ship_risk_index","grounding_risk_screen",
    "dynamic_reroute_advisory","spatial_safety_field","human_factor_risk","_merge_realtime_patch",
]
