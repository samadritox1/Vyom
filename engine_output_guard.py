from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


DOMAIN_PUBLIC_NAMES = {
    "agro": "VYOM KRISHI",
    "marine": "VYOM SAGAR",
    "aviation": "VYOM VAYU",
    "adventure": "VYOM ADVENTURE",
    "disaster": "VYOM RAKSHA",
}

DOMAIN_ENGINE_NAMES = {
    "agro": "VYOM-AGRO",
    "marine": "VYOM-MARINE",
    "aviation": "VYOM-AVIATION",
    "adventure": "VYOM-ADVENTURE",
    "disaster": "VYOM-RAKSHA",
}

DOMAIN_PROMPTS = {
    "agro": """You are the explanation layer for VYOM KRISHI. Use only the validated JSON payload. Do not recalculate agronomy. Do not invent crop stage, soil properties, field observations, disease diagnosis, pest presence, chemical products, application timing, doses, or exact irrigation volumes that the engine did not mark authoritative. Disease and pest outputs are screening/context, not confirmed infection or infestation. Preserve risk, confidence, data quality, precision, and missing information separately. Never guarantee yield or crop outcome.""",
    "marine": """You are the explanation layer for VYOM SAGAR. Use only the validated JSON payload. Do not recalculate vessel safety from raw weather. Never state or imply that a vessel is safe to sail, cleared, approved, or navigation-safe. Do not invent helm/course/speed instructions, COLREG decisions, UKC, CPA/TCPA, bathymetry, cyclone, ice, stability, or route results that are unavailable. Official maritime warnings and approved vessel/operator limits take precedence. Preserve risk, confidence, data quality, precision, and missing information separately.""",
    "aviation": """You are the explanation layer for VYOM VAYU. Use only the validated JSON payload. Do not perform new aviation calculations. Never transform outside/ambient temperature into cabin temperature. Never state or imply that a flight is safe, cleared, dispatchable, legal, airworthy, approved for takeoff/landing, or operationally released. Official aviation-weather products and approved aircraft/operator limits take precedence. Preserve risk, confidence, data quality, precision, and missing information separately.""",
    "adventure": """You are the explanation layer for VYOM ADVENTURE. Use only the validated JSON payload. Do not invent terrain, snowpack, river depth/flow, wildfire perimeter, rescue information, or official bulletins. Never certify a route, climb, crossing, snow slope, exposed ridge, wildfire-area journey, or other hazardous outdoor activity as safe. Do not provide technical maneuver instructions. Preserve risk, confidence, data quality, precision, and missing information separately. Official local warnings and land/rescue guidance take precedence.""",
    "disaster": """You are the explanation layer for VYOM RAKSHA. Use only the validated JSON payload. Do not invent evacuation orders, shelter selections, rescue dispatch, all-clear messages, earthquake predictions, tsunami evacuation decisions, casualty estimates, or infrastructure failures. Official emergency-management orders and CAP/agency warnings take precedence. Distinguish hazard probability from impact risk, confidence, data quality, and missing local exposure/terrain/gauge information.""",
}

COMMON_PROMPT = """
STRICT RULES:
1. Use only values, statuses, sources, warnings, and conclusions explicitly present in the JSON payload.
2. Do not create new numerical values, unit conversions, thresholds, probabilities, distances, times, quantities, or counts.
3. A probability already present as 0-1 may be expressed as the equivalent percentage.
4. If a value is null, unavailable, stale, unconfigured, degraded, or error, state that it could not be verified when relevant.
5. Never say there are no official warnings unless the payload explicitly marks the relevant authoritative warning source as VERIFIED_NONE/SUCCESS_NONE.
6. Preserve modeled/screening language. Do not turn a model screen into an observed fact.
7. Do not add generic lifestyle, packing, passenger, entertainment, or unrelated advice unless explicitly requested.
8. If engine status is not OK, say the specialized analysis could not be completed; do not substitute a confident generic conclusion.
9. Keep the answer concise, technical, and understandable.
10. End with the engine's decision-support limitation when the domain is safety-critical.
"""

PROHIBITED_BY_DOMAIN = {
    "agro": [
        r"\bconfirmed (?:disease|infection|infestation)\b",
        r"\bguaranteed yield\b",
        r"\bapply \d+(?:\.\d+)?\s*(?:ml|l|kg|g)\b",
        r"\bpesticide dose\b",
    ],
    "marine": [
        r"\bsafe to sail\b", r"\bsafe to navigate\b", r"\bnavigation clearance\b",
        r"\bcleared to sail\b", r"\bapproved to sail\b", r"\bturn (?:port|starboard)\b",
        r"\bset course\b", r"\b(?:voyage|route|conditions).{0,24}\b(?:safe|suitable|acceptable)\b",
        r"\bno (?:marine|maritime|navigation) risk\b",
    ],
    "aviation": [
        r"\bsafe to fly\b", r"\bsafe for (?:the )?flight\b", r"\bcleared for takeoff\b",
        r"\bcleared for landing\b", r"\bcleared to fly\b", r"\bapproved for (?:takeoff|landing|departure|flight)\b",
        r"\bairworthy\b", r"\bcabin temperatures?\b", r"\binterior temperatures?\b",
        r"\b(?:flight|departure|landing|route).{0,24}\b(?:safe|suitable|acceptable)\b",
        r"\bno aviation risk\b",
    ],
    "adventure": [
        r"\bsafe to proceed\b", r"\broute is safe\b", r"\bsafe to cross\b", r"\bsafe to climb\b",
        r"\bavalanche[- ]safe\b", r"\bcleared for (?:the )?(?:trek|climb|crossing)\b",
        r"\b(?:route|trail|trek|climb|crossing).{0,24}\b(?:safe|suitable|acceptable)\b",
    ],
    "disaster": [
        r"\bevacuat(?:e|ion) now\b", r"\ball clear\b", r"\bearthquake will (?:occur|happen)\b",
        r"\btsunami evacuation is required\b", r"\brescue teams? should deploy\b",
    ],
}

GENERIC_BAD_PHRASES = [
    "charge your phone", "charge your devices", "bring entertainment", "download entertainment",
    "life-vessel", "life vessel", "pack snacks",
]

WARNING_NEGATION_PATTERNS = [
    r"\bno (?:official )?warnings?\b",
    r"\bno active warnings?\b",
    r"\bno severe weather warnings?\b",
    r"\bno sigmets?\b",
    r"\bno advisories\b",
]


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or isinstance(v, bool):
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _normalize_status(status: Any, records: Any = None) -> str:
    s = str(status or "unknown").strip().lower()
    if s in {"ok", "success", "healthy"}:
        try:
            if records is not None and int(records) == 0:
                return "success_none"
        except Exception:
            pass
        return "success"
    if s in {"success_none", "verified_none", "none_found"}:
        return "success_none"
    if s in {"error", "failed", "failure"}:
        return "error"
    if s in {"unconfigured", "not_configured"}:
        return "unconfigured"
    if s in {"degraded", "partial"}:
        return "degraded"
    if s in {"stale"}:
        return "stale"
    if s in {"unavailable", "offline"}:
        return "unavailable"
    return s or "unknown"


def normalize_source_status(result: Mapping[str, Any], extra_sources: Optional[Sequence[Mapping[str, Any]]] = None) -> Dict[str, str]:
    out: Dict[str, str] = {}
    direct = result.get("source_status") if isinstance(result.get("source_status"), Mapping) else {}
    for k, v in direct.items():
        if isinstance(v, Mapping):
            out[str(k)] = _normalize_status(v.get("status"), v.get("records"))
        else:
            out[str(k)] = _normalize_status(v)

    candidates: List[Mapping[str, Any]] = []
    for key in ("realtime", "real_data_fabric"):
        obj = result.get(key)
        if isinstance(obj, Mapping):
            for item in obj.get("sources") or []:
                if isinstance(item, Mapping):
                    candidates.append(item)
    for item in extra_sources or []:
        if isinstance(item, Mapping):
            candidates.append(item)
    for src in candidates:
        name = str(src.get("source") or src.get("name") or "unknown")
        out[name] = _normalize_status(src.get("status"), src.get("records"))
    return out


def _risk_rows_from_agro(risk: Mapping[str, Any], limit: int = 10) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for name, obj in risk.items():
        if not isinstance(obj, Mapping):
            continue
        p = _safe_float(obj.get("probability"))
        if p is not None:
            rows.append({"hazard": name, "probability": p, "level": obj.get("level"), "scope": obj.get("scope")})
    rows.sort(key=lambda x: -(x.get("probability") or -1))
    return rows[:limit]


def _risk_rows_from_summary(summary: Mapping[str, Any], limit: int = 10) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for name, obj in summary.items():
        if isinstance(obj, Mapping):
            p = _safe_float(obj.get("max_probability"))
            if p is None:
                p = _safe_float(obj.get("risk"))
            if p is not None:
                rows.append({"hazard": name, "probability": p, "level": obj.get("level")})
        else:
            p = _safe_float(obj)
            if p is not None:
                rows.append({"hazard": name, "probability": p, "level": None})
    rows.sort(key=lambda x: -(x.get("probability") or -1))
    return rows[:limit]


def _marine_payload(result: Mapping[str, Any]) -> Dict[str, Any]:
    risk = result.get("risk") if isinstance(result.get("risk"), Mapping) else {}
    ogx = result.get("ocean_guardian_x") if isinstance(result.get("ocean_guardian_x"), Mapping) else {}
    gf = ogx.get("dependency_aware_global_risk") if isinstance(ogx.get("dependency_aware_global_risk"), Mapping) else {}
    return {
        "overall": {
            "legacy_risk_label": risk.get("level") or risk.get("label"),
            "global_risk": gf.get("risk"),
            "safety_level": ogx.get("user_facing_safety_level"),
            "confidence": (result.get("confidence") or {}).get("overall") if isinstance(result.get("confidence"), Mapping) else None,
        },
        "risk": risk,
        "modeled_peak": result.get("modeled_peak") or {},
        "official_warning": result.get("official_warning") or {},
        "precision": result.get("precision") or {},
        "key_modules": {
            "under_keel_clearance": ogx.get("under_keel_clearance"),
            "traffic_collision": ogx.get("traffic_collision"),
            "cyclone_ensemble_exposure": ogx.get("cyclone_ensemble_exposure"),
            "sea_ice": ogx.get("sea_ice"),
            "dynamic_stability": ogx.get("dynamic_stability"),
            "extreme_wave_tail": ogx.get("extreme_wave_tail"),
            "rapidly_developing_sea": ogx.get("rapidly_developing_sea"),
            "route_candidate_optimization": ogx.get("route_candidate_optimization"),
        },
        "data_caveats": result.get("data_caveats") or [],
        "decision_contract": result.get("decision_contract") or {},
    }


def _compact_domain_result(domain: str, result: Mapping[str, Any]) -> Dict[str, Any]:
    if domain == "agro":
        risk = result.get("risk") if isinstance(result.get("risk"), Mapping) else {}
        return {
            "crop": result.get("crop") or {},
            "soil": result.get("soil") or {},
            "field_state": result.get("field_state") or {},
            "weather_consensus": result.get("weather_consensus") or {},
            "risks": _risk_rows_from_agro(risk),
            "irrigation": result.get("irrigation") or {},
            "field_operations": result.get("field_operations") or {},
            "farm_action_portfolio": result.get("farm_action_portfolio") or {},
            "biomass_yield": result.get("biomass_yield") or {},
            "confidence": result.get("confidence") or {},
            "missing_for_higher_precision": result.get("missing_for_higher_precision") or [],
            "llm_contract": result.get("llm_contract") or {},
        }
    if domain == "marine":
        return _marine_payload(result)
    if domain == "aviation":
        risk = result.get("risk") if isinstance(result.get("risk"), Mapping) else {}
        return {
            "overall": {
                "risk_level": risk.get("level"),
                "integrated_peak_probability": risk.get("integrated_peak_probability"),
                "confidence": (result.get("confidence") or {}).get("overall") if isinstance(result.get("confidence"), Mapping) else None,
                "data_quality": (result.get("data_quality") or {}).get("overall") if isinstance(result.get("data_quality"), Mapping) else None,
            },
            "hazards": _risk_rows_from_summary(risk.get("hazards") or {}),
            "official_weather": result.get("official_weather") or {},
            "precision": result.get("precision") or {},
            "route": result.get("route") or {},
            "action_flags": result.get("action_flags") or [],
            "field_semantics": result.get("field_semantics") or {},
            "safety_contract": result.get("safety_contract") or {},
        }
    if domain == "adventure":
        risk = result.get("risk") if isinstance(result.get("risk"), Mapping) else {}
        return {
            "overall": {"risk_level": risk.get("level"), "integrated_peak_probability": risk.get("integrated_peak_probability"), "confidence": (result.get("confidence") or {}).get("overall") if isinstance(result.get("confidence"), Mapping) else None},
            "hazards": _risk_rows_from_summary(risk.get("hazards") or {}),
            "route_terrain": result.get("route_terrain") or {},
            "air_quality": result.get("air_quality") or {},
            "wildfire_smoke": result.get("wildfire_smoke") or {},
            "official_warnings": result.get("official_warnings") or {},
            "precision": result.get("precision") or {},
            "action_flags": result.get("action_flags") or [],
            "safety_contract": result.get("safety_contract") or {},
        }
    if domain == "disaster":
        risk = result.get("risk") if isinstance(result.get("risk"), Mapping) else {}
        probs = risk.get("hazard_probabilities") if isinstance(risk.get("hazard_probabilities"), Mapping) else {}
        rows = [{"hazard": k, "probability": v} for k, v in probs.items() if _safe_float(v) is not None]
        rows.sort(key=lambda x: -float(x["probability"]))
        return {
            "overall": {"risk_level": risk.get("level"), "integrated_risk": risk.get("integrated"), "model_fusion": risk.get("model_fusion"), "official_warning_floor": risk.get("official_warning_floor"), "confidence": (result.get("confidence") or {}).get("overall") if isinstance(result.get("confidence"), Mapping) else None},
            "hazard_probabilities": rows[:12],
            "impact_risk": risk.get("impact_risk") or {},
            "contributors": risk.get("contributors") or [],
            "official_warning_gate": result.get("official_warning_gate") or {},
            "management_priorities": result.get("management_priorities") or [],
            "exposure_vulnerability": result.get("exposure_vulnerability") or {},
            "precision": result.get("precision") or {},
            "decision_contract": result.get("decision_contract") or {},
        }
    return {"result": result}


def _warning_verification(domain: str, compact: Mapping[str, Any], source_status: Mapping[str, str]) -> Dict[str, Any]:
    warning_sources = {
        "marine": ("warning", "imd", "incois", "gmdss", "navtex", "metarea"),
        "aviation": ("sigmet", "warning", "aviation weather"),
        "adventure": ("warning", "alerts", "nws", "avalanche", "bulletin"),
        "disaster": ("warning", "cap", "nws", "tsunami", "imd", "gdacs"),
        "agro": ("warning", "alert"),
    }
    relevant = []
    for name, status in source_status.items():
        if any(tok in name.lower() for tok in warning_sources.get(domain, ())):
            relevant.append({"source": name, "status": status})
    verified_none = bool(relevant) and all(x["status"] == "success_none" for x in relevant)
    verified_active = False
    if domain == "aviation":
        ow = compact.get("official_weather") if isinstance(compact.get("official_weather"), Mapping) else {}
        verified_none = verified_none or bool(ow.get("verified_none"))
        verified_active = bool(ow.get("present"))
    elif domain == "marine":
        ow = compact.get("official_warning") if isinstance(compact.get("official_warning"), Mapping) else {}
        verified_active = bool(ow.get("present") or str(ow.get("level") or "none").lower() not in {"none", "", "unknown"})
    elif domain == "adventure":
        ow = compact.get("official_warnings") if isinstance(compact.get("official_warnings"), Mapping) else {}
        verified_active = bool(ow.get("present"))
    elif domain == "disaster":
        ow = compact.get("official_warning_gate") if isinstance(compact.get("official_warning_gate"), Mapping) else {}
        verified_active = bool(ow.get("warnings_present"))
    return {"verified_none": verified_none, "verified_active": verified_active, "sources": relevant, "status_unknown": not verified_none and not verified_active}


def build_engine_narration_payload(
    domain: str,
    result: Mapping[str, Any],
    query: str,
    location_name: Optional[str] = None,
    extra_sources: Optional[Sequence[Mapping[str, Any]]] = None,
    routing: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    domain = domain.lower()
    compact = _compact_domain_result(domain, result)
    source_status = normalize_source_status(result, extra_sources)
    warning = _warning_verification(domain, compact, source_status)
    payload = {
        "query": query,
        "location_name": location_name,
        "domain": domain,
        "engine": {
            "name": result.get("engine") or DOMAIN_ENGINE_NAMES.get(domain),
            "public_name": DOMAIN_PUBLIC_NAMES.get(domain, domain),
            "version": result.get("version"),
            "status": result.get("status"),
            "generated_at": result.get("generated_at"),
        },
        "analysis": compact,
        "source_status": source_status,
        "official_warning_verification": warning,
        "routing": dict(routing or {}),
    }
    if result.get("status") not in {"ok", "success"}:
        payload["engine_error"] = result.get("reason") or result.get("error")
    return payload


def build_engine_narrator_messages(domain: str, payload: Mapping[str, Any], language: str = "English") -> List[Dict[str, str]]:
    domain = domain.lower()
    system = DOMAIN_PROMPTS.get(domain, "Explain only the validated engine JSON.") + COMMON_PROMPT + f"\nRespond in {language}."
    user = "Explain this validated VYOM engine payload. Use no facts or numbers outside it.\n\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
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
        candidates = {f"{x:g}", f"{x:.1f}", f"{x:.2f}", str(int(round(x)))}
        if 0.0 <= x <= 1.0:
            candidates.add(str(int(round(x * 100.0))))
            candidates.add(f"{x * 100.0:.1f}")
        for c in candidates:
            allowed.add(c.rstrip("0").rstrip(".") if "." in c else c)

    # Numbers that are part of user/source strings (dates, flight numbers, model
    # versions, query values) are also grounded because the exact string is in
    # the validated payload.
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


def validate_engine_narration(domain: str, text: str, payload: Mapping[str, Any]) -> Tuple[bool, List[str]]:
    domain = domain.lower()
    t = (text or "").strip()
    lower = t.lower()
    if not t:
        return False, ["empty_response"]
    problems: List[str] = []

    for pattern in PROHIBITED_BY_DOMAIN.get(domain, []):
        if re.search(pattern, lower, flags=re.I):
            problems.append("prohibited_or_unsupported_operational_claim")
            break

    if any(p in lower for p in GENERIC_BAD_PHRASES):
        problems.append("irrelevant_generic_advice")

    warning = payload.get("official_warning_verification") if isinstance(payload.get("official_warning_verification"), Mapping) else {}
    verified_none = bool(warning.get("verified_none"))
    for pat in WARNING_NEGATION_PATTERNS:
        if re.search(pat, lower, flags=re.I) and not verified_none:
            problems.append("unsupported_no_warning_claim")
            break

    allowed = _allowed_numeric_tokens(payload)
    for raw in re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?", t):
        token = raw.lstrip("+")
        norm = token.rstrip("0").rstrip(".") if "." in token else token
        try:
            xv = abs(float(token))
        except Exception:
            continue
        if xv <= 9 and norm in {str(i) for i in range(1, 10)}:
            continue
        if norm not in allowed:
            problems.append(f"unsupported_numeric_value:{token}")
            break

    return len(problems) == 0, problems


def _pct(v: Any) -> str:
    x = _safe_float(v)
    return "unavailable" if x is None else f"{x * 100:.0f}%"


def deterministic_engine_fallback(domain: str, payload: Mapping[str, Any]) -> str:
    eng = payload.get("engine") if isinstance(payload.get("engine"), Mapping) else {}
    public = eng.get("public_name") or DOMAIN_PUBLIC_NAMES.get(domain, "VYOM")
    if eng.get("status") not in {"ok", "success"}:
        reason = payload.get("engine_error") or "the specialized engine could not complete its analysis"
        return f"{public} could not complete the specialized analysis: {reason}. No high-confidence operational conclusion is provided."

    a = payload.get("analysis") if isinstance(payload.get("analysis"), Mapping) else {}
    warning = payload.get("official_warning_verification") if isinstance(payload.get("official_warning_verification"), Mapping) else {}
    parts: List[str] = []

    if domain == "agro":
        risks = a.get("risks") or []
        conf = a.get("confidence") or {}
        if risks:
            top = risks[0]
            parts.append(f"{public}: the highest returned agricultural weather-screening risk is {top.get('hazard')} at {_pct(top.get('probability'))} ({top.get('level') or 'level unavailable'}).")
        parts.append(f"Overall engine confidence is {_pct(conf.get('overall'))}.")
        missing = a.get("missing_for_higher_precision") or []
        if missing: parts.append("Higher precision needs: " + "; ".join(map(str, missing[:5])) + ".")
        parts.append("This is decision support; disease/pest screens are not diagnoses and chemical-application instructions are not provided.")
    elif domain == "marine":
        overall = a.get("overall") or {}
        parts.append(f"{public}: dependency-aware global modeled risk is {_pct(overall.get('global_risk'))}; user-facing safety level is {overall.get('safety_level') or 'unavailable'}; confidence is {_pct(overall.get('confidence'))}.")
        parts.append("This is maritime decision support only and does not provide navigation or sailing clearance.")
    elif domain == "aviation":
        overall = a.get("overall") or {}
        parts.append(f"{public}: modeled aviation-weather risk is {overall.get('risk_level') or 'UNKNOWN'} with peak modeled probability {_pct(overall.get('integrated_peak_probability'))}, confidence {_pct(overall.get('confidence'))}, and data quality {_pct(overall.get('data_quality'))}.")
        parts.append("This is weather-risk decision support and does not provide operational flight clearance.")
    elif domain == "adventure":
        overall = a.get("overall") or {}
        parts.append(f"{public}: modeled outdoor environmental risk is {overall.get('risk_level') or 'UNKNOWN'} with peak probability {_pct(overall.get('integrated_peak_probability'))} and confidence {_pct(overall.get('confidence'))}.")
        parts.append("This is environmental decision support and does not certify a route, climb, crossing, snow slope, or hazardous outdoor activity as safe.")
    elif domain == "disaster":
        overall = a.get("overall") or {}
        parts.append(f"{public}: integrated modeled disaster impact risk is {_pct(overall.get('integrated_risk'))} ({overall.get('risk_level') or 'level unavailable'}) with confidence {_pct(overall.get('confidence'))}.")
        parts.append("Official emergency-management orders take precedence; VYOM does not invent evacuation, shelter, rescue-dispatch, tsunami, or all-clear orders.")
    else:
        parts.append(f"{public} completed its analysis.")

    if warning.get("verified_active"):
        parts.append("An official warning/advisory is present in the validated context and must not be downgraded by model output.")
    elif warning.get("status_unknown") and domain != "agro":
        parts.append("Official warning status was not fully verified from an authoritative feed.")
    return " ".join(parts)


def build_engine_diagnostics(domain: str, result: Mapping[str, Any], payload: Mapping[str, Any], valid: bool, problems: Sequence[str], routing: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    return {
        "engine": DOMAIN_PUBLIC_NAMES.get(domain, domain),
        "engine_name": result.get("engine"),
        "engine_version": result.get("version"),
        "engine_executed": result.get("status") in {"ok", "success"},
        "source_status": payload.get("source_status") or {},
        "official_warning_verification": payload.get("official_warning_verification") or {},
        "narration_validation_passed": bool(valid),
        "validation_problems": list(problems),
        "strict_narration_guard": True,
        "routing": dict(routing or {}),
    }


__all__ = [
    "DOMAIN_PUBLIC_NAMES", "build_engine_narration_payload", "build_engine_narrator_messages",
    "validate_engine_narration", "deterministic_engine_fallback", "build_engine_diagnostics",
    "normalize_source_status",
]
