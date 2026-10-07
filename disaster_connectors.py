"""Real/authoritative data connectors for VYOM-RAKSHA.

Connectors normalize external information into a common disaster-context shape.
Unavailable or unconfigured sources return explicit status and never fabricate an
'all clear'. Official source terms/licences and rate limits remain the deployer's
responsibility.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

import requests
from cap_policy import parse_active_cap
from alert_policy import utc_time

UA = {"User-Agent": os.getenv("VYOM_USER_AGENT", "VYOM-Raksha/1.0 contact=admin@example.invalid")}


def safe_float(v, default=None):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 6371.0 * 2.0 * math.asin(math.sqrt(a))


class USGSEarthquakeConnector:
    name = "usgs_earthquake"

    def fetch(self, lat: float, lon: float, radius_km: float = 700.0) -> Dict[str, Any]:
        started = time.time()
        feed = os.getenv("USGS_EARTHQUAKE_FEED", "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson")
        try:
            r = requests.get(feed, headers=UA, timeout=12); r.raise_for_status()
            events = []
            for f in (r.json().get("features") or []):
                p = f.get("properties") or {}; g = f.get("geometry") or {}; c = g.get("coordinates") or []
                if len(c) < 2: continue
                elo, ela = safe_float(c[0]), safe_float(c[1]); mag = safe_float(p.get("mag"))
                if None in (elo, ela, mag): continue
                d = haversine_km(lat, lon, ela, elo)
                if d > radius_km: continue
                t_ms = safe_float(p.get("time")); t = None
                if t_ms is not None:
                    t = datetime.fromtimestamp(t_ms/1000.0, tz=timezone.utc).isoformat()
                events.append({
                    "id": f.get("id"), "lat": ela, "lon": elo, "magnitude": mag,
                    "distance_km": round(d, 1), "depth_km": safe_float(c[2]) if len(c)>2 else None,
                    "time": t, "place": p.get("place"), "tsunami": p.get("tsunami"),
                    "alert": p.get("alert"), "status": p.get("status"), "source": "USGS",
                    "detail_url": p.get("detail"),
                })
            events.sort(key=lambda x: (x["magnitude"], -x["distance_km"]), reverse=True)
            return {"patch": {"earthquake_events": events[:40]}, "sources": [{"source":"USGS Earthquake","status":"ok","records":len(events)}], "elapsed_ms":round((time.time()-started)*1000,1)}
        except Exception as exc:
            return {"patch": {}, "sources": [{"source":"USGS Earthquake","status":"error","error":str(exc)}], "elapsed_ms":round((time.time()-started)*1000,1)}


class GDACSConnector:
    name = "gdacs"

    def fetch(self, lat: float, lon: float, days: int = 10, radius_km: float = 1000.0) -> Dict[str, Any]:
        started = time.time()
        today = datetime.now(timezone.utc).date(); start = today - timedelta(days=max(1, days))
        url = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
        params = {
            "eventlist": "EQ;TC;FL;VO;DR;WF",
            "fromdate": start.isoformat(), "todate": today.isoformat(),
            "alertlevel": "red;orange;green",
        }
        try:
            r = requests.get(url, params=params, headers=UA, timeout=15); r.raise_for_status(); obj = r.json()
            # API responses have changed shape over time; recursively inspect common feature/list containers.
            candidates = []
            if isinstance(obj, list): candidates = obj
            elif isinstance(obj, dict):
                for key in ("features", "data", "events", "eventlist"):
                    if isinstance(obj.get(key), list): candidates = obj[key]; break
            events=[]
            for item in candidates:
                props = item.get("properties", item) if isinstance(item, dict) else {}
                geom = item.get("geometry", {}) if isinstance(item, dict) else {}
                coords = geom.get("coordinates") if isinstance(geom, dict) else None
                ela = safe_float(props.get("latitude") or props.get("lat")); elo = safe_float(props.get("longitude") or props.get("lon"))
                if (ela is None or elo is None) and isinstance(coords, list) and len(coords)>=2 and not isinstance(coords[0], list):
                    elo, ela = safe_float(coords[0]), safe_float(coords[1])
                d = haversine_km(lat, lon, ela, elo) if ela is not None and elo is not None else None
                if d is not None and d > radius_km: continue
                events.append({
                    "event_type": props.get("eventtype") or props.get("eventType") or props.get("type"),
                    "event_id": props.get("eventid") or props.get("eventId") or props.get("id"),
                    "name": props.get("name") or props.get("eventname") or props.get("title"),
                    "alertlevel": props.get("alertlevel") or props.get("alertLevel"),
                    "severity": props.get("severity"), "fromdate": props.get("fromdate") or props.get("fromDate"),
                    "todate": props.get("todate") or props.get("toDate"), "lat": ela, "lon": elo,
                    "distance_km": round(d,1) if d is not None else None, "source":"GDACS",
                })
            return {"patch":{"gdacs_events":events},"sources":[{"source":"GDACS","status":"ok","records":len(events)}],"elapsed_ms":round((time.time()-started)*1000,1)}
        except Exception as exc:
            return {"patch":{},"sources":[{"source":"GDACS","status":"error","error":str(exc)}],"elapsed_ms":round((time.time()-started)*1000,1)}


class OpenMeteoHazardConnector:
    name = "open_meteo_hazard_context"

    def fetch(self, lat: float, lon: float) -> Dict[str, Any]:
        started=time.time(); patch={}; sources=[]
        try:
            params={"latitude":lat,"longitude":lon,"daily":"river_discharge,river_discharge_mean,river_discharge_median,river_discharge_max,river_discharge_min,river_discharge_25th_percentile,river_discharge_75th_percentile","forecast_days":7,"timezone":"UTC"}
            r=requests.get("https://flood-api.open-meteo.com/v1/flood",params=params,headers=UA,timeout=12); r.raise_for_status()
            d=r.json().get("daily") or {}
            def first(k):
                a=d.get(k) or []; return safe_float(a[0]) if a else None
            patch["flood_context"]={
                "river_discharge":first("river_discharge"),"river_discharge_mean":first("river_discharge_mean"),
                "river_discharge_median":first("river_discharge_median"),"river_discharge_max":first("river_discharge_max"),
                "river_discharge_min":first("river_discharge_min"),"river_discharge_25th_percentile":first("river_discharge_25th_percentile"),
                "river_discharge_75th_percentile":first("river_discharge_75th_percentile"),
                "source":"Open-Meteo/GloFAS","resolution_note":"~5 km GloFAS context; not a local gauge/hydraulic model",
            }
            sources.append({"source":"Open-Meteo/GloFAS","status":"ok","records":1})
        except Exception as exc:
            sources.append({"source":"Open-Meteo/GloFAS","status":"error","error":str(exc)})
        try:
            params={"latitude":lat,"longitude":lon,"current":"pm2_5,pm10,us_aqi,dust","timezone":"UTC"}
            r=requests.get("https://air-quality-api.open-meteo.com/v1/air-quality",params=params,headers=UA,timeout=10); r.raise_for_status()
            c=r.json().get("current") or {}
            for k in ("pm2_5","pm10","us_aqi","dust"):
                if safe_float(c.get(k)) is not None: patch[k]=safe_float(c.get(k))
            sources.append({"source":"Open-Meteo Air Quality","status":"ok","records":1})
        except Exception as exc:
            sources.append({"source":"Open-Meteo Air Quality","status":"error","error":str(exc)})
        return {"patch":patch,"sources":sources,"elapsed_ms":round((time.time()-started)*1000,1)}


class NASAFIRMSConnector:
    name="nasa_firms"
    def __init__(self,map_key:Optional[str]=None): self.map_key=map_key or os.getenv("NASA_FIRMS_MAP_KEY")
    def fetch(self,lat:float,lon:float,radius_km:float=150.0)->Dict[str,Any]:
        if not self.map_key:
            return {"patch":{},"sources":[{"source":"NASA FIRMS","status":"unconfigured","requires":"NASA_FIRMS_MAP_KEY"}]}
        dlat=radius_km/111.0; dlon=radius_km/max(20.0,111.0*math.cos(math.radians(lat)))
        bbox=f"{lon-dlon},{lat-dlat},{lon+dlon},{lat+dlat}"
        sensor=os.getenv("NASA_FIRMS_SOURCE","VIIRS_NOAA21_NRT")
        url=f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{self.map_key}/{sensor}/{bbox}/1"
        started=time.time()
        try:
            r=requests.get(url,timeout=15,headers=UA); r.raise_for_status(); rows=list(csv.DictReader(io.StringIO(r.text))); fires=[]
            for x in rows:
                la=safe_float(x.get("latitude")); lo=safe_float(x.get("longitude"))
                if la is None or lo is None: continue
                d=haversine_km(lat,lon,la,lo)
                if d<=radius_km:
                    fires.append({"lat":la,"lon":lo,"distance_km":d,"frp":safe_float(x.get("frp"),0),"confidence":x.get("confidence"),"acq_date":x.get("acq_date"),"acq_time":x.get("acq_time"),"satellite":x.get("satellite"),"source":"NASA FIRMS"})
            fires.sort(key=lambda x:x["distance_km"])
            return {"patch":{"fire_detections":fires[:150]},"sources":[{"source":"NASA FIRMS","status":"ok","records":len(fires),"satellite_detection_only":True}],"elapsed_ms":round((time.time()-started)*1000,1)}
        except Exception as exc:
            return {"patch":{},"sources":[{"source":"NASA FIRMS","status":"error","error":str(exc)}],"elapsed_ms":round((time.time()-started)*1000,1)}


class IMDIndiaConnector:
    name="imd_india"
    def fetch(self,lat:float,lon:float)->Dict[str,Any]:
        started=time.time(); patch={}; sources=[]
        # Public IMD API endpoints documented by IMD. Do not infer a cyclone when no official track exists.
        try:
            r=requests.get("https://api.imd.gov.in/api/v1/cyclone_track",headers=UA,timeout=12); r.raise_for_status(); obj=r.json(); pts=[]
            data=obj.get("data") or {}
            for kind in ("observed","forecast"):
                for p in data.get(kind) or []:
                    row=dict(p); row["track_kind"]=kind; row["source"]="IMD"; pts.append(row)
            patch["cyclone_context"]=pts
            sources.append({"source":"IMD Cyclone Track","status":"ok","records":len(pts)})
        except Exception as exc:
            sources.append({"source":"IMD Cyclone Track","status":"error","error":str(exc)})
        try:
            r=requests.get("https://api.imd.gov.in/api/v1/cyclone_wind",headers=UA,timeout=12); r.raise_for_status(); obj=r.json()
            patch["imd_cyclone_wind"] = obj
            sources.append({"source":"IMD Cyclone Wind","status":"ok"})
        except Exception as exc:
            sources.append({"source":"IMD Cyclone Wind","status":"error","error":str(exc)})
        return {"patch":patch,"sources":sources,"elapsed_ms":round((time.time()-started)*1000,1)}


class NWSAlertsConnector:
    name="nws_alerts"
    def fetch(self,lat:float,lon:float)->Dict[str,Any]:
        started=time.time()
        try:
            r=requests.get("https://api.weather.gov/alerts/active",params={"point":f"{lat},{lon}"},headers={**UA,"Accept":"application/geo+json"},timeout=12); r.raise_for_status(); warnings=[]
            invalid = 0
            now = datetime.now(timezone.utc)
            for f in r.json().get("features",[]):
                p=f.get("properties") or {}
                if not p.get("status") or not p.get("messageType"):
                    invalid += 1
                    continue
                if p["status"] != "Actual" or p["messageType"] not in {"Alert", "Update"}:
                    continue
                effective, expires = utc_time(p.get("effective") or p.get("sent")), utc_time(p.get("expires"))
                identifier = p.get("id") or f.get("id")
                if not identifier or effective is None or expires is None or expires <= effective:
                    invalid += 1
                    continue
                if not effective <= now < expires:
                    continue
                warnings.append({"source":"US NWS","identifier":identifier,"sent":p.get("sent"),"event":p.get("event"),"severity":p.get("severity"),"certainty":p.get("certainty"),"urgency":p.get("urgency"),"headline":p.get("headline"),"description":p.get("description"),"instruction":p.get("instruction"),"effective":effective.isoformat(),"expires":expires.isoformat(),"verified_for_location":True})
            return {"patch":{"official_warnings":warnings},"sources":[{"source":"US NWS","status":"degraded" if invalid else "ok","records":len(warnings),"invalid_records":invalid}],"elapsed_ms":round((time.time()-started)*1000,1)}
        except Exception as exc:
            return {"patch":{},"sources":[{"source":"US NWS","status":"error","error":str(exc)}],"elapsed_ms":round((time.time()-started)*1000,1)}


class CAPFeedConnector:
    """Generic OASIS CAP 1.2 XML consumer for a configured official warning feed."""
    name="cap_feed"
    def __init__(self,url:Optional[str]=None): self.url=url or os.getenv("DISASTER_CAP_FEED_URL")
    @staticmethod
    def _text(el, name):
        for ch in list(el):
            if ch.tag.split("}")[-1] == name: return ch.text
        return None
    def fetch(self,lat:float,lon:float)->Dict[str,Any]:
        if not self.url: return {"patch":{},"sources":[{"source":"Configured CAP feed","status":"unconfigured","requires":"DISASTER_CAP_FEED_URL"}]}
        started=time.time()
        try:
            r=requests.get(self.url,headers=UA,timeout=15); r.raise_for_status()
            alerts, invalid = parse_active_cap(r.content, lat, lon)
            return {"patch":{"official_warnings":alerts},"sources":[{"source":"Configured CAP feed","status":"degraded" if invalid else "ok","records":len(alerts),"invalid_records":invalid}],"elapsed_ms":round((time.time()-started)*1000,1)}
        except Exception as exc:
            return {"patch":{},"sources":[{"source":"Configured CAP feed","status":"error","error":str(exc)}],"elapsed_ms":round((time.time()-started)*1000,1)}


class ConfiguredTsunamiWarningConnector:
    """Adapter for a deployment-provided official tsunami JSON endpoint.

    INCOIS ITEWC is authoritative for India, but this module does not scrape its
    human-facing website or invent an undocumented API contract. A deployment can
    point this adapter at an authorized/normalized JSON feed.
    """
    name="official_tsunami"
    def __init__(self,url:Optional[str]=None): self.url=url or os.getenv("DISASTER_TSUNAMI_JSON_URL")
    def fetch(self,lat:float,lon:float)->Dict[str,Any]:
        if not self.url: return {"patch":{},"sources":[{"source":"Official tsunami feed","status":"unconfigured","requires":"DISASTER_TSUNAMI_JSON_URL"}]}
        try:
            r=requests.get(self.url,headers=UA,timeout=12); r.raise_for_status(); obj=r.json()
            warnings=obj if isinstance(obj,list) else obj.get("warnings",[]) if isinstance(obj,dict) else []
            return {"patch":{"official_tsunami_warnings":warnings},"sources":[{"source":"Official tsunami feed","status":"ok","records":len(warnings)}]}
        except Exception as exc:
            return {"patch":{},"sources":[{"source":"Official tsunami feed","status":"error","error":str(exc)}]}


class ConfiguredLocalImpactConnector:
    name="local_impact_context"
    def __init__(self,url:Optional[str]=None): self.url=url or os.getenv("DISASTER_LOCAL_CONTEXT_JSON_URL")
    def fetch(self,lat:float,lon:float)->Dict[str,Any]:
        if not self.url: return {"patch":{},"sources":[{"source":"Local disaster context","status":"unconfigured"}]}
        try:
            r=requests.get(self.url,params={"lat":lat,"lon":lon},headers=UA,timeout=12); r.raise_for_status(); obj=r.json()
            patch=obj.get("context",obj) if isinstance(obj,dict) else {}
            return {"patch":patch,"sources":[{"source":"Local disaster context","status":"ok"}]}
        except Exception as exc:
            return {"patch":{},"sources":[{"source":"Local disaster context","status":"error","error":str(exc)}]}


class ConfiguredObservationConnector:
    """Normalized local observation feed used for continuous verification.

    Expected JSON is either a list or {"observations": [...]} with items:
    source, station_id, lat, lon, observed_at, variable, value, unit, quality.
    """
    name="configured_observations"
    def __init__(self,url:Optional[str]=None): self.url=url or os.getenv("DISASTER_OBSERVATION_JSON_URL")
    def fetch(self,lat:float,lon:float)->Dict[str,Any]:
        if not self.url: return {"patch":{},"observations":[],"sources":[{"source":"Configured observations","status":"unconfigured"}]}
        started=time.time()
        try:
            r=requests.get(self.url,params={"lat":lat,"lon":lon},headers=UA,timeout=12); r.raise_for_status(); obj=r.json()
            obs=obj if isinstance(obj,list) else obj.get("observations",[]) if isinstance(obj,dict) else []
            clean=[]
            for o in obs:
                if not isinstance(o,dict): continue
                if safe_float(o.get("lat")) is None or safe_float(o.get("lon")) is None or safe_float(o.get("value")) is None: continue
                if not o.get("observed_at") or not o.get("variable"): continue
                clean.append(o)
            return {"patch":{},"observations":clean,"sources":[{"source":"Configured observations","status":"ok","records":len(clean)}],"elapsed_ms":round((time.time()-started)*1000,1)}
        except Exception as exc:
            return {"patch":{},"observations":[],"sources":[{"source":"Configured observations","status":"error","error":str(exc)}],"elapsed_ms":round((time.time()-started)*1000,1)}


__all__=[
    "USGSEarthquakeConnector","GDACSConnector","OpenMeteoHazardConnector","NASAFIRMSConnector",
    "IMDIndiaConnector","NWSAlertsConnector","CAPFeedConnector","ConfiguredTsunamiWarningConnector",
    "ConfiguredLocalImpactConnector","ConfiguredObservationConnector",
]
