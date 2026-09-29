from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from aviation_connectors import AviationWeatherConnector
from aviation_data_fabric import AviationDataFabric


class AviationRealtimeService:
    def __init__(self, db_path: Optional[str] = None):
        self.fabric = AviationDataFabric(db_path)
        self.awc = AviationWeatherConnector()
        self.calibration_export = os.getenv(
            "VYOM_AVIATION_DYNAMIC_CALIBRATION",
            "data/aviation/calibration/dynamic_calibration.json",
        )

    @staticmethod
    def _record_source(sources, status_map, name: str, result: Mapping[str, Any]):
        status = str(result.get("status") or "unknown")
        records = result.get("records") or []
        meta = result.get("meta")
        sources.append({"source": name, "status": status, "record_count": len(records), "meta": meta})
        status_map[name] = status
        return status, records, meta

    def collect_live_context(self, lat: float, lon: float, aircraft: Mapping[str, Any]) -> Dict[str, Any]:
        started = time.time()
        patch: Dict[str, Any] = {}
        sources = []
        source_status: Dict[str, str] = {}

        stations = []
        for k in ("departure_icao", "arrival_icao", "alternate_icao"):
            v = aircraft.get(k)
            if v and str(v).upper() not in stations:
                stations.append(str(v).upper())

        metars = []
        tafs = []
        pireps = []

        for st in stations[:3]:
            m = self.awc.metar(st)
            m_status, m_records, m_meta = self._record_source(sources, source_status, f"AWC_METAR:{st}", m)
            metars += m_records
            self.fabric.source_status(
                f"AWC_METAR:{st}", m_status, (m_meta or {}).get("latency_ms") if isinstance(m_meta, Mapping) else None,
                len(m_records), (m_meta or {}).get("error") if isinstance(m_meta, Mapping) else None,
            )

            t = self.awc.taf(st)
            t_status, t_records, t_meta = self._record_source(sources, source_status, f"AWC_TAF:{st}", t)
            tafs += t_records
            self.fabric.source_status(
                f"AWC_TAF:{st}", t_status, (t_meta or {}).get("latency_ms") if isinstance(t_meta, Mapping) else None,
                len(t_records), (t_meta or {}).get("error") if isinstance(t_meta, Mapping) else None,
            )

            p = self.awc.pireps(st, 150)
            p_status, p_records, p_meta = self._record_source(sources, source_status, f"AWC_PIREP:{st}", p)
            pireps += p_records
            self.fabric.source_status(
                f"AWC_PIREP:{st}", p_status, (p_meta or {}).get("latency_ms") if isinstance(p_meta, Mapping) else None,
                len(p_records), (p_meta or {}).get("error") if isinstance(p_meta, Mapping) else None,
            )

        sigmet = self.awc.sigmets(lat, lon)
        sig_status, sigmets, sig_meta = self._record_source(sources, source_status, "AWC_SIGMET", sigmet)
        self.fabric.source_status(
            "AWC_SIGMET", sig_status,
            None if not isinstance(sig_meta, list) else None,
            len(sigmets),
            None,
        )

        if metars:
            patch["metar_context"] = metars[0]
            for m in metars:
                obs_time = m.get("observed_at") or datetime.now(timezone.utc).isoformat()
                olat = m.get("lat") if m.get("lat") is not None else lat
                olon = m.get("lon") if m.get("lon") is not None else lon
                for var, key, unit in (
                    ("temperature_2m", "temperature_c", "C"),
                    ("visibility_m", "visibility_m", "m"),
                    ("wind_speed_10m_kt", "wind_speed_kt", "kt"),
                    ("surface_pressure_hpa", "altimeter_hpa", "hPa"),
                ):
                    if m.get(key) is not None:
                        self.fabric.add_observation(
                            "AWC_METAR", m.get("station"), obs_time, olat, olon,
                            var, m[key], unit, .96, m,
                        )

        if tafs:
            patch["taf_context"] = tafs
        if pireps:
            patch["pirep_context"] = pireps

        patch["source_status"] = source_status
        patch["official_warning_feed_status"] = sig_status

        if sigmets:
            patch["official_warnings"] = sigmets
            patch["official_warning_level"] = "sigmet"
            patch["official_warning_source"] = "NOAA/NWS Aviation Weather Center"
            patch["official_warning_text"] = (
                "One or more SIGMET products were returned in the queried regional context; "
                "their actual geometry, validity, and bulletin text must be checked for operational use."
            )
        elif sig_status == "success_none":
            # Explicitly distinguish a successfully checked empty result from a failed feed.
            patch["official_warning_level"] = "none"
            patch["official_warning_source"] = "NOAA/NWS Aviation Weather Center"
            patch["official_warning_text"] = None

        return {
            "status": "ok",
            "patch": patch,
            "sources": sources,
            "source_status": source_status,
            "fabric_stats": self.fabric.stats(),
            "elapsed_ms": round((time.time() - started) * 1000, 1),
            "dynamic_calibration_path": self.calibration_path(),
        }

    def record_model_forecast(self, lat, lon, fused, members):
        now = datetime.now(timezone.utc).isoformat()
        model_names = ",".join(m.model for m in members)
        for i, row in enumerate(fused):
            valid = str(row.get("time"))
            for var in ("temperature_2m", "visibility_m", "wind_speed_10m_kt", "surface_pressure_hpa"):
                source_key = {
                    "temperature_2m": "temperature_c",
                    "visibility_m": "visibility_m",
                    "wind_speed_10m_kt": "wind_speed_10m_kt",
                    "surface_pressure_hpa": "surface_pressure_hpa",
                }[var]
                if row.get(source_key) is not None:
                    self.fabric.add_forecast(
                        "VYOM_FUSION:" + model_names, now, valid, i + 1, lat, lon,
                        var, row[source_key], payload={"model_count": row.get("model_count")},
                    )

    def verify_and_calibrate(self, lookback_days=30):
        matches = self.fabric.match(lookback_days=lookback_days)
        cells = self.fabric.recompute_calibration()
        path = self.fabric.export_calibration_json(self.calibration_export)
        return {"matches_added": matches, "calibration_cells": cells, "path": path, "stats": self.fabric.stats()}

    def calibration_path(self):
        p = Path(self.calibration_export)
        return str(p) if p.exists() else None

    def health(self):
        return {"status": "ok", "fabric": self.fabric.stats(), "calibration_path": self.calibration_path()}
