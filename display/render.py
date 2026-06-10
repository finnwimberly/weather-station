#!/usr/bin/env python3
"""
E-Paper Display Renderer  (Inky Impression 7.3" — 7-colour ACeP, 800×480)
==========================================================================
Two display modes:
  WEATHER — current conditions + today/tonight + 48h hourly chart
  WAVES   — 4-location grid: swell | wind | 24h tide sparkline

Usage:
    python -m display.render --mock --no-fetch --mode weather
    python -m display.render --mock --no-fetch --mode waves
    python -m display.render --mock --no-fetch --all
    python -m display.render --toggle
"""

import argparse
import json
import logging
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from config import (
    BUOY_STATIONS,
    BEACHES,
    BUOY_BEACH_PAIRINGS,
    STATION_ABBREVIATIONS,
    DISPLAY_MODES,
    STATE_FILE_PATH,
    MOCK_OUTPUT_DIR,
    PROJECT_DIR,
)

logger = logging.getLogger(__name__)
_EASTERN = ZoneInfo("America/New_York")

# =========================================================================
# CONSTANTS
# =========================================================================
WIDTH  = 800
HEIGHT = 480

# ── ACeP colour palette (RGB tuples) ──────────────────────────────────────
BG    = (255, 255, 255)   # white canvas
INK   = (  0,   0,   0)   # primary text / lines
NAVY  = ( 15,  55, 120)   # status bar fill / wave data
IVORY = (255, 255, 255)   # text on dark backgrounds
AMBER = (210,  85,   0)   # hero numbers (wave ht, temperature)
RULE  = (  0,   0,   0)   # divider lines
DIM   = (  0,   0,   0)   # secondary text

# ── Panel coordinates: (x0, y0, x1, y1) ──────────────────────────────────
STATUS_BAR      = (0,   0,   WIDTH, 26)
BOTTOM_BAR      = (0,   454, WIDTH, HEIGHT)

# Weather display panels
WEATHER_CURRENT = (0,   26,  400, 196)
WEATHER_TODAY   = (400, 26,  800, 196)
WEATHER_CHART   = (0,   196, 800, 454)

# Waves grid geometry
WAVES_Y0    = 26            # below status bar
WAVES_Y1    = 454           # above bottom bar
WAVES_COL1  = 200           # swell | wind divider
WAVES_COL2  = 400           # wind  | tide divider
WAVES_ROW_H = (WAVES_Y1 - WAVES_Y0) // 4   # 107 px per location row

# Font sizes
SZ_HERO   = 56
SZ_HEADER = 24
SZ_BODY   = 20
SZ_SMALL  = 16
SZ_STATUS = 14

PADDING = 10


# =========================================================================
# FONT LOADING
# =========================================================================
_FONT_SEARCH = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "C:/Windows/Fonts/arial.ttf",
    os.path.join(PROJECT_DIR, "fonts", "DejaVuSans.ttf"),
]
_BOLD_SEARCH = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/arialbd.ttf",
    os.path.join(PROJECT_DIR, "fonts", "DejaVuSans-Bold.ttf"),
]
_font_path_cache: Dict[str, Optional[str]] = {}


def _find_font_path(search_list, key):
    if key in _font_path_cache:
        return _font_path_cache[key]
    for path in search_list:
        if os.path.exists(path):
            _font_path_cache[key] = path
            return path
    _font_path_cache[key] = None
    return None


def _load_font(size, bold=False):
    search = _BOLD_SEARCH if bold else _FONT_SEARCH
    key = "bold" if bold else "regular"
    path = _find_font_path(search, key)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception as exc:
            logger.warning("Could not load %s at size %d: %s", path, size, exc)
    return ImageFont.load_default()


@dataclass
class Fonts:
    hero:   ImageFont.FreeTypeFont
    header: ImageFont.FreeTypeFont
    body:   ImageFont.FreeTypeFont
    small:  ImageFont.FreeTypeFont
    status: ImageFont.FreeTypeFont

    @classmethod
    def load(cls):
        return cls(
            hero=_load_font(SZ_HERO,     bold=True),
            header=_load_font(SZ_HEADER, bold=True),
            body=_load_font(SZ_BODY,     bold=True),
            small=_load_font(SZ_SMALL,   bold=True),
            status=_load_font(SZ_STATUS, bold=True),
        )


# =========================================================================
# STATE MANAGEMENT
# =========================================================================
@dataclass
class DisplayState:
    display_mode: str = "weather"
    buoy_rotation_index: int = 0   # kept for backward-compat with saved state files
    last_updated: str = ""

    @classmethod
    def load(cls, path=STATE_FILE_PATH):
        try:
            with open(path, "r") as f:
                data = json.load(f)
            mode = data.get("display_mode", "weather")
            # Migrate old mode names
            if mode in ("forecast", "map"):
                mode = "weather"
            if mode == "wave":
                mode = "waves"
            return cls(
                display_mode=mode,
                buoy_rotation_index=data.get("buoy_rotation_index", 0),
                last_updated=data.get("last_updated", ""),
            )
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return cls()

    def save(self, path=STATE_FILE_PATH):
        self.last_updated = datetime.now(timezone.utc).isoformat()
        with open(path, "w") as f:
            json.dump({
                "display_mode":        self.display_mode,
                "buoy_rotation_index": self.buoy_rotation_index,
                "last_updated":        self.last_updated,
            }, f, indent=2)

    def cycle_mode(self):
        modes = DISPLAY_MODES
        idx = modes.index(self.display_mode) if self.display_mode in modes else 0
        self.display_mode = modes[(idx + 1) % len(modes)]
        return self.display_mode


# =========================================================================
# DRAWING HELPERS
# =========================================================================
def fmt(value, suffix="", decimal=1, fallback="--"):
    if value is None:
        return fallback
    if isinstance(value, float):
        return "{:.{}f}{}".format(value, decimal, suffix)
    return "{}{}".format(value, suffix)


def _text_width(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def draw_text_right(draw, x_right, y, text, font, fill=INK):
    w = _text_width(draw, text, font)
    draw.text((x_right - w, y), text, font=font, fill=fill)


def draw_text_center(draw, x_center, y, text, font, fill=INK):
    w = _text_width(draw, text, font)
    draw.text((x_center - w // 2, y), text, font=font, fill=fill)


def draw_hline(draw, x0, x1, y, color=RULE):
    draw.line([(x0, y), (x1, y)], fill=color, width=2)


def draw_vline(draw, x, y0, y1, color=RULE):
    draw.line([(x, y0), (x, y1)], fill=color, width=2)


def truncate(text, font, draw, max_w):
    if _text_width(draw, text, font) <= max_w:
        return text
    while len(text) > 1 and _text_width(draw, text + "…", font) > max_w:
        text = text[:-1]
    return text + "…"


# =========================================================================
# STATUS BAR  (navy, shared by all modes)
# =========================================================================
def _draw_status_bar(draw, fonts, state, location=""):
    x0, y0, x1, y1 = STATUS_BAR
    draw.rectangle([x0, y0, x1, y1], fill=NAVY)

    mode_label = {"weather": "WEATHER", "waves": "WAVES"}.get(
        state.display_mode, state.display_mode.upper()
    )
    draw.text((PADDING, y0 + 5), mode_label, font=fonts.status, fill=IVORY)

    now_eastern = datetime.now(_EASTERN)
    date_str = now_eastern.strftime("%A, %-d %B %Y")
    draw_text_right(draw, x1 - PADDING, y0 + 5, date_str, fonts.status, fill=IVORY)


# =========================================================================
# BOTTOM BAR
# =========================================================================
def _draw_bottom_bar(draw, fonts, state):
    x0, y0, x1, y1 = BOTTOM_BAR
    draw.rectangle([x0, y0, x1, y1], fill=NAVY)


# =========================================================================
# WEATHER MODE — Current Conditions panel  (left, WEATHER_CURRENT)
# =========================================================================
def _draw_current_panel(draw, fonts, current):
    x0, y0, x1, y1 = WEATHER_CURRENT
    draw_vline(draw, x1, y0, y1)
    draw_hline(draw, x0, WIDTH, y1)

    px, py = x0 + PADDING, y0 + PADDING

    draw.text((px, py), "NOW", font=fonts.header, fill=INK)
    py += 30

    if current is None:
        draw.text((px, py), "Conditions unavailable", font=fonts.body, fill=DIM)
        return

    draw.text((px, py), current.description or "--", font=fonts.body, fill=DIM)
    py += 26

    deg = "°"
    temp_str = fmt(current.temperature_f, deg + "F", decimal=0)
    temp_x = px
    draw.text((temp_x, py), temp_str, font=fonts.hero, fill=AMBER)
    temp_w = _text_width(draw, temp_str, fonts.hero)

    # Conditions stacked to the right of the temperature
    cx = temp_x + temp_w + 14
    cy = py - 50
    wind = "Wind: {} {}".format(
        current.wind_dir or "--", fmt(current.wind_speed_mph, " mph"))
    if current.wind_gust_mph:
        wind += " (g{:.0f})".format(current.wind_gust_mph)
    draw.text((cx, cy), wind, font=fonts.small, fill=INK)
    cy += 20
    draw.text((cx, cy),
              "Humidity: {}".format(fmt(current.humidity, "%", decimal=0)),
              font=fonts.small, fill=DIM)
    cy += 20
    draw.text((cx, cy),
              "Pressure: {}".format(fmt(current.pressure_inhg, " inHg", decimal=2)),
              font=fonts.small, fill=DIM)
    cy += 20
    draw.text((cx, cy),
              "Visibility: {}".format(fmt(current.visibility_mi, " mi")),
              font=fonts.small, fill=DIM)
    cy += 20
    draw.text((cx, cy),
              "Dewpoint: {}".format(fmt(current.dewpoint_f, deg + "F")),
              font=fonts.small, fill=DIM)


# =========================================================================
# WEATHER MODE — Today's Forecast panel  (right top, WEATHER_TODAY)
# =========================================================================
def _draw_today_panel(draw, fonts, periods):
    x0, y0, x1, y1 = WEATHER_TODAY
    py = y0 + PADDING
    rx = x1 - PADDING  # right anchor

    if not periods:
        draw_text_right(draw, rx, py, "Forecast unavailable", fonts.body, fill=DIM)
        return

    for p in periods[:2]:
        draw_text_right(draw, rx, py, p.name.upper(), fonts.header, fill=INK)
        py += 28

        deg  = "°"
        temp = "{}{}{}".format(p.temperature, deg, p.temperature_unit)
        draw_text_right(draw, rx, py, temp, fonts.body, fill=INK)
        temp_w = _text_width(draw, temp, fonts.body)
        max_fc_w = rx - temp_w - 10 - (x0 + PADDING)
        if max_fc_w > 20:
            fc = truncate(p.short_forecast, fonts.small, draw, max_fc_w)
            draw.text((x0 + PADDING, py + 2), fc, font=fonts.small, fill=DIM)
        py += 22

        wind_line = "{} {}".format(p.wind_direction, p.wind_speed)
        if p.precip_chance is not None:
            wind_line += "   Precip: {}%".format(p.precip_chance)
        draw_text_right(draw, rx, py, wind_line, fonts.small, fill=DIM)
        py += 30

        if py < y1 - 40:
            draw_hline(draw, x0 + PADDING, rx, py - 4)
            py += 4


# =========================================================================
# WEATHER MODE — Hourly chart  (bottom strip, WEATHER_CHART)
# =========================================================================
def _draw_weather_chart(draw, img, hourly_data, fonts):
    """Render a 2-panel matplotlib chart into WEATHER_CHART and paste into img."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import matplotlib.gridspec as gridspec
    from matplotlib.lines import Line2D
    from io import BytesIO

    x0, y0, x1, y1 = WEATHER_CHART
    w, h = x1 - x0, y1 - y0
    dpi = 100

    if hourly_data is None or hourly_data.error or not hourly_data.times:
        draw.text((x0 + PADDING, y0 + 60),
                  "Hourly chart unavailable", font=fonts.body, fill=DIM)
        return

    # ── Build paired arrays, clipping to first 72 h (3 days) ────────────
    data_list = list(zip(
        hourly_data.times,
        hourly_data.temperature_f,
        hourly_data.precip_pct,
        hourly_data.cloud_cover_pct,
        hourly_data.pressure_inhg,
    ))[:72]
    if not data_list:
        return

    times_dt, temps, precips, clouds, pressures = zip(*data_list)
    t_nums  = mdates.date2num(list(times_dt))
    now_num = float(mdates.date2num(datetime.now(_EASTERN)))

    # ── ACeP-safe colours ────────────────────────────────────────────────
    c_amber = (210/255, 85/255,  0/255)
    c_navy  = (15/255,  55/255, 120/255)
    c_cloud = (160/255, 200/255, 240/255)   # light blue → dithers as sparse blue

    fig = plt.figure(figsize=(w / dpi, h / dpi), dpi=dpi)
    fig.patch.set_facecolor("white")
    gs = gridspec.GridSpec(
        2, 1, figure=fig,
        height_ratios=[0.55, 0.45],
        hspace=0.20,
        left=0.060, right=0.925, top=0.97, bottom=0.26,
    )

    # ── Top: Temperature ─────────────────────────────────────────────────
    ax_t = fig.add_subplot(gs[0])
    ax_t.set_facecolor("white")
    ax_t.plot(t_nums, temps, color=c_amber, linewidth=2.2, solid_capstyle="round")
    ax_t.axvline(now_num, color=c_amber, linewidth=1.5, linestyle="--", alpha=0.7)
    ax_t.set_xlim(t_nums[0], t_nums[-1])
    t_min, t_max = min(temps), max(temps)
    pad = max((t_max - t_min) * 0.18, 3.0)
    ax_t.set_ylim(t_min - pad, t_max + pad)
    ax_t.set_ylabel("°F", fontsize=9, fontweight="bold", labelpad=2)
    ax_t.tick_params(axis="y", labelsize=9, pad=1, length=2)
    ax_t.tick_params(axis="x", bottom=False, labelbottom=False)
    ax_t.spines["top"].set_visible(False)
    ax_t.spines["right"].set_visible(False)

    # ── Bottom: Cloud cover + Precip + Pressure ───────────────────────────
    ax_b = fig.add_subplot(gs[1])
    ax_b.set_facecolor("white")

    ax_b.fill_between(t_nums, clouds, 0, color=c_cloud, alpha=1.0, label="Cloud %")
    ax_b.fill_between(t_nums, precips, 0, color=c_navy, alpha=1.0, label="Precip %")
    ax_b.set_ylim(0, 108)
    ax_b.set_xlim(t_nums[0], t_nums[-1])
    ax_b.set_ylabel("%", fontsize=9, fontweight="bold", labelpad=2)
    ax_b.tick_params(axis="y", labelsize=9, pad=1, length=2)
    ax_b.axvline(now_num, color=c_amber, linewidth=1.5, linestyle="--", alpha=0.7)

    # Pressure on right y-axis
    ax_p = ax_b.twinx()
    ax_p.plot(t_nums, pressures, color="black", linewidth=1.8)
    p_vals = [v for v in pressures if v and v > 0]
    if p_vals:
        spread = max(max(p_vals) - min(p_vals), 0.2)
        ax_p.set_ylim(min(p_vals) - spread * 0.4,
                      max(p_vals) + spread * 0.4)
    ax_p.set_ylabel("inHg", fontsize=8, labelpad=2)
    ax_p.tick_params(axis="y", labelsize=8, pad=1, length=2)
    ax_p.spines["top"].set_visible(False)

    # X-axis time labels — day boundaries for 3-day span, 12h minor ticks
    ax_b.xaxis.set_major_locator(mdates.DayLocator(tz=_EASTERN))
    ax_b.xaxis.set_major_formatter(mdates.DateFormatter("%a %-m/%-d", tz=_EASTERN))
    ax_b.xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 12, 18], tz=_EASTERN))
    ax_b.tick_params(axis="x", which="major", labelsize=9, pad=2, length=3)
    ax_b.tick_params(axis="x", which="minor", length=2)
    ax_b.spines["top"].set_visible(False)

    # Legend centered below second timeseries
    from matplotlib.patches import Patch
    legend_elems = [
        Patch(facecolor=c_cloud, label="Cloud Cover"),
        Patch(facecolor=c_navy, label="Precip %"),
        Line2D([0], [0], color="black", linewidth=1.5, label="Pressure"),
    ]
    ax_b.legend(handles=legend_elems, fontsize=8,
                loc="upper center", bbox_to_anchor=(0.5, -0.40),
                framealpha=0.85, ncol=3,
                borderpad=0.5, handlelength=1.8,
                handletextpad=0.5, columnspacing=1.5)

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches=None)
    plt.close(fig)
    buf.seek(0)
    chart_img = Image.open(buf).convert("RGB").resize((w, h), Image.Resampling.LANCZOS)
    img.paste(chart_img, (x0, y0))


# =========================================================================
# WAVES MODE — tide interpolation helper
# =========================================================================
def _interpolate_tides(predictions, start_time, end_time, n_points=49):
    """
    Cosine-interpolate water level between H/L predictions.
    Returns (times_list, heights_list) with n_points samples.
    """
    if not predictions or len(predictions) < 2:
        return [], []

    preds = sorted(predictions, key=lambda p: p.timestamp)
    times_out, heights_out = [], []
    total_sec = (end_time - start_time).total_seconds()

    for i in range(n_points):
        t = start_time + timedelta(seconds=total_sec * i / (n_points - 1))

        p_before, p_after = None, None
        for p in preds:
            if p.timestamp <= t:
                p_before = p
            elif p_after is None:
                p_after = p

        if p_after is not None and p_before is not None:
            pass   # handled in else below
        elif p_after is not None:
            height = p_after.height_ft
        elif p_before is not None:
            height = p_before.height_ft
        else:
            continue
        if p_before is not None and p_after is not None:
            span = (p_after.timestamp - p_before.timestamp).total_seconds()
            if span <= 0:
                height = p_before.height_ft
            else:
                frac = (t - p_before.timestamp).total_seconds() / span
                height = (
                    (p_before.height_ft + p_after.height_ft) / 2
                    + (p_before.height_ft - p_after.height_ft) / 2
                    * math.cos(math.pi * frac)
                )

        times_out.append(t)
        heights_out.append(height)

    return times_out, heights_out


# =========================================================================
# WAVES MODE — tide sparkline column (all 4 rows in one matplotlib figure)
# =========================================================================
def _render_tide_column(tide_list, col_w, total_h):
    """
    Render a 4-row tide sparkline column as a single PIL image (col_w × total_h).
    tide_list: [TideData or None] for rows 0–3 (top to bottom).
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from io import BytesIO

    n_rows  = len(tide_list)
    dpi     = 100

    fig, axes = plt.subplots(
        n_rows, 1,
        figsize=(col_w / dpi, total_h / dpi),
        dpi=dpi,
    )
    if n_rows == 1:
        axes = [axes]

    fig.patch.set_facecolor("white")
    fig.subplots_adjust(
        left=0.01, right=0.92, top=0.99, bottom=0.04, hspace=0.0
    )

    now_utc  = datetime.now(timezone.utc)
    end_utc  = now_utc + timedelta(hours=24)
    now_et   = now_utc.astimezone(_EASTERN)
    now_num  = float(mdates.date2num(now_et))

    c_navy  = (15/255, 55/255, 120/255)
    c_amber = (210/255, 85/255, 0/255)

    for i, (ax, tides) in enumerate(zip(axes, tide_list)):
        ax.set_facecolor("white")

        if tides is None or tides.error or not tides.predictions:
            ax.text(0.5, 0.5, "no tide data",
                    ha="center", va="center",
                    transform=ax.transAxes, fontsize=6, color=INK)
        else:
            times_i, heights_i = _interpolate_tides(
                tides.predictions, now_utc, end_utc, n_points=49)

            if times_i:
                t_et  = [t.astimezone(_EASTERN) for t in times_i]
                t_num = mdates.date2num(t_et)
                h_min = min(heights_i)

                ax.plot(t_num, heights_i, color=c_navy, linewidth=1.5)
                ax.axvline(now_num, color=c_amber, linewidth=1.8, zorder=5)

                # H/L value annotations within the 24h window (no H/L prefix)
                for p in tides.predictions:
                    if now_utc <= p.timestamp <= end_utc:
                        pt_et  = p.timestamp.astimezone(_EASTERN)
                        pt_num = mdates.date2num(pt_et)
                        ax.plot(pt_num, p.height_ft, "o",
                                color="black", markersize=3.0, zorder=6)
                        ofs = 7 if p.type == "H" else -12
                        ax.annotate(
                            "{:.1f}".format(p.height_ft),
                            (pt_num, p.height_ft),
                            textcoords="offset points", xytext=(0, ofs),
                            fontsize=8, ha="center", color="black",
                            fontweight="bold",
                        )

                ax.set_xlim(t_num[0], t_num[-1])
                h_pad = max((max(heights_i) - h_min) * 0.25, 0.3)
                ax.set_ylim(h_min - 0.5 - h_pad * 0.3,
                            max(heights_i) + h_pad)

        # X-axis ticks (6-hour labels)
        ax.xaxis.set_major_locator(
            mdates.HourLocator(byhour=[6, 12, 18, 0], tz=_EASTERN))
        ax.xaxis.set_major_formatter(
            mdates.DateFormatter("%-I%p", tz=_EASTERN))
        ax.tick_params(axis="x", labelsize=8, pad=1, length=2)
        ax.tick_params(axis="y", labelsize=6, pad=1, length=2)
        ax.set_ylabel("ft", fontsize=6, labelpad=1)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        if i > 0:
            ax.spines["top"].set_visible(True)
            ax.spines["top"].set_linewidth(1.0)
            ax.spines["top"].set_color("black")

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches=None)
    plt.close(fig)
    buf.seek(0)
    result = Image.open(buf).convert("RGB")
    return result.resize((col_w, total_h), Image.Resampling.LANCZOS)


# =========================================================================
# WAVES MODE — swell cell  (col 1)
# =========================================================================
_ROW_LABELS = {
    "hampton":     "Hampton",
    "good_harbor": "Good Hbr",
    "nantasket":   "Nantasket",
    "south_shore": "S Shore",
}


def _draw_swell_cell(draw, fonts, buoy, beach_key, buoy_abbr, x0, x1, y0, y1):
    px = x0 + PADDING
    py = y0 + 5

    location = _ROW_LABELS.get(beach_key, beach_key)
    draw.text((px, py), location, font=fonts.body, fill=INK)
    draw_text_right(draw, x1 - 4, py + 2, buoy_abbr, fonts.small, fill=DIM)
    py += 24

    if buoy is None:
        draw.text((px, py), "No buoy data", font=fonts.small, fill=DIM)
        return

    met  = buoy.met
    spec = buoy.spectral

    # Resolve swell height, period, direction
    if spec and spec.swell_height_ft is not None:
        swell_ht  = spec.swell_height_ft
        swell_per = spec.swell_period
        swell_dir = spec.swell_dir or "--"
    elif met:
        swell_ht  = met.wave_height_ft
        swell_per = met.dom_period
        if met.mean_wave_dir is not None:
            dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
                    "S","SSW","SW","WSW","W","WNW","NW","NNW"]
            swell_dir = dirs[int((met.mean_wave_dir + 11.25) / 22.5) % 16]
        else:
            swell_dir = "--"
    else:
        swell_ht = swell_per = None
        swell_dir = "--"

    ht_str = fmt(swell_ht, "ft")
    draw.text((px, py), ht_str, font=fonts.header, fill=AMBER)
    ht_w = _text_width(draw, ht_str, fonts.header)

    detail = "  {}  {}".format(fmt(swell_per, "s"), swell_dir)
    # align detail text vertically centred with header
    draw.text((px + ht_w, py + 4), detail, font=fonts.small, fill=INK)
    py += 28

    # Water temp if available and space permits
    if met and met.water_temp_f is not None and py + 18 < y1:
        draw.text((px, py),
                  "Water: {:.1f}°F".format(met.water_temp_f),
                  font=fonts.small, fill=DIM)


# =========================================================================
# WAVES MODE — wind cell  (col 2)
# =========================================================================
def _draw_wind_cell(draw, fonts, bw, x0, x1, y0, y1):  # noqa: ARG001 x1 reserved for truncation
    px = x0 + PADDING
    py = y0 + 5

    if bw is None or bw.error:
        draw.text((px, py), "No wind data", font=fonts.small, fill=DIM)
        return

    dir_str   = bw.wind_dir_cardinal or "--"
    speed_str = "{:.0f}kts".format(bw.wind_speed_kts or 0)
    draw.text((px, py), "{} @ {}".format(dir_str, speed_str),
              font=fonts.body, fill=NAVY)
    py += 25

    draw.text((px, py),
              "Gust {:.0f}kts".format(bw.wind_gust_kts or 0),
              font=fonts.small, fill=DIM)
    py += 20

    if bw.temperature_f is not None and py + 18 < y1:
        draw.text((px, py),
                  "Air: {:.0f}°F".format(bw.temperature_f),
                  font=fonts.small, fill=DIM)


# =========================================================================
# WAVES MODE — full grid
# =========================================================================
def _draw_waves_grid(draw, img, fonts, buoys, all_tides, beach_winds):
    col_w_tide = WIDTH - WAVES_COL2                # 400 px
    total_h    = WAVES_Y1 - WAVES_Y0              # 428 px

    # Vertical column dividers
    draw_vline(draw, WAVES_COL1, WAVES_Y0, WAVES_Y1)
    draw_vline(draw, WAVES_COL2, WAVES_Y0, WAVES_Y1)

    # Collect tide data in row order
    tide_list = []
    for buoy_id, beach_key in BUOY_BEACH_PAIRINGS:
        beach = BEACHES[beach_key]
        tides = (all_tides or {}).get(beach.tide_station_id)
        tide_list.append(tides)

    # Render all 4 tide sparklines in one figure, paste into right column
    try:
        tide_col_img = _render_tide_column(tide_list, col_w_tide, total_h)
        img.paste(tide_col_img, (WAVES_COL2, WAVES_Y0))
    except Exception as exc:
        logger.warning("Tide sparkline rendering failed: %s", exc)
        draw.text((WAVES_COL2 + PADDING, WAVES_Y0 + 20),
                  "Tide charts unavailable", font=fonts.small, fill=DIM)

    # Swell + wind cells for each row
    for row_idx, (buoy_id, beach_key) in enumerate(BUOY_BEACH_PAIRINGS):
        ry0 = WAVES_Y0 + row_idx * WAVES_ROW_H
        ry1 = ry0 + WAVES_ROW_H

        if row_idx > 0:
            draw_hline(draw, 0, WAVES_COL2, ry0)

        buoy = (buoys or {}).get(buoy_id)
        bw   = (beach_winds or {}).get(beach_key)
        abbr = STATION_ABBREVIATIONS.get(buoy_id, buoy_id[-3:])

        _draw_swell_cell(draw, fonts, buoy, beach_key, abbr,
                         x0=0,          x1=WAVES_COL1, y0=ry0, y1=ry1)
        _draw_wind_cell(draw, fonts, bw,
                        x0=WAVES_COL1, x1=WAVES_COL2, y0=ry0, y1=ry1)


# =========================================================================
# TOP-LEVEL RENDER FUNCTIONS
# =========================================================================
def render_weather_mode(weather, hourly_chart, fonts, state):
    img  = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    location = weather.location_name if weather else ""
    _draw_status_bar(draw, fonts, state, location=location)
    _draw_current_panel(draw, fonts, weather.current if weather else None)
    _draw_today_panel(draw, fonts, weather.forecast_periods if weather else [])
    _draw_weather_chart(draw, img, hourly_chart, fonts)
    _draw_bottom_bar(draw, fonts, state)
    return img


def render_waves_consolidated(buoys, all_tides, beach_winds, fonts, state):
    img  = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    _draw_status_bar(draw, fonts, state)
    _draw_waves_grid(draw, img, fonts, buoys, all_tides, beach_winds)
    _draw_bottom_bar(draw, fonts, state)
    return img


def render_display(weather, hourly_chart, state,
                   buoys=None, all_tides=None, beach_winds=None):
    fonts = Fonts.load()
    if state.display_mode == "waves":
        return render_waves_consolidated(buoys, all_tides, beach_winds, fonts, state)
    else:
        return render_weather_mode(weather, hourly_chart, fonts, state)


# =========================================================================
# OUTPUT FUNCTIONS
# =========================================================================
def display_mock(image, output_dir=None, label=""):
    out = output_dir or MOCK_OUTPUT_DIR
    os.makedirs(out, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "_{}".format(label) if label else ""
    path = os.path.join(out, "display_{}{}.png".format(timestamp, suffix))
    image.save(path)
    logger.info("Mock display saved to %s", path)
    return path


def display_to_epaper(image):
    """Send RGB image to Inky Impression 7.3" display."""
    try:
        from inky.auto import auto  # type: ignore
        inky = auto(ask_user=True, verbose=True)
    except Exception:
        try:
            from inky import InkyImpression  # type: ignore
            inky = InkyImpression(WIDTH, HEIGHT)
        except ImportError:
            logger.warning("inky library not installed — use --mock for PNG output")
            return False

    try:
        inky.set_image(image, saturation=1.0)
        inky.show()
        return True
    except Exception as exc:
        logger.error("Inky display error: %s", exc)
        return False


# =========================================================================
# DUMMY DATA  (for --no-fetch layout testing)
# =========================================================================
def _make_dummy_buoy(station_id="44029"):
    from fetchers.buoy import BuoyReading, MetData, SpectralData
    now     = datetime.now(timezone.utc)
    station = BUOY_STATIONS.get(station_id)
    name    = station.name if station else station_id

    _variants = {
        "44098": dict(wave_height=1.1, dom_period=7.0, wind_dir=270.0, wind_speed=5.1,
                      swell_height=0.8, swell_period=9.0, swell_dir="W",
                      water_temp=8.2, pressure=1022.1),
        "44029": dict(wave_height=1.6, dom_period=9.0, wind_dir=135.0, wind_speed=6.4,
                      swell_height=1.2, swell_period=11.0, swell_dir="SSE",
                      water_temp=9.1, pressure=1018.5),
        "44013": dict(wave_height=2.1, dom_period=10.0, wind_dir=200.0, wind_speed=7.8,
                      swell_height=1.7, swell_period=12.0, swell_dir="S",
                      water_temp=8.5, pressure=1015.3),
        "44097": dict(wave_height=1.3, dom_period=8.0, wind_dir=315.0, wind_speed=4.2,
                      swell_height=0.9, swell_period=10.0, swell_dir="NW",
                      water_temp=10.3, pressure=1020.8),
    }
    v = _variants.get(station_id, _variants["44029"])

    met = MetData(
        timestamp=now,
        wind_dir=v["wind_dir"], wind_speed=v["wind_speed"], wind_gust=v["wind_speed"] * 1.4,
        wave_height=v["wave_height"], dom_period=v["dom_period"], avg_period=v["dom_period"] * 0.7,
        mean_wave_dir=v["wind_dir"], pressure=v["pressure"],
        air_temp=5.6, water_temp=v["water_temp"], dew_point=2.3,
        visibility=10.0, pressure_tendency=-1.2,
    )
    spectral = SpectralData(
        timestamp=now,
        wave_height=v["wave_height"], swell_height=v["swell_height"],
        swell_period=v["swell_period"], wind_wave_height=v["wave_height"] - v["swell_height"],
        wind_wave_period=5.0, swell_dir=v["swell_dir"], wind_wave_dir="NE",
        steepness="AVERAGE", avg_period=v["dom_period"] * 0.7, mean_wave_dir=v["wind_dir"],
    )
    return BuoyReading(station_id=station_id, station_name=name,
                       met=met, spectral=spectral)


def _make_dummy_tides(station_id="8443970", station_name="Boston"):
    from fetchers.tides import TideData, TidePrediction, WaterLevel
    now = datetime.now(timezone.utc)

    _variants = {
        "8429489": dict(high=9.1, low=0.2, offset_h=1.0,  current=4.5),
        "8441841": dict(high=8.7, low=0.4, offset_h=1.5,  current=3.8),
        "8443970": dict(high=9.5, low=-0.3, offset_h=0.0, current=4.2),
        "8450948": dict(high=4.8, low=0.1, offset_h=3.0,  current=2.9),
    }
    v = _variants.get(station_id, _variants["8443970"])

    preds = []
    for i in range(10):
        t = now + timedelta(hours=6 * i + v["offset_h"] + 3)
        h = v["high"] if i % 2 == 0 else v["low"]
        preds.append(TidePrediction(
            timestamp=t, height_ft=round(h, 2),
            type="H" if i % 2 == 0 else "L"))
    return TideData(
        station_id=station_id, station_name=station_name,
        current_level=WaterLevel(timestamp=now, height_ft=v["current"]),
        predictions=preds,
    )


def _make_dummy_weather():
    from fetchers.weather import WeatherData, CurrentObservation, ForecastPeriod
    now = datetime.now(timezone.utc)

    current = CurrentObservation(
        station_id="KBOS", timestamp=now.isoformat(),
        description="Overcast",
        temperature_f=38.2, dewpoint_f=28.0, humidity=65.0,
        wind_dir="NW", wind_speed_mph=12.5, wind_gust_mph=18.0,
        pressure_inhg=30.12, visibility_mi=10.0,
    )
    periods = []
    names = [
        ("Tonight",   False, 28, "Mostly Clear",   10),
        ("Wednesday", True,  42, "Partly Sunny",    5),
        ("Wed Night", False, 30, "Cloudy",          40),
        ("Thursday",  True,  45, "Rain Likely",     80),
        ("Thu Night", False, 36, "Light Rain",      60),
        ("Friday",    True,  38, "Sleet Likely",    72),
        ("Fri Night", False, 25, "Partly Cloudy",   15),
        ("Saturday",  True,  35, "Mostly Sunny",     5),
    ]
    for n, is_day, temp, fc, precip in names:
        periods.append(ForecastPeriod(
            name=n, start_time="", end_time="",
            is_daytime=is_day, temperature=temp, temperature_unit="F",
            wind_speed="10 to 15 mph", wind_direction="NW",
            short_forecast=fc, detailed_forecast="",
            precip_chance=precip,
        ))
    return WeatherData(location_name="Boston, MA", current=current,
                       forecast_periods=periods)


def _make_dummy_hourly_chart():
    from fetchers.weather import HourlyChartData
    now = datetime.now(_EASTERN).replace(minute=0, second=0, microsecond=0)
    times = [now + timedelta(hours=i) for i in range(120)]

    temps    = [52 + 8 * math.sin(2 * math.pi * (i - 6) / 24)    for i in range(120)]
    precips  = [max(0, 30 * math.sin(math.pi * (i - 16) / 16))   for i in range(120)]
    clouds   = [max(0, 70 * math.sin(2 * math.pi * i / 36))      for i in range(120)]
    pressures = [30.05 + 0.18 * math.sin(2 * math.pi * i / 48)   for i in range(120)]

    return HourlyChartData(
        times=times,
        temperature_f=temps,
        precip_pct=precips,
        cloud_cover_pct=clouds,
        pressure_inhg=pressures,
    )


def _make_dummy_beach_winds():
    from fetchers.wind import BeachWind
    now = datetime.now()

    _variants = {
        "hampton":     dict(speed=18.2, dir=315.0, gust=26.0, temp=34.0),
        "good_harbor": dict(speed=12.5, dir=200.0, gust=17.0, temp=38.0),
        "nantasket":   dict(speed=9.8,  dir=160.0, gust=13.0, temp=41.0),
        "south_shore": dict(speed=14.1, dir=240.0, gust=20.5, temp=43.0),
    }
    winds = {}
    for key, beach in BEACHES.items():
        v = _variants.get(key, dict(speed=12.0, dir=180.0, gust=16.0, temp=38.0))
        times  = [(now + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(24)]
        speeds = [v["speed"] + i * 0.3 for i in range(24)]
        dirs   = [v["dir"] + i * 3    for i in range(24)]
        gusts  = [v["gust"] + i * 0.2 for i in range(24)]
        winds[key] = BeachWind(
            beach_key=key, beach_name=beach.name,
            timestamp=now.strftime("%Y-%m-%dT%H:%M"),
            wind_speed_kts=v["speed"], wind_direction=v["dir"],
            wind_gust_kts=v["gust"], temperature_f=v["temp"],
            hourly_times=times, hourly_speed=speeds,
            hourly_direction=dirs, hourly_gust=gusts,
        )
    return winds


def _make_all_dummy_data():
    buoys       = {sid: _make_dummy_buoy(sid) for sid in BUOY_STATIONS}
    weather     = _make_dummy_weather()
    hourly      = _make_dummy_hourly_chart()
    all_tides   = {}
    for key, beach in BEACHES.items():
        if beach.tide_station_id not in all_tides:
            all_tides[beach.tide_station_id] = _make_dummy_tides(
                beach.tide_station_id, beach.tide_station_name)
    beach_winds = _make_dummy_beach_winds()
    return buoys, weather, hourly, all_tides, beach_winds


# =========================================================================
# CLI
# =========================================================================
def main():
    parser = argparse.ArgumentParser(description="Render Inky Impression display")
    parser.add_argument("--mode", choices=["weather", "waves"],
                        help="Force display mode")
    parser.add_argument("--toggle", action="store_true",
                        help="Cycle display mode and exit")
    parser.add_argument("--mock", action="store_true",
                        help="Save PNG instead of sending to display")
    parser.add_argument("--no-fetch", action="store_true",
                        help="Use dummy data for layout testing")
    parser.add_argument("--all", action="store_true",
                        help="Render both screens (weather + waves)")
    parser.add_argument("--output-dir",
                        help="Directory for mock PNGs")
    parser.add_argument("--state-file", default=STATE_FILE_PATH)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    state = DisplayState.load(args.state_file)

    if args.toggle:
        new_mode = state.cycle_mode()
        state.save(args.state_file)
        print("Display mode cycled to: {}".format(new_mode))
        return

    if args.mode:
        state.display_mode = args.mode

    # ── Fetch (or fabricate) data ─────────────────────────────────────────
    if args.no_fetch or args.all and args.no_fetch:
        buoys, weather, hourly_chart, all_tides, beach_winds = _make_all_dummy_data()
    elif not args.no_fetch:
        from fetchers.buoy    import fetch_all_buoys
        from fetchers.weather import fetch_weather, fetch_hourly_chart_data
        from fetchers.tides   import fetch_all_tides
        from fetchers.wind    import fetch_beach_winds
        buoys       = fetch_all_buoys()
        weather     = fetch_weather()
        hourly_chart = fetch_hourly_chart_data()
        tide_ids    = list(set(b.tide_station_id for b in BEACHES.values()))
        all_tides   = fetch_all_tides(tide_ids)
        beach_winds = fetch_beach_winds()
    else:
        buoys, weather, hourly_chart, all_tides, beach_winds = _make_all_dummy_data()

    fonts = Fonts.load()

    if args.all:
        s = DisplayState(display_mode="weather")
        path = display_mock(
            render_weather_mode(weather, hourly_chart, fonts, s),
            args.output_dir, "weather")
        print("  " + path)

        s = DisplayState(display_mode="waves")
        path = display_mock(
            render_waves_consolidated(buoys, all_tides, beach_winds, fonts, s),
            args.output_dir, "waves")
        print("  " + path)
        return

    # Single render
    image = render_display(weather, hourly_chart, state,
                           buoys=buoys, all_tides=all_tides,
                           beach_winds=beach_winds)
    state.save(args.state_file)

    if args.mock or args.no_fetch:
        path = display_mock(image, args.output_dir)
        print("Display saved to: {}".format(path))
    else:
        if not display_to_epaper(image):
            path = display_mock(image, args.output_dir)
            print("E-paper unavailable, saved to: {}".format(path))


if __name__ == "__main__":
    main()
