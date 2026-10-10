"""Chart telemetry for the private VYOM site, separate from engine risk results."""
import asyncio

SURFACE_FIELDS = (
    "temperature_2m", "relative_humidity_2m", "precipitation", "wind_speed_10m",
    "wind_gusts_10m", "visibility", "uv_index", "surface_pressure",
)


def chart_forecast(surface, marine=None):
    """Keep provider values and missingness; align waves by timestamp, never index."""
    source = surface.get("hourly") or {}
    times = source.get("time") or []
    if not times:
        return {"hourly": {}, "utc_offset_seconds": 0, "source": "Open-Meteo", "status": "unavailable"}
    hourly = {"time": times}
    for field in SURFACE_FIELDS:
        values = source.get(field)
        if isinstance(values, list):
            hourly[field] = values
    sea = (marine or {}).get("hourly") or {}
    wave_times, waves = sea.get("time") or [], sea.get("wave_height") or []
    if wave_times:
        lookup = dict(zip(wave_times, waves))
        hourly["wave_height"] = [lookup.get(t) for t in times]
    return {
        "hourly": hourly, "utc_offset_seconds": 0, "timezone": "GMT",
        "hourly_units": {**(surface.get("hourly_units") or {}), **((marine or {}).get("hourly_units") or {})},
        "source": "Open-Meteo forecast; separate from VYOM engine assessment", "status": "ok",
    }


async def collect_chart_forecast(lat, lon, include_marine=False):
    import httpx

    async with httpx.AsyncClient(timeout=15.0) as client:
        async def get(url, params):
            try:
                response = await client.get(url, params=params)
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError):
                return {}
        common = {"latitude": lat, "longitude": lon, "forecast_hours": 72, "timezone": "UTC"}
        surface_task = get("https://api.open-meteo.com/v1/forecast", {**common, "hourly": ",".join(SURFACE_FIELDS), "wind_speed_unit": "kmh"})
        if include_marine:
            surface, marine = await asyncio.gather(surface_task, get("https://marine-api.open-meteo.com/v1/marine", {**common, "hourly": "wave_height", "cell_selection": "sea"}))
        else:
            surface, marine = await surface_task, None
        return chart_forecast(surface, marine)
