from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


STRICT_AVIATION_NARRATOR_PROMPT = """You are the explanation layer for VYOM VAYU.

The JSON supplied to you is the authoritative output of the VYOM VAYU analytical engine.

STRICT RULES:
1. Do not perform new aviation calculations.
2. Do not reinterpret or rename numerical fields.
3. Do not invent values, warnings, routes, flight levels, aircraft characteristics, airport conditions, or weather observations.
4. Use only information explicitly present in the JSON payload.
5. If a value is null or a source is unavailable/error/stale, say it could not be verified when relevant.
6. Never transform outside/ambient atmospheric temperature into aircraft cabin/interior temperature.
7. Never state that there are no SIGMETs or no official warnings unless the payload explicitly says the authoritative feed was checked successfully and none matched.
8. Never state that weather is safe, a flight is safe, cleared, approved, dispatchable, legal, airworthy, or suitable for takeoff/landing.
9. Preserve risk, confidence, and data quality as separate concepts.
10. Treat turbulence, icing, wind shear, mountain wave, runway contamination, and microburst outputs as model screening unless an observed/authoritative product is explicitly cited.
11. Do not add generic passenger advice, packing advice, entertainment advice, or unrelated travel tips unless the user explicitly asks for those topics.
12. Do not create new numbers or convert values into new numerical units. You may express a probability already in the payload as the corresponding percentage.
13. If the engine status is error, state that the aviation analysis could not be completed and do not substitute a generic weather conclusion.
14. Keep the answer concise and technical but understandable to a normal user.
15. End with a short sentence that VYOM VAYU provides weather-risk decision support and does not provide operational flight clearance.
"""


PROHIBITED_CLEARANCE_PATTERNS = [
    r"\bsafe to fly\b",
    r"\bsafe for (?:the )?flight\b",
    r"\bflight (?:is|looks) safe\b",
    r"\bcleared for takeoff\b",
    r"\bcleared for landing\b",
    r"\bcleared to depart\b",
    r"\bcleared to fly\b",
    r"\bapproved for (?:takeoff|landing|departure|flight)\b",
    r"\bdispatch(?:ed|able)?\b.*\b(?:approved|clear|safe)\b",
    r"\bairworthy\b",
    r"\bno aviation risk\b",
    r"\bno weather risk\b",
]

GENERIC_PASSENGER_PHRASES = [
    "charge your phone",
    "charge your devices",
    "bring entertainment",
    "download entertainment",
    "life-vessel",
    "life vessel",
    "pack snacks",
]

WARNING_NEGATION_PATTERNS = [
    r"\bno sigmets?\b",
    r"\bno official warnings?\b",
    r"\bno severe weather warnings?\b",
    r"\bno active warnings?\b",
    r"\bno weather warnings?\b",
]


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or isinstance(v, bool):
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _compact_source_status(realtime: Mapping[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for src in (realtime or {}).get("sources", []) or []:
        if not isinstance(src, Mapping):
            continue
        name = str(src.get("source") or "unknown")
        status = str(src.get("status") or "unknown")
        out[name] = status
    return out


def _top_hazards(hazards: Mapping[str, Any], limit: int = 8) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for name, obj in (hazards or {}).items():
        if not isinstance(obj, Mapping):
            continue
        p = _safe_float(obj.get("max_probability"))
        rows.append({
            "hazard": name,
            "level": obj.get("level"),
            "max_probability": p,
            "categories": obj.get("categories"),
        })
    rows.sort(key=lambda r: (-1 if r["max_probability"] is None else -r["max_probability"], str(r["hazard"])))
    return rows[:limit]


def _peak_hour_summary(peak_hours: Sequence[Mapping[str, Any]], limit: int = 6) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for h in list(peak_hours or [])[:limit]:
        if not isinstance(h, Mapping):
            continue
        risk = h.get("risk") if isinstance(h.get("risk"), Mapping) else {}
        out.append({
            "time": h.get("time"),
            "integrated_risk": h.get("integrated_risk"),
            "risk_level": h.get("risk_level"),
            "hazard_probabilities": {
                k: risk.get(k)
                for k in (
                    "turbulence",
                    "icing",
                    "convection",
                    "low_level_wind_shear",
                    "ceiling_visibility",
                    "runway_wind",
                    "density_altitude",
                )
                if risk.get(k) is not None
            },
        })
    return out


def build_aviation_narration_payload(
    result: Mapping[str, Any],
    query: str,
    location_name: Optional[str] = None,
    route_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the only data the aviation narrator is allowed to explain."""
    risk = result.get("risk") if isinstance(result.get("risk"), Mapping) else {}
    confidence = result.get("confidence") if isinstance(result.get("confidence"), Mapping) else {}
    data_quality = result.get("data_quality") if isinstance(result.get("data_quality"), Mapping) else {}
    precision = result.get("precision") if isinstance(result.get("precision"), Mapping) else {}
    official = result.get("official_weather") if isinstance(result.get("official_weather"), Mapping) else {}
    realtime = result.get("realtime") if isinstance(result.get("realtime"), Mapping) else {}
    source_status = result.get("source_status") if isinstance(result.get("source_status"), Mapping) else _compact_source_status(realtime)
    semantics = result.get("field_semantics") if isinstance(result.get("field_semantics"), Mapping) else {
        "temperature": "Outside/ambient atmospheric temperature. It is not aircraft cabin temperature.",
        "turbulence": "Modeled turbulence exposure screening unless supported by an explicit PIREP/AIREP or authoritative product.",
        "icing": "Meteorological icing-conduciveness screening, not aircraft certification or operational clearance.",
        "route": "Weather-exposure screening only; it is not an ATC/dispatch route or navigation clearance.",
    }

    payload: Dict[str, Any] = {
        "query": query,
        "location_name": location_name,
        "engine": {
            "name": result.get("engine") or "VYOM-AVIATION",
            "public_name": "VYOM VAYU",
            "version": result.get("version"),
            "status": result.get("status"),
            "generated_at": result.get("generated_at"),
            "horizon_hours": result.get("horizon_hours"),
        },
        "overall": {
            "risk_level": risk.get("level"),
            "integrated_peak_probability": risk.get("integrated_peak_probability"),
            "confidence": confidence.get("overall"),
            "forecast_confidence": confidence.get("forecast"),
            "model_agreement": confidence.get("model_agreement"),
            "data_quality": data_quality.get("overall"),
        },
        "hazards": _top_hazards(risk.get("hazards") if isinstance(risk.get("hazards"), Mapping) else {}),
        "probabilistic": risk.get("probabilistic") if isinstance(risk.get("probabilistic"), Mapping) else {},
        "peak_periods": _peak_hour_summary(result.get("peak_hours") or []),
        "official_weather": {
            "present": official.get("present"),
            "critical": official.get("critical"),
            "level": official.get("level"),
            "categories": official.get("categories") or [],
            "source": official.get("source"),
            "text": official.get("text"),
            "feed_status": official.get("feed_status"),
            "verified_none": official.get("verified_none"),
            "status_unknown": official.get("status_unknown"),
        },
        "source_status": dict(source_status or {}),
        "precision": {
            "level": precision.get("level"),
            "missing_or_unverified": precision.get("missing_or_unverified") or [],
            "abstention_reasons": precision.get("abstention_reasons") or [],
        },
        "route": result.get("route") if isinstance(result.get("route"), Mapping) else {},
        "action_flags": result.get("action_flags") or [],
        "limitations": [
            "The result is weather-risk decision support only.",
            "It does not provide dispatch, takeoff, landing, ATC, airworthiness, or operational flight clearance.",
            "Official aviation-weather products and approved aircraft/operator limits take precedence.",
        ],
        "field_semantics": dict(semantics),
        "safety_contract": result.get("safety_contract") or {},
    }
    if route_context:
        payload["route_context"] = dict(route_context)
    if result.get("status") == "error":
        payload["engine_error"] = result.get("reason")
    return payload


def build_aviation_narrator_messages(payload: Mapping[str, Any], language: str = "English") -> List[Dict[str, str]]:
    system = STRICT_AVIATION_NARRATOR_PROMPT + f"\nRespond in {language}."
    user = (
        "Explain the following validated VYOM VAYU payload. Do not use any facts or numbers outside it.\n\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _walk_numbers(obj: Any) -> Iterable[float]:
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        x = _safe_float(obj)
        if x is not None:
            yield x
        return
    if isinstance(obj, Mapping):
        for v in obj.values():
            yield from _walk_numbers(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_numbers(v)


def _allowed_numeric_tokens(payload: Mapping[str, Any]) -> set[str]:
    allowed: set[str] = set()
    for x in _walk_numbers(payload):
        candidates = {
            f"{x:g}",
            f"{x:.1f}",
            f"{x:.2f}",
            str(int(round(x))),
        }
        if 0.0 <= x <= 1.0:
            candidates.add(str(int(round(x * 100.0))))
            candidates.add(f"{x * 100.0:.1f}")
        for c in candidates:
            allowed.add(c.rstrip("0").rstrip(".") if "." in c else c)
    def walk_strings(obj: Any):
        if isinstance(obj, str):
            yield obj
        elif isinstance(obj, Mapping):
            for v in obj.values():
                yield from walk_strings(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                yield from walk_strings(v)
    for text in walk_strings(payload):
        for raw in re.findall(r"[-+]?\d+(?:\.\d+)?", text):
            tok = raw.lstrip("+")
            allowed.add(tok.rstrip("0").rstrip(".") if "." in tok else tok)
    return allowed


def validate_aviation_narration(text: str, payload: Mapping[str, Any]) -> Tuple[bool, List[str]]:
    problems: List[str] = []
    t = (text or "").strip()
    lower = t.lower()
    if not t:
        return False, ["empty_response"]

    for pat in PROHIBITED_CLEARANCE_PATTERNS:
        if re.search(pat, lower, flags=re.I):
            problems.append("prohibited_clearance_language")
            break

    if "cabin temperature" in lower or "cabin temperatures" in lower or "interior temperature" in lower:
        problems.append("invented_cabin_temperature")

    if any(p in lower for p in GENERIC_PASSENGER_PHRASES):
        problems.append("generic_or_irrelevant_passenger_advice")

    official = payload.get("official_weather") if isinstance(payload.get("official_weather"), Mapping) else {}
    verified_none = bool(official.get("verified_none"))
    for pat in WARNING_NEGATION_PATTERNS:
        if re.search(pat, lower, flags=re.I) and not verified_none:
            problems.append("unsupported_no_warning_claim")
            break

    # Numerical hallucination guard. Allow values already present and percentage
    # representations of probabilities. Ignore single digit list numbering.
    allowed = _allowed_numeric_tokens(payload)
    for raw in re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?", t):
        token = raw.lstrip("+")
        norm = token.rstrip("0").rstrip(".") if "." in token else token
        try:
            xv = abs(float(token))
        except Exception:
            continue
        if xv <= 9 and norm in {"1", "2", "3", "4", "5", "6", "7", "8", "9"}:
            # Could simply be numbered prose; do not reject on that alone.
            continue
        if norm not in allowed:
            problems.append(f"unsupported_numeric_value:{token}")
            break

    return len(problems) == 0, problems


def _fmt_prob(v: Any) -> str:
    x = _safe_float(v)
    return "unavailable" if x is None else f"{x * 100:.0f}%"


def deterministic_aviation_fallback(payload: Mapping[str, Any]) -> str:
    eng = payload.get("engine") if isinstance(payload.get("engine"), Mapping) else {}
    if eng.get("status") != "ok":
        reason = payload.get("engine_error") or "the aviation engine could not complete its analysis"
        return (
            f"VYOM VAYU could not complete the aviation-weather analysis: {reason}. "
            "No operational flight conclusion is provided."
        )

    overall = payload.get("overall") if isinstance(payload.get("overall"), Mapping) else {}
    hazards = payload.get("hazards") if isinstance(payload.get("hazards"), list) else []
    official = payload.get("official_weather") if isinstance(payload.get("official_weather"), Mapping) else {}
    precision = payload.get("precision") if isinstance(payload.get("precision"), Mapping) else {}

    parts = [
        f"VYOM VAYU modeled aviation-weather risk is {overall.get('risk_level') or 'UNKNOWN'} "
        f"with peak modeled probability {_fmt_prob(overall.get('integrated_peak_probability'))}, "
        f"confidence {_fmt_prob(overall.get('confidence'))}, and data quality {_fmt_prob(overall.get('data_quality'))}."
    ]

    top = [h for h in hazards if isinstance(h, Mapping) and h.get("max_probability") is not None][:4]
    if top:
        desc = ", ".join(
            f"{str(h.get('hazard')).replace('_',' ')} {h.get('level')} ({_fmt_prob(h.get('max_probability'))})"
            for h in top
        )
        parts.append("Highest screened hazards: " + desc + ".")

    if official.get("present"):
        cats = official.get("categories") or []
        cat_text = ", ".join(str(c) for c in cats) if cats else "significant aviation weather"
        source = official.get("source") or "authoritative aviation-weather source"
        parts.append(f"Authoritative warning context is present from {source}: {cat_text}.")
    elif official.get("verified_none"):
        parts.append("The configured official aviation-warning feed was checked successfully and no matching warning was found in that query context.")
    else:
        parts.append("Official aviation-warning status could not be fully verified from the available source state.")

    missing = precision.get("missing_or_unverified") or []
    if missing:
        parts.append("Important missing or unverified context: " + ", ".join(str(x) for x in missing[:6]) + ".")

    parts.append("VYOM VAYU provides weather-risk decision support only and does not provide operational flight clearance.")
    return " ".join(parts)


def build_route_narration_payload(
    query: str,
    origin: Mapping[str, Any],
    destination: Mapping[str, Any],
    route_samples: Sequence[Mapping[str, Any]],
    departure_result: Mapping[str, Any],
    arrival_result: Mapping[str, Any],
) -> Dict[str, Any]:
    valid_samples = [s for s in route_samples if isinstance(s, Mapping) and s.get("status") == "ok"]
    peak = max((_safe_float(s.get("risk_probability")) or 0.0 for s in valid_samples), default=None)
    confidence_values = [_safe_float(s.get("confidence")) for s in valid_samples]
    confidence_values = [x for x in confidence_values if x is not None]
    quality_values = [_safe_float(s.get("data_quality")) for s in valid_samples]
    quality_values = [x for x in quality_values if x is not None]
    warning_present = any(bool(s.get("official_warning_present")) for s in valid_samples)
    warning_verified_none = bool(valid_samples) and all(bool(s.get("official_warning_verified_none")) for s in valid_samples)
    warning_unknown = any(str(s.get("official_warning_feed_status") or "").lower() in {"error","degraded","unavailable","stale","not_requested","unknown",""} for s in valid_samples)
    return {
        "query": query,
        "engine": {"name": "VYOM-AVIATION", "public_name": "VYOM VAYU", "mode": "route_screening", "status": "ok" if valid_samples else "error"},
        "route_context": {
            "origin": dict(origin),
            "destination": dict(destination),
            "sampling_method": "great-circle environmental screening points; not an operational flight plan or ATC route",
            "sample_count": len(route_samples),
        },
        "overall": {
            "risk_level": _route_risk_label(peak),
            "integrated_peak_probability": peak,
            "confidence": (sum(confidence_values) / len(confidence_values)) if confidence_values else None,
            "data_quality": (sum(quality_values) / len(quality_values)) if quality_values else None,
        },
        "route_samples": list(route_samples),
        "departure_terminal": build_aviation_narration_payload(departure_result, query, str(origin.get("name") or "origin")),
        "arrival_terminal": build_aviation_narration_payload(arrival_result, query, str(destination.get("name") or "destination")),
        "limitations": [
            "Route sampling is meteorological screening only, not an operational flight plan.",
            "Airspace, NOTAM, dispatch, fuel, ETOPS/EDTO, MEL/CDL, ATC, and aircraft-performance decisions are outside this result.",
            "Official aviation-weather products and operator procedures take precedence.",
        ],
        "field_semantics": {
            "temperature": "Outside/ambient atmospheric temperature only, never cabin temperature.",
            "route_sample": "A sampled environmental point along an approximate great-circle corridor, not a navigation waypoint.",
        },
        "official_weather": {
            "present": warning_present,
            "verified_none": warning_verified_none,
            "status_unknown": warning_unknown,
        },
    }


def _route_risk_label(p: Optional[float]) -> str:
    if p is None:
        return "UNKNOWN"
    if p >= 0.85:
        return "CRITICAL"
    if p >= 0.65:
        return "HIGH"
    if p >= 0.35:
        return "CAUTION"
    return "LOW"


def deterministic_aviation_route_fallback(payload: Mapping[str, Any]) -> str:
    overall = payload.get("overall") if isinstance(payload.get("overall"), Mapping) else {}
    route = payload.get("route_context") if isinstance(payload.get("route_context"), Mapping) else {}
    origin = (route.get("origin") or {}).get("name") if isinstance(route.get("origin"), Mapping) else "origin"
    destination = (route.get("destination") or {}).get("name") if isinstance(route.get("destination"), Mapping) else "destination"
    if (payload.get("engine") or {}).get("status") != "ok":
        return "VYOM VAYU could not complete the route weather screening. No operational flight conclusion is provided."
    return (
        f"VYOM VAYU screened the approximate environmental corridor from {origin} to {destination}. "
        f"The highest modeled aviation-weather risk across the sampled corridor is {overall.get('risk_level') or 'UNKNOWN'} "
        f"with peak modeled probability {_fmt_prob(overall.get('integrated_peak_probability'))} and confidence {_fmt_prob(overall.get('confidence'))}. "
        "The route points are meteorological screening samples, not an ATC or dispatch flight plan. "
        "VYOM VAYU provides weather-risk decision support only and does not provide operational flight clearance."
    )


__all__ = [
    "STRICT_AVIATION_NARRATOR_PROMPT",
    "build_aviation_narration_payload",
    "build_aviation_narrator_messages",
    "validate_aviation_narration",
    "deterministic_aviation_fallback",
    "build_route_narration_payload",
    "deterministic_aviation_route_fallback",
]
