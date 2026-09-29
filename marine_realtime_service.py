"""Operational real-feed orchestration for VYOM-MARINE v4.

Collects authoritative/public feeds, persists provenance, injects only supported
context into the marine engine, stores model forecasts, matches observations, and
exports learned calibration snapshots.
"""
from __future__ import annotations

import json, math, os, time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence

from marine_data_fabric import MarineDataFabric, Forecast, Observation, Warning, AISTrack, basin_name, iso, parse_time, haversine_km, season_key
from marine_connectors import MarineConnectorHub, NDBCConnector, NOAA_COOPS_Connector

UTC=timezone.utc

ATMO_ATTRS={
    'wind_speed_ms':'wind_speed_ms','wind_gust_ms':'wind_gust_ms','wind_direction_deg':'wind_direction_deg',
    'pressure_hpa':'pressure_hpa','visibility_m':'visibility_m','temperature_c':'temperature_c',
}
WAVE_ATTRS={
    'wave_height_m':'wave_height_m','wave_period_s':'wave_period_s','wave_direction_deg':'wave_direction_deg',
    'swell_height_m':'swell_height_m','swell_period_s':'swell_period_s','current_speed_ms':'current_speed_ms','current_direction_deg':'current_direction_deg',
}

class MarineRealtimeService:
    def __init__(self, db_path: Optional[str]=None):
        self.fabric=MarineDataFabric(db_path)
        self.hub=MarineConnectorHub()
        self.ndbc=NDBCConnector(); self.coops=NOAA_COOPS_Connector()

    @staticmethod
    def _read_station_config(env_name: str) -> List[Dict[str,Any]]:
        raw=os.getenv(env_name,'').strip()
        if not raw:return []
        try:
            if os.path.exists(raw):
                with open(raw,'r',encoding='utf-8') as f:return json.load(f)
            return json.loads(raw)
        except Exception:return []

    @staticmethod
    def _point_in_ring(lon: float, lat: float, ring: Sequence[Sequence[float]]) -> bool:
        inside=False; j=len(ring)-1
        for i in range(len(ring)):
            xi,yi=float(ring[i][0]),float(ring[i][1]); xj,yj=float(ring[j][0]),float(ring[j][1])
            if ((yi>lat)!=(yj>lat)) and (lon < (xj-xi)*(lat-yi)/((yj-yi) or 1e-12)+xi): inside=not inside
            j=i
        return inside

    @classmethod
    def _warning_region_relevant(cls, w: Warning, lat: float, lon: float) -> bool:
        txt=((w.region_text or '')+' '+(w.text or '')).lower(); basin=basin_name(lat,lon)
        if w.geometry:
            try:
                typ=w.geometry.get('type'); co=w.geometry.get('coordinates') or []
                polys=co if typ=='MultiPolygon' else [co] if typ=='Polygon' else []
                for poly in polys:
                    if poly and cls._point_in_ring(lon,lat,poly[0]): return True
                return False
            except Exception:
                return False
        if basin=='BAY_OF_BENGAL' and any(x in txt for x in ('bay of bengal','north bay','central bay','south bay','andaman sea','west bengal','odisha','andhra','tamilnadu coast')):return True
        if basin=='ARABIAN_SEA' and any(x in txt for x in ('arabian sea','kerala','karnataka','goa','maharashtra','gujarat','lakshadweep')):return True
        if basin=='INDIAN_OCEAN' and any(x in txt for x in ('indian ocean','comorin','maldives')):return True
        return False

    def collect_configured_observations(self, lat: float, lon: float) -> List[Dict[str,Any]]:
        summaries=[]
        ndbc_stations=self._read_station_config('NDBC_STATIONS_JSON')
        if not ndbc_stations and os.getenv('VYOM_AUTO_NDBC','1')=='1':
            ndbc_stations=self.ndbc.discover_nearest(lat,lon,limit=int(os.getenv('AUTO_NDBC_LIMIT','2')),max_distance_km=float(os.getenv('AUTO_NDBC_RADIUS_KM','900')))
        for s in ndbc_stations:
            try:
                if haversine_km(lat,lon,float(s['lat']),float(s['lon']))>float(s.get('max_distance_km',600)):continue
                t=time.perf_counter();r=self.ndbc.fetch_station(str(s['id']),float(s['lat']),float(s['lon']))
                n=self.fabric.upsert_observations(r.observations);self.fabric.source_health('NOAA_NDBC',not r.errors,(time.perf_counter()-t)*1000,n,'; '.join(r.errors));summaries.append(r.as_dict())
            except Exception as e:self.fabric.source_health('NOAA_NDBC',False,0,0,str(e))
        coops_stations=self._read_station_config('NOAA_COOPS_STATIONS_JSON')
        if not coops_stations and os.getenv('VYOM_AUTO_COOPS','1')=='1':
            coops_stations=self.coops.discover_nearest(lat,lon,limit=int(os.getenv('AUTO_COOPS_LIMIT','2')),max_distance_km=float(os.getenv('AUTO_COOPS_RADIUS_KM','900')))
        for s in coops_stations:
            try:
                if haversine_km(lat,lon,float(s['lat']),float(s['lon']))>float(s.get('max_distance_km',600)):continue
                t=time.perf_counter();r=self.coops.fetch_station(str(s['id']),float(s['lat']),float(s['lon']))
                n=self.fabric.upsert_observations(r.observations);self.fabric.source_health('NOAA_COOPS',not r.errors,(time.perf_counter()-t)*1000,n,'; '.join(r.errors));summaries.append(r.as_dict())
            except Exception as e:self.fabric.source_health('NOAA_COOPS',False,0,0,str(e))
        return summaries

    def collect_live_context(self, lat: float, lon: float, vessel: Optional[Mapping[str,Any]]=None) -> Dict[str,Any]:
        cfg=dict(vessel or {}); started=time.perf_counter(); results=self.hub.collect(lat,lon,cfg)
        source_summaries=[]; merged_context={}; warnings=[]; ais=[]; cyclone_points=[]
        for r in results:
            nobs=self.fabric.upsert_observations(r.observations);nfc=self.fabric.upsert_forecasts(r.forecasts);nw=self.fabric.upsert_warnings(r.warnings);na=self.fabric.upsert_ais(r.ais)
            self.fabric.source_health(r.source,not r.errors,0,nobs+nfc+nw+na,'; '.join(r.errors))
            source_summaries.append(r.as_dict())
            ctx=dict(r.context or {})
            cyclone_points.extend(ctx.pop('cyclone_ensemble',[]) or [])
            merged_context.update(ctx);warnings.extend(r.warnings);ais.extend(r.ais)
        source_summaries.extend(self.collect_configured_observations(lat,lon))

        # Translate nearest recent observations into the engine's assimilation schema.
        near=self.fabric.latest_observations_near(lat,lon,float(os.getenv('MARINE_OBS_RADIUS_KM','300')),float(os.getenv('MARINE_OBS_MAX_AGE_H','12')))
        best={}
        target_map={'wave_height_m':'wave_height_m','wind_speed_ms':'wind_kn','pressure_hpa':'pressure_hpa','visibility_m':'visibility_m'}
        for o in near:
            target=target_map.get(o['variable'])
            if not target:continue
            q=float(o.get('source_quality') or 0.5);age=(datetime.now(UTC)-(parse_time(o['observed_at']) or datetime.now(UTC))).total_seconds()/3600
            score=q*math.exp(-age/8)*math.exp(-float(o.get('distance_km') or 0)/250)
            if target not in best or score>best[target][0]:
                val=float(o['value']);
                if target=='wind_kn':val*=1.943844
                sigma=float(o['sigma']) if o.get('sigma') is not None else {'wave_height_m':0.25,'wind_kn':2.5,'pressure_hpa':1.5,'visibility_m':800}.get(target,1)
                best[target]=(score,{'value':val,'sigma':sigma,'source':o['source'],'station_id':o['station_id'],'observed_at':o['observed_at'],'distance_km':round(o['distance_km'],2)})
        sensor={k:v[1] for k,v in best.items()}

        # Normalize AIS contacts for the engine's CPA/TCPA functions.
        tracks=self.fabric.latest_ais_near(lat,lon,float(os.getenv('AIS_RADIUS_KM','80')),int(os.getenv('AIS_MAX_AGE_MIN','30')))
        contacts=[]
        for a in tracks:
            hd=a.get('heading_deg') if a.get('heading_deg') not in (None,511) else a.get('cog_deg')
            if hd is None:continue
            contacts.append({'id':a.get('mmsi'),'mmsi':a.get('mmsi'),'lat':a['lat'],'lon':a['lon'],'speed_kn':a.get('sog_kn') or 0.0,'heading_deg':hd,'name':a.get('vessel_name'),'observed_at':a.get('observed_at')})

        # Relevant official warning: never use a named-area warning as a global warning.
        relevant=[w for w in warnings if self._warning_region_relevant(w,lat,lon)]
        rank={'NORMAL':0,'CAUTION':1,'HIGH':2,'CRITICAL':3}
        strongest=max(relevant,key=lambda w:rank.get(w.severity,0),default=None)

        # Convert flat cyclone points from official/configured feeds into the engine's member-track schema.
        cyclone_members=[]
        if cyclone_points:
            groups={}
            for p in cyclone_points:
                if not isinstance(p,dict) or p.get('lat') is None or p.get('lon') is None:continue
                mid=str(p.get('member','official'))
                groups.setdefault(mid,{'member':mid,'track':[],'max_wind_kn':0.0,'hazard_radius_km':float(p.get('radius_km') or 150.0),'source':p.get('source')})
                groups[mid]['track'].append({'time':p.get('time'),'lat':float(p['lat']),'lon':float(p['lon'])})
                groups[mid]['max_wind_kn']=max(groups[mid]['max_wind_kn'],float(p.get('wind_kn') or 0.0))
            cyclone_members=list(groups.values())

        # Bathymetry is context only unless operator opts into using non-navigation-grade depth for screening.
        bathy=(merged_context.get('bathymetry') or {})
        ice=(merged_context.get('sea_ice') or {})
        cop=(merged_context.get('copernicus') or {})
        cpv=(cop.get('point_values') or {}) if isinstance(cop,dict) else {}
        ice_conc=ice.get('concentration')
        if ice_conc is None: ice_conc=cpv.get('sea_ice_concentration')
        ext={
            'realtime_feeds':{'collected_at':iso(),'sources':source_summaries},
            'data_provenance':source_summaries,
            'bathymetry_context':bathy,
            'cyclone_wind':merged_context.get('cyclone_wind'),
            'cyclone_cone':merged_context.get('cyclone_cone'),
            'incois_point':merged_context.get('incois_point'),
            'copernicus':merged_context.get('copernicus'),
            'ice_concentration_pct': (float(ice_conc)*100.0 if ice_conc is not None and float(ice_conc)<=1.5 else ice_conc),
            'ice_thickness_m': ice.get('thickness_m'),
            'ice_drift_ms': ice.get('drift_ms'),
        }
        patch={'external_context':ext,'sensor_observations':sensor,'traffic_contacts':contacts}
        if cyclone_members:patch['cyclone_ensemble']=cyclone_members
        if strongest:
            patch.update({'official_warning_level':strongest.severity.lower().replace('high','high'),'official_warning_text':strongest.text,'official_warning_source':strongest.authority,'official_warning_issued_at':strongest.issued_at})
        # GEBCO is not navigation grade. Use it only for generic shallow-water screening when explicitly opted in.
        if os.getenv('VYOM_ALLOW_GEBCO_SCREENING_DEPTH','0')=='1' and bathy.get('water_depth_m') is not None and not cfg.get('water_depth_m'):
            patch['water_depth_m']=bathy['water_depth_m']; ext['water_depth_source']='GEBCO_NON_NAVIGATION_GRADE'

        return {'patch':patch,'sources':source_summaries,'near_observations':near[:20],'elapsed_ms':round((time.perf_counter()-started)*1000,1),'fabric_stats':self.fabric.stats()}

    def persist_engine_forecasts(self, lat: float, lon: float, atmos_members: Sequence[Any], wave_members: Sequence[Any]) -> int:
        recs=[]; now=datetime.now(UTC)
        def add_member(member,attrs,source):
            # Open-Meteo response does not expose the original cycle in this engine; infer cycle as fetch time
            # and preserve valid times/lead hours. This is still useful for verification, but direct-provider
            # connectors should store native cycles whenever available.
            cycle=now
            for i,h in enumerate(member.hours):
                vt=parse_time(h.time) or (now+timedelta(hours=i));lead=max(0,(vt-cycle).total_seconds()/3600)
                for var,attr in attrs.items():
                    v=getattr(h,attr,None)
                    if v is None:continue
                    recs.append(Forecast(source,member.model,iso(cycle),iso(vt),lead,lat,lon,var,float(v),'',None,None,{'gateway':'Open-Meteo'}))
        for m in atmos_members:add_member(m,ATMO_ATTRS,'OPEN_METEO_ATMOS')
        for m in wave_members:add_member(m,WAVE_ATTRS,'OPEN_METEO_MARINE')
        return self.fabric.upsert_forecasts(recs)

    @staticmethod
    def _sea_state_band(hs: Optional[float]) -> str:
        if hs is None:return "unknown"
        h=float(hs)
        return "calm" if h<1 else "moderate" if h<2.5 else "rough" if h<4 else "very_rough"

    @staticmethod
    def _encounter_band(angle: Optional[float]) -> str:
        if angle is None:return "unknown"
        a=abs(float(angle))%360
        if a>180:a=360-a
        return "head" if a<=45 else "beam" if 45<a<135 else "following"

    def log_engine_result(self, result: Mapping[str,Any], vessel_cfg: Mapping[str,Any], lat: float, lon: float) -> int:
        vessel_id=str(vessel_cfg.get("vessel_id") or "")
        if not vessel_id:return 0
        vessel_class=str(vessel_cfg.get("vessel_type") or (result.get("vessel") or {}).get("profile_used") or "generic")
        peak=result.get("modeled_peak") or {}; hs=peak.get("wave_height_m"); ang=peak.get("encounter_angle_deg")
        sea=self._sea_state_band(hs); enc=self._encounter_band(ang); loading=str(vessel_cfg.get("cargo_state") or vessel_cfg.get("ballast_state") or "unknown")
        valid=str(peak.get("time") or iso()); engine_version=str(result.get("version") or "")
        risks=result.get("risk") or {}; count=0
        for event,info in risks.items():
            if not isinstance(info,Mapping):continue
            p=info.get("probability_high_or_worse")
            if p is None:continue
            pid=f"{vessel_id}:{valid}:{event}:{engine_version}"
            if self.fabric.log_risk_prediction(pid,vessel_id,vessel_class,valid,lat,lon,event,float(p),loading,sea,enc,engine_version,{"peak":peak}):count+=1
        return count

    def verify_and_calibrate(self, lookback_days:int=10) -> Dict[str,Any]:
        matched=self.fabric.match_recent(lookback_days=lookback_days,spatial_km=float(os.getenv('CAL_MATCH_RADIUS_KM','75')),temporal_minutes=float(os.getenv('CAL_MATCH_TIME_MIN','90')))
        cells=self.fabric.recompute_calibration(window_days=int(os.getenv('CAL_WINDOW_DAYS','365')),min_samples=int(os.getenv('CAL_MIN_SAMPLES','8')))
        return {'new_matches':matched,'calibration_cells_recomputed':cells,'stats':self.fabric.stats()}

    def direct_forecast_payload(self, lat: float, lon: float, horizon_hours: int=120) -> Dict[str,List[Dict[str,Any]]]:
        models=[x.strip() for x in os.getenv("VYOM_DIRECT_MARINE_MODELS","INCOIS_WW3,INCOIS_HYCOM").split(",") if x.strip()]
        return self.fabric.future_forecast_series(lat,lon,horizon_hours,float(os.getenv("DIRECT_MODEL_RADIUS_KM","150")),models)

    def calibration_path_for(self, lat: float, lon: float, models: Sequence[str], variables: Sequence[str]) -> str:
        path=os.getenv('VYOM_DYNAMIC_CALIBRATION_PATH','data/marine/calibration/dynamic_calibration.json')
        return self.fabric.export_engine_calibration(path,lat,lon,models,variables)

    def health(self)->Dict[str,Any]:return {'sources':self.fabric.source_status(),'stats':self.fabric.stats()}

__all__=['MarineRealtimeService']
