#!/usr/bin/env python3
"""
Weather & Wave Display Station — Main Orchestrator
====================================================
Two display modes toggled by button press:
  WEATHER — current conditions + today/tonight + 48h hourly chart
  WAVES   — 4-location consolidated grid (swell | wind | tide sparkline)

Usage:
    python main.py                      # single fetch + console dashboard
    python main.py --render             # fetch + render current display mode
    python main.py --render --all       # fetch + render both screens as PNGs
    python main.py --loop               # continuous loop with display rendering
    python main.py --loop --epaper      # continuous loop → e-paper hardware
    python main.py --json               # dump all data as JSON
"""

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone

from config import (
    BEACHES,
    BUOY_REFRESH,
)
from fetch_buoy import fetch_all_buoys, print_buoy_summary
from fetch_weather import fetch_weather, fetch_hourly_chart_data, print_weather_summary
from fetch_tides import fetch_all_tides, print_tide_summary
from fetch_wind import fetch_beach_winds, print_beach_wind_summary
from render_display import (
    DisplayState,
    render_display,
    render_weather_mode,
    render_waves_consolidated,
    Fonts,
    display_mock,
    display_to_epaper,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------
def fetch_all_data():
    """Fetch all data sources. Returns (buoys, weather, hourly_chart, all_tides, beach_winds)."""
    logger.info("Fetching buoy data ...")
    buoys = fetch_all_buoys()

    logger.info("Fetching weather forecast ...")
    weather = fetch_weather()

    logger.info("Fetching hourly chart data ...")
    hourly_chart = fetch_hourly_chart_data()

    tide_station_ids = list(set(beach.tide_station_id for beach in BEACHES.values()))
    logger.info("Fetching tides for %d stations ...", len(tide_station_ids))
    all_tides = fetch_all_tides(tide_station_ids)

    logger.info("Fetching coastal wind for %d beaches ...", len(BEACHES))
    beach_winds = fetch_beach_winds()

    return buoys, weather, hourly_chart, all_tides, beach_winds


# ---------------------------------------------------------------------------
# Console dashboard
# ---------------------------------------------------------------------------
def print_dashboard(buoys, weather, all_tides, beach_winds):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print("\n{}".format("#" * 58))
    print("  WEATHER & WAVE STATION  ---  {}".format(now))
    print("#" * 58)

    for reading in buoys.values():
        print_buoy_summary(reading)

    print_weather_summary(weather)

    for td in all_tides.values():
        print_tide_summary(td)

    for bw in beach_winds.values():
        print_beach_wind_summary(bw)

    print("\n{}".format("#" * 58))
    print("  Data fetched at {}".format(now))
    print("{}\n".format("#" * 58))


# ---------------------------------------------------------------------------
# Output helper
# ---------------------------------------------------------------------------
def _output_image(image, mock, label=""):
    if mock:
        path = display_mock(image, label=label)
        logger.info("Saved: %s", path)
        return path
    else:
        if not display_to_epaper(image):
            path = display_mock(image, label=label)
            logger.warning("E-paper unavailable, saved to: %s", path)
            return path
        return None


# ---------------------------------------------------------------------------
# Render both screens
# ---------------------------------------------------------------------------
def render_all_screens(buoys, weather, hourly_chart, all_tides, beach_winds, mock=True):
    fonts = Fonts.load()
    paths = []

    state = DisplayState(display_mode="weather")
    img = render_weather_mode(weather, hourly_chart, fonts, state)
    p = _output_image(img, mock, label="weather")
    if p:
        paths.append(p)

    state = DisplayState(display_mode="waves")
    img = render_waves_consolidated(buoys, all_tides, beach_winds, fonts, state)
    p = _output_image(img, mock, label="waves")
    if p:
        paths.append(p)

    return paths


# ---------------------------------------------------------------------------
# Single render (current mode from state file)
# ---------------------------------------------------------------------------
def render_current(buoys, weather, hourly_chart, all_tides, beach_winds, mock=True):
    state = DisplayState.load()
    image = render_display(weather, hourly_chart, state,
                           buoys=buoys, all_tides=all_tides,
                           beach_winds=beach_winds)
    state.save()
    return _output_image(image, mock)


# ---------------------------------------------------------------------------
# Continuous loop
# ---------------------------------------------------------------------------
def run_loop(mock=True):
    """
    Continuous display loop:
    - Fetch all data every BUOY_REFRESH seconds
    - Render current mode (WEATHER or WAVES) once per fetch cycle
    - Poll every 5 s for button-driven mode changes and re-render immediately
    """
    logger.info("Starting display loop (Ctrl+C to stop) ...")
    logger.info("  Data refresh: every %d minutes", BUOY_REFRESH // 60)

    while True:
        try:
            buoys, weather, hourly_chart, all_tides, beach_winds = fetch_all_data()
            print_dashboard(buoys, weather, all_tides, beach_winds)

            cycle_start  = time.time()
            last_mode    = None

            while (time.time() - cycle_start) < BUOY_REFRESH:
                state = DisplayState.load()

                if state.display_mode != last_mode:
                    image = render_display(weather, hourly_chart, state,
                                           buoys=buoys, all_tides=all_tides,
                                           beach_winds=beach_winds)
                    _output_image(image, mock)
                    state.save()
                    last_mode = state.display_mode
                    logger.info("Rendered %s mode", state.display_mode.upper())

                remaining = BUOY_REFRESH - (time.time() - cycle_start)
                time.sleep(min(5, max(0, remaining)))

        except KeyboardInterrupt:
            print("\n\nShutting down.")
            sys.exit(0)
        except Exception as exc:
            logger.error("Error in main loop: %s", exc, exc_info=True)
            logger.info("Retrying in 60 seconds ...")
            time.sleep(60)


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------
def _safe_asdict(obj):
    try:
        d = asdict(obj)
        return _convert_datetimes(d)
    except Exception:
        return {"error": str(obj)}


def _convert_datetimes(obj):
    if isinstance(obj, dict):
        return {k: _convert_datetimes(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_datetimes(item) for item in obj]
    elif isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def data_to_json(buoys, weather, all_tides, beach_winds):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return {
        "timestamp":    now,
        "buoys":        {k: _safe_asdict(v) for k, v in buoys.items()},
        "weather":      _safe_asdict(weather),
        "tides":        {k: _safe_asdict(v) for k, v in all_tides.items()},
        "beach_winds":  {k: _safe_asdict(v) for k, v in beach_winds.items()},
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Weather & Wave Display Station")
    parser.add_argument("--loop",   action="store_true",
                        help="Run continuously, re-rendering on mode change or data refresh")
    parser.add_argument("--json",   action="store_true",
                        help="Output all fetched data as JSON")
    parser.add_argument("--debug",  action="store_true")
    parser.add_argument("--render", action="store_true",
                        help="Render display image after fetching data")
    parser.add_argument("--all",    action="store_true",
                        help="Render both screens (weather + waves)")
    parser.add_argument("--mock",   action="store_true", default=True)
    parser.add_argument("--epaper", action="store_true",
                        help="Output to e-paper display instead of PNG")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    use_mock = not args.epaper

    if args.loop:
        run_loop(mock=use_mock)
        return

    buoys, weather, hourly_chart, all_tides, beach_winds = fetch_all_data()
    print_dashboard(buoys, weather, all_tides, beach_winds)

    if args.json:
        print("\n--- JSON Output ---")
        data = data_to_json(buoys, weather, all_tides, beach_winds)
        print(json.dumps(data, indent=2, default=str))

    if args.render:
        if args.all:
            paths = render_all_screens(buoys, weather, hourly_chart,
                                       all_tides, beach_winds, mock=use_mock)
            print("\nRendered {} screens:".format(len(paths)))
            for p in paths:
                print("  {}".format(p))
        else:
            path = render_current(buoys, weather, hourly_chart,
                                  all_tides, beach_winds, mock=use_mock)
            if path:
                print("\nDisplay saved to: {}".format(path))


if __name__ == "__main__":
    main()
