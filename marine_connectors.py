"""Real-source connectors for VYOM-MARINE v4.

Connectors never fabricate missing data. Public sources work without secrets where
possible; credentialed sources are enabled only when their environment variables
are present. Every connector returns normalized MarineDataFabric records.
"""
from __future__ import annotations

import csv, io, json, math, os, re, time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlencode

import requests

from marine_data_fabric import Observation, Forecast, Warning, AISTrack, iso, parse_time
# local lightweight numeric helper (keeps this module independent of engine)
def _f(v, default=None):
    try:
        x=float(v); return x if math.isfinite(x) else default
    except Exception:return default

def _haversine_km(lat1,lon1,lat2,lon2):
    r=6371.0088
    p1,p2=math.radians(lat1),math.radians(lat2)
    dp=math.radians(lat2-lat1);dl=math.radians(lon2-lon1)
    a=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(math.sqrt(a))

UTC=timezone.utc
UA='VYOM-MARINE/4.0 research prototype; contact configured by operator'

class ConnectorResult:
    def __init__(self, source:str):
        self.source=source; self.observations=[]; self.forecasts=[]; self.warnings=[]; self.ais=[]; self.context={}; self.errors=[]
    def as_dict(self):
        return {'source':self.source,'observations':len(self.observations),'forecasts':len(self.forecasts),'warnings':len(self.warnings),'ais':len(self.ais),'context':self.context,'errors':self.errors}

class HTTP:
    def __init__(self, timeout:float=12): self.timeout=timeout
    def get_json(self,url:str,params:Optional[Mapping[str,Any]]=None,headers:Optional[Mapping[str,str]]=None):
        h={'User-Agent':UA,'Accept':'application/json'}; h.update(headers or {})
        r=requests.get(url,params=params,headers=h,timeout=self.timeout); r.raise_for_status(); return r.json()
    def get_text(self,url:str,params:Optional[Mapping[str,Any]]=None,headers:Optional[Mapping[str,str]]=None):
        h={'User-Agent':UA}; h.update(headers or {})
        r=requests.get(url,params=params,headers=h,timeout=self.timeout); r.raise_for_status(); return r.text
    def get_bytes(self,url:str,params=None,headers=None):
        h={'User-Agent':UA}; h.update(headers or {})
        r=requests.get(url,params=params,headers=h,timeout=self.timeout); r.raise_for_status(); return r.content

# ------------------------------- IMD --------------------------------------
class IMDConnector:
    BASE='https://api.imd.gov.in/api/v1'
    def __init__(self,http=None): self.http=http or HTTP()
    @staticmethod
    def _severity(text:str)->str:
        t=(text or '').lower()
        if any(x in t for x in ('extremely','very severe','cyclone','hurricane','red','danger','do not venture','great danger')):return 'CRITICAL'
        if any(x in t for x in ('severe','gale','squall','high wave','rough to very rough','orange')):return 'HIGH'
        if any(x in t for x in ('warning','thunder','poor visibility','strong wind','yellow')):return 'CAUTION'
        return 'NORMAL'
    def _warning_from_row(self,kind,row,now):
        text=' | '.join(str(row.get(k,'')) for k in ('TTT Warning','Warning','Weather','Wind','Sea Condition','Visibility','Port Signal') if row.get(k))
        vf=parse_time(row.get('Valid From')) or parse_time(row.get('Date of Issue')) or parse_time(row.get('Date of Observation')) or now
        validity=_f(row.get('Validity'),24) or 24
        vu=vf+timedelta(hours=validity)
        issued=parse_time(row.get('Update Time')) or parse_time(row.get('Date of Issue')) or now
        sid=str(row.get('Id') or row.get('Port Id') or row.get('Port Name') or row.get('Layer') or kind)
        return Warning('IMD',kind,self._severity(text),iso(issued),iso(vf),iso(vu),text or kind,sid,True,None,str(row.get('Layer') or row.get('Port Name') or ''),dict(row))
    def fetch(self,lat:float,lon:float)->ConnectorResult:
        out=ConnectorResult('IMD'); now=datetime.now(UTC)
        endpoints={'PORT_WARNING':'portwarning','SEA_AREA_BULLETIN':'seabulletin','COASTAL_BULLETIN':'coastalbulletin'}
        # Fishermen endpoint naming has changed across IMD deployments; configurable override.
        fw=os.getenv('IMD_FISHERMEN_ENDPOINT','').strip()
        if fw:endpoints['FISHERMEN_WARNING']=fw.rsplit('/',1)[-1]
        for kind,ep in endpoints.items():
            try:
                data=self.http.get_json(f'{self.BASE}/{ep}')
                rows=data if isinstance(data,list) else data.get('data',[]) if isinstance(data,dict) else []
                if isinstance(rows,dict): rows=[rows]
                for row in rows:
                    if isinstance(row,dict):out.warnings.append(self._warning_from_row(kind,row,now))
            except Exception as e: out.errors.append(f'{kind}: {e}')
        # Cyclone track / wind polygons / cone are authoritative context.
        try:
            ct=self.http.get_json(f'{self.BASE}/cyclone_track'); data=(ct or {}).get('data',{}) if isinstance(ct,dict) else {}
            forecast=data.get('forecast',[]) or []
            for i,p in enumerate(forecast):
                if not isinstance(p,dict):continue
                out.context.setdefault('cyclone_ensemble',[]).append({'source':'IMD_OFFICIAL_TRACK','member':'official','time':p.get('Date/Time'),'lat':_f(p.get('lat')),'lon':_f(p.get('lon')),'wind_kn':_f(p.get('MSW (kt)')),'category':p.get('Category')})
            out.context['cyclone_track_raw']=ct
        except Exception as e: out.errors.append(f'CYCLONE_TRACK: {e}')
        for key,ep in [('cyclone_wind','cyclone_wind'),('cyclone_cone','cyclone_cou')]:
            try: out.context[key]=self.http.get_json(f'{self.BASE}/{ep}')
            except Exception as e: out.errors.append(f'{key}: {e}')
        return out

# ------------------------------- NDBC -------------------------------------
class NDBCConnector:
    BASE='https://www.ndbc.noaa.gov/data/realtime2'
    STATIONS='https://www.ndbc.noaa.gov/activestations.xml'
    _station_cache=None; _station_cache_at=0.0
    def __init__(self,http=None):self.http=http or HTTP()
    def discover_nearest(self,lat:float,lon:float,limit:int=3,max_distance_km:float=900.0):
        import xml.etree.ElementTree as ET
        now=time.time()
        if NDBCConnector._station_cache is None or now-NDBCConnector._station_cache_at>3600:
            try:
                root=ET.fromstring(self.http.get_text(self.STATIONS))
                rows=[]
                for el in root.findall('.//station'):
                    la=_f(el.attrib.get('lat'));lo=_f(el.attrib.get('lon'))
                    if la is None or lo is None:continue
                    rows.append({'id':el.attrib.get('id'),'lat':la,'lon':lo,'name':el.attrib.get('name',''),'met':el.attrib.get('met'),'currents':el.attrib.get('currents')})
                NDBCConnector._station_cache=rows;NDBCConnector._station_cache_at=now
            except Exception:
                return []
        ranked=[]
        for st in NDBCConnector._station_cache or []:
            d=_haversine_km(lat,lon,st['lat'],st['lon'])
            if d<=max_distance_km: ranked.append((d,st))
        ranked.sort(key=lambda x:x[0])
        return [dict(st,distance_km=round(d,2)) for d,st in ranked[:max(1,limit)]]
    @staticmethod
    def _parse_time(parts):
        try:return datetime(int(parts[0]),int(parts[1]),int(parts[2]),int(parts[3]),int(parts[4]),tzinfo=UTC)
        except:return None
    def fetch_station(self,station:str,lat:float,lon:float,max_rows:int=48)->ConnectorResult:
        out=ConnectorResult('NOAA_NDBC')
        try:text=self.http.get_text(f'{self.BASE}/{station}.txt')
        except Exception as e:out.errors.append(str(e));return out
        lines=[x for x in text.splitlines() if x.strip()]
        if len(lines)<3:return out
        headers=re.split(r'\s+',lines[0].lstrip('#').strip()); units=re.split(r'\s+',lines[1].lstrip('#').strip())
        idx={h:i for i,h in enumerate(headers)}
        mapping={'WVHT':('wave_height_m','m'),'DPD':('wave_period_s','s'),'APD':('mean_wave_period_s','s'),'MWD':('wave_direction_deg','deg'),'WSPD':('wind_speed_ms','m/s'),'GST':('wind_gust_ms','m/s'),'PRES':('pressure_hpa','hPa'),'VIS':('visibility_nm','nm'),'WTMP':('sea_surface_temperature_c','degC'),'ATMP':('temperature_c','degC')}
        for line in lines[2:2+max_rows]:
            p=re.split(r'\s+',line.strip()); t=self._parse_time(p)
            if not t:continue
            for col,(var,unit) in mapping.items():
                j=idx.get(col)
                if j is None or j>=len(p):continue
                v=_f(p[j]);
                if v is None or abs(v)>=999:continue
                if var=='visibility_nm': var='visibility_m';v*=1852;unit='m'
                out.observations.append(Observation('NOAA_NDBC',station,iso(t),lat,lon,var,v,unit,'AUTO_QC',0.9,iso(),None,{'product':'realtime2','.txt':True}))
        return out

# --------------------------- NOAA CO-OPS ----------------------------------
class NOAA_COOPS_Connector:
    BASE='https://api.tidesandcurrents.noaa.gov/api/prod/datagetter'
    META='https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json'
    _station_cache=None; _station_cache_at=0.0
    def __init__(self,http=None):self.http=http or HTTP()
    def discover_nearest(self,lat:float,lon:float,limit:int=3,max_distance_km:float=900.0,station_type:str='waterlevelsandmet'):
        now=time.time();key=station_type
        cache=NOAA_COOPS_Connector._station_cache if isinstance(NOAA_COOPS_Connector._station_cache,dict) else {}
        entry=cache.get(key)
        if not entry or now-entry[0]>21600:
            try:
                d=self.http.get_json(self.META,params={'type':station_type,'units':'metric'})
                arr=d.get('stations') or d.get('stationList') or []
                rows=[]
                for st in arr:
                    la=_f(st.get('lat'));lo=_f(st.get('lng') if st.get('lng') is not None else st.get('lon'))
                    if la is None or lo is None:continue
                    rows.append({'id':str(st.get('id','')),'lat':la,'lon':lo,'name':st.get('name',''),'state':st.get('state','')})
                cache[key]=(now,rows);NOAA_COOPS_Connector._station_cache=cache
                entry=cache[key]
            except Exception:
                return []
        ranked=[]
        for st in entry[1]:
            d=_haversine_km(lat,lon,st['lat'],st['lon'])
            if d<=max_distance_km:ranked.append((d,st))
        ranked.sort(key=lambda x:x[0])
        return [dict(st,distance_km=round(d,2)) for d,st in ranked[:max(1,limit)]]
    def fetch_station(self,station:str,lat:float,lon:float,hours:int=24)->ConnectorResult:
        out=ConnectorResult('NOAA_COOPS'); end=datetime.now(UTC); begin=end-timedelta(hours=hours)
        products=[('water_level','water_level_m','metric'),('air_pressure','pressure_hpa','metric'),('wind','wind','metric'),('visibility','visibility_nm','metric'),('water_temperature','sea_surface_temperature_c','metric')]
        for prod,var,units in products:
            params={'product':prod,'application':'VYOM','begin_date':begin.strftime('%Y%m%d %H:%M'),'end_date':end.strftime('%Y%m%d %H:%M'),'datum':'MSL' if prod=='water_level' else 'MLLW','station':station,'time_zone':'gmt','units':units,'format':'json'}
            try:d=self.http.get_json(self.BASE,params=params)
            except Exception as e:out.errors.append(f'{prod}:{e}');continue
            for row in d.get('data',[]) if isinstance(d,dict) else []:
                t=parse_time(row.get('t')); 
                if not t:continue
                if prod=='wind':
                    for k,vn,un in [('s','wind_speed_ms','m/s'),('g','wind_gust_ms','m/s'),('d','wind_direction_deg','deg')]:
                        v=_f(row.get(k));
                        if v is not None:out.observations.append(Observation('NOAA_COOPS',station,iso(t),lat,lon,vn,v,un,'GOOD',0.92,iso(),None,{'product':prod}))
                else:
                    v=_f(row.get('v'))
                    if v is not None:
                        if var=='visibility_nm': var='visibility_m';v*=1852
                        out.observations.append(Observation('NOAA_COOPS',station,iso(t),lat,lon,var,v,'','GOOD',0.92,iso(),None,{'product':prod}))
        return out

# ------------------------------ AISHub ------------------------------------
class AISHubConnector:
    URL='https://data.aishub.net/ws.php'
    def __init__(self,http=None):self.http=http or HTTP();self.username=os.getenv('AISHUB_USERNAME','').strip()
    def fetch_area(self,lat:float,lon:float,span_deg:float=0.5,interval_min:int=30)->ConnectorResult:
        out=ConnectorResult('AISHUB')
        if not self.username:out.errors.append('AISHUB_USERNAME not configured');return out
        params={'username':self.username,'format':1,'output':'json','compress':0,'latmin':lat-span_deg,'latmax':lat+span_deg,'lonmin':lon-span_deg,'lonmax':lon+span_deg,'interval':interval_min}
        try:data=self.http.get_json(self.URL,params=params)
        except Exception as e:out.errors.append(str(e));return out
        rows=data if isinstance(data,list) else data.get('DATA',[]) if isinstance(data,dict) else []
        for r in rows:
            if not isinstance(r,dict):continue
            la=_f(r.get('LATITUDE') or r.get('LAT'));lo=_f(r.get('LONGITUDE') or r.get('LON'))
            if la is None or lo is None:continue
            t=parse_time(r.get('TIME') or r.get('TSTAMP')) or datetime.now(UTC)
            out.ais.append(AISTrack('AISHUB',str(r.get('MMSI','')),iso(t),la,lo,_f(r.get('SOG')),_f(r.get('COG')),_f(r.get('HEADING')),_f(r.get('ROT')),str(r.get('NAVSTAT','')),r.get('NAME'),str(r.get('IMO','')),_f(r.get('DRAUGHT')),dict(r)))
        return out

# ---------------------------- MarineTraffic -------------------------------
class MarineTrafficConnector:
    def __init__(self,http=None):self.http=http or HTTP();self.key=os.getenv('MARINETRAFFIC_API_KEY','').strip()
    def fetch_vessel(self,mmsi:Optional[str]=None,imo:Optional[str]=None)->ConnectorResult:
        out=ConnectorResult('MARINETRAFFIC')
        if not self.key:out.errors.append('MARINETRAFFIC_API_KEY not configured');return out
        params={'protocol':'jsono','timespan':30}
        if mmsi:params['mmsi']=mmsi
        elif imo:params['imo']=imo
        else:out.errors.append('mmsi or imo required');return out
        try:data=self.http.get_json(f'https://services.marinetraffic.com/api/exportvessel/{self.key}',params=params)
        except Exception as e:out.errors.append(str(e));return out
        rows=data.get('DATA',[]) if isinstance(data,dict) else data if isinstance(data,list) else []
        for r in rows:
            if not isinstance(r,dict):continue
            la=_f(r.get('LAT'));lo=_f(r.get('LON'))
            if la is None or lo is None:continue
            t=parse_time(r.get('TIMESTAMP')) or datetime.now(UTC)
            sog=_f(r.get('SPEED')); sog=sog/10 if sog is not None and sog>60 else sog
            dra=_f(r.get('DRAUGHT')); dra=dra/10 if dra is not None and dra>25 else dra
            out.ais.append(AISTrack('MARINETRAFFIC',str(r.get('MMSI','')),iso(t),la,lo,sog,_f(r.get('COURSE')),_f(r.get('HEADING')),_f(r.get('ROT')),str(r.get('STATUS','')),r.get('SHIPNAME'),str(r.get('IMO','')),dra,dict(r)))
        return out

# --------------------------- GEBCO local/OPeNDAP --------------------------
class GEBCOConnector:
    def __init__(self):
        self.path=os.getenv('GEBCO_NETCDF_PATH','').strip(); self.opendap=os.getenv('GEBCO_OPENDAP_URL','').strip(); self._ds=None
    def depth(self,lat:float,lon:float)->ConnectorResult:
        out=ConnectorResult('GEBCO')
        src=self.path or self.opendap
        if not src:out.errors.append('GEBCO_NETCDF_PATH or GEBCO_OPENDAP_URL not configured');return out
        try:
            import xarray as xr
            if self._ds is None:self._ds=xr.open_dataset(src)
            ds=self._ds
            latname=next((n for n in ('lat','latitude','y') if n in ds.coords),None); lonname=next((n for n in ('lon','longitude','x') if n in ds.coords),None)
            var=next((n for n in ('elevation','z','depth') if n in ds.data_vars),None)
            if not latname or not lonname or not var:raise ValueError('Could not identify GEBCO lat/lon/elevation variables')
            v=float(ds[var].sel({latname:lat,lonname:lon},method='nearest').values)
            out.context['bathymetry']={'source':'GEBCO','elevation_m':v,'water_depth_m':max(0.0,-v),'navigation_grade':False}
        except Exception as e:out.errors.append(str(e))
        return out

# ------------------------------ INCOIS RSMC -------------------------------
class INCOISRSMCConnector:
    INDEX='https://www.incois.gov.in/oceanservices/rsmc_download.jsp'
    def __init__(self,http=None):
        self.http=http or HTTP(); self.cache=os.getenv('INCOIS_CACHE_DIR','./data/incois_cache');os.makedirs(self.cache,exist_ok=True)
        self.base=os.getenv('INCOIS_RSMC_FILE_BASE','').strip()
    def discover_latest(self)->Dict[str,str]:
        text=self.http.get_text(self.INDEX); files=re.findall(r'(rsmc_combined_ww3_\d{8}\.nc|RSMC_hycom_\d{8}\.nc)',text,re.I)
        ans={}
        for f in files:
            k='ww3' if 'ww3' in f.lower() else 'hycom';
            if k not in ans or f>ans[k]:ans[k]=f
        return ans
    def _url_for(self,name):
        if self.base:return self.base.rstrip('/')+'/'+name
        # HTML does not expose a stable direct base in every deployment. Allow operator override.
        return ''
    def fetch_point_from_local(self,path:str,lat:float,lon:float,source_name:str)->ConnectorResult:
        out=ConnectorResult(source_name)
        try:
            import xarray as xr
            ds=xr.open_dataset(path)
            latn=next((n for n in ('lat','latitude','LATITUDE','y') if n in ds.coords),None); lonn=next((n for n in ('lon','longitude','LONGITUDE','x') if n in ds.coords),None)
            if not latn or not lonn:raise ValueError('lat/lon coordinates not identified')
            point=ds.sel({latn:lat,lonn:lon},method='nearest')
            aliases={
                'wave_height_m':['hs','swh','significant_wave_height','VHM0','HTSGW'],
                'wave_period_s':['tp','peak_wave_period','VTPK','PERPW'],
                'wave_direction_deg':['dp','wave_direction','VMDR','DIRPW'],
                'sea_surface_temperature_c':['sst','water_temp','thetao'],
                'sea_level_msl_m':['ssh','zos','surf_el'],
                'current_u_ms':['u','uo','water_u'],
                'current_v_ms':['v','vo','water_v'],
            }
            found={}
            for target,cands in aliases.items():
                found[target]=next((c for c in cands if c in point),None)
            vals={}
            for target,c in found.items():
                if not c: continue
                try:
                    da=point[c]
                    vals[target]=float(da.isel(time=0).values if 'time' in da.dims else da.values)
                except Exception: pass
            out.context['incois_point']={'source':source_name,'values':vals,'path':path}

            # Normalize every time step into forecast records when a time coordinate exists.
            times=[]
            if 'time' in point.coords:
                try:
                    import pandas as pd
                    times=[pd.Timestamp(t).to_pydatetime() for t in point['time'].values]
                except Exception:
                    times=[]
            if times:
                cycle=times[0]
                if cycle.tzinfo is None: cycle=cycle.replace(tzinfo=UTC)
                for i,t in enumerate(times[:192]):
                    if t.tzinfo is None:t=t.replace(tzinfo=UTC)
                    lead=max(0.0,(t-cycle).total_seconds()/3600.0)
                    scalar={}
                    for target,c in found.items():
                        if not c:continue
                        try:
                            da=point[c]
                            scalar[target]=float(da.isel(time=i).values if 'time' in da.dims else da.values)
                        except Exception:pass
                    # derive current vector products if components are present
                    if scalar.get('current_u_ms') is not None and scalar.get('current_v_ms') is not None:
                        u,v=scalar['current_u_ms'],scalar['current_v_ms']
                        scalar['current_speed_ms']=math.hypot(u,v)
                        # oceanographic direction TO; convert to compass degrees
                        scalar['current_direction_deg']=(math.degrees(math.atan2(u,v))+360.0)%360.0
                    for var,val in scalar.items():
                        if var in ('current_u_ms','current_v_ms'):continue
                        if not math.isfinite(val):continue
                        out.forecasts.append(Forecast(source_name,source_name,iso(cycle),iso(t),lead,lat,lon,var,val,'',None,os.path.basename(path),{'path':path,'native_variable':found.get(var)}))
        except Exception as e:out.errors.append(str(e))
        return out
    def fetch(self,lat:float,lon:float)->ConnectorResult:
        out=ConnectorResult('INCOIS_RSMC')
        # Operationally safe default: use operator-downloaded NetCDFs, never guess a direct download URL.
        ww3=os.getenv('INCOIS_WW3_NETCDF_PATH','').strip(); hy=os.getenv('INCOIS_HYCOM_NETCDF_PATH','').strip()
        for p,name in ((ww3,'INCOIS_WW3'),(hy,'INCOIS_HYCOM')):
            if p and os.path.exists(p):
                r=self.fetch_point_from_local(p,lat,lon,name);out.context.update(r.context);out.errors.extend(r.errors);out.forecasts.extend(r.forecasts)
        try:out.context['latest_files']=self.discover_latest()
        except Exception as e:out.errors.append(f'discovery:{e}')
        return out

# ------------------------------ Copernicus --------------------------------
class CopernicusConnector:
    def __init__(self):
        self.dataset=os.getenv('COPERNICUS_DATASET_ID','').strip(); self.variables=[x.strip() for x in os.getenv('COPERNICUS_VARIABLES','').split(',') if x.strip()]
    def fetch_point(self,lat:float,lon:float,start:Optional[datetime]=None,end:Optional[datetime]=None)->ConnectorResult:
        out=ConnectorResult('COPERNICUS_MARINE')
        if not self.dataset:out.errors.append('COPERNICUS_DATASET_ID not configured');return out
        try:
            import copernicusmarine
            st=start or datetime.now(UTC)-timedelta(hours=3); en=end or datetime.now(UTC)+timedelta(hours=6)
            kwargs=dict(dataset_id=self.dataset,minimum_longitude=lon-0.01,maximum_longitude=lon+0.01,minimum_latitude=lat-0.01,maximum_latitude=lat+0.01,start_datetime=st.isoformat(),end_datetime=en.isoformat())
            if self.variables:kwargs['variables']=self.variables
            ds=copernicusmarine.open_dataset(**kwargs)
            out.context['copernicus']={'dataset_id':self.dataset,'variables':list(ds.data_vars),'available':True}
            # normalize common variable aliases if present
            aliases={'sea_ice_concentration':['siconc','ice_concentration'],'sea_surface_temperature_c':['thetao','sst'],'sea_level_msl_m':['zos','ssh'],'current_u_ms':['uo'],'current_v_ms':['vo'],'wave_height_m':['VHM0','swh'],'wave_period_s':['VTPK','tp']}
            vals={}
            for target,cands in aliases.items():
                for c in cands:
                    if c in ds:
                        try:vals[target]=float(ds[c].isel({d:0 for d in ds[c].dims}).values);break
                        except:pass
            out.context['copernicus']['point_values']=vals
        except Exception as e:out.errors.append(str(e))
        return out

# ------------------------------ configurable ERDDAP -----------------------
class ERDDAPConnector:
    def __init__(self,http=None):self.http=http or HTTP();self.base=os.getenv('INCOIS_ERDDAP_BASE','https://erddap.incois.gov.in/erddap').rstrip('/')
    @staticmethod
    def load_specs():
        raw=os.getenv('INCOIS_ERDDAP_DATASETS_JSON','').strip()
        if not raw:return []
        try:
            if os.path.exists(raw):return json.load(open(raw,encoding='utf-8'))
            return json.loads(raw)
        except Exception:return []
    def fetch_table(self,dataset_id:str,columns:Sequence[str],constraints:Sequence[str])->ConnectorResult:
        out=ConnectorResult('INCOIS_ERDDAP')
        if not dataset_id:return out
        q=','.join(columns)+''.join('&'+x for x in constraints);url=f'{self.base}/tabledap/{dataset_id}.json?{q}'
        try:d=self.http.get_json(url)
        except Exception as e:out.errors.append(str(e));return out
        table=d.get('table',{}) if isinstance(d,dict) else {}; names=table.get('columnNames',[])
        out.context['rows']=[dict(zip(names,r)) for r in table.get('rows',[])]
        return out
    def fetch_observations(self,spec:Mapping[str,Any],lat:float,lon:float)->ConnectorResult:
        out=ConnectorResult('INCOIS_ERDDAP')
        ds=str(spec.get('dataset_id','')).strip()
        if not ds:out.errors.append('dataset_id missing');return out
        tcol=str(spec.get('time_col','time'));lac=str(spec.get('lat_col','latitude'));loc=str(spec.get('lon_col','longitude'));scol=str(spec.get('station_col','station'))
        variables=spec.get('variables') or {}
        if not isinstance(variables,dict) or not variables:out.errors.append('variables mapping missing');return out
        cols=[tcol,lac,loc,scol]+[str(v.get('column') if isinstance(v,dict) else v) for v in variables.values()]
        # preserve order while removing duplicates
        cols=list(dict.fromkeys(x for x in cols if x))
        radius=float(spec.get('radius_deg',3.0));hours=float(spec.get('lookback_hours',24.0))
        since=iso(datetime.now(UTC)-timedelta(hours=hours))
        cons=[f'{lac}>={lat-radius}',f'{lac}<={lat+radius}',f'{loc}>={lon-radius}',f'{loc}<={lon+radius}',f'{tcol}>={since}']
        cons.extend(str(x) for x in (spec.get('constraints') or []))
        r=self.fetch_table(ds,cols,cons);out.errors.extend(r.errors)
        for row in r.context.get('rows',[]):
            la=_f(row.get(lac));lo=_f(row.get(loc));t=parse_time(row.get(tcol))
            if la is None or lo is None or not t:continue
            sid=str(row.get(scol) or ds)
            for target,meta in variables.items():
                if isinstance(meta,dict): col=str(meta.get('column',''));unit=str(meta.get('unit',''));scale=_f(meta.get('scale'),1.0) or 1.0;offset=_f(meta.get('offset'),0.0) or 0.0
                else: col=str(meta);unit='';scale=1.0;offset=0.0
                v=_f(row.get(col))
                if v is None:continue
                v=v*scale+offset
                out.observations.append(Observation('INCOIS_ERDDAP',sid,iso(t),la,lo,str(target),v,unit,'PROVIDER_QC',float(spec.get('source_quality',0.9)),iso(),_f(spec.get('sigma')),{'dataset_id':ds,'native_column':col}))
        out.context={'dataset_id':ds,'records':len(out.observations)}
        return out


# ------------------------------ WMO/CAP ------------------------------------
class CAPWarningConnector:
    """Generic CAP 1.x warning-feed reader.

    Configure WMO_CAP_FEEDS as a comma-separated list of CAP XML URLs. This is
    intentionally generic because WWMIWS dissemination endpoints can vary by
    authority/METAREA; the connector preserves sender/identifier/provenance.
    """
    def __init__(self,http=None): self.http=http or HTTP()
    @staticmethod
    def _sev(v:str)->str:
        t=(v or '').lower()
        return 'CRITICAL' if t in ('extreme','severe') else 'HIGH' if t=='moderate' else 'CAUTION' if t=='minor' else 'NORMAL'
    def fetch(self)->ConnectorResult:
        import xml.etree.ElementTree as ET
        out=ConnectorResult('WMO_CAP')
        urls=[x.strip() for x in os.getenv('WMO_CAP_FEEDS','').split(',') if x.strip()]
        if not urls: out.errors.append('WMO_CAP_FEEDS not configured'); return out
        for url in urls:
            try:
                text=self.http.get_text(url); root=ET.fromstring(text)
                alerts=[root] if root.tag.endswith('alert') else [e for e in root.iter() if e.tag.endswith('alert')]
                for a in alerts:
                    def find(name, node=a):
                        e=next((x for x in node.iter() if x.tag.endswith(name)),None); return e.text.strip() if e is not None and e.text else ''
                    identifier=find('identifier'); sender=find('sender'); sent=parse_time(find('sent')) or datetime.now(UTC)
                    infos=[x for x in a.iter() if x.tag.endswith('info')]
                    for info in infos or [a]:
                        def fi(name):
                            e=next((x for x in info.iter() if x.tag.endswith(name)),None); return e.text.strip() if e is not None and e.text else ''
                        onset=parse_time(fi('onset')) or sent; expires=parse_time(fi('expires')) or (onset+timedelta(hours=12))
                        text_msg=' | '.join(x for x in (fi('headline'),fi('description'),fi('instruction')) if x)
                        area=next((x for x in info.iter() if x.tag.endswith('area')),None); area_desc=''; geometry=None
                        if area is not None:
                            e=next((x for x in area.iter() if x.tag.endswith('areaDesc')),None); area_desc=e.text.strip() if e is not None and e.text else ''
                            poly=next((x for x in area.iter() if x.tag.endswith('polygon')),None)
                            if poly is not None and poly.text:
                                coords=[]
                                for pair in poly.text.split():
                                    try: la,lo=map(float,pair.split(','));coords.append([lo,la])
                                    except: pass
                                if coords: geometry={'type':'Polygon','coordinates':[coords]}
                        out.warnings.append(Warning(sender or 'WMO/GMDSS','CAP_'+(fi('event') or 'MARINE_WARNING'),self._sev(fi('severity')),iso(sent),iso(onset),iso(expires),text_msg or fi('event'),identifier,True,geometry,area_desc,{'source_url':url,'urgency':fi('urgency'),'certainty':fi('certainty')}))
            except Exception as e: out.errors.append(f'{url}: {e}')
        return out

# ------------------------ Generic cyclone ensemble -------------------------
class CycloneEnsembleConnector:
    """Operator-configurable cyclone ensemble JSON connector.

    JSON may be a list of {member,time,lat,lon,wind_kn,radius_km} points or a
    mapping containing `members`. This allows GEFS/ECMWF/RSMC processing jobs to
    feed the engine without hard-wiring a fragile provider-specific parser.
    """
    def __init__(self,http=None): self.http=http or HTTP()
    def fetch(self)->ConnectorResult:
        out=ConnectorResult('CYCLONE_ENSEMBLE')
        url=os.getenv('CYCLONE_ENSEMBLE_JSON_URL','').strip(); path=os.getenv('CYCLONE_ENSEMBLE_JSON_PATH','').strip()
        try:
            if url: data=self.http.get_json(url)
            elif path and os.path.exists(path): data=json.load(open(path,encoding='utf-8'))
            else: out.errors.append('CYCLONE_ENSEMBLE_JSON_URL/PATH not configured'); return out
            rows=data.get('members',[]) if isinstance(data,dict) else data
            norm=[]
            for r in rows or []:
                if not isinstance(r,dict):continue
                la=_f(r.get('lat'));lo=_f(r.get('lon'))
                if la is None or lo is None:continue
                norm.append({'member':str(r.get('member','0')),'time':r.get('time'),'lat':la,'lon':lo,'wind_kn':_f(r.get('wind_kn')),'radius_km':_f(r.get('radius_km')),'source':r.get('source','configured_ensemble')})
            out.context['cyclone_ensemble']=norm
        except Exception as e: out.errors.append(str(e))
        return out

# ------------------------------ Sea ice ------------------------------------
class SeaIceNetCDFConnector:
    """Nearest-cell sea-ice concentration from a configured NetCDF/OPeNDAP file."""
    def __init__(self): self.path=os.getenv('SEA_ICE_NETCDF_PATH','').strip() or os.getenv('SEA_ICE_OPENDAP_URL','').strip(); self._ds=None
    def fetch(self,lat:float,lon:float)->ConnectorResult:
        out=ConnectorResult('SEA_ICE')
        if not self.path: out.errors.append('SEA_ICE_NETCDF_PATH/SEA_ICE_OPENDAP_URL not configured'); return out
        try:
            import xarray as xr
            if self._ds is None:self._ds=xr.open_dataset(self.path)
            ds=self._ds; latn=next((n for n in ('lat','latitude','y') if n in ds.coords),None);lonn=next((n for n in ('lon','longitude','x') if n in ds.coords),None)
            var=next((n for n in ('siconc','ice_concentration','sea_ice_fraction','cdr_seaice_conc') if n in ds.data_vars),None)
            if not latn or not lonn or not var:raise ValueError('sea-ice coordinate/variable aliases not found')
            da=ds[var].sel({latn:lat,lonn:lon},method='nearest'); val=float(da.isel({d:0 for d in da.dims}).values)
            if val>1.5: val/=100.0
            out.context['sea_ice']={'concentration':max(0.0,min(1.0,val)),'source':'configured sea-ice product','navigation_grade':False}
        except Exception as e:out.errors.append(str(e))
        return out

# ------------------------------ aggregator --------------------------------
class MarineConnectorHub:
    def __init__(self):
        self.imd=IMDConnector(); self.ge=GEBCOConnector(); self.incois=INCOISRSMCConnector(); self.ais=AISHubConnector(); self.mt=MarineTrafficConnector(); self.cop=CopernicusConnector(); self.erddap=ERDDAPConnector(); self.cap=CAPWarningConnector(); self.cyclone=CycloneEnsembleConnector(); self.ice=SeaIceNetCDFConnector()
    def collect(self,lat:float,lon:float,vessel:Optional[Mapping[str,Any]]=None)->List[ConnectorResult]:
        vessel=dict(vessel or {}); results=[]
        for conn,fn in [(self.imd,lambda:self.imd.fetch(lat,lon)),(self.incois,lambda:self.incois.fetch(lat,lon)),(self.ge,lambda:self.ge.depth(lat,lon))]:
            try:results.append(fn())
            except Exception as e:r=ConnectorResult(type(conn).__name__);r.errors.append(str(e));results.append(r)
        if os.getenv('VYOM_ENABLE_INCOIS_ERDDAP','0')=='1':
            for spec in self.erddap.load_specs():
                if isinstance(spec,dict):results.append(self.erddap.fetch_observations(spec,lat,lon))
        if os.getenv('VYOM_ENABLE_AIS','0')=='1':
            results.append(self.ais.fetch_area(lat,lon,float(os.getenv('AIS_SPAN_DEG','0.5'))))
        if os.getenv('VYOM_ENABLE_COPERNICUS','0')=='1':results.append(self.cop.fetch_point(lat,lon))
        if os.getenv('VYOM_ENABLE_WMO_CAP','0')=='1':results.append(self.cap.fetch())
        if os.getenv('VYOM_ENABLE_CYCLONE_ENSEMBLE','0')=='1':results.append(self.cyclone.fetch())
        if os.getenv('VYOM_ENABLE_SEA_ICE','0')=='1':results.append(self.ice.fetch(lat,lon))
        return results

__all__=['ConnectorResult','IMDConnector','NDBCConnector','NOAA_COOPS_Connector','AISHubConnector','MarineTrafficConnector','GEBCOConnector','INCOISRSMCConnector','CopernicusConnector','ERDDAPConnector','CAPWarningConnector','CycloneEnsembleConnector','SeaIceNetCDFConnector','MarineConnectorHub']
