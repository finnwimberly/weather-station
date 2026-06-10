"""
NDBC Buoy Data Fetcher
======================
Pulls real-time meteorological and spectral wave data from NOAA's
National Data Buoy Center for configured stations.

Data sources:
  - {station}.txt  → standard met: wind, pressure, air/water temp, wave height
  - {station}.spec → spectral wave summary: swell ht/period/dir, wind wave ht/period/dir

NDBC updates roughly every 30-60 min; data appears ~25 min past the hour.
Missing values are encoded as "MM" in the text files.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests

from config import (
    BUOY_STATIONS,
    NDBC_REALTIME_BASE,
    REQUEST_TIMEOUT,
    USER_AGENT,
    UNITS,
    m_to_ft,
    c_to_f,
    mps_to_knots,
    hpa_to_inhg,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class MetData:
    """Latest standard meteorological observation from a buoy."""
    timestamp: datetime
    wind_dir: Optional[float] = None     # degrees true
    wind_speed: Optional[float] = None   # m/s  (raw)
    wind_gust: Optional[float] = None    # m/s  (raw)
    wave_height: Optional[float] = None  # m    (raw)
    dom_period: Optional[float] = None   # sec
    avg_period: Optional[float] = None   # sec
    mean_wave_dir: Optional[float] = None  # degrees true
    pressure: Optional[float] = None     # hPa  (raw)
    air_temp: Optional[float] = None     # °C   (raw)
    water_temp: Optional[float] = None   # °C   (raw)
    dew_point: Optional[float] = None    # °C   (raw)
    visibility: Optional[float] = None   # nmi
    pressure_tendency: Optional[float] = None  # hPa

    # --- Converted (imperial) properties ---
    @property
    def wave_height_ft(self) -> Optional[float]:
        return round(m_to_ft(self.wave_height), 1) if self.wave_height is not None else None

    @property
    def wind_speed_kts(self) -> Optional[float]:
        return round(mps_to_knots(self.wind_speed), 1) if self.wind_speed is not None else None

    @property
    def wind_gust_kts(self) -> Optional[float]:
        return round(mps_to_knots(self.wind_gust), 1) if self.wind_gust is not None else None

    @property
    def air_temp_f(self) -> Optional[float]:
        return round(c_to_f(self.air_temp), 1) if self.air_temp is not None else None

    @property
    def water_temp_f(self) -> Optional[float]:
        return round(c_to_f(self.water_temp), 1) if self.water_temp is not None else None

    @property
    def pressure_inhg(self) -> Optional[float]:
        return round(hpa_to_inhg(self.pressure), 2) if self.pressure is not None else None

    @property
    def wind_dir_cardinal(self) -> Optional[str]:
        if self.wind_dir is None:
            return None
        dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
                "S","SSW","SW","WSW","W","WNW","NW","NNW"]
        idx = int((self.wind_dir + 11.25) / 22.5) % 16
        return dirs[idx]


@dataclass
class SpectralData:
    """Latest spectral wave summary from a buoy."""
    timestamp: datetime
    wave_height: Optional[float] = None    # m
    swell_height: Optional[float] = None   # m
    swell_period: Optional[float] = None   # sec
    wind_wave_height: Optional[float] = None  # m
    wind_wave_period: Optional[float] = None  # sec
    swell_dir: Optional[str] = None        # compass (e.g. "SSE")
    wind_wave_dir: Optional[str] = None    # compass
    steepness: Optional[str] = None        # e.g. "STEEP", "AVERAGE", "N/A"
    avg_period: Optional[float] = None     # sec
    mean_wave_dir: Optional[float] = None  # degrees true

    @property
    def wave_height_ft(self) -> Optional[float]:
        return round(m_to_ft(self.wave_height), 1) if self.wave_height is not None else None

    @property
    def swell_height_ft(self) -> Optional[float]:
        return round(m_to_ft(self.swell_height), 1) if self.swell_height is not None else None

    @property
    def wind_wave_height_ft(self) -> Optional[float]:
        return round(m_to_ft(self.wind_wave_height), 1) if self.wind_wave_height is not None else None


@dataclass
class BuoyReading:
    """Combined buoy data: station info + latest met + latest spectral."""
    station_id: str
    station_name: str
    met: Optional[MetData] = None
    spectral: Optional[SpectralData] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
def _parse_float(value: str) -> Optional[float]:
    """Convert a string to float, returning None for missing ('MM') values."""
    if value.strip() in ("MM", "N/A", ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_str(value: str) -> Optional[str]:
    """Return None for missing values, otherwise stripped string."""
    stripped = value.strip()
    return None if stripped in ("MM", "") else stripped


def _parse_timestamp(parts: list[str]) -> datetime:
    """Parse the first 5 columns (#YY MM DD hh mm) into a UTC datetime."""
    return datetime(
        year=int(parts[0]),
        month=int(parts[1]),
        day=int(parts[2]),
        hour=int(parts[3]),
        minute=int(parts[4]),
        tzinfo=timezone.utc,
    )


# ---------------------------------------------------------------------------
# Met data parser  (.txt file)
# ---------------------------------------------------------------------------
# Column layout:
# #YY MM DD hh mm WDIR WSPD GST WVHT DPD APD MWD PRES ATMP WTMP DEWP VIS PTDY TIDE
#  0  1  2  3  4   5    6    7   8    9  10  11  12   13   14   15   16  17   18

def _parse_met_line(line: str) -> MetData:
    cols = line.split()
    ts = _parse_timestamp(cols[:5])
    return MetData(
        timestamp=ts,
        wind_dir=_parse_float(cols[5]),
        wind_speed=_parse_float(cols[6]),
        wind_gust=_parse_float(cols[7]),
        wave_height=_parse_float(cols[8]),
        dom_period=_parse_float(cols[9]),
        avg_period=_parse_float(cols[10]),
        mean_wave_dir=_parse_float(cols[11]),
        pressure=_parse_float(cols[12]),
        air_temp=_parse_float(cols[13]),
        water_temp=_parse_float(cols[14]),
        dew_point=_parse_float(cols[15]),
        visibility=_parse_float(cols[16]),
        pressure_tendency=_parse_float(cols[17]),
    )


# ---------------------------------------------------------------------------
# Spectral data parser  (.spec file)
# ---------------------------------------------------------------------------
# Column layout:
# #YY MM DD hh mm WVHT SwH SwP WWH WWP SwD WWD STEEPNESS APD MWD
#  0  1  2  3  4   5    6   7   8   9  10  11      12     13  14

def _parse_spec_line(line: str) -> SpectralData:
    cols = line.split()
    ts = _parse_timestamp(cols[:5])
    return SpectralData(
        timestamp=ts,
        wave_height=_parse_float(cols[5]),
        swell_height=_parse_float(cols[6]),
        swell_period=_parse_float(cols[7]),
        wind_wave_height=_parse_float(cols[8]),
        wind_wave_period=_parse_float(cols[9]),
        swell_dir=_parse_str(cols[10]),
        wind_wave_dir=_parse_str(cols[11]),
        steepness=_parse_str(cols[12]),
        avg_period=_parse_float(cols[13]),
        mean_wave_dir=_parse_float(cols[14]),
    )


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------
def _fetch_text(url: str) -> Optional[str]:
    """GET a URL, return body text or None on failure."""
    try:
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None


def _first_data_line(text: str) -> Optional[str]:
    """Return the first non-comment line (skip lines starting with '#')."""
    for line in text.strip().splitlines():
        if not line.startswith("#"):
            return line
    return None


def _recent_data_lines(text: str, count: int = 12) -> list[str]:
    """Return the first N non-comment data lines (most recent observations)."""
    lines = []
    for line in text.strip().splitlines():
        if not line.startswith("#"):
            lines.append(line)
            if len(lines) >= count:
                break
    return lines


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def fetch_met(station_id: str) -> Optional[MetData]:
    """Fetch the latest standard met observation for a station."""
    url = f"{NDBC_REALTIME_BASE}/{station_id}.txt"
    text = _fetch_text(url)
    if text is None:
        return None
    line = _first_data_line(text)
    if line is None:
        return None
    try:
        return _parse_met_line(line)
    except (IndexError, ValueError) as exc:
        logger.warning("Failed to parse met data for %s: %s", station_id, exc)
        return None


def fetch_spectral(station_id: str) -> Optional[SpectralData]:
    """Fetch the latest spectral wave summary for a station."""
    url = f"{NDBC_REALTIME_BASE}/{station_id}.spec"
    text = _fetch_text(url)
    if text is None:
        return None
    line = _first_data_line(text)
    if line is None:
        return None
    try:
        return _parse_spec_line(line)
    except (IndexError, ValueError) as exc:
        logger.warning("Failed to parse spectral data for %s: %s", station_id, exc)
        return None


def fetch_met_history(station_id: str, count: int = 12) -> list[MetData]:
    """Fetch recent met observations (for trend charts). Default ~6 hours."""
    url = f"{NDBC_REALTIME_BASE}/{station_id}.txt"
    text = _fetch_text(url)
    if text is None:
        return []
    results = []
    for line in _recent_data_lines(text, count):
        try:
            results.append(_parse_met_line(line))
        except (IndexError, ValueError):
            continue
    return results


def fetch_buoy(station_id: str) -> BuoyReading:
    """
    Fetch all available data for a single buoy station.
    Returns a BuoyReading with met + spectral data (either may be None).
    """
    station = BUOY_STATIONS.get(station_id)
    name = station.name if station else station_id

    met = fetch_met(station_id)
    spectral = fetch_spectral(station_id)

    error = None
    if met is None and spectral is None:
        error = f"No data available for station {station_id}"

    return BuoyReading(
        station_id=station_id,
        station_name=name,
        met=met,
        spectral=spectral,
        error=error,
    )


def fetch_all_buoys() -> dict[str, BuoyReading]:
    """Fetch data for all configured buoy stations. Returns {station_id: BuoyReading}."""
    results = {}
    for station_id in BUOY_STATIONS:
        logger.info("Fetching buoy %s ...", station_id)
        results[station_id] = fetch_buoy(station_id)
    return results


# ---------------------------------------------------------------------------
# Pretty-print for testing
# ---------------------------------------------------------------------------
def print_buoy_summary(reading: BuoyReading) -> None:
    """Print a human-readable summary of a buoy reading."""
    print(f"\n{'='*55}")
    print(f"  {reading.station_name}  (#{reading.station_id})")
    print(f"{'='*55}")

    if reading.error:
        print(f"  ⚠  {reading.error}")
        return

    if reading.met:
        m = reading.met
        print(f"  Observed:  {m.timestamp.strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"  Wave Ht:   {m.wave_height_ft} ft   |  Period: {m.dom_period} s")
        print(f"  Wind:      {m.wind_dir_cardinal} @ {m.wind_speed_kts} kts"
              f"  (gust {m.wind_gust_kts} kts)")
        print(f"  Air Temp:  {m.air_temp_f}°F   |  Water: {m.water_temp_f}°F")
        print(f"  Pressure:  {m.pressure_inhg} inHg")
    else:
        print("  Met data:  unavailable")

    if reading.spectral:
        s = reading.spectral
        print(f"  ---")
        print(f"  Swell:     {s.swell_height_ft} ft @ {s.swell_period} s"
              f"  from {s.swell_dir}")
        print(f"  Wind Wave: {s.wind_wave_height_ft} ft @ {s.wind_wave_period} s"
              f"  from {s.wind_wave_dir}")
        print(f"  Steepness: {s.steepness}")
    else:
        print("  Spectral:  unavailable")


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    print("Fetching all buoy stations...")
    buoys = fetch_all_buoys()
    for reading in buoys.values():
        print_buoy_summary(reading)
