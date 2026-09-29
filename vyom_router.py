from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional


PERSONA_DOMAIN = {
    "Farmer": "agro",
    "Marine/Fisherman": "marine",
    "Aviation": "aviation",
    "Adventurer": "adventure",
    "Disaster Management": "disaster",
    "Smart City Planner": "general",
    "General Citizen": "general",
}

DOMAIN_PERSONA = {
    "agro": "Farmer",
    "marine": "Marine/Fisherman",
    "aviation": "Aviation",
    "adventure": "Adventurer",
    "disaster": "Disaster Management",
    "general": "General Citizen",
}

# Deliberately conservative. Explicit persona always wins. Keyword routing is used
# only when the user has General Citizen / blank persona, so it cannot silently
# override a domain the user selected in the UI.
KEYWORDS = {
    "agro": [
        r"\bcrop\b", r"\bfarm(?:er|ing)?\b", r"\birrigat", r"\bsoil moisture\b", r"\bpaddy\b",
        r"\brice field\b", r"\bwheat\b", r"\bmaize\b", r"\bharvest\b", r"\bplanting\b",
    ],
    "marine": [
        r"\bvessel\b", r"\bship\b", r"\bfishing boat\b", r"\bsea state\b", r"\bswell\b",
        r"\bwave height\b", r"\bais\b", r"\btcpa\b", r"\bdcpa\b", r"\bunder[- ]keel\b",
        r"\bmarine\b", r"\bocean route\b", r"\bport warning\b",
    ],
    "aviation": [
        r"\bflight\b", r"\baircraft\b", r"\bairport\b", r"\bmetar\b", r"\btaf\b", r"\bsigmet\b",
        r"\bicing\b", r"\bturbulence\b", r"\bcrosswind\b", r"\brunway\b", r"\bfly(?:ing)?\b",
    ],
    "adventure": [
        r"\btrek(?:king)?\b", r"\bhik(?:e|ing)\b", r"\bcamping\b", r"\bmountaineer", r"\bclimb(?:ing)?\b",
        r"\btrail\b", r"\bhigh altitude\b", r"\bavalanche\b", r"\briver crossing\b", r"\boutdoor expedition\b",
    ],
    "disaster": [
        r"\bdisaster\b", r"\bemergency management\b", r"\bevacuat", r"\bflood risk\b", r"\bflash flood\b",
        r"\blandslide\b", r"\btsunami\b", r"\bearthquake\b", r"\bcyclone impact\b", r"\bwildfire\b",
    ],
}

ROUTE_PATTERNS = [
    r"\bfrom\b.+\bto\b",
    r"\broute\b.+\bto\b",
    r"\bcorridor\b.+\bto\b",
]


def _keyword_scores(query: str) -> Dict[str, int]:
    q = (query or "").lower()
    scores: Dict[str, int] = {k: 0 for k in KEYWORDS}
    for domain, pats in KEYWORDS.items():
        for pat in pats:
            if re.search(pat, q, flags=re.I):
                scores[domain] += 1
    return scores


def route_query(persona: Optional[str], query: str) -> Dict[str, Any]:
    persona = persona or "General Citizen"
    explicit = PERSONA_DOMAIN.get(persona, "general")
    q = query or ""
    route_intent = any(re.search(p, q, flags=re.I) for p in ROUTE_PATTERNS)

    if explicit != "general":
        return {
            "domain": explicit,
            "persona": DOMAIN_PERSONA[explicit],
            "source": "explicit_persona",
            "confidence": 1.0,
            "route_intent": route_intent,
        }

    scores = _keyword_scores(q)
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_domain, top_score = ordered[0] if ordered else ("general", 0)
    second = ordered[1][1] if len(ordered) > 1 else 0
    # Require at least one strong domain cue and no tie. This intentionally
    # favors general-weather handling when the query is ambiguous.
    if top_score > 0 and top_score > second:
        confidence = min(0.95, 0.68 + 0.09 * top_score)
        return {
            "domain": top_domain,
            "persona": DOMAIN_PERSONA[top_domain],
            "source": "deterministic_query_router",
            "confidence": round(confidence, 2),
            "route_intent": route_intent,
            "scores": scores,
        }

    return {
        "domain": "general",
        "persona": persona,
        "source": "general_fallback",
        "confidence": 0.60 if top_score == second and top_score > 0 else 1.0,
        "route_intent": route_intent,
        "scores": scores,
    }


def extract_route_endpoints(query: str):
    """Best-effort deterministic extraction for simple 'from A to B' queries.

    Returns (origin, destination) or None. It intentionally does not guess when
    wording is ambiguous; main.py can then fall back to its structured LLM extractor.
    """
    q = (query or "").strip()
    m = re.search(r"\bfrom\s+(.+?)\s+\bto\s+(.+?)(?:[?.!,;]|$)", q, flags=re.I)
    if not m:
        return None
    origin = m.group(1).strip(" -,:;.")
    destination = m.group(2).strip(" -,:;.")
    # Remove obvious trailing domain-purpose phrases without trying to geocode them.
    destination = re.sub(r"\s+(?:for|with|and tell|and analyze|tomorrow|today)\b.*$", "", destination, flags=re.I).strip()
    if len(origin) < 2 or len(destination) < 2:
        return None
    return origin, destination


def is_specialized(domain: str) -> bool:
    return domain in {"agro", "marine", "aviation", "adventure", "disaster"}


__all__ = ["route_query", "extract_route_endpoints", "is_specialized", "PERSONA_DOMAIN", "DOMAIN_PERSONA"]
