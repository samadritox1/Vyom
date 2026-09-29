"""Real observational and authoritative-feed adapters for VYOM-AVIATION.

The adapters are intentionally thin and fail closed: an unavailable feed is
reported as unavailable, never converted into 'no hazard'.
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import requests

AWC_BASE = os.getenv("AWC_API_BASE", "https://aviationweather.gov/api/data")
USER_AGENT = os.getenv("VYOM_AVIATION_USER_AGENT", "VYOM-Aviation/1.0 research-prototype")


def safe_float(v: Any, default=None):
    try:
        if v is None or isinstance(v, bool): return default
        x=float(v)
        return x if math.isfinite(x) else default
    except Exception: return default


def haversine_km(lat1,lon1,lat2,lon2):
    p1,p2=math.radians(lat1),math.radians(lat2); dp=math.radians(lat2-lat1); dl=math.radians(lon2-lon1)
    a=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 6371*2*math.asin(math.sqrt(a))


def _get(path: str, params: Mapping[str,Any], timeout: int=10) -> Tuple[Optional[Any], Dict[str,Any]]:
    url=f"{AWC_BASE.rstrip('/')}/{path.lstrip('/')}"
    t=time.time()
    try:
        r=requests.get(url,params=params,headers={"User-Agent":USER_AGENT,"Accept":"application/json"},timeout=timeout)
        meta={"url":r.url,"status_code":r.status_code,"latency_ms":round((time.time()-t)*1000,1),"source":"NOAA/NWS Aviation Weather Center"}
        if r.status_code==204: return [],meta
        r.raise_for_status()
        return r.json(),meta
    except Exception as exc:
        return None,{"url":url,"error":str(exc),"latency_ms":round((time.time()-t)*1000,1),"source":"NOAA/NWS Aviation Weather Center"}


def _cloud_ceiling_ft(row: Mapping[str,Any]) -> Optional[float]:
    clouds=row.get("clouds") or []
    bases=[]
    if isinstance(clouds,list):
        for c in clouds:
            if not isinstance(c,Mapping): continue
            cover=str(c.get("cover") or c.get("cover3") or "").upper()
            base=safe_float(c.get("base"))
            if cover in {"BKN","OVC","VV"} and base is not None: bases.append(base)
    return min(bases) if bases else None


def normalize_metar(row: Mapping[str,Any]) -> Dict[str,Any]:
    vis=row.get("visib")
    vis_sm=safe_float(vis)
    # AWC JSON visibility is commonly statute miles; retain raw plus normalized metres.
    return {
        "station":row.get("icaoId") or row.get("stationId") or row.get("id"),
        "observed_at":row.get("obsTime") or row.get("reportTime") or row.get("receiptTime"),
        "lat":safe_float(row.get("lat")),"lon":safe_float(row.get("lon")),
        "temperature_c":safe_float(row.get("temp")),"dewpoint_c":safe_float(row.get("dewp")),
        "wind_direction_deg":safe_float(row.get("wdir")),"wind_speed_kt":safe_float(row.get("wspd")),"wind_gust_kt":safe_float(row.get("wgst")),
        "visibility_sm":vis_sm,"visibility_m":None if vis_sm is None else vis_sm*1609.344,
        "altimeter_hpa":safe_float(row.get("altim")),"sea_level_pressure_hpa":safe_float(row.get("slp")),
        "ceiling_ft":_cloud_ceiling_ft(row),"flight_category":row.get("fltCat"),"weather":row.get("wxString"),
        "raw":row.get("rawOb") or row.get("rawText"),"clouds":row.get("clouds") or [],"source":"AWC_METAR",
    }


def fetch_metar(station: str, hours: float=2.0) -> Dict[str,Any]:
    data,meta=_get("metar",{"ids":station.upper(),"format":"json","hours":max(1,min(30,hours))})
    rows=data if isinstance(data,list) else []
    records=[normalize_metar(x) for x in rows if isinstance(x,Mapping)]
    status = "error" if data is None else ("success" if records else "success_none")
    return {"status":status,"meta":meta,"records":records}


def fetch_taf(station: str, hours: int=24) -> Dict[str,Any]:
    data,meta=_get("taf",{"ids":station.upper(),"format":"json"})
    rows=data if isinstance(data,list) else []
    normalized=[]
    for x in rows:
        if not isinstance(x,Mapping): continue
        normalized.append({"station":x.get("icaoId") or x.get("stationId") or station.upper(),"issued_at":x.get("issueTime"),"valid_from":x.get("validTimeFrom"),"valid_to":x.get("validTimeTo"),"raw":x.get("rawTAF") or x.get("rawText"),"fcsts":x.get("fcsts") or x.get("forecast") or [],"source":"AWC_TAF","original":x})
    status = "error" if data is None else ("success" if normalized else "success_none")
    return {"status":status,"meta":meta,"records":normalized}


def fetch_pireps(station: str, distance_nm: int=150, age_hours: float=3.0) -> Dict[str,Any]:
    data,meta=_get("pirep",{"id":station.upper(),"distance":max(10,min(500,int(distance_nm))),"age":max(0.5,min(24,float(age_hours))),"format":"json"})
    rows=data if isinstance(data,list) else []
    normalized=[]
    for x in rows:
        if not isinstance(x,Mapping): continue
        normalized.append({"report_time":x.get("reportTime") or x.get("obsTime"),"lat":safe_float(x.get("lat")),"lon":safe_float(x.get("lon")),"altitude_ft":safe_float(x.get("fltLvl")) or safe_float(x.get("altitude")),"aircraft_type":x.get("acType"),"turbulence":x.get("turb") or x.get("turbulence"),"icing":x.get("icg") or x.get("icing"),"temperature_c":safe_float(x.get("temp")),"raw":x.get("rawOb") or x.get("rawText"),"source":"AWC_PIREP","original":x})
    status = "error" if data is None else ("success" if normalized else "success_none")
    return {"status":status,"meta":meta,"records":normalized}


def _extract_polygon_coords(row: Mapping[str,Any]) -> List[Tuple[float,float]]:
    coords=[]
    # GeoJSON-like input
    geom=row.get("geometry")
    if isinstance(geom,Mapping):
        c=geom.get("coordinates")
        if isinstance(c,list):
            def walk(v):
                if isinstance(v,list) and len(v)>=2 and all(isinstance(q,(int,float)) for q in v[:2]): coords.append((float(v[1]),float(v[0])))
                elif isinstance(v,list):
                    for q in v: walk(q)
            walk(c)
    # AWC JSON may provide coords arrays under different keys.
    for key in ("coords","polygon","points"):
        c=row.get(key)
        if isinstance(c,list):
            for q in c:
                if isinstance(q,Mapping) and safe_float(q.get("lat")) is not None and safe_float(q.get("lon")) is not None: coords.append((float(q["lat"]),float(q["lon"])))
                elif isinstance(q,(list,tuple)) and len(q)>=2:
                    a,b=safe_float(q[0]),safe_float(q[1])
                    if a is not None and b is not None: coords.append((b,a))
    return coords


def _near_polygon(lat: float,lon: float,row: Mapping[str,Any],radius_km: float) -> bool:
    coords=_extract_polygon_coords(row)
    if not coords: return True  # keep if geometry cannot be decoded; engine will show source/uncertainty
    return min(haversine_km(lat,lon,a,b) for a,b in coords)<=radius_km


def fetch_sigmet_context(lat: float, lon: float, radius_km: float=900.0) -> Dict[str,Any]:
    records=[]; metas=[]
    # International SIGMET is global. airsigmet provides U.S. SIGMET/AIRMET-like products.
    for path in ("isigmet","airsigmet"):
        data,meta=_get(path,{"format":"json"})
        metas.append(meta)
        if not isinstance(data,list): continue
        for x in data:
            if isinstance(x,Mapping) and _near_polygon(lat,lon,x,radius_km):
                records.append({"product":path,"hazard":x.get("hazard") or x.get("phenom") or x.get("hazardType"),"valid_from":x.get("validTimeFrom") or x.get("validTime"),"valid_to":x.get("validTimeTo"),"raw":x.get("rawSigmet") or x.get("rawText"),"geometry":x.get("geometry"),"source":"AWC_SIGMET","original":x})
    successful = any((m.get("status_code") or 0) in range(200, 300) for m in metas if isinstance(m, Mapping))
    errored = all("error" in m for m in metas if isinstance(m, Mapping)) if metas else True
    if records:
        status = "success"
    elif successful:
        status = "success_none"
    elif errored:
        status = "error"
    else:
        status = "degraded"
    return {"status":status,"meta":metas,"records":records}


def fetch_station_info(station: str) -> Dict[str,Any]:
    data,meta=_get("stationinfo",{"ids":station.upper(),"format":"json"})
    rows=data if isinstance(data,list) else ([] if data is None else [data])
    status = "error" if data is None else ("success" if rows else "success_none")
    return {"status":status,"meta":meta,"records":rows}


def fetch_airport_info(station: str) -> Dict[str,Any]:
    data,meta=_get("airport",{"ids":station.upper(),"format":"json"})
    if data is None:
        # stationinfo provides a useful fallback even when airport schema differs.
        return fetch_station_info(station)
    rows=data if isinstance(data,list) else [data]
    status = "success" if rows else "success_none"
    return {"status":status,"meta":meta,"records":rows}


class AviationWeatherConnector:
    def metar(self, station: str): return fetch_metar(station)
    def taf(self, station: str): return fetch_taf(station)
    def pireps(self, station: str, distance_nm: int=150): return fetch_pireps(station,distance_nm)
    def sigmets(self, lat: float, lon: float, radius_km: float=900): return fetch_sigmet_context(lat,lon,radius_km)
    def airport(self, station: str): return fetch_airport_info(station)


__all__=["AviationWeatherConnector","fetch_metar","fetch_taf","fetch_pireps","fetch_sigmet_context","fetch_station_info","fetch_airport_info","normalize_metar"]
