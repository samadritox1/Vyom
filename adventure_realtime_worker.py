"""Periodic VYOM-ADVENTURE data collection / calibration worker."""
import json,os,time
from adventure_realtime_service import AdventureRealtimeService

def load_locations():
    path=os.getenv("ADVENTURE_WORKER_LOCATIONS","[]")
    try:
        if os.path.exists(path):
            with open(path,"r",encoding="utf-8") as f:return json.load(f)
        obj=json.loads(path)
        return obj if isinstance(obj,list) else obj.get("locations",[])
    except Exception:return []

def main():
    svc=AdventureRealtimeService(); interval=max(900,int(os.getenv("ADVENTURE_WORKER_INTERVAL_SECONDS","3600")))
    while True:
        for loc in load_locations():
            try: svc.collect_live_context(float(loc["lat"]),float(loc["lon"]),loc)
            except Exception as exc: print("Adventure worker location error:",exc)
        try: print(svc.verify_and_calibrate(int(os.getenv("ADVENTURE_CAL_LOOKBACK_DAYS","30"))))
        except Exception as exc: print("Adventure calibration error:",exc)
        time.sleep(interval)

if __name__=="__main__": main()
