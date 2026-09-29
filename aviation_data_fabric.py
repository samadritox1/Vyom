from __future__ import annotations
import json, math, os, sqlite3, tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

DB_PATH_DEFAULT=os.getenv('VYOM_AVIATION_DB_PATH','aviation_data.db')

def sf(v,d=None):
    try:
        x=float(v); return x if math.isfinite(x) else d
    except Exception:return d

def season(ts:str)->str:
    try:m=datetime.fromisoformat(str(ts).replace('Z','+00:00')).month
    except Exception:m=datetime.now(timezone.utc).month
    return 'DJF' if m in (12,1,2) else 'MAM' if m in (3,4,5) else 'JJA' if m in (6,7,8) else 'SON'

def lead_bin(h:int)->str:
    return '0_6' if h<=6 else '7_24' if h<=24 else '25_72' if h<=72 else '73_120' if h<=120 else '121_plus'

def region(lat,lon): return f"CELL_{int(math.floor(float(lat)/5)*5)}_{int(math.floor(float(lon)/5)*5)}"

class AviationDataFabric:
    def __init__(self,path:Optional[str]=None):
        self.path=path or DB_PATH_DEFAULT
        Path(self.path).parent.mkdir(parents=True,exist_ok=True) if Path(self.path).parent!=Path('.') else None
        self._init()
    def con(self):
        c=sqlite3.connect(self.path,timeout=20); c.row_factory=sqlite3.Row; return c
    def _init(self):
        with self.con() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY, source TEXT, station TEXT, observed_at TEXT, lat REAL, lon REAL, variable TEXT, value REAL, unit TEXT, quality REAL, payload TEXT);
            CREATE TABLE IF NOT EXISTS forecasts(id INTEGER PRIMARY KEY, model TEXT, cycle_time TEXT, valid_time TEXT, lead_hours INTEGER, lat REAL, lon REAL, variable TEXT, value REAL, member TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS matches(id INTEGER PRIMARY KEY, forecast_id INTEGER, observation_id INTEGER, model TEXT, variable TEXT, region TEXT, season TEXT, lead_bin TEXT, error REAL, created_at TEXT, UNIQUE(forecast_id,observation_id));
            CREATE TABLE IF NOT EXISTS calibration(key TEXT PRIMARY KEY, model TEXT, variable TEXT, region TEXT, season TEXT, lead_bin TEXT, samples INTEGER, bias REAL, mae REAL, rmse REAL, reliability REAL, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS source_status(source TEXT PRIMARY KEY,last_attempt TEXT,last_success TEXT,status TEXT,latency_ms REAL,records INTEGER,error TEXT);
            ''')
    def source_status(self,source,status,latency_ms=None,records=0,error=None):
        now=datetime.now(timezone.utc).isoformat()
        with self.con() as c:c.execute('INSERT INTO source_status VALUES(?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET last_attempt=excluded.last_attempt,last_success=CASE WHEN excluded.status="ok" THEN excluded.last_attempt ELSE source_status.last_success END,status=excluded.status,latency_ms=excluded.latency_ms,records=excluded.records,error=excluded.error',(source,now,now if status=='ok' else None,status,latency_ms,records,error))
    def add_observation(self,source,station,observed_at,lat,lon,variable,value,unit='',quality=.9,payload=None):
        if sf(value) is None:return
        with self.con() as c:c.execute('INSERT INTO observations(source,station,observed_at,lat,lon,variable,value,unit,quality,payload) VALUES(?,?,?,?,?,?,?,?,?,?)',(source,station,observed_at,lat,lon,variable,float(value),unit,float(quality),json.dumps(payload or {})))
    def add_forecast(self,model,cycle_time,valid_time,lead_hours,lat,lon,variable,value,member='',payload=None):
        if sf(value) is None:return
        with self.con() as c:c.execute('INSERT INTO forecasts(model,cycle_time,valid_time,lead_hours,lat,lon,variable,value,member,payload) VALUES(?,?,?,?,?,?,?,?,?,?)',(model,cycle_time,valid_time,int(lead_hours),lat,lon,variable,float(value),member,json.dumps(payload or {})))
    def match(self,max_km=50,max_minutes=90,lookback_days=14):
        # SQL prefilter + distance/time check. Matches one nearest observation per forecast.
        from datetime import timedelta
        cutoff=(datetime.now(timezone.utc)-timedelta(days=lookback_days)).isoformat()
        with self.con() as c:
            fs=c.execute('SELECT * FROM forecasts WHERE valid_time>=? ORDER BY id DESC',(cutoff,)).fetchall()
            made=0
            for f in fs:
                obs=c.execute('SELECT * FROM observations WHERE variable=? ORDER BY observed_at DESC',(f['variable'],)).fetchall()
                best=None;score=1e99
                try: ft=datetime.fromisoformat(f['valid_time'].replace('Z','+00:00'))
                except: continue
                for o in obs:
                    try: ot=datetime.fromisoformat(o['observed_at'].replace('Z','+00:00'))
                    except: continue
                    dt=abs((ot-ft).total_seconds())/60
                    if dt>max_minutes: continue
                    dy=(o['lat']-f['lat'])*111; dx=(o['lon']-f['lon'])*111*math.cos(math.radians(f['lat'])); d=math.hypot(dx,dy)
                    if d>max_km: continue
                    sc=d+dt/10
                    if sc<score:best=o;score=sc
                if best:
                    e=f['value']-best['value']; reg=region(f['lat'],f['lon']); se=season(f['valid_time']); lb=lead_bin(f['lead_hours']);
                    c.execute('INSERT OR IGNORE INTO matches(forecast_id,observation_id,model,variable,region,season,lead_bin,error,created_at) VALUES(?,?,?,?,?,?,?,?,?)',(f['id'],best['id'],f['model'],f['variable'],reg,se,lb,e,datetime.now(timezone.utc).isoformat())); made+=c.total_changes>0
            return made
    def recompute_calibration(self,min_samples=5):
        with self.con() as c:
            groups=c.execute('SELECT model,variable,region,season,lead_bin,COUNT(*) n,AVG(error) bias,AVG(ABS(error)) mae,AVG(error*error) mse FROM matches GROUP BY model,variable,region,season,lead_bin').fetchall()
            n=0
            for g in groups:
                if g['n']<min_samples:continue
                rmse=math.sqrt(g['mse'] or 0); rel=max(.2,min(.98,1/(1+rmse)))
                key='|'.join([g['model'],g['variable'],g['season'],g['lead_bin']])
                c.execute('INSERT INTO calibration VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET samples=excluded.samples,bias=excluded.bias,mae=excluded.mae,rmse=excluded.rmse,reliability=excluded.reliability,updated_at=excluded.updated_at',(key,g['model'],g['variable'],g['region'],g['season'],g['lead_bin'],g['n'],g['bias'],g['mae'],rmse,rel,datetime.now(timezone.utc).isoformat())); n+=1
            return n
    def export_calibration_json(self,path:str)->str:
        with self.con() as c:rows=c.execute('SELECT * FROM calibration').fetchall()
        out={}
        for r in rows:out[r['key']]={'bias':r['bias'],'rmse':r['rmse'],'reliability':r['reliability'],'samples':r['samples'],'region':r['region'],'mae':r['mae']}
        Path(path).parent.mkdir(parents=True,exist_ok=True); Path(path).write_text(json.dumps(out,indent=2),encoding='utf-8'); return path
    def stats(self):
        with self.con() as c:return {t:c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('observations','forecasts','matches','calibration')}
