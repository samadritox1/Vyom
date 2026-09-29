"""Persistent observation/forecast/calibration store for VYOM-ADVENTURE."""
from __future__ import annotations
import json, math, os, sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def now_iso(): return datetime.now(timezone.utc).isoformat()

def safe_float(v, default=None):
    try:
        x=float(v); return x if math.isfinite(x) else default
    except Exception: return default

def season_key(dt: datetime, lat: float=0.0):
    m=dt.month
    s="DJF" if m in (12,1,2) else "MAM" if m in (3,4,5) else "JJA" if m in (6,7,8) else "SON"
    if lat<0: s={"DJF":"JJA","JJA":"DJF","MAM":"SON","SON":"MAM"}[s]
    return s

def lead_bin(h:int):
    return "0_6" if h<=6 else "7_24" if h<=24 else "25_72" if h<=72 else "73_120" if h<=120 else "121_plus"

def region_key(lat:float,lon:float):
    # 5-degree calibration cell + hemispheric/continental parent label.
    a=math.floor(lat/5)*5; b=math.floor(lon/5)*5
    return f"CELL5_{a:+03.0f}_{b:+04.0f}"


class AdventureDataFabric:
    def __init__(self, db_path: Optional[str]=None):
        self.db_path=db_path or os.getenv("VYOM_ADVENTURE_DB_PATH","data/adventure/vyom_adventure.db")
        Path(self.db_path).parent.mkdir(parents=True,exist_ok=True)
        self.conn=sqlite3.connect(self.db_path,check_same_thread=False)
        self.conn.row_factory=sqlite3.Row
        self._init()

    def _init(self):
        c=self.conn.cursor()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS observations(
          id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, station_id TEXT,
          lat REAL, lon REAL, observed_at TEXT, received_at TEXT,
          variable TEXT, value REAL, unit TEXT, quality REAL, metadata TEXT,
          UNIQUE(source, station_id, observed_at, variable));
        CREATE TABLE IF NOT EXISTS forecasts(
          id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT, cycle_time TEXT,
          valid_time TEXT, lead_hours INTEGER, lat REAL, lon REAL,
          variable TEXT, value REAL, member TEXT, quality REAL, metadata TEXT);
        CREATE TABLE IF NOT EXISTS matches(
          id INTEGER PRIMARY KEY AUTOINCREMENT, forecast_id INTEGER, observation_id INTEGER,
          model TEXT, variable TEXT, region TEXT, season TEXT, lead_bin TEXT,
          forecast REAL, observed REAL, error REAL, matched_at TEXT,
          UNIQUE(forecast_id, observation_id));
        CREATE TABLE IF NOT EXISTS calibration(
          key TEXT PRIMARY KEY, model TEXT, variable TEXT, region TEXT,
          season TEXT, lead_bin TEXT, samples INTEGER, bias REAL, mae REAL,
          rmse REAL, reliability REAL, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS source_status(
          source TEXT PRIMARY KEY, last_attempt TEXT, last_success TEXT,
          status TEXT, latency_ms REAL, records INTEGER, error TEXT);
        CREATE TABLE IF NOT EXISTS model_versions(
          source TEXT PRIMARY KEY, version TEXT, recorded_at TEXT, metadata TEXT);
        """)
        self.conn.commit()

    def log_source_status(self, source:str,status:str,latency_ms:float=0,records:int=0,error:str=""):
        last_success=now_iso() if status=="ok" else None
        self.conn.execute("""INSERT INTO source_status(source,last_attempt,last_success,status,latency_ms,records,error)
          VALUES(?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET last_attempt=excluded.last_attempt,
          last_success=COALESCE(excluded.last_success,source_status.last_success),status=excluded.status,
          latency_ms=excluded.latency_ms,records=excluded.records,error=excluded.error""",
          (source,now_iso(),last_success,status,latency_ms,records,error))
        self.conn.commit()

    def add_observation(self, source:str, station_id:str, lat:float,lon:float,observed_at:str,
                        variable:str,value:float,unit:str="",quality:float=.8,metadata:Optional[Dict]=None):
        try:
            self.conn.execute("""INSERT OR IGNORE INTO observations(source,station_id,lat,lon,observed_at,received_at,variable,value,unit,quality,metadata)
             VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(source,station_id,lat,lon,observed_at,now_iso(),variable,float(value),unit,float(quality),json.dumps(metadata or {})))
            self.conn.commit(); return True
        except Exception: return False

    def add_forecast(self, model:str,cycle_time:str,valid_time:str,lead_hours:int,lat:float,lon:float,
                     variable:str,value:float,member:str="det",quality:float=.7,metadata:Optional[Dict]=None):
        cur=self.conn.execute("""INSERT INTO forecasts(model,cycle_time,valid_time,lead_hours,lat,lon,variable,value,member,quality,metadata)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(model,cycle_time,valid_time,int(lead_hours),lat,lon,variable,float(value),member,quality,json.dumps(metadata or {})))
        self.conn.commit(); return cur.lastrowid

    def match_forecasts(self, lookback_days:int=10, max_distance_km:float=35.0, max_time_minutes:int=90):
        cutoff=(datetime.now(timezone.utc)-timedelta(days=lookback_days)).isoformat()
        fs=self.conn.execute("SELECT * FROM forecasts WHERE valid_time>=?",(cutoff,)).fetchall()
        obs=self.conn.execute("SELECT * FROM observations WHERE observed_at>=?",(cutoff,)).fetchall()
        count=0
        from math import radians,sin,cos,asin,sqrt
        def dist(a,b,c,d):
            p1,p2=radians(a),radians(c); dp=radians(c-a); dl=radians(d-b)
            x=sin(dp/2)**2+cos(p1)*cos(p2)*sin(dl/2)**2
            return 12742*asin(sqrt(x))
        for f in fs:
            best=None; score=1e18
            try: ft=datetime.fromisoformat(f["valid_time"].replace("Z","+00:00"))
            except Exception: continue
            for o in obs:
                if o["variable"]!=f["variable"]: continue
                try: ot=datetime.fromisoformat(o["observed_at"].replace("Z","+00:00"))
                except Exception: continue
                dt=abs((ot-ft).total_seconds())/60
                if dt>max_time_minutes: continue
                dk=dist(f["lat"],f["lon"],o["lat"],o["lon"])
                if dk>max_distance_km: continue
                s=dt+2*dk
                if s<score: best=o; score=s
            if best:
                reg=region_key(f["lat"],f["lon"]); seas=season_key(ft,f["lat"]); lb=lead_bin(f["lead_hours"])
                err=f["value"]-best["value"]
                try:
                    self.conn.execute("""INSERT OR IGNORE INTO matches(forecast_id,observation_id,model,variable,region,season,lead_bin,forecast,observed,error,matched_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(f["id"],best["id"],f["model"],f["variable"],reg,seas,lb,f["value"],best["value"],err,now_iso()))
                    if self.conn.total_changes: count+=1
                except Exception: pass
        self.conn.commit(); return count

    def recompute_calibration(self, min_samples:int=5):
        rows=self.conn.execute("SELECT model,variable,region,season,lead_bin,error FROM matches").fetchall()
        groups={}
        for r in rows:
            k=(r["model"],r["variable"],r["region"],r["season"],r["lead_bin"])
            groups.setdefault(k,[]).append(float(r["error"]))
        cells=0
        for k,errs in groups.items():
            if len(errs)<min_samples: continue
            bias=sum(errs)/len(errs); mae=sum(abs(x) for x in errs)/len(errs); rmse=math.sqrt(sum(x*x for x in errs)/len(errs))
            reliability=clamp(1.0/(1.0+rmse/max(0.1,mae+0.1)),.25,.98)
            key="|".join(k)
            self.conn.execute("""INSERT INTO calibration(key,model,variable,region,season,lead_bin,samples,bias,mae,rmse,reliability,updated_at)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET samples=excluded.samples,bias=excluded.bias,
             mae=excluded.mae,rmse=excluded.rmse,reliability=excluded.reliability,updated_at=excluded.updated_at""",
             (key,*k,len(errs),bias,mae,rmse,reliability,now_iso()))
            cells+=1
        self.conn.commit(); return cells

    def export_calibration_json(self,path:str):
        out={}
        for r in self.conn.execute("SELECT * FROM calibration").fetchall():
            # engine calibration lookup does not currently include region in key; export global-ish rows too
            simple=f'{r["model"]}|{r["variable"]}|{r["season"]}|{r["lead_bin"]}'
            current=out.get(simple)
            if current is None or r["samples"]>current.get("samples",0):
                out[simple]={"bias":r["bias"],"rmse":r["rmse"],"reliability":r["reliability"],"samples":r["samples"],"region":r["region"]}
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        with open(path,"w",encoding="utf-8") as f: json.dump(out,f,indent=2)
        return path

    def health(self):
        return {"db_path":self.db_path,"observations":self.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
                "forecasts":self.conn.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0],
                "matches":self.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0],
                "calibration_cells":self.conn.execute("SELECT COUNT(*) FROM calibration").fetchone()[0],
                "sources":[dict(r) for r in self.conn.execute("SELECT * FROM source_status ORDER BY source").fetchall()]}

def clamp(x,lo=0.0,hi=1.0): return max(lo,min(hi,float(x)))

__all__=["AdventureDataFabric","region_key","season_key","lead_bin"]
