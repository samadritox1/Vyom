"""VYOM Marine Data Fabric v4.0

Persistent observation/forecast/warning/AIS store plus continuous verification,
hierarchical calibration, learned model weights, and drift diagnostics.

The default backend is SQLite so the project works locally with no extra
infrastructure. The schema is deliberately simple enough to migrate later to
PostgreSQL/TimescaleDB/PostGIS.
"""
from __future__ import annotations

import json, math, os, sqlite3, statistics, threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

UTC = timezone.utc
EARTH_RADIUS_KM = 6371.0088

# ---------------------------- helpers -------------------------------------

def utcnow() -> datetime:
    return datetime.now(UTC)

def iso(dt: Optional[datetime] = None) -> str:
    dt = dt or utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace('+00:00','Z')

def parse_time(v: Any) -> Optional[datetime]:
    if v is None: return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    s = str(v).strip()
    if not s: return None
    for fmt in (None, '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S', '%d.%m.%y/%H%M'):
        try:
            if fmt is None:
                return datetime.fromisoformat(s.replace('Z','+00:00')).astimezone(UTC)
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except Exception:
            pass
    return None

def sf(v: Any, default: Optional[float]=None) -> Optional[float]:
    try:
        x=float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo,min(hi,x))

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1,p2=math.radians(lat1),math.radians(lat2)
    dp=math.radians(lat2-lat1); dl=math.radians(lon2-lon1)
    a=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*EARTH_RADIUS_KM*math.asin(math.sqrt(a))

def circular_error_deg(pred: float, obs: float) -> float:
    return math.degrees(math.atan2(math.sin(math.radians(pred-obs)), math.cos(math.radians(pred-obs))))

def lead_band(hours: float) -> str:
    h=max(0,float(hours))
    if h<=24: return '0_24h'
    if h<=72: return '25_72h'
    if h<=120:return '73_120h'
    if h<=168:return '121_168h'
    return '169h_plus'

def season_key(dt: datetime, lat: float=0.0) -> str:
    m=dt.month
    if abs(lat)<23.5:
        return {12:'DJF',1:'DJF',2:'DJF',3:'MAM',4:'MAM',5:'MAM',6:'JJA',7:'JJA',8:'JJA',9:'SON',10:'SON',11:'SON'}[m]
    if lat>=0:
        return 'winter' if m in (12,1,2) else 'spring' if m in (3,4,5) else 'summer' if m in (6,7,8) else 'autumn'
    return 'summer' if m in (12,1,2) else 'autumn' if m in (3,4,5) else 'winter' if m in (6,7,8) else 'spring'

def basin_name(lat: float, lon: float) -> str:
    # Deterministic coarse ocean basins. Not a hydrographic legal boundary.
    if 0 <= lat <= 30 and 75 <= lon <= 100: return 'BAY_OF_BENGAL'
    if -5 <= lat <= 30 and 50 <= lon < 75: return 'ARABIAN_SEA'
    if -60 <= lat <= 30 and 20 <= lon <= 120: return 'INDIAN_OCEAN'
    if lat>=0 and -100<=lon<=20: return 'NORTH_ATLANTIC'
    if lat<0 and -70<=lon<=20: return 'SOUTH_ATLANTIC'
    if lat>=0 and (lon>=120 or lon<=-100): return 'NORTH_PACIFIC'
    if lat<0 and (lon>=120 or lon<=-70): return 'SOUTH_PACIFIC'
    if lat>=55: return 'ARCTIC'
    if lat<=-55: return 'SOUTHERN_OCEAN'
    return 'GLOBAL_OCEAN'

def region_keys(lat: float, lon: float) -> List[str]:
    cell5=f'CELL5_{math.floor(lat/5)*5:+03d}_{math.floor(lon/5)*5:+04d}'
    cell10=f'CELL10_{math.floor(lat/10)*10:+03d}_{math.floor(lon/10)*10:+04d}'
    return [cell5,cell10,basin_name(lat,lon),'GLOBAL']

DIRECTION_VARIABLES={'wave_direction_deg','wind_direction_deg','current_direction_deg','swell_direction_deg'}
DEFAULT_EVENT_THRESHOLDS={
    'wave_height_m':4.0,'wind_speed_ms':17.5,'wind_gust_ms':25.0,
    'visibility_m':1000.0,'current_speed_ms':1.5,
}

# ---------------------------- records -------------------------------------
@dataclass
class Observation:
    source: str; station_id: str; observed_at: str; lat: float; lon: float
    variable: str; value: float; unit: str=''; qc: str='GOOD'; source_quality: float=0.8
    received_at: str=''; sigma: Optional[float]=None; provenance: Optional[Dict[str,Any]]=None

@dataclass
class Forecast:
    source: str; model: str; cycle_time: str; valid_time: str; lead_hours: float
    lat: float; lon: float; variable: str; value: float; unit: str=''; member: Optional[str]=None
    model_version: Optional[str]=None; provenance: Optional[Dict[str,Any]]=None

@dataclass
class Warning:
    authority: str; warning_type: str; severity: str; issued_at: str; valid_from: str; valid_until: str
    text: str; source_id: str=''; authoritative: bool=True; geometry: Optional[Dict[str,Any]]=None
    region_text: Optional[str]=None; raw: Optional[Dict[str,Any]]=None

@dataclass
class AISTrack:
    provider: str; mmsi: str; observed_at: str; lat: float; lon: float; sog_kn: Optional[float]=None
    cog_deg: Optional[float]=None; heading_deg: Optional[float]=None; rot: Optional[float]=None
    nav_status: Optional[str]=None; vessel_name: Optional[str]=None; imo: Optional[str]=None
    draught_m: Optional[float]=None; raw: Optional[Dict[str,Any]]=None

# ---------------------------- database ------------------------------------
class MarineDataFabric:
    def __init__(self, path: Optional[str]=None):
        self.path=path or os.getenv('VYOM_MARINE_DB','./data/vyom_marine_data.db')
        os.makedirs(os.path.dirname(os.path.abspath(self.path)),exist_ok=True)
        self._lock=threading.RLock()
        self._init()

    def _conn(self):
        c=sqlite3.connect(self.path,timeout=30,check_same_thread=False)
        c.row_factory=sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=NORMAL')
        return c

    def _init(self):
        with self._conn() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS observations(
              id INTEGER PRIMARY KEY, source TEXT, station_id TEXT, observed_at TEXT, received_at TEXT,
              lat REAL, lon REAL, variable TEXT, value REAL, unit TEXT, qc TEXT, source_quality REAL,
              sigma REAL, provenance TEXT, UNIQUE(source,station_id,observed_at,variable));
            CREATE INDEX IF NOT EXISTS idx_obs_vt ON observations(variable,observed_at);
            CREATE INDEX IF NOT EXISTS idx_obs_xy ON observations(lat,lon);
            CREATE TABLE IF NOT EXISTS forecasts(
              id INTEGER PRIMARY KEY, source TEXT, model TEXT, cycle_time TEXT, valid_time TEXT, lead_hours REAL,
              lat REAL, lon REAL, variable TEXT, value REAL, unit TEXT, member TEXT, model_version TEXT,
              provenance TEXT, UNIQUE(model,cycle_time,valid_time,lat,lon,variable,member));
            CREATE INDEX IF NOT EXISTS idx_fc_vt ON forecasts(variable,valid_time);
            CREATE INDEX IF NOT EXISTS idx_fc_mod ON forecasts(model,variable,lead_hours);
            CREATE TABLE IF NOT EXISTS matches(
              id INTEGER PRIMARY KEY, forecast_id INTEGER, observation_id INTEGER, model TEXT, variable TEXT,
              cycle_time TEXT, valid_time TEXT, observed_at TEXT, lead_hours REAL, lat REAL, lon REAL,
              region TEXT, basin TEXT, season TEXT, prediction REAL, observation REAL, error REAL,
              abs_error REAL, sq_error REAL, spatial_km REAL, temporal_min REAL, event_threshold REAL,
              event_prob REAL, event_obs INTEGER, matched_at TEXT,
              UNIQUE(forecast_id,observation_id));
            CREATE INDEX IF NOT EXISTS idx_match_cal ON matches(model,variable,region,season,lead_hours);
            CREATE TABLE IF NOT EXISTS calibration(
              id INTEGER PRIMARY KEY, model TEXT, variable TEXT, region TEXT, season TEXT, lead_band TEXT,
              sample_size INTEGER, bias REAL, mae REAL, rmse REAL, crps REAL, brier REAL, reliability REAL,
              updated_at TEXT, window_days INTEGER, UNIQUE(model,variable,region,season,lead_band,window_days));
            CREATE TABLE IF NOT EXISTS warnings(
              id INTEGER PRIMARY KEY, authority TEXT, warning_type TEXT, severity TEXT, issued_at TEXT,
              valid_from TEXT, valid_until TEXT, text TEXT, source_id TEXT, authoritative INTEGER,
              geometry TEXT, region_text TEXT, raw TEXT, UNIQUE(authority,warning_type,source_id,issued_at));
            CREATE INDEX IF NOT EXISTS idx_warn_valid ON warnings(valid_from,valid_until);
            CREATE TABLE IF NOT EXISTS ais_tracks(
              id INTEGER PRIMARY KEY, provider TEXT, mmsi TEXT, observed_at TEXT, lat REAL, lon REAL,
              sog_kn REAL, cog_deg REAL, heading_deg REAL, rot REAL, nav_status TEXT, vessel_name TEXT,
              imo TEXT, draught_m REAL, raw TEXT, UNIQUE(provider,mmsi,observed_at));
            CREATE INDEX IF NOT EXISTS idx_ais_time ON ais_tracks(observed_at);
            CREATE TABLE IF NOT EXISTS risk_predictions(
              id INTEGER PRIMARY KEY, prediction_id TEXT UNIQUE, vessel_id TEXT, vessel_class TEXT, predicted_at TEXT, valid_time TEXT,
              lat REAL, lon REAL, region TEXT, loading_state TEXT, sea_state_band TEXT, encounter_band TEXT, event_type TEXT,
              probability REAL, engine_version TEXT, metadata TEXT);
            CREATE INDEX IF NOT EXISTS idx_risk_vessel ON risk_predictions(vessel_id,event_type,valid_time);
            CREATE TABLE IF NOT EXISTS vessel_labeled_cases(
              id INTEGER PRIMARY KEY, case_id TEXT UNIQUE, vessel_id TEXT, vessel_class TEXT, occurred_at TEXT, lat REAL, lon REAL,
              region TEXT, loading_state TEXT, sea_state_band TEXT, encounter_band TEXT, event_type TEXT, outcome INTEGER,
              predicted_probability REAL, source TEXT, verified INTEGER, metadata TEXT);
            CREATE INDEX IF NOT EXISTS idx_vessel_cases ON vessel_labeled_cases(vessel_class,event_type,region,occurred_at);
            CREATE TABLE IF NOT EXISTS vessel_calibration(
              id INTEGER PRIMARY KEY, vessel_class TEXT, event_type TEXT, region TEXT, loading_state TEXT, sea_state_band TEXT, encounter_band TEXT,
              sample_size INTEGER, mean_pred REAL, observed_rate REAL, brier REAL, calibration_gap REAL, updated_at TEXT,
              UNIQUE(vessel_class,event_type,region,loading_state,sea_state_band,encounter_band));
            CREATE TABLE IF NOT EXISTS source_status(
              source TEXT PRIMARY KEY, last_success TEXT, last_attempt TEXT, status TEXT, latency_ms REAL,
              records INTEGER, message TEXT);
            CREATE TABLE IF NOT EXISTS model_versions(
              model TEXT PRIMARY KEY, version TEXT, seen_at TEXT, metadata TEXT);
            ''')

    @staticmethod
    def _j(v): return json.dumps(v,ensure_ascii=False,separators=(',',':')) if v is not None else None

    def upsert_observations(self, records: Sequence[Observation]) -> int:
        if not records:return 0
        n=0
        with self._lock,self._conn() as c:
            for r in records:
                try:
                    c.execute('''INSERT OR REPLACE INTO observations(source,station_id,observed_at,received_at,lat,lon,variable,value,unit,qc,source_quality,sigma,provenance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                              (r.source,r.station_id,r.observed_at,r.received_at or iso(),r.lat,r.lon,r.variable,r.value,r.unit,r.qc,r.source_quality,r.sigma,self._j(r.provenance)))
                    n+=1
                except Exception: pass
        return n

    def upsert_forecasts(self, records: Sequence[Forecast]) -> int:
        if not records:return 0
        n=0
        with self._lock,self._conn() as c:
            for r in records:
                try:
                    c.execute('''INSERT OR REPLACE INTO forecasts(source,model,cycle_time,valid_time,lead_hours,lat,lon,variable,value,unit,member,model_version,provenance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                              (r.source,r.model,r.cycle_time,r.valid_time,r.lead_hours,r.lat,r.lon,r.variable,r.value,r.unit,str(r.member) if r.member is not None else '',r.model_version,self._j(r.provenance)))
                    n+=1
                except Exception: pass
        return n

    def upsert_warnings(self, records: Sequence[Warning]) -> int:
        n=0
        with self._lock,self._conn() as c:
            for r in records:
                try:
                    c.execute('''INSERT OR REPLACE INTO warnings(authority,warning_type,severity,issued_at,valid_from,valid_until,text,source_id,authoritative,geometry,region_text,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                              (r.authority,r.warning_type,r.severity,r.issued_at,r.valid_from,r.valid_until,r.text,r.source_id,int(r.authoritative),self._j(r.geometry),r.region_text,self._j(r.raw)))
                    n+=1
                except Exception: pass
        return n

    def upsert_ais(self, records: Sequence[AISTrack]) -> int:
        n=0
        with self._lock,self._conn() as c:
            for r in records:
                try:
                    c.execute('''INSERT OR REPLACE INTO ais_tracks(provider,mmsi,observed_at,lat,lon,sog_kn,cog_deg,heading_deg,rot,nav_status,vessel_name,imo,draught_m,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                              (r.provider,r.mmsi,r.observed_at,r.lat,r.lon,r.sog_kn,r.cog_deg,r.heading_deg,r.rot,r.nav_status,r.vessel_name,r.imo,r.draught_m,self._j(r.raw)))
                    n+=1
                except Exception: pass
        return n

    def source_health(self, source: str, ok: bool, latency_ms: float=0, records: int=0, message: str=''):
        now=iso()
        with self._conn() as c:
            prev=c.execute('SELECT last_success FROM source_status WHERE source=?',(source,)).fetchone()
            last_success=now if ok else (prev['last_success'] if prev else None)
            c.execute('''INSERT INTO source_status(source,last_success,last_attempt,status,latency_ms,records,message) VALUES(?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET last_success=excluded.last_success,last_attempt=excluded.last_attempt,status=excluded.status,latency_ms=excluded.latency_ms,records=excluded.records,message=excluded.message''',
                      (source,last_success,now,'ok' if ok else 'error',latency_ms,records,message[:500]))

    def source_status(self) -> List[Dict[str,Any]]:
        with self._conn() as c:return [dict(r) for r in c.execute('SELECT * FROM source_status ORDER BY source')]

    # --------------------- matching / verification -------------------------
    def match_recent(self, lookback_days: int=10, spatial_km: float=75.0, temporal_minutes: float=90.0, limit: int=30000) -> int:
        since=iso(utcnow()-timedelta(days=lookback_days)); inserted=0
        with self._lock,self._conn() as c:
            obs=list(c.execute('SELECT * FROM observations WHERE observed_at>=? ORDER BY observed_at DESC LIMIT ?',(since,limit)))
            for o in obs:
                ot=parse_time(o['observed_at'])
                if not ot:continue
                t0=iso(ot-timedelta(minutes=temporal_minutes)); t1=iso(ot+timedelta(minutes=temporal_minutes))
                fcs=list(c.execute('SELECT * FROM forecasts WHERE variable=? AND valid_time BETWEEN ? AND ?',(o['variable'],t0,t1)))
                for f in fcs:
                    d=haversine_km(o['lat'],o['lon'],f['lat'],f['lon'])
                    if d>spatial_km:continue
                    ft=parse_time(f['valid_time']); tm=abs((ft-ot).total_seconds())/60 if ft else 1e9
                    pred=float(f['value']); ob=float(o['value'])
                    err=circular_error_deg(pred,ob) if o['variable'] in DIRECTION_VARIABLES else pred-ob
                    reg=region_keys(o['lat'],o['lon'])[0]; bas=basin_name(o['lat'],o['lon']); seas=season_key(ot,o['lat'])
                    thr=DEFAULT_EVENT_THRESHOLDS.get(o['variable'])
                    if thr is not None:
                        # deterministic pseudo-probability around threshold; ensemble CRPS handled separately
                        spread=max(abs(thr)*0.08,0.25)
                        z=(pred-thr)/spread
                        p=1/(1+math.exp(-z))
                        if o['variable']=='visibility_m': p=1-p
                        event_obs=int(ob>=thr) if o['variable']!='visibility_m' else int(ob<=thr)
                    else:p=event_obs=None
                    try:
                        c.execute('''INSERT OR IGNORE INTO matches(forecast_id,observation_id,model,variable,cycle_time,valid_time,observed_at,lead_hours,lat,lon,region,basin,season,prediction,observation,error,abs_error,sq_error,spatial_km,temporal_min,event_threshold,event_prob,event_obs,matched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                                  (f['id'],o['id'],f['model'],o['variable'],f['cycle_time'],f['valid_time'],o['observed_at'],f['lead_hours'],o['lat'],o['lon'],reg,bas,seas,pred,ob,err,abs(err),err*err,d,tm,thr,p,event_obs,iso()))
                        inserted += c.total_changes>0
                    except Exception: pass
        return inserted

    @staticmethod
    def _metrics(rows: Sequence[sqlite3.Row]) -> Dict[str,Optional[float]]:
        if not rows:return {'n':0,'bias':None,'mae':None,'rmse':None,'brier':None,'reliability':None}
        es=[float(r['error']) for r in rows]; aes=[abs(x) for x in es]
        briers=[]; rel_pairs=[]
        for r in rows:
            if r['event_prob'] is not None and r['event_obs'] is not None:
                p=float(r['event_prob']); y=int(r['event_obs']); briers.append((p-y)**2); rel_pairs.append((p,y))
        # reliability skill in [0,1], based on mean calibration gap across 5 bins
        rel=None
        if rel_pairs:
            gaps=[]
            for bi in range(5):
                lo=bi/5; hi=(bi+1)/5
                xs=[x for x in rel_pairs if lo<=x[0]<(hi if bi<4 else hi+1e-9)]
                if xs:gaps.append(abs(statistics.mean(p for p,_ in xs)-statistics.mean(y for _,y in xs)))
            rel=clamp(1-(statistics.mean(gaps) if gaps else 1),0,1)
        return {'n':len(es),'bias':statistics.mean(es),'mae':statistics.mean(aes),'rmse':math.sqrt(statistics.mean(x*x for x in es)),
                'brier':statistics.mean(briers) if briers else None,'reliability':rel}

    def recompute_calibration(self, window_days: int=365, min_samples: int=8) -> int:
        since=iso(utcnow()-timedelta(days=window_days)); n=0
        with self._lock,self._conn() as c:
            # rows are duplicated into hierarchical regions to make fallback explicit
            base=list(c.execute('SELECT * FROM matches WHERE matched_at>=?',(since,)))
            groups: Dict[Tuple[str,str,str,str,str],List[sqlite3.Row]]={}
            for r in base:
                for reg in (r['region'],r['basin'],'GLOBAL'):
                    key=(r['model'],r['variable'],reg,r['season'],lead_band(r['lead_hours']))
                    groups.setdefault(key,[]).append(r)
                    key2=(r['model'],r['variable'],reg,'all',lead_band(r['lead_hours']))
                    groups.setdefault(key2,[]).append(r)
            for (model,var,reg,seas,lb),rows in groups.items():
                if len(rows)<min_samples: continue
                m=self._metrics(rows)
                c.execute('''INSERT INTO calibration(model,variable,region,season,lead_band,sample_size,bias,mae,rmse,crps,brier,reliability,updated_at,window_days) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(model,variable,region,season,lead_band,window_days) DO UPDATE SET sample_size=excluded.sample_size,bias=excluded.bias,mae=excluded.mae,rmse=excluded.rmse,brier=excluded.brier,reliability=excluded.reliability,updated_at=excluded.updated_at''',
                          (model,var,reg,seas,lb,m['n'],m['bias'],m['mae'],m['rmse'],None,m['brier'],m['reliability'],iso(),window_days))
                n+=1
        return n

    def calibration_cell(self, model: str, variable: str, lat: float, lon: float, season: str, lead_hours: float, shrink_k: float=50.0, window_days: int=365) -> Dict[str,Any]:
        regs=region_keys(lat,lon); lb=lead_band(lead_hours)
        with self._conn() as c:
            parent=None
            # Work broad -> local, shrinking each local estimate to parent.
            for reg in reversed(regs):
                row=c.execute('SELECT * FROM calibration WHERE model=? AND variable=? AND region=? AND season IN (?,"all") AND lead_band=? AND window_days=? ORDER BY CASE WHEN season=? THEN 0 ELSE 1 END LIMIT 1',
                              (model,variable,reg,season,lb,window_days,season)).fetchone()
                if not row: continue
                cur=dict(row); n=max(0,int(cur['sample_size'] or 0))
                if parent:
                    a=n/(n+shrink_k)
                    for k in ('bias','mae','rmse','brier','reliability'):
                        lv=sf(cur.get(k)); pv=sf(parent.get(k))
                        if lv is not None and pv is not None: cur[k]=a*lv+(1-a)*pv
                        elif pv is not None: cur[k]=pv
                    cur['shrinkage_alpha']=a
                parent=cur
            if parent:return parent
        return {'model':model,'variable':variable,'region':'DEFAULT','season':season,'lead_band':lb,'sample_size':0,'bias':0.0,'rmse':None,'mae':None,'reliability':0.5,'brier':None}

    def learned_weights(self, models: Sequence[str], variable: str, lat: float, lon: float, season: str, lead_hours: float, temperature: float=1.0) -> Dict[str,float]:
        scores={}
        for m in models:
            c=self.calibration_cell(m,variable,lat,lon,season,lead_hours)
            rm=sf(c.get('rmse'),999); bi=abs(sf(c.get('bias'),0) or 0); br=sf(c.get('brier')); rel=sf(c.get('reliability'),0.5) or 0.5; n=int(c.get('sample_size') or 0)
            if rm>=999: score=5.0
            else: score=rm + 0.35*bi + (0.75*br if br is not None else 0.15) + 0.5*(1-rel) + 1/(1+math.sqrt(max(n,0)))
            scores[m]=score
        mn=min(scores.values()) if scores else 0
        ex={m:math.exp(-(s-mn)/max(temperature,1e-6)) for m,s in scores.items()}; z=sum(ex.values()) or 1
        return {m:v/z for m,v in ex.items()}

    def drift_report(self, model: str, variable: str, days_recent: int=30, days_baseline: int=365) -> Dict[str,Any]:
        now=utcnow()
        with self._conn() as c:
            r=list(c.execute('SELECT error FROM matches WHERE model=? AND variable=? AND matched_at>=?',(model,variable,iso(now-timedelta(days=days_recent)))))
            b=list(c.execute('SELECT error FROM matches WHERE model=? AND variable=? AND matched_at>=?',(model,variable,iso(now-timedelta(days=days_baseline)))))
        def rmse(rows): return math.sqrt(statistics.mean(float(x['error'])**2 for x in rows)) if rows else None
        rr,rb=rmse(r),rmse(b); d=(rr-rb)/rb if rr is not None and rb not in (None,0) else None
        return {'model':model,'variable':variable,'recent_rmse':rr,'baseline_rmse':rb,'relative_drift':d,'degraded':bool(d is not None and d>0.25),'n_recent':len(r),'n_baseline':len(b)}

    def export_engine_calibration(self, path: str, lat: float, lon: float, models: Sequence[str], variables: Sequence[str]) -> str:
        # Export in the format expected by VYOM's existing CalibrationStore.
        now=utcnow(); seas=season_key(now,lat); out={'atmosphere':{},'waves':{},'_meta':{'generated_at':iso(now),'lat':lat,'lon':lon,'source':'MarineDataFabric'}}
        fam=lambda v:'waves' if v in {'wave_height_m','wave_period_s','wave_direction_deg','swell_height_m','swell_period_s','current_speed_ms','current_direction_deg'} else 'atmosphere'
        for m in models:
            for v in variables:
                f=fam(v); out[f].setdefault(m,{}).setdefault(v,{})
                for lb,h in [('0_24h',12),('25_72h',48),('73_120h',96),('121_168h',144)]:
                    c=self.calibration_cell(m,v,lat,lon,seas,h)
                    if int(c.get('sample_size') or 0) <= 0:
                        continue
                    out[f][m][v].setdefault(lb,{})[seas]={
                        'bias':sf(c.get('bias'),0) or 0,'rmse':sf(c.get('rmse'),1) or 1,
                        'crps':sf(c.get('crps')),'reliability':sf(c.get('reliability'),0.5) or 0.5,'sample_size':int(c.get('sample_size') or 0)}
        os.makedirs(os.path.dirname(os.path.abspath(path)),exist_ok=True)
        with open(path,'w',encoding='utf-8') as f: json.dump(out,f,indent=2)
        return path

    def log_risk_prediction(self, prediction_id: str, vessel_id: str, vessel_class: str, valid_time: str,
                            lat: float, lon: float, event_type: str, probability: float,
                            loading_state: str="", sea_state_band: str="", encounter_band: str="",
                            engine_version: str="", metadata: Optional[Mapping[str,Any]]=None) -> bool:
        with self._conn() as c:
            try:
                c.execute("INSERT OR REPLACE INTO risk_predictions(prediction_id,vessel_id,vessel_class,predicted_at,valid_time,lat,lon,region,loading_state,sea_state_band,encounter_band,event_type,probability,engine_version,metadata) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (prediction_id,vessel_id,vessel_class,iso(),valid_time,lat,lon,basin_name(lat,lon),loading_state,sea_state_band,encounter_band,event_type,clamp(float(probability),0,1),engine_version,self._j(metadata)))
                return True
            except Exception:
                return False

    def log_vessel_labeled_case(self, case_id: str, vessel_id: str, vessel_class: str, occurred_at: str,
                                lat: float, lon: float, event_type: str, outcome: int, predicted_probability: float,
                                loading_state: str="", sea_state_band: str="", encounter_band: str="",
                                source: str="operator_verified", verified: bool=True, metadata: Optional[Mapping[str,Any]]=None) -> bool:
        # Explicit labels only: absence of an incident is never auto-labeled as a negative case.
        with self._conn() as c:
            try:
                c.execute("INSERT OR REPLACE INTO vessel_labeled_cases(case_id,vessel_id,vessel_class,occurred_at,lat,lon,region,loading_state,sea_state_band,encounter_band,event_type,outcome,predicted_probability,source,verified,metadata) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (case_id,vessel_id,vessel_class,occurred_at,lat,lon,basin_name(lat,lon),loading_state,sea_state_band,encounter_band,event_type,int(bool(outcome)),clamp(float(predicted_probability),0,1),source,int(bool(verified)),self._j(metadata)))
                return True
            except Exception:
                return False

    def recompute_vessel_calibration(self, min_samples: int=12) -> int:
        with self._conn() as c:
            rows=list(c.execute("SELECT * FROM vessel_labeled_cases WHERE verified=1"))
            groups: Dict[Tuple[str,str,str,str,str,str],List[sqlite3.Row]]={}
            for r in rows:
                variants=[
                    (r["vessel_class"],r["event_type"],r["region"],r["loading_state"] or "all",r["sea_state_band"] or "all",r["encounter_band"] or "all"),
                    (r["vessel_class"],r["event_type"],r["region"],"all","all","all"),
                    (r["vessel_class"],r["event_type"],"GLOBAL","all","all","all"),
                ]
                for k in variants: groups.setdefault(k,[]).append(r)
            n=0
            for k,rs in groups.items():
                if len(rs)<min_samples: continue
                ps=[float(x["predicted_probability"]) for x in rs]; ys=[int(x["outcome"]) for x in rs]
                mp=statistics.mean(ps); orate=statistics.mean(ys); brier=statistics.mean((p-y)**2 for p,y in zip(ps,ys)); gap=abs(mp-orate)
                c.execute("INSERT INTO vessel_calibration(vessel_class,event_type,region,loading_state,sea_state_band,encounter_band,sample_size,mean_pred,observed_rate,brier,calibration_gap,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(vessel_class,event_type,region,loading_state,sea_state_band,encounter_band) DO UPDATE SET sample_size=excluded.sample_size,mean_pred=excluded.mean_pred,observed_rate=excluded.observed_rate,brier=excluded.brier,calibration_gap=excluded.calibration_gap,updated_at=excluded.updated_at",
                          (*k,len(rs),mp,orate,brier,gap,iso()))
                n+=1
            return n

    def vessel_calibration_cell(self, vessel_class: str, event_type: str, lat: float, lon: float,
                                loading_state: str="all", sea_state_band: str="all", encounter_band: str="all") -> Dict[str,Any]:
        regs=[basin_name(lat,lon),"GLOBAL"]
        with self._conn() as c:
            for reg in regs:
                for q in (
                    (vessel_class,event_type,reg,loading_state or "all",sea_state_band or "all",encounter_band or "all"),
                    (vessel_class,event_type,reg,"all","all","all"),
                ):
                    r=c.execute("SELECT * FROM vessel_calibration WHERE vessel_class=? AND event_type=? AND region=? AND loading_state=? AND sea_state_band=? AND encounter_band=? LIMIT 1",q).fetchone()
                    if r:return dict(r)
        return {"sample_size":0,"vessel_class":vessel_class,"event_type":event_type,"region":"DEFAULT","brier":None,"calibration_gap":None}

    def active_warnings(self, now: Optional[datetime]=None) -> List[Dict[str,Any]]:
        n=iso(now or utcnow())
        with self._conn() as c:
            rows=c.execute('SELECT * FROM warnings WHERE valid_from<=? AND valid_until>=? ORDER BY authoritative DESC,severity DESC',(n,n)).fetchall()
        out=[]
        for r in rows:
            d=dict(r)
            for k in ('geometry','raw'):
                try:d[k]=json.loads(d[k]) if d[k] else None
                except:pass
            out.append(d)
        return out

    def latest_observations_near(self, lat: float, lon: float, radius_km: float=250, hours: float=12) -> List[Dict[str,Any]]:
        since=iso(utcnow()-timedelta(hours=hours)); out=[]
        with self._conn() as c: rows=c.execute('SELECT * FROM observations WHERE observed_at>=? ORDER BY observed_at DESC',(since,)).fetchall()
        for r in rows:
            d=haversine_km(lat,lon,r['lat'],r['lon'])
            if d<=radius_km:
                x=dict(r); x['distance_km']=d; out.append(x)
        return out

    def future_forecast_series(self, lat: float, lon: float, horizon_hours: int=120, radius_km: float=150, models: Optional[Sequence[str]]=None) -> Dict[str,List[Dict[str,Any]]]:
        now=utcnow(); t0=iso(now-timedelta(hours=3)); t1=iso(now+timedelta(hours=horizon_hours))
        with self._conn() as c:
            if models:
                ph=",".join("?" for _ in models)
                rows=c.execute(f"SELECT * FROM forecasts WHERE valid_time BETWEEN ? AND ? AND model IN ({ph}) ORDER BY valid_time",(t0,t1,*models)).fetchall()
            else:
                rows=c.execute("SELECT * FROM forecasts WHERE valid_time BETWEEN ? AND ? ORDER BY valid_time",(t0,t1)).fetchall()
        grouped: Dict[Tuple[str,str],Tuple[float,Dict[str,Any]]]={}
        for r in rows:
            d=haversine_km(lat,lon,float(r["lat"]),float(r["lon"]))
            if d>radius_km:continue
            key=(r["model"],r["valid_time"])
            if key not in grouped or d<grouped[key][0]:
                grouped[key]=(d,{"model":r["model"],"valid_time":r["valid_time"],"cycle_time":r["cycle_time"],"lead_hours":r["lead_hours"],"lat":r["lat"],"lon":r["lon"],"values":{}})
            grouped[key][1]["values"][r["variable"]]=r["value"]
        out: Dict[str,List[Dict[str,Any]]]={}
        for (_, _),(_,item) in grouped.items():out.setdefault(item["model"],[]).append(item)
        for m in out:out[m].sort(key=lambda x:x["valid_time"])
        return out

    def latest_ais_near(self, lat: float, lon: float, radius_km: float=50, minutes: int=30) -> List[Dict[str,Any]]:
        since=iso(utcnow()-timedelta(minutes=minutes)); out=[]
        with self._conn() as c: rows=c.execute('SELECT * FROM ais_tracks WHERE observed_at>=? ORDER BY observed_at DESC',(since,)).fetchall()
        seen=set()
        for r in rows:
            if r['mmsi'] in seen:continue
            d=haversine_km(lat,lon,r['lat'],r['lon'])
            if d<=radius_km:
                x=dict(r); x['distance_km']=d; out.append(x); seen.add(r['mmsi'])
        return out

    def stats(self) -> Dict[str,Any]:
        with self._conn() as c:
            return {t:c.execute(f'SELECT COUNT(*) n FROM {t}').fetchone()['n'] for t in ('observations','forecasts','matches','calibration','warnings','ais_tracks','risk_predictions','vessel_labeled_cases','vessel_calibration')}

__all__=['MarineDataFabric','Observation','Forecast','Warning','AISTrack','iso','parse_time','haversine_km','region_keys','basin_name','season_key','lead_band','circular_error_deg']
