"""
Weather Station Configuration
=============================
Station definitions, API endpoints, and display settings.
"""

import os
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# NDBC Buoy Stations
# ---------------------------------------------------------------------------
@dataclass
class BuoyStation:
    station_id: str
    name: str
    lat: float
    lon: float
    depth_m: float


BUOY_STATIONS = {
    "44029": BuoyStation("44029", "Mass Bay (Buoy A01)",       42.523,  -70.566, 65.0),
    "44098": BuoyStation("44098", "Jeffrey's Ledge, NH",       42.800,  -70.169, 76.5),
    "44013": BuoyStation("44013", "Boston 16NM East",          42.346,  -70.651, 64.6),
    "44097": BuoyStation("44097", "Block Island, RI",          40.967,  -71.124, 49.4),
}


# ---------------------------------------------------------------------------
# Beach Stations (paired with buoys for tide + location data)
# ---------------------------------------------------------------------------
@dataclass
class Beach:
    name: str
    lat: float
    lon: float
    tide_station_id: str
    tide_station_name: str


BEACHES = {
    "hampton":     Beach("Hampton Beach, NH",                42.9073, -70.8120, "8429489", "Hampton Harbor"),
    "good_harbor": Beach("Good Harbor Beach, Gloucester",    42.6195, -70.6314, "8441841", "Gloucester Harbor"),
    "nantasket":   Beach("Nantasket Beach, Hull",            42.2870, -70.8590, "8443970", "Boston"),
    "south_shore": Beach("South Shore Beach, Little Compton", 41.4943, -71.1365, "8450948", "Sakonnet River"),
}


# Ordered pairing: each screen shows one buoy + one beach
BUOY_BEACH_PAIRINGS = [
    ("44098", "hampton"),       # Jeffrey's Ledge  -> Hampton Beach, NH
    ("44029", "good_harbor"),   # Mass Bay A01     -> Good Harbor, Gloucester
    ("44013", "nantasket"),     # Boston 16NM East -> Nantasket Beach, Hull
    ("44097", "south_shore"),   # Block Island     -> South Shore, Little Compton
]


# ---------------------------------------------------------------------------
# NOAA API Endpoints
# ---------------------------------------------------------------------------
NDBC_REALTIME_BASE = "https://www.ndbc.noaa.gov/data/realtime2"
NWS_API_BASE       = "https://api.weather.gov"
COOPS_API_BASE     = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

# ---------------------------------------------------------------------------
# NWS Weather Forecast Location  (default: Boston Harbor area)
# Change lat/lon to your location
# ---------------------------------------------------------------------------
FORECAST_LAT = 42.36
FORECAST_LON = -71.05

# ---------------------------------------------------------------------------
# CO-OPS Tide Station  (legacy default — Boston, MA = 8443970)
# Beaches above define per-beach tide stations
# ---------------------------------------------------------------------------
TIDE_STATION_ID = "8443970"

# ---------------------------------------------------------------------------
# Request settings
# ---------------------------------------------------------------------------
USER_AGENT = "(WeatherStation/1.0, finn.wimberly@whoi.edu)"
REQUEST_TIMEOUT = 15  # seconds

# ---------------------------------------------------------------------------
# Data refresh intervals (seconds) — used by the scheduler in main.py
# ---------------------------------------------------------------------------
BUOY_REFRESH    = 30 * 60   # 30 min  (NDBC updates ~hourly)
WEATHER_REFRESH = 30 * 60   # 30 min
TIDE_REFRESH    = 60 * 60   # 60 min
WIND_REFRESH    = 15 * 60   # 15 min  (Open-Meteo HRRR updates frequently)
WAVE_CYCLE_INTERVAL = 30    # seconds between buoy screen rotations

# ---------------------------------------------------------------------------
# Unit preferences
# ---------------------------------------------------------------------------
UNITS = "imperial"  # "imperial" or "metric"

# Conversion helpers
def m_to_ft(meters: float) -> float:
    return meters * 3.28084

def c_to_f(celsius: float) -> float:
    return celsius * 9 / 5 + 32

def mps_to_mph(mps: float) -> float:
    return mps * 2.23694

def mps_to_knots(mps: float) -> float:
    return mps * 1.94384

def hpa_to_inhg(hpa: float) -> float:
    return hpa * 0.02953


# ---------------------------------------------------------------------------
# Display settings
# ---------------------------------------------------------------------------
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE_PATH = os.path.join(PROJECT_DIR, ".display_state.json")
MOCK_OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")

DISPLAY_MODES = ["weather", "waves"]

# Short labels for the station-rotation indicator in the status bar
STATION_ABBREVIATIONS = {
    "44029": "A01",
    "44098": "JL",
    "44013": "B16",
    "44097": "BI",
}

BEACH_ABBREVIATIONS = {
    "hampton": "HB",
    "good_harbor": "GH",
    "nantasket": "NK",
    "south_shore": "SS",
}

# ---------------------------------------------------------------------------
# Map settings
# ---------------------------------------------------------------------------
MAP_BOUNDS = {
    "lat_min": 40.5,
    "lat_max": 43.2,
    "lon_min": -71.8,
    "lon_max": -69.5,
}
