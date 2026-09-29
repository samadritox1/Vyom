"""Realtime orchestration and continuous verification for VYOM-ADVENTURE."""
from __future__ import annotations
import os,time
from datetime import datetime,timezone
from typing import Any,Dict,Mapping,Optional
from adventure_connectors import OpenMeteoEnvironmentConnector,NASAFIRMSConnector,NWSAlertsConnector,ConfiguredOfficialFeedConnector,ConfiguredAvalancheBulletinConnector
from adventure_data_fabric import AdventureDataFabric

class AdventureRealtimeService:
    def __init__(self):
        self.fabric=AdventureDataFabric()
        self.env=OpenMeteoEnvironmentConnector(); self.firms=NASAFIRMSConnector()
        self.nws=NWSAlertsConnector(); self.configured=ConfiguredOfficialFeedConnector(); self.avalanche=ConfiguredAvalancheBulletinConnector()
        self.calibration_path=os.getenv("VYOM_ADVENTURE_DYNAMIC_CALIBRATION","data/adventure/calibration/dynamic_calibration.json")

    def _merge(self,out:Dict[str,Any],result:Dict[str,Any],source_name:str):
        patch=result.get("patch") or {}; outpatch=out.setdefault("patch",{})
        for k,v in patch.items():
            if isinstance(v,list): outpatch.setdefault(k,[]); outpatch[k].extend(v)
            elif isinstance(v,dict):
                if k not in outpatch: outpatch[k]=v
                elif isinstance(outpatch[k],dict): outpatch[k].update(v)
            elif outpatch.get(k) is None: outpatch[k]=v
        out.setdefault("sources",[]).extend(result.get("sources") or [])
        status="ok" if any(s.get("status")=="ok" for s in result.get("sources") or []) else "degraded"
        self.fabric.log_source_status(source_name,status,result.get("elapsed_ms",0),sum(int(s.get("records",0) or 0) for s in result.get("sources") or []),"")

    def collect_live_context(self,lat:float,lon:float,context:Optional[Mapping[str,Any]]=None)->Dict[str,Any]:
        started=time.time(); out={"patch":{},"sources":[],"status":"ok"}
        for conn in (self.env,self.firms,self.configured,self.avalanche):
            try:self._merge(out,conn.fetch(lat,lon),conn.name)
            except Exception as exc:
                out["sources"].append({"source":conn.name,"status":"error","error":str(exc)}); self.fabric.log_source_status(conn.name,"error",error=str(exc))
        # NWS only when deployment requests it or the context says US. Avoid pretending global authority.
        c=dict(context or {})
        if os.getenv("ADVENTURE_ENABLE_NWS","0")=="1" or str(c.get("country_code","")).upper()=="US":
            try:self._merge(out,self.nws.fetch(lat,lon),self.nws.name)
            except Exception as exc: out["sources"].append({"source":"US NWS","status":"error","error":str(exc)})
        if any(s.get("status")=="error" for s in out["sources"]): out["status"]="degraded"
        out["elapsed_ms"]=round((time.time()-started)*1000,1)
        out["dynamic_calibration_path"]=self.calibration_path if os.path.exists(self.calibration_path) else None
        return out

    def record_model_forecast(self,lat:float,lon:float,fused,members):
        now=datetime.now(timezone.utc)
        vars=("temperature_2m","relative_humidity_2m","precipitation","wind_speed_10m","wind_gusts_10m","visibility","snowfall","snow_depth")
        count=0
        for m in members:
            for row in m.hours[:120]:
                lead=int(row.get("lead_hours",0)); valid=row.get("time")
                for var in vars:
                    val=row.get(var)
                    if val is None: continue
                    self.fabric.add_forecast(m.model,now.isoformat(),str(valid),lead,lat,lon,var,float(val),quality=m.quality)
                    count+=1
        return count

    def verify_and_calibrate(self,lookback_days:int=30):
        matches=self.fabric.match_forecasts(lookback_days=lookback_days)
        cells=self.fabric.recompute_calibration(min_samples=int(os.getenv("ADVENTURE_CAL_MIN_SAMPLES","5")))
        path=self.fabric.export_calibration_json(self.calibration_path)
        return {"status":"ok","matches_created":matches,"calibration_cells":cells,"calibration_path":path}

    def health(self): return self.fabric.health()

__all__=["AdventureRealtimeService"]
