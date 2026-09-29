"""
VYOM-AGRO
========= 
Probabilistic crop-soil-weather decision engine for agricultural advisory.

Design goals
------------
1. Keep agronomic calculations deterministic/structured and OUTSIDE the LLM.
2. Treat weather as a distribution, not a single deterministic truth.
3. Maintain a root-zone water balance using FAO-56 concepts.
4. Separate RISK from CONFIDENCE.
5. Abstain from false precision when crop/soil/field-state information is weak.
6. Produce structured advisory telemetry that an NLP layer can translate.

Important scientific limitation
-------------------------------
Built-in crop/soil profiles are starter defaults only. They are deliberately marked as
uncalibrated. Production use requires local agronomic validation, observed soil hydraulic
properties, crop/cultivar parameters, irrigation-system efficiency, and retrospective
forecast verification.

Dependencies: Python 3.10+, requests
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import hashlib
import json
import math
import os
import random
import statistics

import requests


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

MM_TO_L_PER_ACRE = 4046.8564224
EPS = 1e-9


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        y = float(x)
        return y if math.isfinite(y) else default
    except (TypeError, ValueError):
        return default


def weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float:
    if not values:
        return 0.0
    s = sum(weights)
    if s <= EPS:
        return statistics.mean(values)
    return sum(v * w for v, w in zip(values, weights)) / s


def weighted_std(values: Sequence[float], weights: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mu = weighted_mean(values, weights)
    s = sum(weights)
    if s <= EPS:
        return statistics.pstdev(values)
    return math.sqrt(max(0.0, sum(w * (v - mu) ** 2 for v, w in zip(values, weights)) / s))


def percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    q = clamp(q, 0.0, 1.0)
    pos = q * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    return xs[lo] * (hi - pos) + xs[hi] * (pos - lo)


def logistic(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def risk_label(p: float) -> str:
    p = clamp(p, 0.0, 1.0)
    if p < 0.20:
        return "Low"
    if p < 0.40:
        return "Guarded"
    if p < 0.65:
        return "Elevated"
    if p < 0.85:
        return "High"
    return "Very High"


def parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except Exception:
        return None


def saturation_vapour_pressure_kpa(temp_c: float) -> float:
    return 0.6108 * math.exp((17.27 * temp_c) / (temp_c + 237.3))


def vapour_pressure_deficit_kpa(temp_c: float, rh_pct: float) -> float:
    return max(0.0, saturation_vapour_pressure_kpa(temp_c) * (1.0 - clamp(rh_pct, 0.0, 100.0) / 100.0))


def dewpoint_c(temp_c: float, rh_pct: float) -> float:
    rh = clamp(rh_pct, 1.0, 100.0)
    a, b = 17.625, 243.04
    gamma = math.log(rh / 100.0) + (a * temp_c) / (b + temp_c)
    return (b * gamma) / (a - gamma)


def wind_10m_to_2m_kmh(wind_10m_kmh: float) -> float:
    """FAO logarithmic wind-height conversion, z=10 m. Returns km/h."""
    u10_ms = max(0.0, wind_10m_kmh) / 3.6
    u2_ms = u10_ms * 4.87 / math.log(67.8 * 10.0 - 5.42)
    return u2_ms * 3.6


def deterministic_seed(*parts: Any) -> int:
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:16], 16)


# ---------------------------------------------------------------------------
# Agronomic profiles
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SoilProfile:
    name: str
    theta_fc: float              # field capacity, m3/m3
    theta_wp: float              # wilting point, m3/m3
    theta_sat: float             # saturation, m3/m3
    infiltration_mm_h: float
    drainage_mm_day: float
    rew_mm: float                # readily evaporable water
    tew_mm: float                # total evaporable water
    default_cn: float            # optional hydrologic context; not used as hard truth
    calibrated: bool = False


SOIL_PROFILES: Dict[str, SoilProfile] = {
    # Generic texture-class starter values. Replace with measured/local values in production.
    "sand": SoilProfile("sand", 0.12, 0.05, 0.40, 25.0, 35.0, 6.0, 18.0, 60.0),
    "sandy_loam": SoilProfile("sandy_loam", 0.20, 0.09, 0.43, 16.0, 22.0, 7.0, 22.0, 68.0),
    "loam": SoilProfile("loam", 0.27, 0.12, 0.46, 10.0, 14.0, 8.0, 25.0, 75.0),
    "clay_loam": SoilProfile("clay_loam", 0.34, 0.17, 0.50, 6.0, 9.0, 9.0, 28.0, 82.0),
    "clay": SoilProfile("clay", 0.40, 0.22, 0.52, 3.0, 5.0, 10.0, 30.0, 88.0),
}


@dataclass(frozen=True)
class CropProfile:
    name: str
    base_temp_c: float
    upper_temp_c: float
    maturity_gdd: float
    # cumulative fractions of maturity GDD at stage boundaries
    stage_breaks: Tuple[float, float, float]  # end initial, end development, end mid
    kcb_ini: float
    kcb_mid: float
    kcb_end: float
    p_base: float
    root_min_m: float
    root_max_m: float
    crop_height_m: float
    heat_threshold_c: float
    cold_threshold_c: float
    stage_heat_sensitivity: Mapping[str, float]
    stage_water_sensitivity: Mapping[str, float]
    stage_lodging_sensitivity: Mapping[str, float]
    calibrated: bool = False


DEFAULT_STAGE_HEAT = {
    "initial": 0.70,
    "development": 0.90,
    "mid": 1.25,
    "late": 0.90,
}
DEFAULT_STAGE_WATER = {
    "initial": 0.75,
    "development": 1.00,
    "mid": 1.25,
    "late": 0.85,
}
DEFAULT_STAGE_LODGING = {
    "initial": 0.25,
    "development": 0.60,
    "mid": 1.00,
    "late": 1.20,
}


# Starter presets; local/cultivar calibration is required before high-stakes use.
CROP_PROFILES: Dict[str, CropProfile] = {
    "generic": CropProfile(
        "generic", 10.0, 35.0, 1400.0, (0.15, 0.45, 0.75),
        0.20, 1.05, 0.50, 0.50, 0.15, 0.90, 1.0, 35.0, 5.0,
        DEFAULT_STAGE_HEAT, DEFAULT_STAGE_WATER, DEFAULT_STAGE_LODGING,
    ),
    "wheat": CropProfile(
        "wheat", 0.0, 30.0, 1900.0, (0.15, 0.45, 0.78),
        0.15, 1.10, 0.25, 0.55, 0.15, 1.20, 1.0, 32.0, 0.0,
        DEFAULT_STAGE_HEAT, DEFAULT_STAGE_WATER, DEFAULT_STAGE_LODGING,
    ),
    "maize": CropProfile(
        "maize", 8.0, 34.0, 1500.0, (0.14, 0.42, 0.76),
        0.20, 1.15, 0.45, 0.55, 0.15, 1.20, 2.0, 35.0, 8.0,
        DEFAULT_STAGE_HEAT, DEFAULT_STAGE_WATER, DEFAULT_STAGE_LODGING,
    ),
    "rice": CropProfile(
        "rice", 10.0, 35.0, 1600.0, (0.15, 0.45, 0.78),
        0.30, 1.15, 0.70, 0.20, 0.10, 0.60, 1.0, 36.0, 10.0,
        DEFAULT_STAGE_HEAT, DEFAULT_STAGE_WATER, DEFAULT_STAGE_LODGING,
    ),
    "potato": CropProfile(
        "potato", 7.0, 30.0, 1200.0, (0.18, 0.45, 0.78),
        0.20, 1.10, 0.65, 0.35, 0.15, 0.60, 0.6, 30.0, 4.0,
        DEFAULT_STAGE_HEAT, DEFAULT_STAGE_WATER, DEFAULT_STAGE_LODGING,
    ),
    "tomato": CropProfile(
        "tomato", 10.0, 35.0, 1450.0, (0.15, 0.42, 0.78),
        0.25, 1.10, 0.65, 0.40, 0.15, 0.80, 1.0, 34.0, 8.0,
        DEFAULT_STAGE_HEAT, DEFAULT_STAGE_WATER, DEFAULT_STAGE_LODGING,
    ),
}


# ---------------------------------------------------------------------------
# Crop-stage impact, disease and pest screening profiles
# ---------------------------------------------------------------------------

# These profiles are screening defaults, not diagnoses or treatment rules.
# They deliberately stop at "conditions conducive" / "development window".

@dataclass(frozen=True)
class DiseaseProfile:
    key: str
    crop: str
    name: str
    temp_min_c: float
    temp_opt_low_c: float
    temp_opt_high_c: float
    temp_max_c: float
    min_rh_pct: float
    min_leaf_wet_hours: float
    stage_sensitivity: Mapping[str, float]
    calibrated: bool = False


@dataclass(frozen=True)
class PestProfile:
    key: str
    crop: str
    name: str
    base_temp_c: float
    upper_temp_c: float
    degree_days_per_generation: float
    stage_sensitivity: Mapping[str, float]
    calibrated: bool = False


GENERIC_STAGE_SENSITIVITY = {
    "initial": 0.70,
    "development": 1.00,
    "mid": 1.20,
    "late": 0.85,
}

# Literature-informed screening presets. They should be locally validated before
# high-stakes use; the output labels them as screening indices, not infection odds.
DISEASE_PROFILES: Dict[str, DiseaseProfile] = {
    "potato_late_blight": DiseaseProfile(
        "potato_late_blight", "potato", "Potato late blight weather conduciveness",
        7.0, 10.0, 22.0, 27.0, 90.0, 10.0,
        {"initial": 0.6, "development": 1.0, "mid": 1.2, "late": 1.1},
    ),
    "tomato_late_blight": DiseaseProfile(
        "tomato_late_blight", "tomato", "Tomato late blight weather conduciveness",
        7.0, 10.0, 22.0, 27.0, 90.0, 10.0,
        {"initial": 0.7, "development": 1.0, "mid": 1.15, "late": 1.0},
    ),
    "rice_blast": DiseaseProfile(
        "rice_blast", "rice", "Rice blast weather conduciveness",
        18.0, 25.0, 28.0, 32.0, 90.0, 8.0,
        {"initial": 0.8, "development": 1.0, "mid": 1.25, "late": 1.0},
    ),
    "wheat_stripe_rust": DiseaseProfile(
        "wheat_stripe_rust", "wheat", "Wheat stripe rust weather conduciveness",
        3.0, 7.0, 16.0, 23.0, 85.0, 8.0,
        {"initial": 0.5, "development": 1.0, "mid": 1.25, "late": 0.9},
    ),
    "maize_leaf_blight": DiseaseProfile(
        "maize_leaf_blight", "maize", "Maize foliar blight weather conduciveness",
        15.0, 18.0, 27.0, 32.0, 85.0, 6.0,
        {"initial": 0.5, "development": 1.0, "mid": 1.2, "late": 1.0},
    ),
}

PEST_PROFILES: Dict[str, PestProfile] = {
    # Fall armyworm thermal constants vary by study. This preset is intentionally
    # marked uncalibrated and is used only as a phenology/scouting-window index.
    "fall_armyworm": PestProfile(
        "fall_armyworm", "maize", "Fall armyworm thermal development",
        10.9, 39.0, 559.0,
        {"initial": 1.2, "development": 1.2, "mid": 0.8, "late": 0.5},
    ),
}

# Hazard sensitivity multipliers. The matrix makes crop stage explicit and is
# applied to physical hazard scores instead of treating all stages alike.
HAZARD_STAGE_MATRIX: Dict[str, Dict[str, Mapping[str, float]]] = {
    "generic": {
        "heat": DEFAULT_STAGE_HEAT,
        "water_stress": DEFAULT_STAGE_WATER,
        "waterlogging": {"initial": 1.0, "development": 1.0, "mid": 1.1, "late": 0.9},
        "cold": {"initial": 1.2, "development": 1.0, "mid": 1.0, "late": 0.8},
        "lodging": DEFAULT_STAGE_LODGING,
        "disease": GENERIC_STAGE_SENSITIVITY,
    },
    "wheat": {
        "heat": {"initial": 0.6, "development": 0.9, "mid": 1.35, "late": 1.2},
        "water_stress": {"initial": 0.7, "development": 1.0, "mid": 1.35, "late": 1.0},
        "waterlogging": {"initial": 1.1, "development": 1.0, "mid": 1.0, "late": 0.9},
        "cold": {"initial": 0.8, "development": 0.8, "mid": 1.2, "late": 0.8},
        "lodging": {"initial": 0.2, "development": 0.6, "mid": 1.0, "late": 1.3},
        "disease": GENERIC_STAGE_SENSITIVITY,
    },
    "maize": {
        "heat": {"initial": 0.7, "development": 0.9, "mid": 1.45, "late": 1.0},
        "water_stress": {"initial": 0.7, "development": 1.0, "mid": 1.5, "late": 1.0},
        "waterlogging": {"initial": 1.15, "development": 1.0, "mid": 1.1, "late": 0.9},
        "cold": {"initial": 1.3, "development": 1.0, "mid": 0.8, "late": 0.7},
        "lodging": {"initial": 0.2, "development": 0.7, "mid": 1.1, "late": 1.25},
        "disease": GENERIC_STAGE_SENSITIVITY,
    },
    "rice": {
        "heat": {"initial": 0.7, "development": 0.9, "mid": 1.4, "late": 1.1},
        "water_stress": {"initial": 0.9, "development": 1.0, "mid": 1.35, "late": 1.0},
        "waterlogging": {"initial": 0.65, "development": 0.65, "mid": 0.7, "late": 0.75},
        "cold": {"initial": 1.2, "development": 1.0, "mid": 1.2, "late": 0.9},
        "lodging": {"initial": 0.2, "development": 0.6, "mid": 1.0, "late": 1.25},
        "disease": {"initial": 0.8, "development": 1.0, "mid": 1.3, "late": 1.0},
    },
    "potato": {
        "heat": {"initial": 0.8, "development": 1.0, "mid": 1.25, "late": 1.0},
        "water_stress": {"initial": 0.8, "development": 1.1, "mid": 1.3, "late": 1.0},
        "waterlogging": {"initial": 1.0, "development": 1.2, "mid": 1.35, "late": 1.2},
        "cold": {"initial": 1.2, "development": 1.0, "mid": 0.8, "late": 0.7},
        "lodging": {"initial": 0.2, "development": 0.4, "mid": 0.5, "late": 0.5},
        "disease": {"initial": 0.7, "development": 1.1, "mid": 1.3, "late": 1.2},
    },
    "tomato": {
        "heat": {"initial": 0.7, "development": 1.0, "mid": 1.4, "late": 1.0},
        "water_stress": {"initial": 0.8, "development": 1.0, "mid": 1.35, "late": 1.0},
        "waterlogging": {"initial": 1.0, "development": 1.1, "mid": 1.25, "late": 1.1},
        "cold": {"initial": 1.2, "development": 1.0, "mid": 1.0, "late": 0.8},
        "lodging": {"initial": 0.3, "development": 0.6, "mid": 0.8, "late": 0.8},
        "disease": {"initial": 0.7, "development": 1.0, "mid": 1.25, "late": 1.1},
    },
}


def stage_hazard_sensitivity(crop_name: str, stage: str, hazard: str) -> float:
    crop_matrix = HAZARD_STAGE_MATRIX.get(crop_name, HAZARD_STAGE_MATRIX["generic"])
    mapping = crop_matrix.get(hazard) or HAZARD_STAGE_MATRIX["generic"].get(hazard, GENERIC_STAGE_SENSITIVITY)
    return float(mapping.get(stage, 1.0))


def disease_profiles_for_crop(crop_name: str, requested: Optional[str] = None) -> List[DiseaseProfile]:
    if requested:
        key = requested.strip().lower().replace(" ", "_")
        if key in DISEASE_PROFILES:
            return [DISEASE_PROFILES[key]]
    return [p for p in DISEASE_PROFILES.values() if p.crop == crop_name]


def pest_profiles_for_crop(crop_name: str, requested: Optional[str] = None) -> List[PestProfile]:
    if requested:
        key = requested.strip().lower().replace(" ", "_")
        if key in PEST_PROFILES:
            return [PEST_PROFILES[key]]
    return [p for p in PEST_PROFILES.values() if p.crop == crop_name]


# ---------------------------------------------------------------------------
# Inputs / state
# ---------------------------------------------------------------------------

@dataclass
class FieldConfig:
    # Crop context
    crop: str = "generic"
    variety: Optional[str] = None
    planting_date: Optional[str] = None
    crop_stage: Optional[str] = None
    cumulative_gdd: Optional[float] = None

    # Optional biological screening targets. These NEVER imply a diagnosis.
    disease: Optional[str] = None
    pest: Optional[str] = None
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

    # Hydrology overrides. If curve_number is absent a conservative texture-based
    # starter value is used and confidence is reduced.
    curve_number: Optional[float] = None
    drainage_factor: float = 1.0
    canopy_cover_fraction: Optional[float] = None

    # Yield / biomass calibration hooks. Absolute yield is suppressed unless the
    # user explicitly supplies a calibrated potential yield.
    potential_yield_t_ha: Optional[float] = None
    harvest_index: Optional[float] = None
    normalized_water_productivity_g_m2: Optional[float] = None
    yield_parameters_calibrated: bool = False

    # Persistent state checkpoint from the application/database layer.
    field_id: Optional[str] = None
    prior_state_timestamp: Optional[str] = None
    prior_state_depletion_mm: Optional[float] = None
    prior_state_surface_depletion_mm: Optional[float] = None
    prior_state_excess_water_mm: Optional[float] = None
    prior_state_cumulative_gdd: Optional[float] = None
    prior_state_root_depth_m: Optional[float] = None

    # Whether user-supplied crop/soil parameters are locally calibrated.
    crop_profile_calibrated: bool = False
    soil_profile_calibrated: bool = False
    root_zone_sensor: bool = False

    # Optional operational bounds. These are weather-window filters, NOT
    # pesticide-product or chemical-use instructions.
    spray_min_wind_kmh: float = 3.0
    spray_max_wind_kmh: float = 15.0
    spray_max_gust_kmh: float = 25.0
    spray_max_temp_c: float = 30.0
    spray_min_rh_pct: float = 40.0
    spray_max_rh_pct: float = 90.0
    spray_rain_free_hours: int = 6

    @classmethod
    def from_mapping(cls, x: Optional[Mapping[str, Any]]) -> "FieldConfig":
        if not x:
            return cls()
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in dict(x).items() if k in valid})


@dataclass
class HourWeather:
    time: str
    temperature_c: float
    rh_pct: float
    precipitation_mm: float
    wind_kmh: float
    gust_kmh: float
    et0_mm: float
    vpd_kpa: float
    shortwave_wm2: float = 0.0
    precip_probability_pct: Optional[float] = None


@dataclass
class ForecastMember:
    name: str
    hours: List[HourWeather]
    base_weight: float = 1.0
    data_completeness: float = 1.0


@dataclass
class FieldState:
    depletion_mm: float
    surface_depletion_mm: float
    cumulative_gdd: float
    root_depth_m: float
    excess_water_mm: float = 0.0
    timestamp: Optional[str] = None


@dataclass
class SimulationResult:
    loss: float
    stress_mm: float
    runoff_mm: float
    deep_percolation_mm: float
    saturated_hours: float
    max_depletion_mm: float
    final_depletion_mm: float
    heat_dose: float
    cold_dose: float
    disease_conducive_hours: float
    max_gust_kmh: float
    mean_vpd_kpa: float
    irrigation_gross_mm: float
    irrigation_hour: int
    # Advanced diagnostics
    final_excess_water_mm: float = 0.0
    aeration_stress_hours: float = 0.0
    vpd_stress_hours: float = 0.0
    canopy_heat_dose: float = 0.0
    leaf_wet_hours: float = 0.0
    transpiration_mm: float = 0.0
    potential_transpiration_mm: float = 0.0
    soil_evaporation_mm: float = 0.0
    gdd_added: float = 0.0
    stage_stress_integral: float = 0.0
    normalized_transpiration_sum: float = 0.0


# ---------------------------------------------------------------------------
# Forecast calibration / weighting
# ---------------------------------------------------------------------------

class CalibrationStore:
    """
    Forecast verification/calibration store with backward-compatible JSON support.

    Legacy shape:
      {"ecmwf_ifs025": {"temperature_2m": {"bias": 0.2, "rmse": 1.8}}}

    Advanced optional shape inside each model:
      {
        "temperature_2m": {"bias": 0.2, "rmse": 1.8, "crps": 1.1,
                           "reliability": 0.90, "sample_size": 800},
        "lead_time": {
          "0-24": {"temperature_2m": {...}},
          "25-72": {"temperature_2m": {...}},
          "73-168": {"temperature_2m": {...}}
        },
        "season": {
          "monsoon": {"precipitation": {...}},
          "winter": {...}, "summer": {...}, "post_monsoon": {...}
        }
      }

    Corrections and skill weights may therefore vary by variable, forecast lead
    and season while preserving compatibility with the simple v1 file.
    """

    DEFAULT_ERROR = {
        "temperature_2m": 2.2,
        "relative_humidity_2m": 12.0,
        "precipitation": 4.0,
        "wind_speed_10m": 6.0,
        "wind_gusts_10m": 9.0,
        "et0_fao_evapotranspiration": 0.12,
    }

    def __init__(self, data: Optional[Mapping[str, Any]] = None):
        self.data: Dict[str, Any] = dict(data or {})

    @classmethod
    def from_json(cls, path: Optional[str]) -> "CalibrationStore":
        if not path:
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            # Accept {"models": {...}} as well as the v1 direct model mapping.
            if isinstance(raw, Mapping) and isinstance(raw.get("models"), Mapping):
                raw = raw["models"]
            return cls(raw)
        except Exception:
            return cls()

    @staticmethod
    def lead_bucket(lead_hour: int) -> str:
        if lead_hour <= 24:
            return "0-24"
        if lead_hour <= 72:
            return "25-72"
        return "73-168"

    @staticmethod
    def season_name(month: Optional[int]) -> str:
        if month in (6, 7, 8, 9):
            return "monsoon"
        if month in (12, 1, 2):
            return "winter"
        if month in (3, 4, 5):
            return "summer"
        return "post_monsoon"

    def has_model(self, model: str) -> bool:
        return model in self.data and isinstance(self.data[model], Mapping)

    def _nodes(self, model: str, variable: str, lead_hour: int = 0, month: Optional[int] = None) -> List[Mapping[str, Any]]:
        md = self.data.get(model, {})
        if not isinstance(md, Mapping):
            return []
        nodes: List[Mapping[str, Any]] = []
        base = md.get(variable)
        if isinstance(base, Mapping):
            nodes.append(base)
        lead = md.get("lead_time", {})
        if isinstance(lead, Mapping):
            n = lead.get(self.lead_bucket(int(lead_hour)), {})
            if isinstance(n, Mapping) and isinstance(n.get(variable), Mapping):
                nodes.append(n[variable])
        season = md.get("season", {})
        if isinstance(season, Mapping):
            sn = season.get(self.season_name(month), {})
            if isinstance(sn, Mapping) and isinstance(sn.get(variable), Mapping):
                nodes.append(sn[variable])
        return nodes

    def metric(self, model: str, variable: str, key: str, default: float,
               lead_hour: int = 0, month: Optional[int] = None) -> float:
        value = default
        # More-specific nodes override broad nodes.
        for node in self._nodes(model, variable, lead_hour, month):
            if key in node:
                value = safe_float(node.get(key), value)
        return value

    def correction(self, model: str, variable: str, value: float,
                   lead_hour: int = 0, month: Optional[int] = None) -> float:
        multiplier = 1.0
        bias = 0.0
        for node in self._nodes(model, variable, lead_hour, month):
            if "multiplier" in node:
                multiplier = safe_float(node.get("multiplier"), multiplier)
            if "bias" in node:
                bias = safe_float(node.get("bias"), bias)
        value = value * multiplier - bias
        if variable in ("precipitation", "et0_fao_evapotranspiration", "wind_speed_10m", "wind_gusts_10m"):
            value = max(0.0, value)
        if variable == "relative_humidity_2m":
            value = clamp(value, 0.0, 100.0)
        return value

    def rmse(self, model: str, variable: str, lead_hour: int = 0, month: Optional[int] = None) -> float:
        return max(0.001, self.metric(
            model, variable, "rmse", self.DEFAULT_ERROR.get(variable, 1.0), lead_hour, month
        ))

    def crps(self, model: str, variable: str, lead_hour: int = 0, month: Optional[int] = None) -> Optional[float]:
        x = self.metric(model, variable, "crps", -1.0, lead_hour, month)
        return x if x >= 0 else None

    def reliability(self, model: str, variable: str, lead_hour: int = 0, month: Optional[int] = None) -> float:
        return clamp(self.metric(model, variable, "reliability", 0.65, lead_hour, month), 0.05, 1.0)

    def sample_size(self, model: str, variable: str, lead_hour: int = 0, month: Optional[int] = None) -> int:
        return int(max(0.0, self.metric(model, variable, "sample_size", 0.0, lead_hour, month)))

    def variable_weight(self, model: str, variable: str, lead_hour: int = 0, month: Optional[int] = None) -> float:
        default = max(self.DEFAULT_ERROR.get(variable, 1.0), EPS)
        rmse_ratio = self.rmse(model, variable, lead_hour, month) / default
        crps = self.crps(model, variable, lead_hour, month)
        crps_ratio = (crps / default) if crps is not None else rmse_ratio
        rel = self.reliability(model, variable, lead_hour, month)
        n = self.sample_size(model, variable, lead_hour, month)
        evidence = 0.55 + 0.45 * (1.0 - math.exp(-n / 250.0)) if n > 0 else 0.55
        # Exponential skill weighting is smooth, strictly positive and prevents a
        # single weak metric from making a model disappear completely.
        return max(0.02, math.exp(-0.55 * rmse_ratio - 0.25 * crps_ratio) * rel * evidence)

    def composite_model_weight(self, model: str, lead_hour: int = 24, month: Optional[int] = None) -> float:
        vars_and_importance = {
            "temperature_2m": 0.18,
            "relative_humidity_2m": 0.12,
            "precipitation": 0.30,
            "wind_speed_10m": 0.10,
            "et0_fao_evapotranspiration": 0.30,
        }
        logs = []
        weights = []
        for var, imp in vars_and_importance.items():
            w = self.variable_weight(model, var, lead_hour, month)
            logs.append(math.log(max(w, EPS)))
            weights.append(imp)
        return math.exp(sum(a * b for a, b in zip(logs, weights)) / max(sum(weights), EPS))

    def model_weight(self, model: str) -> float:
        # Backward-compatible v1 interface.
        return self.composite_model_weight(model, 24, datetime.utcnow().month)

    def quality(self, models: Sequence[str]) -> float:
        if not models:
            return 0.0
        scores = []
        for m in models:
            if not self.has_model(m):
                scores.append(0.25)
                continue
            ns = []
            rels = []
            for v in self.DEFAULT_ERROR:
                ns.append(self.sample_size(m, v, 24, datetime.utcnow().month))
                rels.append(self.reliability(m, v, 24, datetime.utcnow().month))
            evidence = 1.0 - math.exp(-statistics.mean(ns) / 250.0) if any(ns) else 0.35
            scores.append(clamp(0.45 * evidence + 0.55 * statistics.mean(rels), 0.2, 1.0))
        return clamp(statistics.mean(scores), 0.0, 1.0)


# ---------------------------------------------------------------------------
# Open-Meteo data acquisition
# ---------------------------------------------------------------------------

DEFAULT_MODELS = (
    "ecmwf_ifs025",
    "gfs_seamless",
    "icon_seamless",
    "ukmo_seamless",
)


def _fetch_json(url: str, params: Mapping[str, Any], timeout: float = 8.0) -> Dict[str, Any]:
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise ValueError("Unexpected API response")
    return data


def fetch_forecast_member(
    lat: float,
    lon: float,
    model: str,
    horizon_hours: int,
    calibration: CalibrationStore,
) -> Optional[ForecastMember]:
    url = "https://api.open-meteo.com/v1/forecast"
    hourly_vars = [
        "temperature_2m",
        "relative_humidity_2m",
        "precipitation",
        "wind_speed_10m",
        "wind_gusts_10m",
        "et0_fao_evapotranspiration",
        "vapour_pressure_deficit",
        "shortwave_radiation",
        "precipitation_probability",
    ]
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(hourly_vars),
        "forecast_hours": max(24, min(168, horizon_hours)),
        "timezone": "auto",
        "models": model,
    }
    try:
        d = _fetch_json(url, params)
        h = d.get("hourly", {})
        times = h.get("time") or []
        if not times:
            return None

        def arr(key: str) -> List[Any]:
            return list(h.get(key) or [])

        arrays = {k: arr(k) for k in hourly_vars}
        n = min(horizon_hours, len(times))
        hours: List[HourWeather] = []
        present = expected = 0
        month = datetime.utcnow().month

        for i in range(n):
            temp = arrays["temperature_2m"]
            rh = arrays["relative_humidity_2m"]
            rain = arrays["precipitation"]
            wind = arrays["wind_speed_10m"]
            gust = arrays["wind_gusts_10m"]
            et0 = arrays["et0_fao_evapotranspiration"]
            vpd = arrays["vapour_pressure_deficit"]
            sw = arrays["shortwave_radiation"]
            pp = arrays["precipitation_probability"]

            t = safe_float(temp[i] if i < len(temp) else None, 20.0)
            rhi = safe_float(rh[i] if i < len(rh) else None, 60.0)
            pi = safe_float(rain[i] if i < len(rain) else None, 0.0)
            wi = safe_float(wind[i] if i < len(wind) else None, 0.0)
            gi = safe_float(gust[i] if i < len(gust) else None, wi)
            ei = safe_float(et0[i] if i < len(et0) else None, 0.0)
            si = safe_float(sw[i] if i < len(sw) else None, 0.0)
            ppi = safe_float(pp[i] if i < len(pp) else None, -1.0)

            raw_checks = [
                temp[i] if i < len(temp) else None,
                rh[i] if i < len(rh) else None,
                rain[i] if i < len(rain) else None,
                wind[i] if i < len(wind) else None,
                et0[i] if i < len(et0) else None,
            ]
            expected += len(raw_checks)
            present += sum(x is not None for x in raw_checks)

            # Lead-time/season/variable-specific calibration.
            t = calibration.correction(model, "temperature_2m", t, i, month)
            rhi = calibration.correction(model, "relative_humidity_2m", rhi, i, month)
            pi = calibration.correction(model, "precipitation", pi, i, month)
            wi = calibration.correction(model, "wind_speed_10m", wi, i, month)
            gi = calibration.correction(model, "wind_gusts_10m", gi, i, month)
            ei = calibration.correction(model, "et0_fao_evapotranspiration", ei, i, month)
            vi = vapour_pressure_deficit_kpa(t, rhi)

            hours.append(HourWeather(
                time=str(times[i]),
                temperature_c=t,
                rh_pct=clamp(rhi, 0.0, 100.0),
                precipitation_mm=max(0.0, pi),
                wind_kmh=max(0.0, wi),
                gust_kmh=max(max(0.0, wi), gi),
                et0_mm=max(0.0, ei),
                vpd_kpa=max(0.0, vi),
                shortwave_wm2=max(0.0, si),
                precip_probability_pct=(clamp(ppi, 0.0, 100.0) if ppi >= 0 else None),
            ))

        completeness = present / expected if expected else 0.0
        return ForecastMember(
            name=model,
            hours=hours,
            base_weight=calibration.composite_model_weight(model, 24, month),
            data_completeness=completeness,
        )
    except Exception:
        return None


def fetch_best_match_field_snapshot(lat: float, lon: float) -> Dict[str, Any]:
    """
    Model-derived soil layers are contextual estimates, not substitutes for a field sensor.
    """
    url = "https://api.open-meteo.com/v1/forecast"
    vars_ = [
        "soil_temperature_0cm",
        "soil_temperature_6cm",
        "soil_moisture_0_to_1cm",
        "soil_moisture_1_to_3cm",
        "soil_moisture_3_to_9cm",
        "soil_moisture_9_to_27cm",
        "soil_moisture_27_to_81cm",
        "precipitation_probability",
    ]
    try:
        d = _fetch_json(url, {
            "latitude": lat,
            "longitude": lon,
            "hourly": ",".join(vars_),
            "forecast_hours": 48,
            "timezone": "auto",
        })
        h = d.get("hourly", {})
        out: Dict[str, Any] = {}
        for k in vars_:
            vals = h.get(k) or []
            out[k] = vals[0] if vals else None
        out["precipitation_probability_48h"] = h.get("precipitation_probability") or []
        return out
    except Exception:
        return {}


def fetch_historical_gdd(
    lat: float,
    lon: float,
    planting_date: date,
    crop: CropProfile,
    end_date: Optional[date] = None,
) -> Optional[float]:
    """Compute season-to-date GDD from archived daily Tmin/Tmax when planting date is known."""
    end_date = end_date or (date.today() - timedelta(days=1))
    if planting_date > end_date:
        return 0.0
    # Guard against accidental multi-year requests.
    if (end_date - planting_date).days > 370:
        return None
    url = "https://archive-api.open-meteo.com/v1/archive"
    try:
        d = _fetch_json(url, {
            "latitude": lat,
            "longitude": lon,
            "start_date": planting_date.isoformat(),
            "end_date": end_date.isoformat(),
            "daily": "temperature_2m_min,temperature_2m_max",
            "timezone": "auto",
        }, timeout=12.0)
        daily = d.get("daily", {})
        tmin = daily.get("temperature_2m_min") or []
        tmax = daily.get("temperature_2m_max") or []
        total = 0.0
        for lo, hi in zip(tmin, tmax):
            if lo is None or hi is None:
                continue
            total += growing_degree_day(float(lo), float(hi), crop.base_temp_c, crop.upper_temp_c)
        return total
    except Exception:
        return None


def fetch_day_of_year_climatology(
    lat: float,
    lon: float,
    target: Optional[date] = None,
    years: int = 15,
    window_days: int = 15,
) -> Dict[str, Any]:
    """
    Seasonally normalized climatology around the target day-of-year.

    This fixes the v1 mistake of comparing today's temperature against an
    all-month/all-season multi-year mean. Samples are restricted to +/- window_days
    around the same day-of-year for the previous `years` years.
    """
    target = target or date.today()
    end_year = target.year - 1
    start_year = max(1940, end_year - max(5, years) + 1)
    url = "https://archive-api.open-meteo.com/v1/archive"
    try:
        d = _fetch_json(url, {
            "latitude": lat,
            "longitude": lon,
            "start_date": f"{start_year}-01-01",
            "end_date": f"{end_year}-12-31",
            "daily": "temperature_2m_mean,precipitation_sum,et0_fao_evapotranspiration",
            "timezone": "auto",
        }, timeout=20.0)
        daily = d.get("daily", {})
        dates = daily.get("time") or []
        temps = daily.get("temperature_2m_mean") or []
        rains = daily.get("precipitation_sum") or []
        et0s = daily.get("et0_fao_evapotranspiration") or []

        target_doy = target.timetuple().tm_yday
        tvals: List[float] = []
        rvals: List[float] = []
        evals: List[float] = []
        for i, ds in enumerate(dates):
            try:
                dd = date.fromisoformat(str(ds)[:10])
            except Exception:
                continue
            doy = dd.timetuple().tm_yday
            diff = abs(doy - target_doy)
            diff = min(diff, 365 - diff)
            if diff > window_days:
                continue
            if i < len(temps) and temps[i] is not None:
                tvals.append(float(temps[i]))
            if i < len(rains) and rains[i] is not None:
                rvals.append(float(rains[i]))
            if i < len(et0s) and et0s[i] is not None:
                evals.append(float(et0s[i]))

        def stats(xs: Sequence[float]) -> Dict[str, Optional[float]]:
            if not xs:
                return {"mean": None, "std": None, "p10": None, "p90": None, "n": 0}
            return {
                "mean": round(statistics.mean(xs), 3),
                "std": round(statistics.pstdev(xs), 3) if len(xs) > 1 else 0.0,
                "p10": round(percentile(xs, 0.10), 3),
                "p90": round(percentile(xs, 0.90), 3),
                "n": len(xs),
            }

        return {
            "status": "ok",
            "method": f"day-of-year +/-{window_days} days, {start_year}-{end_year}",
            "temperature_c": stats(tvals),
            "precipitation_mm_day": stats(rvals),
            "et0_mm_day": stats(evals),
        }
    except Exception as e:
        return {"status": "unavailable", "reason": str(e)}


def standardized_anomaly(value: Optional[float], climatology: Mapping[str, Any]) -> Dict[str, Optional[float]]:
    if value is None:
        return {"anomaly": None, "z_score": None}
    mu = climatology.get("mean")
    sd = climatology.get("std")
    if mu is None:
        return {"anomaly": None, "z_score": None}
    anomaly = float(value) - float(mu)
    z = anomaly / float(sd) if sd is not None and float(sd) > 1e-6 else None
    return {
        "anomaly": round(anomaly, 3),
        "z_score": round(z, 3) if z is not None else None,
    }


# ---------------------------------------------------------------------------
# Crop stage / coefficients
# ---------------------------------------------------------------------------


def growing_degree_day(tmin: float, tmax: float, tbase: float, tupper: float) -> float:
    lo = max(tbase, min(tupper, tmin))
    hi = max(tbase, min(tupper, tmax))
    return max(0.0, (lo + hi) / 2.0 - tbase)


def stage_from_fraction(frac: float, crop: CropProfile) -> str:
    a, b, c = crop.stage_breaks
    if frac < a:
        return "initial"
    if frac < b:
        return "development"
    if frac < c:
        return "mid"
    return "late"


def stage_from_gdd(gdd: float, crop: CropProfile) -> Tuple[str, float]:
    frac = clamp(gdd / max(crop.maturity_gdd, 1.0), 0.0, 1.25)
    return stage_from_fraction(frac, crop), frac


def normalized_user_stage(stage: Optional[str]) -> Optional[str]:
    if not stage:
        return None
    s = stage.strip().lower()
    aliases = {
        "emergence": "initial", "seedling": "initial", "initial": "initial",
        "vegetative": "development", "development": "development",
        "flowering": "mid", "reproductive": "mid", "grain filling": "mid", "mid": "mid",
        "ripening": "late", "maturity": "late", "harvest": "late", "late": "late",
    }
    return aliases.get(s)


def stage_fraction_anchor(stage: str, crop: CropProfile) -> float:
    a, b, c = crop.stage_breaks
    return {
        "initial": a * 0.5,
        "development": (a + b) / 2.0,
        "mid": (b + c) / 2.0,
        "late": (c + 1.0) / 2.0,
    }.get(stage, 0.5)


def kcb_for_fraction(frac: float, crop: CropProfile) -> float:
    a, b, c = crop.stage_breaks
    if frac <= a:
        return crop.kcb_ini
    if frac <= b:
        x = (frac - a) / max(EPS, b - a)
        return crop.kcb_ini + x * (crop.kcb_mid - crop.kcb_ini)
    if frac <= c:
        return crop.kcb_mid
    x = clamp((frac - c) / max(EPS, 1.0 - c), 0.0, 1.0)
    return crop.kcb_mid + x * (crop.kcb_end - crop.kcb_mid)


def root_depth_for_fraction(frac: float, crop: CropProfile) -> float:
    _, b, _ = crop.stage_breaks
    x = clamp(frac / max(b, EPS), 0.0, 1.0)
    return crop.root_min_m + x * (crop.root_max_m - crop.root_min_m)


# ---------------------------------------------------------------------------
# Initial state and water balance
# ---------------------------------------------------------------------------


def total_available_water_mm(soil: SoilProfile, root_depth_m: float) -> float:
    return max(1.0, 1000.0 * (soil.theta_fc - soil.theta_wp) * root_depth_m)


def depletion_from_vwc(vwc: float, soil: SoilProfile, root_depth_m: float) -> float:
    # Dr = water missing relative to field capacity within effective root zone.
    return clamp(1000.0 * (soil.theta_fc - vwc) * root_depth_m, 0.0, total_available_water_mm(soil, root_depth_m))


def vwc_from_depletion(depletion_mm: float, soil: SoilProfile, root_depth_m: float) -> float:
    return clamp(
        soil.theta_fc - depletion_mm / max(1000.0 * root_depth_m, EPS),
        soil.theta_wp,
        soil.theta_sat,
    )


def initial_field_state(
    cfg: FieldConfig,
    soil: SoilProfile,
    crop: CropProfile,
    cumulative_gdd: float,
    stage_frac: float,
    model_soil_snapshot: Mapping[str, Any],
) -> Tuple[FieldState, Dict[str, Any]]:
    root_depth = cfg.root_depth_m or cfg.prior_state_root_depth_m or root_depth_for_fraction(stage_frac, crop)
    root_depth = clamp(float(root_depth), 0.08, max(crop.root_max_m, 0.10))
    taw = total_available_water_mm(soil, root_depth)

    source = "assumed_mid_depletion"
    confidence = 0.25
    prior_age_hours: Optional[float] = None
    prior_confidence = 0.0

    # Recent persisted field state is a useful prior, but confidence decays with age.
    prior_dr: Optional[float] = None
    if cfg.prior_state_depletion_mm is not None:
        prior_dr = clamp(float(cfg.prior_state_depletion_mm), 0.0, taw)
        if cfg.prior_state_timestamp:
            try:
                ts = datetime.fromisoformat(cfg.prior_state_timestamp.replace("Z", "+00:00"))
                now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
                prior_age_hours = max(0.0, (now - ts).total_seconds() / 3600.0)
                prior_confidence = 0.80 * math.exp(-prior_age_hours / 72.0)
            except Exception:
                prior_confidence = 0.45
        else:
            prior_confidence = 0.45

    # Strong current observation takes precedence. If both a prior and a current
    # observation exist, use a precision-weighted blend rather than a hard switch.
    if cfg.root_zone_vwc is not None:
        obs_dr = depletion_from_vwc(float(cfg.root_zone_vwc), soil, root_depth)
        obs_conf = 0.97 if cfg.root_zone_sensor else 0.78
        if prior_dr is not None and prior_confidence > 0.10:
            wp = prior_confidence ** 2
            wo = obs_conf ** 2
            dr = (wp * prior_dr + wo * obs_dr) / max(wp + wo, EPS)
            source = "persistent_state_plus_root_zone_observation"
            confidence = clamp(0.5 * prior_confidence + 0.75 * obs_conf, 0.0, 0.99)
        else:
            dr = obs_dr
            source = "measured_or_user_root_zone_vwc"
            confidence = obs_conf
    elif cfg.initial_depletion_fraction is not None:
        obs_dr = clamp(float(cfg.initial_depletion_fraction), 0.0, 1.0) * taw
        if prior_dr is not None and prior_confidence > 0.10:
            dr = 0.40 * prior_dr + 0.60 * obs_dr
            source = "persistent_state_plus_user_depletion_fraction"
            confidence = max(0.62, prior_confidence)
        else:
            dr = obs_dr
            source = "user_depletion_fraction"
            confidence = 0.70
    elif prior_dr is not None and prior_confidence > 0.15:
        dr = prior_dr
        source = "persistent_field_state"
        confidence = prior_confidence
    else:
        candidates = [
            model_soil_snapshot.get("soil_moisture_27_to_81cm"),
            model_soil_snapshot.get("soil_moisture_9_to_27cm"),
            model_soil_snapshot.get("soil_moisture_3_to_9cm"),
        ]
        vals = [float(v) for v in candidates if v is not None and 0.01 <= float(v) <= 0.7]
        if vals:
            estimated_vwc = statistics.mean(vals)
            dr = depletion_from_vwc(estimated_vwc, soil, root_depth)
            source = "nwp_soil_moisture_estimate"
            confidence = 0.45
        else:
            dr = 0.50 * taw

    surface_vwc = cfg.surface_vwc
    if surface_vwc is None:
        surface_vwc = model_soil_snapshot.get("soil_moisture_0_to_1cm")
    if surface_vwc is not None:
        wetfrac = clamp((safe_float(surface_vwc) - soil.theta_wp) / max(soil.theta_fc - soil.theta_wp, EPS), 0.0, 1.0)
        de = soil.tew_mm * (1.0 - wetfrac)
    elif cfg.prior_state_surface_depletion_mm is not None:
        de = clamp(float(cfg.prior_state_surface_depletion_mm), 0.0, soil.tew_mm)
    else:
        de = soil.tew_mm * 0.5

    excess = max(0.0, safe_float(cfg.prior_state_excess_water_mm, 0.0))
    max_excess = max(1.0, 1000.0 * (soil.theta_sat - soil.theta_fc) * root_depth)
    excess = clamp(excess, 0.0, max_excess)

    prior_gdd = cfg.prior_state_cumulative_gdd
    if prior_gdd is not None:
        cumulative_gdd = max(float(cumulative_gdd), float(prior_gdd))

    state = FieldState(
        depletion_mm=clamp(dr, 0.0, taw),
        surface_depletion_mm=clamp(de, 0.0, soil.tew_mm),
        cumulative_gdd=max(0.0, cumulative_gdd),
        root_depth_m=root_depth,
        excess_water_mm=excess,
        timestamp=datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
    )
    return state, {
        "water_state_source": source,
        "water_state_confidence": round(confidence, 3),
        "prior_state_age_hours": round(prior_age_hours, 1) if prior_age_hours is not None else None,
        "initial_depletion_mm": round(state.depletion_mm, 2),
        "initial_excess_water_mm": round(state.excess_water_mm, 2),
        "TAW_mm": round(taw, 2),
        "root_depth_m": round(root_depth, 3),
        "state_timestamp": state.timestamp,
    }


def depletion_fraction_from_state(state: FieldState, soil: SoilProfile) -> float:
    taw = total_available_water_mm(soil, state.root_depth_m)
    return clamp(state.depletion_mm / taw, 0.0, 1.0)


def kcmax_fao56(kcb: float, wind_10m_kmh: float, rh_pct: float, crop_height_m: float) -> float:
    u2_ms = wind_10m_to_2m_kmh(wind_10m_kmh) / 3.6
    h = max(0.1, crop_height_m)
    kcmax = 1.2 + (0.04 * (u2_ms - 2.0) - 0.004 * (rh_pct - 45.0)) * ((h / 3.0) ** 0.3)
    return max(kcb + 0.05, kcmax)


def evaporation_coefficient(
    surface_depletion_mm: float,
    soil: SoilProfile,
    kcb: float,
    kcmax: float,
) -> float:
    if surface_depletion_mm <= soil.rew_mm:
        kr = 1.0
    else:
        kr = clamp(
            (soil.tew_mm - surface_depletion_mm) / max(soil.tew_mm - soil.rew_mm, EPS),
            0.0, 1.0,
        )
    return max(0.0, min(kcmax - kcb, kr * (kcmax - kcb)))


def adjusted_depletion_fraction_p(p_base: float, daily_etc_mm: float) -> float:
    # FAO-56 style adjustment: p = p_table + 0.04(5 - ETc), bounded to agronomic range.
    return clamp(p_base + 0.04 * (5.0 - daily_etc_mm), 0.10, 0.80)


def stress_coefficient_ks(depletion_mm: float, taw_mm: float, raw_mm: float) -> float:
    if depletion_mm <= raw_mm:
        return 1.0
    return clamp((taw_mm - depletion_mm) / max(taw_mm - raw_mm, EPS), 0.0, 1.0)


def effective_curve_number(base_cn: float, depletion_mm: float, taw_mm: float, slope_percent: float) -> float:
    """Moisture/slope-adjusted NRCS-style curve number screening parameter."""
    cn2 = clamp(base_cn, 35.0, 98.0)
    # Standard AMC conversions around the normal-condition CN2.
    cn1 = cn2 / max(2.281 - 0.01281 * cn2, EPS)
    cn3 = cn2 / max(0.427 + 0.00573 * cn2, EPS)
    wetness = 1.0 - clamp(depletion_mm / max(taw_mm, EPS), 0.0, 1.0)
    if wetness < 0.35:
        x = wetness / 0.35
        cn = cn1 + x * (cn2 - cn1)
    else:
        x = clamp((wetness - 0.35) / 0.65, 0.0, 1.0)
        cn = cn2 + x * (cn3 - cn2)
    # Mild slope adjustment only; this is a field-scale screening approximation.
    cn += min(6.0, max(0.0, slope_percent) * 0.22)
    return clamp(cn, 30.0, 98.5)


def scs_curve_number_runoff_mm(storm_precip_mm: float, curve_number: float) -> float:
    """NRCS CN runoff depth for cumulative storm precipitation, in millimetres."""
    p = max(0.0, storm_precip_mm)
    cn = clamp(curve_number, 1.0, 99.9)
    s_mm = 25400.0 / cn - 254.0
    ia = 0.20 * s_mm
    if p <= ia:
        return 0.0
    return max(0.0, ((p - ia) ** 2) / max(p + 0.80 * s_mm, EPS))


def infiltration_and_runoff(
    input_mm: float,
    depletion_mm: float,
    taw_mm: float,
    soil: SoilProfile,
    slope_percent: float,
    curve_number: Optional[float] = None,
    storm_total_before_mm: float = 0.0,
) -> Tuple[float, float]:
    """
    Hybrid field-scale partitioning:
    - event runoff increment from an NRCS-CN curve,
    - hourly infiltration-capacity limit,
    - antecedent root-zone wetness and slope adjustment.

    This is stronger than the v1 fixed hourly-capacity rule, but it remains a
    field-screening hydrology model rather than a watershed routing model.
    """
    if input_mm <= 0:
        return 0.0, 0.0
    base_cn = curve_number if curve_number is not None else soil.default_cn
    cn = effective_curve_number(base_cn, depletion_mm, taw_mm, slope_percent)
    q_before = scs_curve_number_runoff_mm(storm_total_before_mm, cn)
    q_after = scs_curve_number_runoff_mm(storm_total_before_mm + input_mm, cn)
    cn_increment = max(0.0, q_after - q_before)

    wetness = 1.0 - clamp(depletion_mm / max(taw_mm, EPS), 0.0, 1.0)
    saturation_penalty = 1.0 - 0.72 * wetness
    slope_penalty = 1.0 / (1.0 + max(0.0, slope_percent) / 18.0)
    capacity = soil.infiltration_mm_h * clamp(saturation_penalty * slope_penalty, 0.10, 1.0)
    capacity_runoff = max(0.0, input_mm - capacity)

    runoff = clamp(max(cn_increment, capacity_runoff), 0.0, input_mm)
    infiltrated = max(0.0, input_mm - runoff)
    return infiltrated, runoff


# ---------------------------------------------------------------------------
# Scenario construction
# ---------------------------------------------------------------------------


def normalize_member_weights(members: Sequence[ForecastMember]) -> List[float]:
    raw = [max(0.01, m.base_weight * clamp(m.data_completeness, 0.1, 1.0)) for m in members]
    s = sum(raw)
    return [r / s for r in raw] if s > 0 else [1.0 / len(members)] * len(members)


def sample_index(weights: Sequence[float], rng: random.Random) -> int:
    x = rng.random()
    acc = 0.0
    for i, w in enumerate(weights):
        acc += w
        if x <= acc:
            return i
    return len(weights) - 1


def _ar1_noise(rng: random.Random, n: int, sigma: float, rho: float = 0.70) -> List[float]:
    x = 0.0
    out = []
    innovation_sigma = sigma * math.sqrt(max(0.0, 1.0 - rho * rho))
    for _ in range(n):
        x = rho * x + rng.gauss(0.0, innovation_sigma)
        out.append(x)
    return out


def build_probabilistic_scenarios(
    members: Sequence[ForecastMember],
    calibration: CalibrationStore,
    scenario_count: int,
    seed: int,
) -> List[ForecastMember]:
    """
    Generate physically coupled weather scenarios.

    A scenario starts from ONE complete NWP member, preserving covariance among
    temperature/RH/rain/wind/ET0. Residual perturbations are then applied using
    lead-time- and variable-specific verification errors. We intentionally do not
    independently cherry-pick variables from different models.
    """
    if not members:
        return []
    rng = random.Random(seed)
    month = datetime.utcnow().month
    scenarios: List[ForecastMember] = []

    for sidx in range(max(len(members), scenario_count)):
        # Selection weight changes with the central lead time of the scenario.
        lead_ref = min(120, 24 + (sidx % 4) * 24)
        raw = [
            calibration.composite_model_weight(m.name, lead_ref, month) * clamp(m.data_completeness, 0.1, 1.0)
            for m in members
        ]
        total = sum(raw)
        weights = [x / total for x in raw] if total > 0 else [1.0 / len(members)] * len(members)
        idx = sample_index(weights, rng)
        base = members[idx]
        n = len(base.hours)

        # Build lead-varying AR residual streams. The innovations are scaled hour
        # by hour, so uncertainty grows/decreases according to verification data.
        tnoise = [0.0] * n
        rhnoise = [0.0] * n
        wnoise = [0.0] * n
        enoise = [0.0] * n
        state_t = state_rh = state_w = state_e = 0.0
        rho = 0.70
        for i in range(n):
            def step(prev: float, sigma: float) -> float:
                innovation = sigma * math.sqrt(max(0.0, 1.0 - rho * rho))
                return rho * prev + rng.gauss(0.0, innovation)
            state_t = step(state_t, calibration.rmse(base.name, "temperature_2m", i, month) * 0.45)
            state_rh = step(state_rh, calibration.rmse(base.name, "relative_humidity_2m", i, month) * 0.45)
            state_w = step(state_w, calibration.rmse(base.name, "wind_speed_10m", i, month) * 0.40)
            state_e = step(state_e, calibration.rmse(base.name, "et0_fao_evapotranspiration", i, month) * 0.35)
            tnoise[i], rhnoise[i], wnoise[i], enoise[i] = state_t, state_rh, state_w, state_e

        hours: List[HourWeather] = []
        for i, h in enumerate(base.hours):
            t = h.temperature_c + tnoise[i]
            rh = clamp(h.rh_pct + rhnoise[i], 1.0, 100.0)
            wind = max(0.0, h.wind_kmh + wnoise[i])
            gust = max(wind, h.gust_kmh + 1.25 * wnoise[i])
            et0 = max(0.0, h.et0_mm + enoise[i])

            rain_err = calibration.rmse(base.name, "precipitation", i, month)
            rain_sigma = clamp(0.16 + rain_err / 20.0, 0.16, 0.60)
            pop = h.precip_probability_pct
            if h.precipitation_mm > 0.01:
                mult = math.exp(rng.gauss(-0.5 * rain_sigma * rain_sigma, rain_sigma))
                precip = max(0.0, h.precipitation_mm * mult)
                # If POP exists, allow some ensemble members to become dry.
                if pop is not None and rng.random() > clamp(pop / 100.0, 0.05, 0.99):
                    precip *= 0.15
            else:
                wet_prob = 0.03 if pop is None else clamp(pop / 100.0, 0.0, 0.35)
                precip = max(0.0, rng.gammavariate(1.2, 0.18)) if rng.random() < wet_prob else 0.0

            hours.append(HourWeather(
                time=h.time,
                temperature_c=t,
                rh_pct=rh,
                precipitation_mm=precip,
                wind_kmh=wind,
                gust_kmh=gust,
                et0_mm=et0,
                vpd_kpa=vapour_pressure_deficit_kpa(t, rh),
                shortwave_wm2=max(0.0, h.shortwave_wm2),
                precip_probability_pct=h.precip_probability_pct,
            ))
        scenarios.append(ForecastMember(
            name=f"{base.name}:scenario:{sidx}",
            hours=hours,
            base_weight=1.0,
            data_completeness=base.data_completeness,
        ))
    return scenarios


# ---------------------------------------------------------------------------
# Core simulation
# ---------------------------------------------------------------------------


def generic_leaf_wetness_proxy(h: HourWeather) -> float:
    dp = dewpoint_c(h.temperature_c, h.rh_pct)
    dew_margin = h.temperature_c - dp
    rain_term = 1.0 if h.precipitation_mm >= 0.05 else 0.0
    rh_term = logistic((h.rh_pct - 88.0) / 3.0)
    dew_term = logistic((1.8 - dew_margin) / 0.5)
    radiation_dry = logistic((150.0 - h.shortwave_wm2) / 80.0)
    wind_dry = logistic((12.0 - h.wind_kmh) / 4.0)
    return clamp(max(rain_term, 0.45 * rh_term + 0.25 * dew_term + 0.20 * radiation_dry + 0.10 * wind_dry), 0.0, 1.0)


def generic_disease_conduciveness(h: HourWeather, leaf_wetness: float) -> float:
    """Generic weather-conduciveness only; never a disease diagnosis."""
    temp_factor = math.exp(-0.5 * ((h.temperature_c - 22.0) / 7.0) ** 2)
    humidity_factor = logistic((h.rh_pct - 82.0) / 5.0)
    return clamp(leaf_wetness * temp_factor * humidity_factor, 0.0, 1.0)


def trapezoid_temperature_response(temp_c: float, lo: float, opt_lo: float, opt_hi: float, hi: float) -> float:
    if temp_c <= lo or temp_c >= hi:
        return 0.0
    if opt_lo <= temp_c <= opt_hi:
        return 1.0
    if temp_c < opt_lo:
        return clamp((temp_c - lo) / max(opt_lo - lo, EPS), 0.0, 1.0)
    return clamp((hi - temp_c) / max(hi - opt_hi, EPS), 0.0, 1.0)


def canopy_temperature_proxy_c(h: HourWeather, ks: float) -> float:
    """
    Screening proxy for canopy-vs-air temperature. It is intentionally bounded and
    is NOT a replacement for infrared canopy-temperature observations.
    """
    radiation_load = 2.2 * clamp(h.shortwave_wm2 / 850.0, 0.0, 1.0)
    stomatal_penalty = (1.0 - clamp(ks, 0.0, 1.0)) * (1.2 + 1.8 * clamp(h.vpd_kpa, 0.0, 3.5))
    wind_cooling = 0.8 * clamp(h.wind_kmh / 25.0, 0.0, 1.0)
    delta = clamp(radiation_load + stomatal_penalty - wind_cooling, -1.5, 6.0)
    return h.temperature_c + delta


def disease_screening_for_weather(
    weather: Sequence[HourWeather],
    crop: CropProfile,
    cfg: FieldConfig,
    initial_gdd: float,
) -> List[Dict[str, Any]]:
    profiles = disease_profiles_for_crop(crop.name, cfg.disease)
    out: List[Dict[str, Any]] = []
    for profile in profiles:
        gdd = initial_gdd
        run = 0.0
        max_run = 0.0
        weighted_hours = 0.0
        wet_hours = 0.0
        for h in weather:
            temp_eff = clamp(h.temperature_c, crop.base_temp_c, crop.upper_temp_c)
            gdd += max(0.0, temp_eff - crop.base_temp_c) / 24.0
            stage, _ = stage_from_gdd(gdd, crop)
            lw = generic_leaf_wetness_proxy(h)
            tf = trapezoid_temperature_response(
                h.temperature_c,
                profile.temp_min_c,
                profile.temp_opt_low_c,
                profile.temp_opt_high_c,
                profile.temp_max_c,
            )
            humidity_ok = h.rh_pct >= profile.min_rh_pct
            conducive = tf * lw * (1.0 if humidity_ok else logistic((h.rh_pct - profile.min_rh_pct) / 3.0))
            stage_factor = profile.stage_sensitivity.get(stage, 1.0)
            weighted_hours += conducive * stage_factor
            if lw >= 0.60 and tf >= 0.45 and h.rh_pct >= profile.min_rh_pct:
                run += 1.0
                wet_hours += 1.0
                max_run = max(max_run, run)
            else:
                run = 0.0
        duration_factor = clamp(max_run / max(profile.min_leaf_wet_hours, 1.0), 0.0, 1.6)
        cumulative_factor = 1.0 - math.exp(-weighted_hours / max(8.0, profile.min_leaf_wet_hours * 1.8))
        score = clamp(0.58 * clamp(duration_factor, 0.0, 1.0) + 0.42 * cumulative_factor, 0.0, 1.0)
        out.append({
            "key": profile.key,
            "name": profile.name,
            "conduciveness_index": round(score, 3),
            "level": risk_label(score),
            "max_consecutive_conducive_hours": round(max_run, 1),
            "estimated_leaf_wet_hours": round(wet_hours, 1),
            "calibrated": profile.calibrated,
            "scope": "weather-conduciveness screening only; pathogen presence and crop infection are not inferred",
        })
    return out


def pest_thermal_development_for_weather(
    weather: Sequence[HourWeather],
    crop: CropProfile,
    cfg: FieldConfig,
) -> List[Dict[str, Any]]:
    profiles = pest_profiles_for_crop(crop.name, cfg.pest)
    out: List[Dict[str, Any]] = []
    for profile in profiles:
        starting = max(0.0, safe_float(cfg.pest_degree_days_accumulated, 0.0))
        dd = starting
        milestones: Dict[str, Optional[int]] = {"25%": None, "50%": None, "75%": None, "100%": None}
        thresholds = {k: profile.degree_days_per_generation * f for k, f in [("25%", .25), ("50%", .5), ("75%", .75), ("100%", 1.0)]}
        for i, h in enumerate(weather):
            t = clamp(h.temperature_c, profile.base_temp_c, profile.upper_temp_c)
            dd += max(0.0, t - profile.base_temp_c) / 24.0
            for key, threshold in thresholds.items():
                if milestones[key] is None and dd >= threshold:
                    milestones[key] = i
        added = dd - starting
        fraction = dd / max(profile.degree_days_per_generation, EPS)
        stage, _ = stage_from_gdd(
            max(0.0, cfg.cumulative_gdd if cfg.cumulative_gdd is not None else 0.5 * crop.maturity_gdd), crop
        )
        host_sensitivity = profile.stage_sensitivity.get(stage, 1.0)
        activity_index = clamp((added / max(profile.degree_days_per_generation * 0.18, EPS)) * host_sensitivity, 0.0, 1.0)
        out.append({
            "key": profile.key,
            "name": profile.name,
            "degree_days_start": round(starting, 1),
            "degree_days_added_forecast": round(added, 1),
            "degree_days_end": round(dd, 1),
            "generation_fraction_end": round(fraction, 3),
            "thermal_activity_index": round(activity_index, 3),
            "milestone_hours_from_now": milestones,
            "calibrated": profile.calibrated,
            "scope": "thermal-development/scouting-window screening; pest presence and damage are not inferred",
        })
    return out


def simulate_field(
    weather: Sequence[HourWeather],
    initial_state: FieldState,
    crop: CropProfile,
    soil: SoilProfile,
    cfg: FieldConfig,
    irrigation_hour: int = -1,
    irrigation_gross_mm: float = 0.0,
    initial_depletion_perturbation_mm: float = 0.0,
) -> SimulationResult:
    state = FieldState(**asdict(initial_state))
    state.depletion_mm = max(0.0, state.depletion_mm + initial_depletion_perturbation_mm)

    stress_mm = runoff_mm = deep_mm = 0.0
    saturated_hours = heat_dose = cold_dose = 0.0
    disease_hours = aeration_hours = vpd_stress_hours = 0.0
    canopy_heat_dose = leaf_wet_hours = 0.0
    transpiration = potential_transpiration = soil_evaporation = 0.0
    normalized_transpiration_sum = 0.0
    stage_stress_integral = 0.0
    max_dep = state.depletion_mm
    max_gust = 0.0
    vpds: List[float] = []
    initial_gdd = state.cumulative_gdd

    initial_stage, initial_frac = stage_from_gdd(state.cumulative_gdd, crop)
    kcb0 = kcb_for_fraction(initial_frac, crop)
    etc24_est = sum(max(0.0, x.et0_mm) * max(0.15, kcb0) for x in weather[:24])
    p_adj = adjusted_depletion_fraction_p(crop.p_base, etc24_est)

    storm_total = 0.0
    dry_hours = 99
    drainage_factor = clamp(cfg.drainage_factor, 0.1, 3.0)

    for hour_idx, h in enumerate(weather):
        temp_eff = clamp(h.temperature_c, crop.base_temp_c, crop.upper_temp_c)
        state.cumulative_gdd += max(0.0, temp_eff - crop.base_temp_c) / 24.0
        stage, frac = stage_from_gdd(state.cumulative_gdd, crop)

        desired_root = cfg.root_depth_m or root_depth_for_fraction(frac, crop)
        desired_root = clamp(desired_root, 0.08, max(crop.root_max_m, 0.1))
        if desired_root > state.root_depth_m:
            # Preserve volumetric depletion approximately as the root zone expands.
            old_depth = state.root_depth_m
            old_taw = total_available_water_mm(soil, old_depth)
            old_frac_dep = state.depletion_mm / max(old_taw, EPS)
            state.root_depth_m = desired_root
            state.depletion_mm = old_frac_dep * total_available_water_mm(soil, desired_root)

        taw = total_available_water_mm(soil, state.root_depth_m)
        raw = p_adj * taw
        state.depletion_mm = clamp(state.depletion_mm, 0.0, taw)
        ks = stress_coefficient_ks(state.depletion_mm, taw, raw)
        kcb = kcb_for_fraction(frac, crop)
        kcmax = kcmax_fao56(kcb, h.wind_kmh, h.rh_pct, crop.crop_height_m)
        ke = evaporation_coefficient(state.surface_depletion_mm, soil, kcb, kcmax)

        tr_pot = max(0.0, kcb * h.et0_mm)
        tr = max(0.0, ks * kcb * h.et0_mm)
        evap = max(0.0, ke * h.et0_mm)
        etc = tr + evap
        potential_transpiration += tr_pot
        transpiration += tr
        soil_evaporation += evap
        if h.et0_mm > 0.02:
            normalized_transpiration_sum += tr / h.et0_mm

        gross = irrigation_gross_mm if hour_idx == irrigation_hour else 0.0
        effective_irrigation = gross * clamp(cfg.irrigation_efficiency, 0.05, 1.0)
        rain = max(0.0, h.precipitation_mm)

        if rain > 0.05:
            if dry_hours >= 6:
                storm_total = 0.0
            dry_hours = 0
        else:
            dry_hours += 1
            if dry_hours >= 6:
                storm_total = 0.0

        # Rainfall event runoff uses the cumulative storm CN response.
        rain_infil, rain_runoff = infiltration_and_runoff(
            rain, state.depletion_mm, taw, soil, cfg.slope_percent,
            curve_number=cfg.curve_number, storm_total_before_mm=storm_total,
        )
        storm_total += rain

        # Irrigation runoff is treated separately; it should not inflate the rain-event CN history.
        irr_infil, irr_runoff = infiltration_and_runoff(
            effective_irrigation, state.depletion_mm, taw, soil, cfg.slope_percent,
            curve_number=(cfg.curve_number if cfg.curve_number is not None else max(45.0, soil.default_cn - 8.0)),
            storm_total_before_mm=0.0,
        )
        infiltrated = rain_infil + irr_infil
        runoff = rain_runoff + irr_runoff
        runoff_mm += runoff

        # Recharge depletion first. Water above field capacity is explicitly stored
        # in an excess-water/aeration bucket instead of being instantly discarded.
        recharge = min(state.depletion_mm, infiltrated)
        state.depletion_mm -= recharge
        surplus = max(0.0, infiltrated - recharge)
        excess_capacity = max(1.0, 1000.0 * (soil.theta_sat - soil.theta_fc) * state.root_depth_m)
        state.excess_water_mm += surplus
        if state.excess_water_mm > excess_capacity:
            overflow = state.excess_water_mm - excess_capacity
            state.excess_water_mm = excess_capacity
            deep_mm += overflow

        # Gravity drainage from the above-field-capacity bucket.
        drainage = min(
            state.excess_water_mm,
            max(0.0, soil.drainage_mm_day * drainage_factor / 24.0),
        )
        state.excess_water_mm -= drainage
        deep_mm += drainage

        state.surface_depletion_mm = max(0.0, state.surface_depletion_mm - min(infiltrated, soil.tew_mm))

        # ET first consumes above-FC excess, then root-zone available storage.
        etc_remaining = etc
        from_excess = min(state.excess_water_mm, etc_remaining)
        state.excess_water_mm -= from_excess
        etc_remaining -= from_excess
        state.depletion_mm = clamp(state.depletion_mm + etc_remaining, 0.0, taw)
        state.surface_depletion_mm = clamp(state.surface_depletion_mm + evap, 0.0, soil.tew_mm)

        if state.depletion_mm > raw:
            normalized_stress = (state.depletion_mm - raw) / max(taw - raw, EPS)
            sens = stage_hazard_sensitivity(crop.name, stage, "water_stress")
            stress_mm += normalized_stress * max(tr_pot, 0.02) * sens
            stage_stress_integral += normalized_stress * sens

        excess_fraction = state.excess_water_mm / max(excess_capacity, EPS)
        if excess_fraction >= 0.08:
            saturated_hours += 1.0
            aeration_sens = stage_hazard_sensitivity(crop.name, stage, "waterlogging")
            aeration_hours += clamp(excess_fraction, 0.0, 1.0) * aeration_sens
            stage_stress_integral += 0.35 * clamp(excess_fraction, 0.0, 1.0) * aeration_sens

        tcan = canopy_temperature_proxy_c(h, ks)
        heat_sens = stage_hazard_sensitivity(crop.name, stage, "heat")
        cold_sens = stage_hazard_sensitivity(crop.name, stage, "cold")
        heat_excess = max(0.0, h.temperature_c - crop.heat_threshold_c)
        canopy_heat_excess = max(0.0, tcan - crop.heat_threshold_c)
        cold_excess = max(0.0, crop.cold_threshold_c - h.temperature_c)
        heat_dose += (heat_excess ** 1.30) * heat_sens
        canopy_heat_dose += (canopy_heat_excess ** 1.25) * heat_sens
        cold_dose += (cold_excess ** 1.20) * cold_sens

        vpd_threshold = 1.6 if crop.name in ("rice", "potato") else (2.0 if crop.name in ("tomato", "wheat") else 2.3)
        if h.vpd_kpa > vpd_threshold:
            vpd_stress_hours += clamp((h.vpd_kpa - vpd_threshold) / 1.5, 0.0, 1.5) * stage_hazard_sensitivity(crop.name, stage, "water_stress")

        leaf_wet = generic_leaf_wetness_proxy(h)
        if leaf_wet >= 0.60:
            leaf_wet_hours += 1.0
        disease_hours += generic_disease_conduciveness(h, leaf_wet) * stage_hazard_sensitivity(crop.name, stage, "disease")
        max_gust = max(max_gust, h.gust_kmh)
        vpds.append(h.vpd_kpa)
        max_dep = max(max_dep, state.depletion_mm)

    # Irrigation objective: action-sensitive terms receive the highest weight.
    # Heat/cold terms are included only lightly because irrigation can influence
    # canopy temperature but cannot control synoptic thermal hazards.
    loss = (
        4.8 * stress_mm
        + 2.4 * aeration_hours
        + 0.65 * runoff_mm
        + 0.32 * deep_mm
        + 0.055 * irrigation_gross_mm
        + 0.08 * canopy_heat_dose
        + 0.05 * vpd_stress_hours
    )

    return SimulationResult(
        loss=loss,
        stress_mm=stress_mm,
        runoff_mm=runoff_mm,
        deep_percolation_mm=deep_mm,
        saturated_hours=saturated_hours,
        max_depletion_mm=max_dep,
        final_depletion_mm=state.depletion_mm,
        heat_dose=heat_dose,
        cold_dose=cold_dose,
        disease_conducive_hours=disease_hours,
        max_gust_kmh=max_gust,
        mean_vpd_kpa=statistics.mean(vpds) if vpds else 0.0,
        irrigation_gross_mm=irrigation_gross_mm,
        irrigation_hour=irrigation_hour,
        final_excess_water_mm=state.excess_water_mm,
        aeration_stress_hours=aeration_hours,
        vpd_stress_hours=vpd_stress_hours,
        canopy_heat_dose=canopy_heat_dose,
        leaf_wet_hours=leaf_wet_hours,
        transpiration_mm=transpiration,
        potential_transpiration_mm=potential_transpiration,
        soil_evaporation_mm=soil_evaporation,
        gdd_added=max(0.0, state.cumulative_gdd - initial_gdd),
        stage_stress_integral=stage_stress_integral,
        normalized_transpiration_sum=normalized_transpiration_sum,
    )


# ---------------------------------------------------------------------------
# Robust irrigation optimization
# ---------------------------------------------------------------------------


def cvar(losses: Sequence[float], alpha: float = 0.80) -> float:
    if not losses:
        return 0.0
    xs = sorted(losses)
    start = int(math.floor(alpha * len(xs)))
    tail = xs[min(start, len(xs) - 1):]
    return statistics.mean(tail) if tail else xs[-1]


def candidate_irrigation_amounts_mm(
    state: FieldState,
    soil: SoilProfile,
    cfg: FieldConfig,
) -> List[float]:
    taw = total_available_water_mm(soil, state.root_depth_m)
    # Gross amount to approximately refill current depletion, capped by operational max.
    refill_gross = state.depletion_mm / max(clamp(cfg.irrigation_efficiency, 0.05, 1.0), EPS)
    upper = min(cfg.max_single_irrigation_mm, max(10.0, refill_gross * 1.20, 0.45 * taw))
    step = 2.5 if upper <= 20 else 5.0
    vals = [0.0]
    x = step
    while x <= upper + 1e-6:
        vals.append(round(x, 2))
        x += step
    if refill_gross > 0 and refill_gross <= cfg.max_single_irrigation_mm:
        vals.append(round(refill_gross, 2))
    return sorted(set(vals))


def optimize_irrigation(
    scenarios: Sequence[ForecastMember],
    state: FieldState,
    crop: CropProfile,
    soil: SoilProfile,
    cfg: FieldConfig,
    horizon_hours: int,
    water_state_confidence: float,
) -> Dict[str, Any]:
    if not scenarios:
        return {
            "recommended": False,
            "reason": "No forecast scenarios available",
            "depth_mm": None,
            "timing_hour": None,
        }

    amounts = candidate_irrigation_amounts_mm(state, soil, cfg)
    timing_candidates = [-1, 0, 6, 12, 18, 24]
    timing_candidates = [t for t in timing_candidates if t < horizon_hours]

    # Initial water state uncertainty: low-confidence NWP estimates get a much wider range.
    taw = total_available_water_mm(soil, state.root_depth_m)
    state_sigma = (1.0 - water_state_confidence) * 0.22 * taw

    all_candidates: List[Dict[str, Any]] = []
    for amount in amounts:
        candidate_times = [-1] if amount == 0.0 else [t for t in timing_candidates if t >= 0]
        for start_hour in candidate_times:
            losses: List[float] = []
            stress: List[float] = []
            sat: List[float] = []
            runoff: List[float] = []
            deep: List[float] = []
            for i, scenario in enumerate(scenarios):
                # Deterministic perturbation across scenarios; zero-mean bounded spread.
                phase = ((i % 9) - 4) / 4.0
                init_delta = phase * state_sigma
                result = simulate_field(
                    scenario.hours[:horizon_hours], state, crop, soil, cfg,
                    irrigation_hour=start_hour,
                    irrigation_gross_mm=amount,
                    initial_depletion_perturbation_mm=init_delta,
                )
                losses.append(result.loss)
                stress.append(result.stress_mm)
                sat.append(result.saturated_hours)
                runoff.append(result.runoff_mm)
                deep.append(result.deep_percolation_mm)

            expected = statistics.mean(losses)
            tail = cvar(losses, 0.80)
            robust = expected + 0.45 * tail
            all_candidates.append({
                "amount_mm": amount,
                "start_hour": start_hour,
                "expected_loss": expected,
                "cvar80_loss": tail,
                "robust_objective": robust,
                "mean_stress_mm": statistics.mean(stress),
                "p_waterlogging": sum(x >= 3.0 for x in sat) / len(sat),
                "mean_runoff_mm": statistics.mean(runoff),
                "mean_deep_percolation_mm": statistics.mean(deep),
            })

    all_candidates.sort(key=lambda x: x["robust_objective"])
    best = all_candidates[0]
    no_irrig = next((x for x in all_candidates if x["amount_mm"] == 0.0), None)
    benefit = 0.0 if not no_irrig else max(0.0, no_irrig["robust_objective"] - best["robust_objective"])

    # Distribution-aware depth range: alternatives within 8% of best objective are practically equivalent.
    tolerance = max(0.5, abs(best["robust_objective"]) * 0.08)
    near = [x for x in all_candidates if x["robust_objective"] <= best["robust_objective"] + tolerance]
    near_depths = [x["amount_mm"] for x in near]

    recommended = best["amount_mm"] > 0.0 and benefit > 0.35
    depth = best["amount_mm"] if recommended else 0.0
    timing = best["start_hour"] if recommended else None

    return {
        "recommended": recommended,
        "gross_depth_mm": round(depth, 2),
        "gross_depth_range_mm": [round(min(near_depths), 2), round(max(near_depths), 2)] if near_depths else [0.0, 0.0],
        "timing_hour_from_now": timing,
        "expected_robust_benefit_score": round(benefit, 3),
        "p_waterlogging_after_action": round(best["p_waterlogging"], 3),
        "expected_stress_mm_after_action": round(best["mean_stress_mm"], 3),
        "expected_runoff_mm_after_action": round(best["mean_runoff_mm"], 3),
        "expected_deep_percolation_mm_after_action": round(best["mean_deep_percolation_mm"], 3),
        "liters_per_acre": round(depth * MM_TO_L_PER_ACRE) if recommended else 0,
        "liters_for_field": round(depth * MM_TO_L_PER_ACRE * max(0.0, cfg.field_area_acres)) if recommended else 0,
        "decision_rule": "minimize expected loss + 0.45*CVaR80",
    }


# ---------------------------------------------------------------------------
# Impact models and operational windows
# ---------------------------------------------------------------------------


def weighted_consensus_hourly(
    members: Sequence[ForecastMember],
    horizon: int,
    calibration: Optional[CalibrationStore] = None,
) -> List[HourWeather]:
    if not members:
        return []
    calibration = calibration or CalibrationStore()
    n = min(horizon, min(len(m.hours) for m in members))
    month = datetime.utcnow().month
    out: List[HourWeather] = []

    def weights_for(var: str, lead: int) -> List[float]:
        raw = [
            calibration.variable_weight(m.name, var, lead, month) * clamp(m.data_completeness, 0.1, 1.0)
            for m in members
        ]
        total = sum(raw)
        return [x / total for x in raw] if total > 0 else [1.0 / len(members)] * len(members)

    for i in range(n):
        hs = [m.hours[i] for m in members]
        wt = weights_for("temperature_2m", i)
        wrh = weights_for("relative_humidity_2m", i)
        wp = weights_for("precipitation", i)
        ww = weights_for("wind_speed_10m", i)
        we = weights_for("et0_fao_evapotranspiration", i)
        t = weighted_mean([h.temperature_c for h in hs], wt)
        rh = weighted_mean([h.rh_pct for h in hs], wrh)
        out.append(HourWeather(
            time=hs[0].time,
            temperature_c=t,
            rh_pct=rh,
            precipitation_mm=weighted_mean([h.precipitation_mm for h in hs], wp),
            wind_kmh=weighted_mean([h.wind_kmh for h in hs], ww),
            gust_kmh=weighted_mean([h.gust_kmh for h in hs], ww),
            et0_mm=weighted_mean([h.et0_mm for h in hs], we),
            vpd_kpa=vapour_pressure_deficit_kpa(t, rh),
            shortwave_wm2=weighted_mean([h.shortwave_wm2 for h in hs], wt),
            precip_probability_pct=weighted_mean(
                [safe_float(h.precip_probability_pct, 0.0) for h in hs], wp
            ) if any(h.precip_probability_pct is not None for h in hs) else None,
        ))
    return out


def spray_weather_windows(consensus: Sequence[HourWeather], cfg: FieldConfig) -> Dict[str, Any]:
    """
    Chemical-application timing is intentionally not operationalized here.

    VYOM may report weather hazards relevant to field work, but it does not tell
    a user when/how to apply hazardous agricultural chemicals. A qualified adult
    agronomist/operator and the registered product label must control that decision.
    """
    return {
        "weather_only": True,
        "chemical_application_window": None,
        "suitable_windows": [],
        "note": "Chemical-application timing is not provided by this engine. Use a qualified adult/agronomist and the registered product label for any such operation.",
    }


def impact_probabilities(
    scenarios: Sequence[ForecastMember],
    state: FieldState,
    crop: CropProfile,
    soil: SoilProfile,
    cfg: FieldConfig,
    horizon_hours: int,
) -> Dict[str, Any]:
    results = [
        simulate_field(s.hours[:horizon_hours], state, crop, soil, cfg)
        for s in scenarios
    ]
    if not results:
        return {}

    taw = total_available_water_mm(soil, state.root_depth_m)
    stage, _ = stage_from_gdd(state.cumulative_gdd, crop)

    p_water_stress = sum(r.stress_mm > 0.30 for r in results) / len(results)
    p_severe_water_stress = sum(r.max_depletion_mm > 0.90 * taw for r in results) / len(results)
    p_waterlogging = sum((r.aeration_stress_hours >= 2.5 or r.final_excess_water_mm > 2.0) for r in results) / len(results)
    p_severe_waterlogging = sum(r.aeration_stress_hours >= 8.0 for r in results) / len(results)
    p_heat = sum((r.canopy_heat_dose >= 5.0 or r.heat_dose >= 6.0) for r in results) / len(results)
    p_cold = sum(r.cold_dose >= 4.0 for r in results) / len(results)
    p_high_vpd = sum(r.vpd_stress_hours >= 4.0 for r in results) / len(results)
    p_disease_weather = sum(r.disease_conducive_hours >= 8.0 for r in results) / len(results)

    lodging_scores = []
    for r in results:
        gust_factor = logistic((r.max_gust_kmh - 45.0) / 8.0)
        wet_factor = clamp((r.aeration_stress_hours + 0.5 * r.saturated_hours) / 8.0, 0.0, 1.0)
        stage_factor = stage_hazard_sensitivity(crop.name, stage, "lodging")
        lodging_scores.append(clamp(gust_factor * (0.35 + 0.65 * wet_factor) * stage_factor, 0.0, 1.0))
    p_lodging = statistics.mean(lodging_scores)

    def pack(p: float) -> Dict[str, Any]:
        return {"probability": round(clamp(p, 0.0, 1.0), 3), "level": risk_label(p)}

    # Pathogen-specific weather screening over every scenario. This estimates the
    # probability that the WEATHER crosses the profile's conducive threshold; it
    # does not estimate disease infection probability.
    pathogen_out = []
    profiles = disease_profiles_for_crop(crop.name, cfg.disease)
    for profile in profiles:
        scores = []
        max_runs = []
        for scenario in scenarios:
            screening = disease_screening_for_weather(
                scenario.hours[:horizon_hours], crop, cfg, state.cumulative_gdd
            )
            row = next((x for x in screening if x["key"] == profile.key), None)
            if row:
                scores.append(float(row["conduciveness_index"]))
                max_runs.append(float(row["max_consecutive_conducive_hours"]))
        if scores:
            prob_threshold = sum(x >= 0.60 for x in scores) / len(scores)
            pathogen_out.append({
                "key": profile.key,
                "name": profile.name,
                "probability_weather_crosses_conducive_threshold": round(prob_threshold, 3),
                "level": risk_label(prob_threshold),
                "mean_conduciveness_index": round(statistics.mean(scores), 3),
                "p90_conduciveness_index": round(percentile(scores, 0.90), 3),
                "median_max_consecutive_conducive_hours": round(percentile(max_runs, 0.50), 1),
                "calibrated": profile.calibrated,
                "scope": "weather screening only; not a diagnosis or treatment recommendation",
            })

    return {
        "water_stress": pack(p_water_stress),
        "severe_water_stress": pack(p_severe_water_stress),
        "waterlogging": pack(p_waterlogging),
        "severe_waterlogging": pack(p_severe_waterlogging),
        "heat_stress": pack(p_heat),
        "cold_stress": pack(p_cold),
        "high_vpd_stress": pack(p_high_vpd),
        "disease_conducive_weather": {
            **pack(p_disease_weather),
            "scope": "generic weather conduciveness; not pathogen-specific infection probability",
        },
        "pathogen_weather_screening": pathogen_out,
        "lodging_proxy": {
            **pack(p_lodging),
            "scope": "weather/soil lodging proxy; cultivar, stand density and stem strength are not explicitly measured",
        },
        "scenario_statistics": {
            "mean_vpd_kpa": round(statistics.mean(r.mean_vpd_kpa for r in results), 3),
            "mean_runoff_mm": round(statistics.mean(r.runoff_mm for r in results), 2),
            "mean_deep_percolation_mm": round(statistics.mean(r.deep_percolation_mm for r in results), 2),
            "mean_aeration_stress_hours": round(statistics.mean(r.aeration_stress_hours for r in results), 2),
            "mean_leaf_wet_hours": round(statistics.mean(r.leaf_wet_hours for r in results), 2),
            "mean_transpiration_mm": round(statistics.mean(r.transpiration_mm for r in results), 2),
            "mean_potential_transpiration_mm": round(statistics.mean(r.potential_transpiration_mm for r in results), 2),
            "median_final_depletion_mm": round(percentile([r.final_depletion_mm for r in results], 0.50), 2),
            "p10_final_depletion_mm": round(percentile([r.final_depletion_mm for r in results], 0.10), 2),
            "p90_final_depletion_mm": round(percentile([r.final_depletion_mm for r in results], 0.90), 2),
            "median_final_excess_water_mm": round(percentile([r.final_excess_water_mm for r in results], 0.50), 2),
        },
    }


def biomass_yield_projection(
    scenarios: Sequence[ForecastMember],
    state: FieldState,
    crop: CropProfile,
    soil: SoilProfile,
    cfg: FieldConfig,
    horizon_hours: int,
) -> Dict[str, Any]:
    """
    AquaCrop/FAO-inspired forecast-horizon production screening.

    - Biomass increment can be estimated from normalized transpiration if a
      locally calibrated WP* is supplied.
    - Relative yield-water response is reported for the forecast horizon.
    - Absolute t/ha output is suppressed unless the user supplies a calibrated
      potential yield.
    """
    results = [simulate_field(s.hours[:horizon_hours], state, crop, soil, cfg) for s in scenarios]
    if not results:
        return {"status": "unavailable"}

    ky_defaults = {
        "generic": 1.00,
        "wheat": 1.05,
        "maize": 1.20,
        "rice": 1.05,
        "potato": 1.10,
        "tomato": 1.05,
    }
    ky = ky_defaults.get(crop.name, 1.0)
    rel_factors = []
    biomass_indices = []
    biomass_g_m2 = []
    for r in results:
        tr_ratio = clamp(r.transpiration_mm / max(r.potential_transpiration_mm, EPS), 0.0, 1.0) if r.potential_transpiration_mm > 0 else 1.0
        water_factor = clamp(1.0 - ky * (1.0 - tr_ratio), 0.0, 1.0)
        # Aeration and thermal penalties are deliberately mild over a short forecast horizon.
        aeration_factor = math.exp(-0.018 * r.aeration_stress_hours)
        thermal_factor = math.exp(-0.004 * r.canopy_heat_dose - 0.003 * r.cold_dose)
        rel_factors.append(clamp(water_factor * aeration_factor * thermal_factor, 0.0, 1.0))
        biomass_indices.append(r.normalized_transpiration_sum)
        if cfg.normalized_water_productivity_g_m2 is not None:
            biomass_g_m2.append(max(0.0, float(cfg.normalized_water_productivity_g_m2)) * r.normalized_transpiration_sum)

    rel_median = percentile(rel_factors, 0.50)
    rel_p10 = percentile(rel_factors, 0.10)
    rel_p90 = percentile(rel_factors, 0.90)
    out: Dict[str, Any] = {
        "status": "ok",
        "forecast_horizon_relative_yield_factor_median": round(rel_median, 3),
        "forecast_horizon_relative_yield_factor_p10_p90": [round(rel_p10, 3), round(rel_p90, 3)],
        "normalized_biomass_increment_index_median": round(percentile(biomass_indices, 0.50), 3),
        "method": "FAO yield-response-to-water + AquaCrop-inspired normalized-transpiration biomass increment",
        "absolute_yield_authoritative": None,
        "scope": "forecast-horizon stress/yield-retention screening, not a full-season crop simulator",
    }
    if biomass_g_m2:
        out["biomass_increment_g_m2_median"] = round(percentile(biomass_g_m2, 0.50), 2)
        out["biomass_increment_g_m2_p10_p90"] = [
            round(percentile(biomass_g_m2, 0.10), 2), round(percentile(biomass_g_m2, 0.90), 2)
        ]
        out["water_productivity_input_calibrated"] = bool(cfg.yield_parameters_calibrated)

    if cfg.potential_yield_t_ha is not None and cfg.yield_parameters_calibrated:
        potential = max(0.0, float(cfg.potential_yield_t_ha))
        out["absolute_yield_authoritative"] = {
            "median_t_ha": round(potential * rel_median, 3),
            "p10_p90_t_ha": [round(potential * rel_p10, 3), round(potential * rel_p90, 3)],
            "potential_yield_input_t_ha": potential,
        }
    elif cfg.potential_yield_t_ha is not None:
        out["absolute_yield_planning_only_t_ha"] = round(max(0.0, float(cfg.potential_yield_t_ha)) * rel_median, 3)
        out["absolute_yield_note"] = "Suppressed as authoritative because yield_parameters_calibrated=false."
    return out


def field_operation_windows(
    consensus: Sequence[HourWeather],
    state: FieldState,
    crop: CropProfile,
    soil: SoilProfile,
    cfg: FieldConfig,
) -> Dict[str, Any]:
    stage, _ = stage_from_gdd(state.cumulative_gdd, crop)
    taw = total_available_water_mm(soil, state.root_depth_m)
    initial_wetness = 1.0 - state.depletion_mm / max(taw, EPS)

    traffic_flags: List[bool] = []
    harvest_flags: List[bool] = []
    sow_flags: List[bool] = []
    for i, h in enumerate(consensus):
        prior_rain = sum(x.precipitation_mm for x in consensus[max(0, i-12):i+1])
        next_rain = sum(x.precipitation_mm for x in consensus[i:i+12])
        traffic_ok = prior_rain < 3.0 and h.precipitation_mm < 0.3 and initial_wetness < 0.88 and h.gust_kmh < 35.0
        harvest_ok = stage == "late" and next_rain < 1.0 and h.rh_pct < 82.0 and h.gust_kmh < 35.0
        sow_ok = stage in ("initial", "development") and h.temperature_c > crop.base_temp_c + 2.0 and next_rain < 20.0 and h.precipitation_mm < 5.0
        traffic_flags.append(traffic_ok)
        harvest_flags.append(harvest_ok)
        sow_flags.append(sow_ok)

    def windows(flags: Sequence[bool], minimum: int = 3) -> List[Dict[str, Any]]:
        out = []
        start = None
        ext = list(flags) + [False]
        for i, ok in enumerate(ext):
            if ok and start is None:
                start = i
            elif not ok and start is not None:
                if i - start >= minimum:
                    out.append({
                        "start_hour": start,
                        "end_hour": i - 1,
                        "start_time": consensus[start].time,
                        "end_time": consensus[i-1].time,
                    })
                start = None
        return out[:5]

    return {
        "trafficability": {
            "suitable_windows": windows(traffic_flags, 3),
            "scope": "weather/soil-wetness proxy; machinery, compaction tolerance and field access are not directly measured",
        },
        "harvest_weather": {
            "applicable": stage == "late",
            "suitable_windows": windows(harvest_flags, 4),
            "scope": "weather window only; crop moisture and harvest maturity must be measured separately",
        },
        "sowing_weather": {
            "suitable_windows": windows(sow_flags, 4),
            "scope": "weather/temperature screening only; seedbed condition and agronomic calendar remain controlling",
        },
    }


def optimize_farm_actions(
    risks: Mapping[str, Any],
    irrigation: Mapping[str, Any],
    spray: Mapping[str, Any],
    operations: Mapping[str, Any],
    pest: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Build a robust decision portfolio without pretending unprovided costs are known."""
    actions: List[Dict[str, Any]] = []

    def prob(key: str) -> float:
        try:
            return float(risks.get(key, {}).get("probability", 0.0))
        except Exception:
            return 0.0

    if irrigation.get("recommended"):
        benefit = max(0.0, safe_float(irrigation.get("expected_robust_benefit_score"), 0.0))
        actions.append({
            "action": "irrigation",
            "priority_score": round(clamp(0.55 + 0.08 * benefit + 0.30 * prob("water_stress"), 0.0, 1.0), 3),
            "reason": "risk-sensitive irrigation optimizer found a lower expected+tail-loss action than no irrigation",
            "details": {
                "gross_depth_mm": irrigation.get("gross_depth_mm"),
                "timing_hour_from_now": irrigation.get("timing_hour_from_now"),
            },
        })

    wl = prob("waterlogging")
    if wl >= 0.35:
        actions.append({
            "action": "drainage_and_waterlogging_inspection",
            "priority_score": round(wl, 3),
            "reason": "root-zone excess-water scenarios indicate elevated aeration stress",
            "details": {"probability": wl},
        })

    heat = prob("heat_stress")
    if heat >= 0.40:
        actions.append({
            "action": "protect_heat_sensitive_operations",
            "priority_score": round(heat, 3),
            "reason": "crop-stage-weighted heat/canopy-temperature stress is elevated",
            "details": {"probability": heat},
        })

    disease_rows = risks.get("pathogen_weather_screening", []) or []
    if disease_rows:
        d = max(disease_rows, key=lambda x: safe_float(x.get("probability_weather_crosses_conducive_threshold"), 0.0))
        dp = safe_float(d.get("probability_weather_crosses_conducive_threshold"), 0.0)
        if dp >= 0.35:
            actions.append({
                "action": "crop_disease_scouting",
                "priority_score": round(dp, 3),
                "reason": f"weather is conducive for {d.get('name', d.get('key', 'a crop disease'))}",
                "details": {"weather_conduciveness_only": True},
            })

    for row in pest:
        idx = safe_float(row.get("thermal_activity_index"), 0.0)
        if idx >= 0.45:
            actions.append({
                "action": "pest_scouting",
                "priority_score": round(idx, 3),
                "reason": f"thermal development window is active for {row.get('name', row.get('key', 'selected pest'))}",
                "details": {"presence_not_inferred": True},
            })

    traffic = operations.get("trafficability", {}).get("suitable_windows", [])
    if not traffic:
        actions.append({
            "action": "avoid_unnecessary_field_traffic",
            "priority_score": round(max(0.35, wl), 3),
            "reason": "no robust trafficability window was found in the screened horizon",
            "details": {},
        })

    actions.sort(key=lambda x: x["priority_score"], reverse=True)
    return {
        "method": "multi-action robust priority portfolio; economic optimum requires user-specific costs/constraints",
        "ranked_actions": actions,
        "automatic_execution": False,
    }


# ---------------------------------------------------------------------------
# Confidence / precision governance
# ---------------------------------------------------------------------------


def forecast_confidence(members: Sequence[ForecastMember], horizon: int = 48) -> Dict[str, Any]:
    if not members:
        return {"score": 0.0, "temperature_spread_c": None, "rain_spread_mm": None}
    weights = normalize_member_weights(members)
    n = min(horizon, min(len(m.hours) for m in members))
    if n <= 0:
        return {"score": 0.0, "temperature_spread_c": None, "rain_spread_mm": None}

    temp_spreads = []
    rain_totals = []
    for i in range(n):
        temp_spreads.append(weighted_std([m.hours[i].temperature_c for m in members], weights))
    for m in members:
        rain_totals.append(sum(h.precipitation_mm for h in m.hours[:n]))
    rain_spread = weighted_std(rain_totals, weights)
    temp_spread = statistics.mean(temp_spreads) if temp_spreads else 0.0
    completeness = statistics.mean(m.data_completeness for m in members)

    # Smooth confidence penalties; not a risk probability.
    disagreement_penalty = math.exp(-0.28 * temp_spread - 0.08 * rain_spread)
    member_factor = clamp(len(members) / 4.0, 0.25, 1.0)
    score = clamp(disagreement_penalty * completeness * member_factor, 0.05, 1.0)
    return {
        "score": round(score, 3),
        "temperature_spread_c": round(temp_spread, 2),
        "rain_total_spread_mm": round(rain_spread, 2),
        "member_count": len(members),
        "data_completeness": round(completeness, 3),
    }


def precision_mode(
    cfg: FieldConfig,
    crop_known: bool,
    stage_known: bool,
    soil_known: bool,
    water_state_source: str,
) -> str:
    if cfg.root_zone_sensor and crop_known and stage_known and soil_known:
        return "sensor_corrected_field_balance"
    if water_state_source in (
        "measured_or_user_root_zone_vwc",
        "user_depletion_fraction",
        "persistent_state_plus_root_zone_observation",
        "persistent_state_plus_user_depletion_fraction",
    ) and crop_known and stage_known and soil_known:
        return "field_balance"
    if water_state_source == "persistent_field_state" and crop_known and stage_known and soil_known:
        return "stateful_field_balance"
    if crop_known and stage_known:
        return "crop_stage_aware"
    if crop_known:
        return "crop_aware"
    return "weather_only"


def overall_confidence(
    fconf: float,
    calibration_quality: float,
    field_conf: float,
    crop_conf: float,
    soil_conf: float,
) -> float:
    vals = [
        clamp(fconf, 0.01, 1.0),
        clamp(calibration_quality, 0.01, 1.0),
        clamp(field_conf, 0.01, 1.0),
        clamp(crop_conf, 0.01, 1.0),
        clamp(soil_conf, 0.01, 1.0),
    ]
    # Geometric mean punishes one very weak component without collapsing everything to zero.
    score = math.exp(sum(math.log(v) for v in vals) / len(vals))
    return clamp(score, 0.0, 1.0)


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------


def build_agro_advisory(
    lat: float,
    lon: float,
    field_config: Optional[Mapping[str, Any]] = None,
    horizon_hours: int = 120,
    scenario_count: int = 64,
    model_names: Sequence[str] = DEFAULT_MODELS,
    calibration_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    VYOM-AGRO v2 public API.

    Architecture:
      NWP members -> variable/lead/season calibration -> coupled probabilistic
      scenarios -> persistent crop/soil state -> impact models -> robust decisions
      -> structured result for the NLP layer.
    """
    cfg = FieldConfig.from_mapping(field_config)
    horizon_hours = max(24, min(168, int(horizon_hours)))
    scenario_count = max(16, min(160, int(scenario_count)))

    crop_key = (cfg.crop or "generic").strip().lower().replace(" ", "_")
    soil_key = (cfg.soil_type or "loam").strip().lower().replace(" ", "_")
    crop = CROP_PROFILES.get(crop_key, CROP_PROFILES["generic"])
    soil = SOIL_PROFILES.get(soil_key, SOIL_PROFILES["loam"])
    crop_known = crop_key in CROP_PROFILES and crop_key != "generic"
    soil_known = soil_key in SOIL_PROFILES

    calibration_path = calibration_path or os.getenv("VYOM_AGRO_CALIBRATION_PATH")
    calibration = CalibrationStore.from_json(calibration_path)

    members: List[ForecastMember] = []
    for model in model_names:
        m = fetch_forecast_member(lat, lon, model, horizon_hours, calibration)
        if m and len(m.hours) >= 24:
            members.append(m)

    if not members:
        return {
            "engine": "VYOM-AGRO",
            "version": "2.0.0",
            "status": "unavailable",
            "reason": "No usable agricultural forecast members were retrieved.",
            "risk": {},
            "confidence": {"overall": 0.0},
        }

    snapshot = fetch_best_match_field_snapshot(lat, lon)

    # ---------------------- crop phenology / state ----------------------
    user_stage = normalized_user_stage(cfg.crop_stage)
    stage_source = "unknown"
    cumulative_gdd: Optional[float] = None

    if cfg.cumulative_gdd is not None:
        cumulative_gdd = max(0.0, float(cfg.cumulative_gdd))
        stage_source = "user_cumulative_gdd"
    else:
        pd = parse_date(cfg.planting_date)
        if pd:
            hist_gdd = fetch_historical_gdd(lat, lon, pd, crop)
            if hist_gdd is not None:
                cumulative_gdd = hist_gdd
                stage_source = "historical_weather_plus_planting_date"

    if cumulative_gdd is None and cfg.prior_state_cumulative_gdd is not None:
        cumulative_gdd = max(0.0, float(cfg.prior_state_cumulative_gdd))
        stage_source = "persistent_field_state"

    if cumulative_gdd is None and user_stage:
        cumulative_gdd = stage_fraction_anchor(user_stage, crop) * crop.maturity_gdd
        stage_source = "user_stage"

    if cumulative_gdd is None:
        cumulative_gdd = 0.50 * crop.maturity_gdd
        stage_source = "assumed_midseason_for_screening_only"

    stage, frac = stage_from_gdd(cumulative_gdd, crop)
    if user_stage and stage_source not in ("user_cumulative_gdd", "historical_weather_plus_planting_date"):
        stage = user_stage
        frac = stage_fraction_anchor(stage, crop)
        cumulative_gdd = frac * crop.maturity_gdd

    stage_known = stage_source != "assumed_midseason_for_screening_only"
    state, state_meta = initial_field_state(cfg, soil, crop, cumulative_gdd, frac, snapshot)

    # ---------------------- probabilistic weather ----------------------
    seed = deterministic_seed(round(lat, 3), round(lon, 3), date.today().isoformat(), crop_key, soil_key)
    scenarios = build_probabilistic_scenarios(members, calibration, scenario_count, seed)
    consensus = weighted_consensus_hourly(members, horizon_hours, calibration)

    # ---------------------- impact and decision models ----------------------
    risks = impact_probabilities(scenarios, state, crop, soil, cfg, horizon_hours)
    irrigation = optimize_irrigation(
        scenarios, state, crop, soil, cfg, horizon_hours,
        water_state_confidence=state_meta["water_state_confidence"],
    )
    spray = spray_weather_windows(consensus[:72], cfg)
    pest = pest_thermal_development_for_weather(consensus, crop, cfg)
    production = biomass_yield_projection(scenarios, state, crop, soil, cfg, horizon_hours)
    operations = field_operation_windows(consensus[:min(120, len(consensus))], state, crop, soil, cfg)
    action_portfolio = optimize_farm_actions(risks, irrigation, spray, operations, pest)

    # ---------------------- confidence governance ----------------------
    fconf = forecast_confidence(members, min(48, horizon_hours))
    calibration_q = calibration.quality([m.name for m in members])
    crop_conf = 0.90 if stage_known and crop_known else (0.65 if crop_known else 0.35)
    if cfg.crop_profile_calibrated:
        crop_conf = min(1.0, crop_conf + 0.08)
    soil_conf = 0.85 if soil_known else 0.40
    if cfg.soil_profile_calibrated:
        soil_conf = min(1.0, soil_conf + 0.10)

    total_conf = overall_confidence(
        fconf=fconf["score"],
        calibration_quality=calibration_q,
        field_conf=float(state_meta["water_state_confidence"]),
        crop_conf=crop_conf,
        soil_conf=soil_conf,
    )

    mode = precision_mode(cfg, crop_known, stage_known, soil_known, state_meta["water_state_source"])

    exact_volume_allowed = mode in ("field_balance", "sensor_corrected_field_balance") and total_conf >= 0.60
    if not exact_volume_allowed:
        irrigation["liters_per_acre_authoritative"] = None
        irrigation["liters_for_field_authoritative"] = None
        irrigation["precision_note"] = (
            "Use the modeled depth/range as a planning estimate only. Exact delivery volume is suppressed "
            "until root-zone water state, crop stage, soil parameters and forecast calibration are sufficiently verified."
        )
    else:
        irrigation["liters_per_acre_authoritative"] = irrigation.get("liters_per_acre")
        irrigation["liters_for_field_authoritative"] = irrigation.get("liters_for_field")

    # ---------------------- calibrated model weights ----------------------
    month = datetime.utcnow().month
    model_weights_by_variable: Dict[str, Dict[str, float]] = {}
    for var in ("temperature_2m", "relative_humidity_2m", "precipitation", "wind_speed_10m", "et0_fao_evapotranspiration"):
        raw = {
            m.name: calibration.variable_weight(m.name, var, min(48, horizon_hours), month) * clamp(m.data_completeness, 0.1, 1.0)
            for m in members
        }
        sm = sum(raw.values())
        model_weights_by_variable[var] = {
            k: round(v / sm, 4) if sm > 0 else round(1.0 / len(raw), 4)
            for k, v in raw.items()
        }

    # ---------------------- weather / anomaly summaries ----------------------
    rain24 = sum(h.precipitation_mm for h in consensus[:24])
    rain72 = sum(h.precipitation_mm for h in consensus[:72])
    et024 = sum(h.et0_mm for h in consensus[:24])
    mean_temp24 = statistics.mean(h.temperature_c for h in consensus[:24]) if consensus[:24] else None
    max_temp48 = max((h.temperature_c for h in consensus[:48]), default=None)
    min_temp48 = min((h.temperature_c for h in consensus[:48]), default=None)
    max_gust48 = max((h.gust_kmh for h in consensus[:48]), default=None)

    climatology = fetch_day_of_year_climatology(lat, lon)
    climate_anomaly: Dict[str, Any] = {"status": climatology.get("status", "unavailable")}
    if climatology.get("status") == "ok":
        climate_anomaly.update({
            "method": climatology.get("method"),
            "temperature_24h": standardized_anomaly(mean_temp24, climatology.get("temperature_c", {})),
            "precipitation_24h": standardized_anomaly(rain24, climatology.get("precipitation_mm_day", {})),
            "et0_24h": standardized_anomaly(et024, climatology.get("et0_mm_day", {})),
            "climatology": climatology,
        })
    else:
        climate_anomaly["reason"] = climatology.get("reason")

    # Forecast phenology projection from consensus temperature.
    projected_gdd = state.cumulative_gdd
    transitions: List[Dict[str, Any]] = []
    previous_stage, _ = stage_from_gdd(projected_gdd, crop)
    for i, h in enumerate(consensus):
        t = clamp(h.temperature_c, crop.base_temp_c, crop.upper_temp_c)
        projected_gdd += max(0.0, t - crop.base_temp_c) / 24.0
        st, fr = stage_from_gdd(projected_gdd, crop)
        if st != previous_stage:
            transitions.append({
                "hour_from_now": i,
                "time": h.time,
                "from": previous_stage,
                "to": st,
                "maturity_fraction": round(fr, 3),
            })
            previous_stage = st

    missing_context = []
    if not crop_known:
        missing_context.append("verified crop/cultivar profile")
    if not stage_known:
        missing_context.append("planting date or verified crop stage")
    if not soil_known:
        missing_context.append("verified soil texture/hydraulic profile")
    if float(state_meta["water_state_confidence"]) < 0.60:
        missing_context.append("root-zone soil-water observation")
    if calibration_q < 0.60:
        missing_context.append("local forecast-verification calibration")
    if cfg.curve_number is None:
        missing_context.append("field-specific hydrologic curve number/infiltration calibration")
    if production.get("absolute_yield_authoritative") is None:
        missing_context.append("locally calibrated potential yield/WP* for absolute yield prediction")

    state_checkpoint = {
        "timestamp": state.timestamp or datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "depletion_mm": round(state.depletion_mm, 3),
        "surface_depletion_mm": round(state.surface_depletion_mm, 3),
        "excess_water_mm": round(state.excess_water_mm, 3),
        "cumulative_gdd": round(state.cumulative_gdd, 3),
        "root_depth_m": round(state.root_depth_m, 4),
        "field_id": cfg.field_id,
        "purpose": "application-layer persistence checkpoint for the next advisory",
    }

    return {
        "engine": "VYOM-AGRO",
        "version": "2.0.0",
        "status": "ok",
        "location": {"lat": lat, "lon": lon},
        "horizon_hours": horizon_hours,
        "scenario_count": len(scenarios),
        "precision_mode": mode,
        "crop": {
            "requested": cfg.crop,
            "profile_used": crop.name,
            "variety": cfg.variety,
            "stage": stage,
            "stage_source": stage_source,
            "cumulative_gdd": round(state.cumulative_gdd, 1),
            "maturity_fraction": round(frac, 3),
            "projected_gdd_end": round(projected_gdd, 1),
            "projected_stage_transitions": transitions,
            "root_depth_m": state_meta["root_depth_m"],
            "profile_calibrated": bool(cfg.crop_profile_calibrated or crop.calibrated),
        },
        "soil": {
            "requested": cfg.soil_type,
            "profile_used": soil.name,
            "theta_fc": soil.theta_fc,
            "theta_wp": soil.theta_wp,
            "theta_sat": soil.theta_sat,
            "curve_number_used": cfg.curve_number if cfg.curve_number is not None else soil.default_cn,
            "profile_calibrated": bool(cfg.soil_profile_calibrated or soil.calibrated),
        },
        "field_state": state_meta,
        "state_checkpoint": state_checkpoint,
        "weather_consensus": {
            "rain_24h_mm": round(rain24, 2),
            "rain_72h_mm": round(rain72, 2),
            "et0_24h_mm": round(et024, 2),
            "mean_temp_24h_c": round(mean_temp24, 2) if mean_temp24 is not None else None,
            "max_temp_48h_c": round(max_temp48, 1) if max_temp48 is not None else None,
            "min_temp_48h_c": round(min_temp48, 1) if min_temp48 is not None else None,
            "max_gust_48h_kmh": round(max_gust48, 1) if max_gust48 is not None else None,
            "model_weights_by_variable": model_weights_by_variable,
            "scenario_coupling": "whole-model member preserved before residual perturbation",
        },
        "climate_anomaly": climate_anomaly,
        "risk": risks,
        "irrigation": irrigation,
        "spray_weather": spray,
        "pest_thermal_development": pest,
        "biomass_yield": production,
        "field_operations": operations,
        "farm_action_portfolio": action_portfolio,
        "confidence": {
            "overall": round(total_conf, 3),
            "forecast": fconf,
            "field_state": round(float(state_meta["water_state_confidence"]), 3),
            "crop": round(crop_conf, 3),
            "soil": round(soil_conf, 3),
            "local_model_calibration": round(calibration_q, 3),
            "risk_is_not_confidence": True,
        },
        "missing_for_higher_precision": sorted(set(missing_context)),
        "implementation_status": {
            "multi_model_forecast_fusion": "implemented; variable/lead/season calibration hooks",
            "persistent_field_state": "implemented at engine checkpoint level; application persistence optional",
            "crop_phenology_gdd": "implemented",
            "root_zone_water_balance": "implemented",
            "rainfall_runoff_infiltration": "implemented hybrid event-CN + infiltration-capacity screening",
            "probabilistic_irrigation_cvar": "implemented",
            "vpd_heat_cold_water_stress": "implemented",
            "waterlogging_aeration": "implemented excess-water bucket + drainage",
            "disease_weather": "implemented pathogen-specific weather screening for available profiles",
            "pest_degree_day": "implemented for configured profiles",
            "crop_stage_impact_matrix": "implemented",
            "biomass_yield": "implemented forecast-horizon framework; absolute yield gated by calibration",
            "day_of_year_climate_anomaly": "implemented",
            "farm_action_optimization": "implemented robust priority portfolio; full economic optimum needs user cost inputs",
            "risk_confidence_separation": "implemented",
            "precision_abstention": "implemented",
        },
        "field_semantics": {
            "risk": "Modeled agricultural weather/field-state screening; risk is not confidence.",
            "disease": "Weather conduciveness only; not confirmed disease or infection.",
            "pest": "Thermal-development context only; not confirmed infestation.",
            "irrigation": "Use only engine-returned authoritative/planning fields; do not derive a new exact irrigation volume in narration.",
            "yield": "Absolute yield is non-authoritative unless the dedicated authoritative field is populated."
        },
        "engine_diagnostics": {
            "engine_executed": True,
            "validation_contract": "risk_confidence_precision_separated",
            "guarded_narration_expected": True
        },
        "safety_contract": {
            "decision_support_only": True,
            "chemical_application_instructions": False,
            "disease_diagnosis": False,
            "pest_diagnosis": False
        },
        "llm_contract": {
            "allowed": [
                "Translate/explain these structured results in the user's language.",
                "State risk and confidence separately.",
                "Explain recommendations using returned reason fields.",
                "Say when a quantity is planning-only or screening-only.",
            ],
            "forbidden": [
                "Do not recalculate irrigation from ET0 minus rain.",
                "Do not invent crop stage, soil properties, disease diagnosis, pest presence, chemical product or dose.",
                "Do not turn a low-confidence estimate into an exact recommendation.",
                "Do not describe disease-weather conduciveness as confirmed infection.",
                "Do not describe pest degree-day development as confirmed infestation.",
                "Do not output absolute yield as authoritative unless absolute_yield_authoritative is present.",
            ],
        },
    }


# ---------------------------------------------------------------------------
# Offline/synthetic entry point for tests and local development
# ---------------------------------------------------------------------------


def analyze_from_members(
    members: Sequence[ForecastMember],
    field_config: Optional[Mapping[str, Any]] = None,
    horizon_hours: int = 72,
    calibration: Optional[CalibrationStore] = None,
) -> Dict[str, Any]:
    """Pure/offline analysis path for unit tests and replaying archived forecasts."""
    if not members:
        raise ValueError("At least one ForecastMember is required")
    cfg = FieldConfig.from_mapping(field_config)
    crop_key = (cfg.crop or "generic").strip().lower().replace(" ", "_")
    soil_key = (cfg.soil_type or "loam").strip().lower().replace(" ", "_")
    crop = CROP_PROFILES.get(crop_key, CROP_PROFILES["generic"])
    soil = SOIL_PROFILES.get(soil_key, SOIL_PROFILES["loam"])
    calibration = calibration or CalibrationStore()

    user_stage = normalized_user_stage(cfg.crop_stage) or "development"
    frac = stage_fraction_anchor(user_stage, crop)
    gdd = cfg.cumulative_gdd if cfg.cumulative_gdd is not None else frac * crop.maturity_gdd
    state, state_meta = initial_field_state(cfg, soil, crop, gdd, frac, {})

    seed = deterministic_seed("offline", crop_key, soil_key, len(members), horizon_hours)
    scenarios = build_probabilistic_scenarios(members, calibration, 48, seed)
    consensus = weighted_consensus_hourly(members, horizon_hours, calibration)
    risks = impact_probabilities(scenarios, state, crop, soil, cfg, horizon_hours)
    irrigation = optimize_irrigation(
        scenarios, state, crop, soil, cfg, horizon_hours, state_meta["water_state_confidence"]
    )
    spray = spray_weather_windows(consensus, cfg)
    pest = pest_thermal_development_for_weather(consensus, crop, cfg)
    production = biomass_yield_projection(scenarios, state, crop, soil, cfg, horizon_hours)
    operations = field_operation_windows(consensus, state, crop, soil, cfg)
    actions = optimize_farm_actions(risks, irrigation, spray, operations, pest)

    return {
        "engine": "VYOM-AGRO",
        "version": "2.0.0-offline",
        "risk": risks,
        "irrigation": irrigation,
        "spray_weather": spray,
        "pest_thermal_development": pest,
        "biomass_yield": production,
        "field_operations": operations,
        "farm_action_portfolio": actions,
        "field_state": state_meta,
        "state_checkpoint": {
            "timestamp": state.timestamp,
            "depletion_mm": state.depletion_mm,
            "surface_depletion_mm": state.surface_depletion_mm,
            "excess_water_mm": state.excess_water_mm,
            "cumulative_gdd": state.cumulative_gdd,
            "root_depth_m": state.root_depth_m,
        },
    }


if __name__ == "__main__":
    print("VYOM-AGRO module loaded. Import build_agro_advisory() from your FastAPI backend.")
