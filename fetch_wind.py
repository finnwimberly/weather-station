"""
Open-Meteo Coastal Wind Fetcher
================================
Pulls wind speed, direction, gusts, and hourly forecast for beach
locations using the Open-Meteo API (NOAA HRRR 3 km model).

API docs: https://open-meteo.com/en/docs

No API key required. Free for non-commercial use.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import requests

from config import (
    BEACHES,
    REQUEST_TIMEOUT,
    USER_AGENT,
)

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class BeachWind:
    """Current + hourly wind conditions at a beach location."""
    beach_key: str
    beach_name: str
    timestamp: str = ""
    wind_speed_kts: Optional[float] = None
    wind_direction: Optional[float] = None
    wind_gust_kts: Optional[float] = None
    temperature_f: Optional[float] = None
    hourly_times: List[str] = field(default_factory=list)
    hourly_speed: List[float] = field(default_factory=list)
    hourly_direction: List[float] = field(default_factory=list)
    hourly_gust: List[float] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def wind_dir_cardinal(self) -> Optional[str]:
        """Convert degrees to 16-point compass direction."""
        if self.wind_direction is None:
            return None
        dirs = [
            "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
        ]
        idx = int((self.wind_direction + 11.25) / 22.5) % 16
        return dirs[idx]

    def hourly_dir_cardinal(self, deg):
        """Convert a single degree value to cardinal direction."""
        if deg is None:
            return "--"
        dirs = [
            "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
        ]
        idx = int((deg + 11.25) / 22.5) % 16
        return dirs[idx]

    def upcoming_hours(self, count=6):
        """
        Return the next `count` hourly forecasts from now onward.
        Each entry is a dict: {time, speed, direction, dir_cardinal, gust}.
        """
        now = datetime.now().strftime("%Y-%m-%dT%H:00")
        results = []
        started = False
        for i, t in enumerate(self.hourly_times):
            if t >= now:
                started = True
            if started and i < len(self.hourly_speed):
                direction = self.hourly_direction[i] if i < len(self.hourly_direction) else None
                results.append({
                    "time": t,
                    "speed": self.hourly_speed[i],
                    "direction": direction,
                    "dir_cardinal": self.hourly_dir_cardinal(direction),
                    "gust": self.hourly_gust[i] if i < len(self.hourly_gust) else None,
                })
            if len(results) >= count:
                break
        return results


# ---------------------------------------------------------------------------
# Fetch all beach winds (single API call)
# ---------------------------------------------------------------------------
def fetch_beach_winds() -> Dict[str, BeachWind]:
    """
    Fetch current + hourly wind data for all configured beaches.
    Makes one API call per beach to avoid URL-encoding issues with
    comma-separated multi-location requests.

    Returns {beach_key: BeachWind} dict.
    """
    results = {}
    for key, beach in BEACHES.items():
        logger.info("Fetching wind for %s ...", beach.name)
        results[key] = _fetch_single_beach_wind(key, beach)
    return results


def _fetch_single_beach_wind(key: str, beach) -> "BeachWind":
    """Fetch wind data for a single beach location."""
    params = {
        "latitude": beach.lat,
        "longitude": beach.lon,
        "current": "wind_speed_10m,wind_direction_10m,wind_gusts_10m,temperature_2m",
        "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
        "wind_speed_unit": "kn",
        "temperature_unit": "fahrenheit",
        "timezone": "America/New_York",
        "forecast_hours": 24,
    }

    try:
        resp = requests.get(
            OPEN_METEO_URL,
            params=params,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        loc = resp.json()
    except requests.RequestException as exc:
        logger.warning("Failed to fetch wind for %s: %s", beach.name, exc)
        return BeachWind(beach_key=key, beach_name=beach.name, error=str(exc))
    except ValueError as exc:
        logger.warning("Invalid JSON from Open-Meteo for %s: %s", beach.name, exc)
        return BeachWind(beach_key=key, beach_name=beach.name, error=str(exc))

    try:
        current = loc.get("current", {})
        hourly = loc.get("hourly", {})
        return BeachWind(
            beach_key=key,
            beach_name=beach.name,
            timestamp=current.get("time", ""),
            wind_speed_kts=_safe_float(current.get("wind_speed_10m")),
            wind_direction=_safe_float(current.get("wind_direction_10m")),
            wind_gust_kts=_safe_float(current.get("wind_gusts_10m")),
            temperature_f=_safe_float(current.get("temperature_2m")),
            hourly_times=hourly.get("time", []),
            hourly_speed=_safe_float_list(hourly.get("wind_speed_10m", [])),
            hourly_direction=_safe_float_list(hourly.get("wind_direction_10m", [])),
            hourly_gust=_safe_float_list(hourly.get("wind_gusts_10m", [])),
        )
    except Exception as exc:
        logger.warning("Error parsing wind data for %s: %s", beach.name, exc)
        return BeachWind(beach_key=key, beach_name=beach.name, error=str(exc))


def _safe_float(val) -> Optional[float]:
    """Convert to float, returning None for missing/invalid values."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_float_list(vals) -> List[float]:
    """Convert a list to floats, substituting 0.0 for invalid entries."""
    result = []
    for v in vals:
        try:
            result.append(float(v) if v is not None else 0.0)
        except (ValueError, TypeError):
            result.append(0.0)
    return result


def _error_results(beach_keys, error_msg) -> Dict[str, BeachWind]:
    """Return error BeachWind objects for all beaches."""
    return {
        key: BeachWind(
            beach_key=key,
            beach_name=BEACHES[key].name,
            error=error_msg,
        )
        for key in beach_keys
    }


# ---------------------------------------------------------------------------
# Pretty-print for testing
# ---------------------------------------------------------------------------
def print_beach_wind_summary(bw):
    print("\n{}".format("=" * 55))
    print("  Wind: {}".format(bw.beach_name))
    print("=" * 55)

    if bw.error:
        print("  Error: {}".format(bw.error))
        return

    print("  Current ({})".format(bw.timestamp))
    print("  {} @ {:.1f} kts".format(bw.wind_dir_cardinal or "--", bw.wind_speed_kts or 0))
    print("  Gust: {:.1f} kts".format(bw.wind_gust_kts or 0))
    print("  Air Temp: {:.1f} F".format(bw.temperature_f or 0))

    upcoming = bw.upcoming_hours(6)
    if upcoming:
        print("\n  --- Next 6 Hours ---")
        for h in upcoming:
            # Parse the hour for display
            try:
                dt = datetime.strptime(h["time"], "%Y-%m-%dT%H:%M")
                hour_str = dt.strftime("%-I%p").lower()
            except (ValueError, TypeError):
                hour_str = h["time"][-5:]
            speed = h["speed"] if h["speed"] is not None else 0
            gust = h["gust"] if h["gust"] is not None else 0
            print("  {:>5s}  {:>3s} {:5.1f} kts  (g {:.1f})".format(
                hour_str, h["dir_cardinal"], speed, gust))


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    winds = fetch_beach_winds()
    for bw in winds.values():
        print_beach_wind_summary(bw)
