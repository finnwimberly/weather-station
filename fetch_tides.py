"""
CO-OPS Tides & Currents Fetcher
================================
Pulls tide predictions and water level data from NOAA's
Center for Operational Oceanographic Products and Services.

API docs: https://api.tidesandcurrents.noaa.gov/api/prod/

No API key required; just pass an 'application' parameter.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional

_EASTERN = ZoneInfo("America/New_York")

import requests

from config import (
    COOPS_API_BASE,
    TIDE_STATION_ID,
    REQUEST_TIMEOUT,
    USER_AGENT,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class TidePrediction:
    """A single high/low tide prediction."""
    timestamp: datetime
    height_ft: float
    type: str  # "H" (high) or "L" (low)

    @property
    def label(self) -> str:
        return "High" if self.type == "H" else "Low"


@dataclass
class WaterLevel:
    """Current (observed) water level."""
    timestamp: datetime
    height_ft: float
    sigma: Optional[float] = None  # standard deviation


@dataclass
class TideData:
    """Complete tide package: current water level + upcoming predictions."""
    station_id: str
    station_name: str
    current_level: Optional[WaterLevel] = None
    predictions: list[TidePrediction] = None
    error: Optional[str] = None

    def __post_init__(self):
        if self.predictions is None:
            self.predictions = []

    @property
    def next_high(self) -> Optional[TidePrediction]:
        now = datetime.now(timezone.utc)
        for p in self.predictions:
            if p.type == "H" and p.timestamp > now:
                return p
        return None

    @property
    def next_low(self) -> Optional[TidePrediction]:
        now = datetime.now(timezone.utc)
        for p in self.predictions:
            if p.type == "L" and p.timestamp > now:
                return p
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _coops_request(params: dict) -> Optional[dict]:
    """Make a request to the CO-OPS API and return the JSON response."""
    base_params = {
        "station": TIDE_STATION_ID,
        "units": "english",
        "time_zone": "gmt",
        "format": "json",
        "application": "WeatherStation",
    }
    base_params.update(params)

    try:
        resp = requests.get(
            COOPS_API_BASE,
            params=base_params,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()

        # CO-OPS returns errors inside the JSON body
        if "error" in data:
            logger.warning("CO-OPS API error: %s", data["error"].get("message", data["error"]))
            return None
        return data

    except requests.RequestException as exc:
        logger.warning("Failed to fetch tides: %s", exc)
        return None
    except ValueError as exc:
        logger.warning("Invalid JSON from CO-OPS: %s", exc)
        return None


def _parse_coops_time(time_str: str) -> datetime:
    """Parse CO-OPS timestamp like '2026-03-04 01:30' (GMT) → UTC-aware datetime."""
    return datetime.strptime(time_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fetch tide predictions (high/low for next 48 hours)
# ---------------------------------------------------------------------------
def _fetch_predictions(station_id: str) -> list[TidePrediction]:
    now = datetime.utcnow()
    begin = now.strftime("%Y%m%d")
    end = (now + timedelta(hours=48)).strftime("%Y%m%d")

    data = _coops_request({
        "station": station_id,
        "product": "predictions",
        "datum": "MLLW",
        "begin_date": begin,
        "end_date": end,
        "interval": "hilo",  # only high/low tides
    })
    if data is None:
        return []

    predictions = []
    for entry in data.get("predictions", []):
        try:
            predictions.append(TidePrediction(
                timestamp=_parse_coops_time(entry["t"]),
                height_ft=round(float(entry["v"]), 2),
                type=entry.get("type", "?"),
            ))
        except (KeyError, ValueError) as exc:
            logger.debug("Skipping tide entry: %s", exc)
            continue

    return predictions


# ---------------------------------------------------------------------------
# Fetch current water level
# ---------------------------------------------------------------------------
def _fetch_water_level(station_id: str) -> Optional[WaterLevel]:
    data = _coops_request({
        "station": station_id,
        "product": "water_level",
        "datum": "MLLW",
        "date": "latest",
    })
    if data is None:
        return None

    entries = data.get("data", [])
    if not entries:
        return None

    latest = entries[-1]
    try:
        sigma = float(latest.get("s", 0)) if latest.get("s") else None
        return WaterLevel(
            timestamp=_parse_coops_time(latest["t"]),
            height_ft=round(float(latest["v"]), 2),
            sigma=sigma,
        )
    except (KeyError, ValueError) as exc:
        logger.warning("Failed to parse water level: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Fetch station metadata (cached)
# ---------------------------------------------------------------------------
_station_name_cache: Dict[str, str] = {}


def _fetch_station_name(station_id: str) -> str:
    """Get the human-readable station name from the metadata API (cached)."""
    if station_id in _station_name_cache:
        return _station_name_cache[station_id]

    url = f"https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/{station_id}.json"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT,
                            headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        data = resp.json()
        stations = data.get("stations", [])
        if stations:
            name = stations[0].get("name", station_id)
            _station_name_cache[station_id] = name
            return name
    except Exception:
        pass
    _station_name_cache[station_id] = station_id
    return station_id


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def fetch_tides(station_id: str = TIDE_STATION_ID) -> TideData:
    """
    Fetch current water level + high/low tide predictions for a station.
    """
    station_name = _fetch_station_name(station_id)
    predictions = _fetch_predictions(station_id)
    current_level = _fetch_water_level(station_id)

    error = None
    if not predictions and current_level is None:
        error = f"No tide data available for station {station_id}"

    return TideData(
        station_id=station_id,
        station_name=station_name,
        current_level=current_level,
        predictions=predictions,
        error=error,
    )


# ---------------------------------------------------------------------------
# Multi-station fetch
# ---------------------------------------------------------------------------
def fetch_all_tides(station_ids: List[str]) -> Dict[str, TideData]:
    """Fetch tide data for multiple stations. Returns {station_id: TideData}."""
    results = {}
    for sid in station_ids:
        if sid not in results:
            logger.info("Fetching tides for station %s ...", sid)
            results[sid] = fetch_tides(sid)
    return results


# ---------------------------------------------------------------------------
# Pretty-print for testing
# ---------------------------------------------------------------------------
def print_tide_summary(td: TideData) -> None:
    print(f"\n{'='*55}")
    print(f"  Tides: {td.station_name}  (#{td.station_id})")
    print(f"{'='*55}")

    if td.error:
        print(f"  ⚠  {td.error}")
        return

    if td.current_level:
        wl = td.current_level
        et = wl.timestamp.astimezone(_EASTERN)
        print(f"  Water Level: {wl.height_ft} ft MLLW"
              f"  ({et.strftime('%H:%M ET')})")

    nh = td.next_high
    nl = td.next_low
    if nh:
        et = nh.timestamp.astimezone(_EASTERN)
        print(f"  Next High:   {nh.height_ft} ft"
              f"  at {et.strftime('%H:%M ET %b %d')}")
    if nl:
        et = nl.timestamp.astimezone(_EASTERN)
        print(f"  Next Low:    {nl.height_ft} ft"
              f"  at {et.strftime('%H:%M ET %b %d')}")

    if td.predictions:
        print(f"\n  --- Upcoming Tides (next 48h) ---")
        for p in td.predictions[:8]:
            et = p.timestamp.astimezone(_EASTERN)
            print(f"  {p.label:5s}  {p.height_ft:+6.2f} ft"
                  f"  {et.strftime('%a %b %d %H:%M ET')}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    td = fetch_tides()
    print_tide_summary(td)
