"""
NWS Weather Forecast Fetcher
=============================
Pulls forecast data from the National Weather Service api.weather.gov.

Workflow:
  1. GET /points/{lat},{lon}  → returns grid office + coords
  2. GET /gridpoints/{office}/{x},{y}/forecast  → 7-day / 12-hr periods
  3. GET /gridpoints/{office}/{x},{y}/forecast/hourly  → hourly forecast
  4. GET /stations/{id}/observations/latest  → latest observation

No API key required; just a User-Agent header with contact info.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

import requests

from config import (
    NWS_API_BASE,
    FORECAST_LAT,
    FORECAST_LON,
    REQUEST_TIMEOUT,
    USER_AGENT,
)

_EASTERN = ZoneInfo("America/New_York")
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class HourlyChartData:
    """48-hour hourly forecast for the bottom chart panel."""
    times: List[datetime] = field(default_factory=list)
    temperature_f: List[float] = field(default_factory=list)
    precip_pct: List[float] = field(default_factory=list)
    cloud_cover_pct: List[float] = field(default_factory=list)
    pressure_inhg: List[float] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class ForecastPeriod:
    """One period from the NWS 7-day forecast (typically 12 hours)."""
    name: str                    # e.g. "Tonight", "Tuesday"
    start_time: str              # ISO 8601
    end_time: str                # ISO 8601
    is_daytime: bool
    temperature: int             # °F (NWS default for US forecasts)
    temperature_unit: str        # "F" or "C"
    wind_speed: str              # e.g. "10 to 15 mph"
    wind_direction: str          # e.g. "NW"
    short_forecast: str          # e.g. "Mostly Clear"
    detailed_forecast: str       # full text
    precip_chance: Optional[int] = None  # percent


@dataclass
class CurrentObservation:
    """Latest observation from the nearest NWS station."""
    station_id: str
    timestamp: str
    description: str             # e.g. "Overcast"
    temperature_f: Optional[float] = None
    dewpoint_f: Optional[float] = None
    humidity: Optional[float] = None     # percent
    wind_dir: Optional[str] = None       # cardinal
    wind_speed_mph: Optional[float] = None
    wind_gust_mph: Optional[float] = None
    pressure_inhg: Optional[float] = None
    visibility_mi: Optional[float] = None


@dataclass
class WeatherData:
    """Full weather package: current conditions + forecast periods."""
    location_name: str
    current: Optional[CurrentObservation] = None
    forecast_periods: list[ForecastPeriod] = None
    error: Optional[str] = None

    def __post_init__(self):
        if self.forecast_periods is None:
            self.forecast_periods = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_json(url: str) -> Optional[dict]:
    """GET a URL expecting JSON, return parsed dict or None."""
    try:
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/geo+json",
                "Cache-Control": "no-cache",
            },
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None
    except ValueError as exc:
        logger.warning("Invalid JSON from %s: %s", url, exc)
        return None


def _c_to_f(c: Optional[float]) -> Optional[float]:
    """Convert Celsius to Fahrenheit, handling None."""
    return round(c * 9 / 5 + 32, 1) if c is not None else None


def _safe_val(obs_prop: dict) -> Optional[float]:
    """Extract .value from an NWS observation property dict."""
    if obs_prop is None:
        return None
    return obs_prop.get("value")


# ---------------------------------------------------------------------------
# Step 1: Resolve lat/lon → grid metadata
# ---------------------------------------------------------------------------
_grid_cache: dict[tuple[float, float], dict] = {}


def _resolve_grid(lat: float, lon: float) -> Optional[dict]:
    """
    Call /points/{lat},{lon} to get the forecast office, grid coords,
    and observation station URLs.  Result is cached.
    """
    key = (lat, lon)
    if key in _grid_cache:
        return _grid_cache[key]

    url = f"{NWS_API_BASE}/points/{lat},{lon}"
    data = _get_json(url)
    if data is None:
        return None

    props = data.get("properties", {})
    result = {
        "forecast_url": props.get("forecast"),
        "forecast_hourly_url": props.get("forecastHourly"),
        "grid_data_url": props.get("forecastGridData"),
        "observation_stations_url": props.get("observationStations"),
        "city": props.get("relativeLocation", {}).get("properties", {}).get("city", ""),
        "state": props.get("relativeLocation", {}).get("properties", {}).get("state", ""),
    }
    _grid_cache[key] = result
    return result


# ---------------------------------------------------------------------------
# Step 2: Fetch 7-day forecast
# ---------------------------------------------------------------------------
def _fetch_forecast_periods(forecast_url: str) -> list[ForecastPeriod]:
    data = _get_json(forecast_url)
    if data is None:
        return []

    periods = []
    for p in data.get("properties", {}).get("periods", []):
        # Extract precipitation probability
        precip = None
        prob = p.get("probabilityOfPrecipitation")
        if prob and prob.get("value") is not None:
            precip = int(prob["value"])

        periods.append(ForecastPeriod(
            name=p.get("name", ""),
            start_time=p.get("startTime", ""),
            end_time=p.get("endTime", ""),
            is_daytime=p.get("isDaytime", True),
            temperature=p.get("temperature", 0),
            temperature_unit=p.get("temperatureUnit", "F"),
            wind_speed=p.get("windSpeed", ""),
            wind_direction=p.get("windDirection", ""),
            short_forecast=p.get("shortForecast", ""),
            detailed_forecast=p.get("detailedForecast", ""),
            precip_chance=precip,
        ))
    return periods


# ---------------------------------------------------------------------------
# Step 3: Fetch current observation
# ---------------------------------------------------------------------------
def _fetch_current_observation(stations_url: str) -> Optional[CurrentObservation]:
    from datetime import timezone as _tz
    data = _get_json(stations_url)
    if data is None:
        return None

    features = data.get("features", [])
    if not features:
        logger.warning("No observation stations found")
        return None

    now_utc = datetime.now(_tz.utc)
    props = None
    # Try up to 3 stations; skip any whose latest observation is >2 hours old
    for feature in features[:3]:
        station_id = feature.get("properties", {}).get("stationIdentifier", "")
        obs_url = f"{NWS_API_BASE}/stations/{station_id}/observations/latest"
        obs_data = _get_json(obs_url)
        if obs_data is None:
            continue
        candidate = obs_data.get("properties", {})
        ts_str = candidate.get("timestamp", "")
        if ts_str:
            try:
                obs_time = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                age_hours = (now_utc - obs_time).total_seconds() / 3600
                if age_hours > 2.0 and feature is not features[min(2, len(features) - 1)]:
                    logger.info("Station %s obs is %.1fh old, trying next", station_id, age_hours)
                    continue
            except ValueError:
                pass
        props = candidate
        break

    if props is None:
        return None

    # Temperature & dewpoint come in Celsius from the API
    temp_c = _safe_val(props.get("temperature"))
    dewp_c = _safe_val(props.get("dewpoint"))
    humidity = _safe_val(props.get("relativeHumidity"))

    # Wind in m/s from API
    wind_speed_ms = _safe_val(props.get("windSpeed"))
    wind_gust_ms = _safe_val(props.get("windGust"))
    wind_speed_mph = round(wind_speed_ms * 2.23694, 1) if wind_speed_ms else None
    wind_gust_mph = round(wind_gust_ms * 2.23694, 1) if wind_gust_ms else None

    # Pressure in Pa from API → inHg
    pressure_pa = _safe_val(props.get("barometricPressure"))
    pressure_inhg = round(pressure_pa * 0.00029530, 2) if pressure_pa else None

    # Visibility in meters → miles
    vis_m = _safe_val(props.get("visibility"))
    vis_mi = round(vis_m / 1609.34, 1) if vis_m else None

    # Wind direction in degrees → cardinal
    wind_deg = _safe_val(props.get("windDirection"))
    wind_dir = None
    if wind_deg is not None:
        dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
                "S","SSW","SW","WSW","W","WNW","NW","NNW"]
        wind_dir = dirs[int((wind_deg + 11.25) / 22.5) % 16]

    return CurrentObservation(
        station_id=station_id,
        timestamp=props.get("timestamp", ""),
        description=props.get("textDescription", ""),
        temperature_f=_c_to_f(temp_c),
        dewpoint_f=_c_to_f(dewp_c),
        humidity=round(humidity, 1) if humidity else None,
        wind_dir=wind_dir,
        wind_speed_mph=wind_speed_mph,
        wind_gust_mph=wind_gust_mph,
        pressure_inhg=pressure_inhg,
        visibility_mi=vis_mi,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def fetch_weather(lat: float = FORECAST_LAT,
                  lon: float = FORECAST_LON) -> WeatherData:
    """
    Fetch current conditions + 7-day forecast for a lat/lon.
    Returns a WeatherData object.
    """
    grid = _resolve_grid(lat, lon)
    if grid is None:
        return WeatherData(
            location_name="Unknown",
            error=f"Could not resolve grid for ({lat}, {lon})",
        )

    location = f"{grid['city']}, {grid['state']}" if grid["city"] else f"({lat}, {lon})"

    forecast = _fetch_forecast_periods(grid["forecast_url"])
    current = _fetch_current_observation(grid["observation_stations_url"])

    return WeatherData(
        location_name=location,
        current=current,
        forecast_periods=forecast,
    )


# ---------------------------------------------------------------------------
# Hourly chart data (Open-Meteo: temp, precip, cloud cover, pressure)
# ---------------------------------------------------------------------------
def fetch_hourly_chart_data(lat: float = FORECAST_LAT,
                            lon: float = FORECAST_LON,
                            hours: int = 120) -> HourlyChartData:
    """
    Fetch 48h of hourly temp, precip %, cloud cover, and pressure from
    Open-Meteo for rendering the bottom chart on the weather display.
    """
    params = {
        "latitude":           lat,
        "longitude":          lon,
        "hourly":             "temperature_2m,precipitation_probability,cloud_cover,surface_pressure",
        "temperature_unit":   "fahrenheit",
        "timezone":           "America/New_York",
        "forecast_hours":     hours,
    }
    try:
        resp = requests.get(
            OPEN_METEO_URL, params=params,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Failed to fetch hourly chart data: %s", exc)
        return HourlyChartData(error=str(exc))

    hourly     = data.get("hourly", {})
    time_strs  = hourly.get("time", [])
    temps      = hourly.get("temperature_2m", [])
    precips    = hourly.get("precipitation_probability", [])
    clouds     = hourly.get("cloud_cover", [])
    pressures  = hourly.get("surface_pressure", [])

    times = []
    for ts in time_strs:
        try:
            times.append(datetime.strptime(ts, "%Y-%m-%dT%H:%M")
                         .replace(tzinfo=_EASTERN))
        except ValueError:
            pass

    def _floats(lst, n):
        return [float(v) if v is not None else 0.0 for v in lst[:n]]

    n = len(times)
    return HourlyChartData(
        times=times,
        temperature_f=_floats(temps, n),
        precip_pct=_floats(precips, n),
        cloud_cover_pct=_floats(clouds, n),
        pressure_inhg=[v * 0.02953 for v in _floats(pressures, n)],
    )


# ---------------------------------------------------------------------------
# Pretty-print for testing
# ---------------------------------------------------------------------------
def print_weather_summary(wx: WeatherData) -> None:
    print(f"\n{'='*55}")
    print(f"  Weather: {wx.location_name}")
    print(f"{'='*55}")

    if wx.error:
        print(f"  ⚠  {wx.error}")
        return

    if wx.current:
        c = wx.current
        print(f"  Station:     {c.station_id}")
        print(f"  Conditions:  {c.description}")
        print(f"  Temperature: {c.temperature_f}°F")
        print(f"  Wind:        {c.wind_dir} @ {c.wind_speed_mph} mph"
              f"  (gust {c.wind_gust_mph} mph)")
        print(f"  Humidity:    {c.humidity}%")
        print(f"  Pressure:    {c.pressure_inhg} inHg")
        print(f"  Visibility:  {c.visibility_mi} mi")
    else:
        print("  Current conditions: unavailable")

    if wx.forecast_periods:
        print(f"\n  --- 7-Day Forecast ---")
        for p in wx.forecast_periods[:6]:  # show next 3 days (6 periods)
            precip = f"  ({p.precip_chance}% precip)" if p.precip_chance else ""
            print(f"  {p.name:15s}  {p.temperature}°{p.temperature_unit}"
                  f"  {p.wind_direction} {p.wind_speed}"
                  f"  {p.short_forecast}{precip}")
    else:
        print("  Forecast: unavailable")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    wx = fetch_weather()
    print_weather_summary(wx)
