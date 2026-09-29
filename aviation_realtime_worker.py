import os,time,json
from aviation_realtime_service import AviationRealtimeService

def main():
    svc=AviationRealtimeService(); interval=max(300,int(os.getenv('VYOM_AVIATION_WORKER_INTERVAL_SECONDS','1800')))
    locations=[]
    p=os.getenv('VYOM_AVIATION_WORKER_LOCATIONS','[]')
    try:
        if os.path.exists(p):
            locations=json.load(open(p,'r',encoding='utf-8'))
        else:
            obj=json.loads(p)
            locations=obj if isinstance(obj,list) else obj.get('locations',[])
    except Exception: pass
    while True:
        for item in locations:
            try: svc.collect_live_context(float(item['lat']),float(item['lon']),item.get('aircraft') or {})
            except Exception as e: print('aviation worker collect:',e)
        try: print('aviation calibration:',svc.verify_and_calibrate())
        except Exception as e: print('aviation worker calibrate:',e)
        time.sleep(interval)
if __name__=='__main__':main()
