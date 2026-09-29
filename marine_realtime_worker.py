"""Long-running VYOM-MARINE real-feed ingestion/calibration worker.

Run separately in production:
    python marine_realtime_worker.py

It does not need to run for the API to work; request-time collection remains
available. The worker improves calibration by continuously ingesting configured
observation stations and recomputing verification statistics.
"""
from __future__ import annotations
import json, os, time, traceback
from marine_realtime_service import MarineRealtimeService

INTERVAL=max(300,int(os.getenv('MARINE_WORKER_INTERVAL_SEC','3600')))
CAL_EVERY=max(1,int(os.getenv('MARINE_CALIBRATE_EVERY_CYCLES','6')))
LOCATIONS_ENV=os.getenv('MARINE_WORKER_LOCATIONS_JSON','[]')

def locations():
    try:
        if os.path.exists(LOCATIONS_ENV):
            with open(LOCATIONS_ENV,'r',encoding='utf-8') as f:return json.load(f)
        return json.loads(LOCATIONS_ENV)
    except Exception:return []

def main():
    svc=MarineRealtimeService(); cycle=0
    print('VYOM-MARINE realtime worker started')
    while True:
        cycle+=1
        try:
            locs=locations()
            if not locs:
                # Configured station observation feeds can still be harvested around nominal global point;
                # station max-distance filters should be set generously in the station config.
                locs=[{'lat':15.0,'lon':85.0,'name':'default-indian-ocean'}]
            for loc in locs:
                lat=float(loc['lat']);lon=float(loc['lon'])
                summary=svc.collect_live_context(lat,lon,{})
                print('collect',loc.get('name',f'{lat},{lon}'),summary['fabric_stats'])
            if cycle%CAL_EVERY==0:
                print('calibration',svc.verify_and_calibrate(lookback_days=int(os.getenv('CAL_MATCH_LOOKBACK_DAYS','10'))))
        except KeyboardInterrupt:break
        except Exception:
            traceback.print_exc()
        time.sleep(INTERVAL)

if __name__=='__main__':main()
