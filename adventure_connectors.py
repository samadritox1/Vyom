"""Real-data connectors for VYOM-ADVENTURE.

Connectors return normalized context only. Missing credentials/data produce an
explicit degraded/unavailable result; they never fabricate official warnings.
"""
from __future__ import annotations
import csv, io, json, math, os, time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional
import requests

UA={"User-Agent":os.getenv("VYOM_USER_AGENT","VYOM-Adventure/1.0 contact=admin@example.invalid")}

def safe_float(v,default=None):
    try:
        x=float(v); return x if math.isfinite(x) else default
    except Exception:return default

def haversine_km(a,b,c,d):
    from math import radians,sin,cos,asin,sqrt
    p1,p2=radians(a),radians(c); dp=radians(c-a); dl=radians(d-b)
    x=sin(dp/2)**2+cos(p1)*cos(p2)*sin(dl/2)**2
    return 12742*asin(sqrt(x))

class OpenMeteoEnvironmentConnector:
    name="open_meteo_environment"
    def fetch(self,lat:float,lon:float)->Dict[str,Any]:
        started=time.time(); patch={}; sources=[]
        # Air-quality context
        try:
            params={"latitude":lat,"longitude":lon,"current":"pm2_5,pm10,us_aqi,dust,uv_index","timezone":"UTC"}
            r=requests.get("https://air-quality-api.open-meteo.com/v1/air-quality",params=params,timeout=8,headers=UA); r.raise_for_status()
            cur=(r.json().get("current") or {})
            for k in ("pm2_5","pm10","us_aqi","dust","uv_index"):
                if safe_float(cur.get(k)) is not None: patch[k]=safe_float(cur.get(k))
            sources.append({"source":"Open-Meteo Air Quality","status":"ok","observed_or_forecast":"forecast/current-analysis"})
        except Exception as exc:
            sources.append({"source":"Open-Meteo Air Quality","status":"error","error":str(exc)})
        # Global flood model context (GloFAS). This is coarse model context, not a local crossing gauge.
        try:
            params={"latitude":lat,"longitude":lon,"daily":"river_discharge,river_discharge_mean,river_discharge_median,river_discharge_max,river_discharge_min,river_discharge_25th_percentile,river_discharge_75th_percentile","forecast_days":3,"timezone":"UTC"}
            r=requests.get("https://flood-api.open-meteo.com/v1/flood",params=params,timeout=10,headers=UA); r.raise_for_status()
            d=r.json().get("daily") or {}
            def first(name):
                arr=d.get(name) or []; return safe_float(arr[0]) if arr else None
            patch["flood_context"]={
                "river_discharge":first("river_discharge"),"river_discharge_mean":first("river_discharge_mean"),
                "river_discharge_median":first("river_discharge_median"),"river_discharge_max":first("river_discharge_max"),
                "river_discharge_min":first("river_discharge_min"),"river_discharge_25th_percentile":first("river_discharge_25th_percentile"),
                "river_discharge_75th_percentile":first("river_discharge_75th_percentile"),
                "source":"Open-Meteo/GloFAS","navigation_or_crossing_grade":False,
            }
            sources.append({"source":"Open-Meteo/GloFAS","status":"ok","resolution_note":"~5 km model; not a local crossing gauge"})
        except Exception as exc:
            sources.append({"source":"Open-Meteo/GloFAS","status":"error","error":str(exc)})
        return {"patch":patch,"sources":sources,"elapsed_ms":round((time.time()-started)*1000,1)}

class NASAFIRMSConnector:
    name="nasa_firms"
    def __init__(self,map_key:Optional[str]=None): self.map_key=map_key or os.getenv("NASA_FIRMS_MAP_KEY")
    def fetch(self,lat:float,lon:float,radius_km:float=100.0)->Dict[str,Any]:
        if not self.map_key:
            return {"patch":{},"sources":[{"source":"NASA FIRMS","status":"unconfigured","requires":"NASA_FIRMS_MAP_KEY"}]}
        # Approximate bounding box; satellite detections are contextual, not evacuation guidance.
        dlat=radius_km/111.0; dlon=radius_km/max(20.0,111.0*math.cos(math.radians(lat)))
        bbox=f"{lon-dlon},{lat-dlat},{lon+dlon},{lat+dlat}"
        url=f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{self.map_key}/VIIRS_NOAA21_NRT/{bbox}/1"
        started=time.time()
        try:
            r=requests.get(url,timeout=12,headers=UA); r.raise_for_status()
            rows=list(csv.DictReader(io.StringIO(r.text)))
            fires=[]
            for x in rows:
                la=safe_float(x.get("latitude")); lo=safe_float(x.get("longitude"))
                if la is None or lo is None: continue
                dist=haversine_km(lat,lon,la,lo)
                if dist<=radius_km:
                    fires.append({"lat":la,"lon":lo,"distance_km":dist,"frp":safe_float(x.get("frp"),0),
                                  "confidence":x.get("confidence"),"acq_date":x.get("acq_date"),"acq_time":x.get("acq_time"),
                                  "satellite":x.get("satellite"),"source":"NASA FIRMS"})
            fires.sort(key=lambda f:f["distance_km"])
            return {"patch":{"fire_detections":fires[:100]},"sources":[{"source":"NASA FIRMS","status":"ok","records":len(fires),"satellite_detection_only":True}],"elapsed_ms":round((time.time()-started)*1000,1)}
        except Exception as exc:
            return {"patch":{},"sources":[{"source":"NASA FIRMS","status":"error","error":str(exc)}],"elapsed_ms":round((time.time()-started)*1000,1)}

class NWSAlertsConnector:
    name="nws_alerts"
    def fetch(self,lat:float,lon:float)->Dict[str,Any]:
        """Optional U.S. official alert feed. Non-U.S. deployments should configure a local official source."""
        started=time.time()
        try:
            r=requests.get("https://api.weather.gov/alerts/active",params={"point":f"{lat},{lon}"},headers={**UA,"Accept":"application/geo+json"},timeout=10)
            r.raise_for_status(); data=r.json(); warnings=[]
            for feat in data.get("features",[]):
                p=feat.get("properties") or {}
                warnings.append({"source":"US NWS","event":p.get("event"),"severity":p.get("severity"),"certainty":p.get("certainty"),
                                 "urgency":p.get("urgency"),"headline":p.get("headline"),"effective":p.get("effective"),"expires":p.get("expires")})
            return {"patch":{"official_warnings":warnings},"sources":[{"source":"US NWS","status":"ok","records":len(warnings)}],"elapsed_ms":round((time.time()-started)*1000,1)}
        except Exception as exc:
            return {"patch":{},"sources":[{"source":"US NWS","status":"error","error":str(exc)}],"elapsed_ms":round((time.time()-started)*1000,1)}

class ConfiguredOfficialFeedConnector:
    name="configured_official_feed"
    def fetch(self,lat:float,lon:float)->Dict[str,Any]:
        url=os.getenv("ADVENTURE_OFFICIAL_ALERT_JSON_URL")
        if not url: return {"patch":{},"sources":[{"source":"Configured official alerts","status":"unconfigured"}]}
        try:
            r=requests.get(url,timeout=10,headers=UA); r.raise_for_status(); obj=r.json()
            warnings=obj if isinstance(obj,list) else obj.get("warnings",[]) if isinstance(obj,dict) else []
            return {"patch":{"official_warnings":warnings},"sources":[{"source":"Configured official alerts","status":"ok","records":len(warnings)}]}
        except Exception as exc:
            return {"patch":{},"sources":[{"source":"Configured official alerts","status":"error","error":str(exc)}]}

class ConfiguredAvalancheBulletinConnector:
    name="configured_avalanche_bulletin"
    def fetch(self,lat:float,lon:float)->Dict[str,Any]:
        url=os.getenv("ADVENTURE_AVALANCHE_JSON_URL")
        if not url: return {"patch":{},"sources":[{"source":"Official avalanche bulletin","status":"unconfigured"}]}
        try:
            r=requests.get(url,timeout=10,headers=UA); r.raise_for_status(); obj=r.json()
            # Feed contract: deployment adapter should normalize the relevant local bulletin.
            return {"patch":{"official_avalanche_bulletin":obj},"sources":[{"source":"Official avalanche bulletin","status":"ok"}]}
        except Exception as exc:
            return {"patch":{},"sources":[{"source":"Official avalanche bulletin","status":"error","error":str(exc)}]}

__all__=["OpenMeteoEnvironmentConnector","NASAFIRMSConnector","NWSAlertsConnector","ConfiguredOfficialFeedConnector","ConfiguredAvalancheBulletinConnector"]
