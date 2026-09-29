"""Simple periodic collection/calibration worker for VYOM-RAKSHA."""
from __future__ import annotations
import json, os, time
from pathlib import Path
from disaster_realtime_service import DisasterRealtimeService


def load_locations():
    path=os.getenv("DISASTER_WORKER_LOCATIONS","[]")
    try:
        if os.path.exists(path): obj=json.loads(Path(path).read_text(encoding="utf-8"))
        else: obj=json.loads(path)
        return obj if isinstance(obj,list) else obj.get("locations",[])
    except Exception: return []


def run_once():
    svc=DisasterRealtimeService(); out=[]
    for loc in load_locations():
        try:
            out.append({"name":loc.get("name"),"result":svc.collect_live_context(float(loc["lat"]),float(loc["lon"]),loc),"forecast_collection":svc.collect_forecasts_for_calibration(float(loc["lat"]),float(loc["lon"]),120)})
        except Exception as exc: out.append({"name":loc.get("name"),"error":str(exc)})
    verification=svc.verify_and_calibrate(int(os.getenv("DISASTER_CAL_LOOKBACK_DAYS","30")))
    return {"locations":out,"verification":verification}


def main():
    interval=max(900,int(os.getenv("DISASTER_WORKER_INTERVAL_SECONDS","3600")))
    while True:
        result=run_once(); print(json.dumps(result,indent=2,default=str)); time.sleep(interval)


if __name__=="__main__": main()
