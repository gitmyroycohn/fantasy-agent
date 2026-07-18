"""
Game-time weather for MLB ballparks.

Data source: Open-Meteo (https://open-meteo.com) — free, no API key.

Public API
----------
fetch_game_weather(cbs_home_team, game_date=None)
    → {"temp_f": int, "wind_mph": float, "wind_dir": int, "wind_label": str,
       "precip_pct": int, "score_bonus": float, "summary": str}

weather_score_bonus(cbs_home_team, game_date=None)
    → float   (positive = hitter-friendly conditions, negative = pitcher-friendly)

WIND DIRECTION LOGIC
--------------------
Each park has a compass bearing from home plate to CF. We compare the
wind direction (the direction it's coming FROM) to the CF bearing to
determine if wind is blowing out (hitter-friendly) or in (pitcher-friendly).

    wind_toward_bearing = (wind_from_dir + 180) % 360
    diff = angular_distance(wind_toward_bearing, CF_bearing)
    diff = 0   → dead-on blowing out to CF (maximum hitter boost)
    diff = 180 → blowing straight in from CF (maximum pitcher boost)
"""

import logging
from datetime import date, datetime
from functools import lru_cache
from math import cos, radians

import requests

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 10

# (latitude, longitude, CF_bearing_from_home_plate_degrees)
# CF_bearing: compass degrees from home plate toward center field.
# Used to compute whether wind is blowing out or in.
_PARKS: dict[str, tuple[float, float, int]] = {
    # ── American League ──────────────────────────────────────────────
    "BAL": (39.284,  -76.622,  83),   # Camden Yards         — CF mostly E
    "BOS": (42.347,  -71.098,  96),   # Fenway Park          — CF roughly E
    "NYY": (40.829,  -73.926,  7),    # Yankee Stadium       — CF roughly N
    "TBR": (27.768,  -82.653, 340),   # Tropicana Field      — dome (indoor)
    "TOR": (43.641,  -79.389, 352),   # Rogers Centre        — dome (indoor)
    "CWS": (41.830,  -87.634,  4),    # Guaranteed Rate      — CF roughly N
    "CLE": (41.496,  -81.685,  4),    # Progressive Field    — CF roughly N
    "DET": (42.339,  -83.049, 15),    # Comerica Park        — CF NNE
    "KCR": (39.052,  -94.480, 13),    # Kauffman Stadium     — CF N
    "MIN": (44.982,  -93.278, 356),   # Target Field         — CF roughly N
    "HOU": (29.757,  -95.355, 349),   # Minute Maid Park     — CF N (retractable)
    "LAA": (33.800, -117.883,  20),   # Angel Stadium        — CF NNE
    "OAK": (37.752, -122.201, 330),   # (historical)
    "ATH": (38.581, -121.494,   0),   # Sacramento Sutter Health (2025+)
    "SEA": (47.591, -122.332, 340),   # T-Mobile Park        — CF NNW
    "TEX": (32.747,  -97.084,  11),   # Globe Life Field     — CF N (retractable)
    # ── National League ──────────────────────────────────────────────
    "NYM": (40.757,  -73.846,  4),    # Citi Field           — CF roughly N
    "PHI": (39.906,  -75.166, 356),   # Citizens Bank Park   — CF roughly N
    "MIA": (25.778,  -80.220,  11),   # loanDepot park       — CF N (retractable)
    "ATL": (33.890,  -84.468,  11),   # Truist Park          — CF N
    "WSH": (38.873,  -77.007, 354),   # Nationals Park       — CF roughly N
    "CHC": (41.948,  -87.655,  89),   # Wrigley Field        — CF roughly E (famous winds)
    "CIN": (39.097,  -84.507, 357),   # GABP                 — CF roughly N
    "MIL": (43.028,  -87.971, 358),   # American Family      — CF roughly N (retractable)
    "PIT": (40.447,  -80.006,  14),   # PNC Park             — CF NNE
    "STL": (38.623,  -90.193,  7),    # Busch Stadium        — CF roughly N
    "ARI": (33.446, -112.067, 353),   # Chase Field          — CF roughly N (retractable)
    "COL": (39.756, -104.994, 346),   # Coors Field          — CF NNW
    "LAD": (34.074, -118.240,  24),   # Dodger Stadium       — CF NNE
    "SFG": (37.778, -122.389,  56),   # Oracle Park          — CF NE (sea wind)
    "SDP": (32.708, -117.157,  23),   # Petco Park           — CF NNE
}

# Dome/retractable parks — weather has zero effect
_DOME_PARKS = {"TBR", "TOR", "HOU", "TEX", "MIL", "ARI", "MIA"}

# Game-time window: average of 5pm–8pm local hours (indices in hourly array)
# Works for most day and night games
_GAME_HOURS = (17, 18, 19, 20)   # 5pm – 8pm


def fetch_game_weather(cbs_home_team: str,
                       game_date: date | None = None) -> dict:
    """
    Return weather conditions for a ballpark on the given date.

    Keys:
        temp_f      int    temperature in °F (average game-time window)
        wind_mph    float  wind speed mph (average game-time window)
        wind_dir    int    wind direction degrees (meteorological: coming FROM)
        wind_label  str    e.g. "blowing out to CF", "blowing in", "crosswind"
        precip_pct  int    precipitation probability % (max over game window)
        score_bonus float  matchup score adjustment (positive = hitter-friendly)
        summary     str    one-line human-readable description
    """
    if game_date is None:
        # BUG 5 item 7: use ET-aware "today", not UTC date.today() --
        # on GitHub Actions/Render, date.today() is UTC and rolls over
        # ~8pm ET, which would fetch tomorrow's weather for a chunk of
        # the evening.
        from mlb.schedule import _today_et
        game_date = _today_et()

    team = cbs_home_team.upper()

    if team in _DOME_PARKS:
        return _dome_result()

    park = _PARKS.get(team)
    if park is None:
        return _unknown_result()

    lat, lng, cf_bearing = park

    try:
        raw = _fetch_hourly(lat, lng, game_date.isoformat())
    except Exception as e:
        logger.warning("weather fetch failed for %s: %s", team, e)
        return _unknown_result()

    hours     = raw.get("hourly", {})
    times     = hours.get("time", [])
    temps     = hours.get("temperature_2m", [])
    winds     = hours.get("wind_speed_10m", [])
    dirs      = hours.get("wind_direction_10m", [])
    precips   = hours.get("precipitation_probability", [])

    # Extract game-window values
    game_temps, game_winds, game_dirs, game_precips = [], [], [], []
    for i, t in enumerate(times):
        try:
            hour = int(t[11:13])   # "2026-06-26T19:00" → 19
        except (ValueError, IndexError):
            continue
        if hour in _GAME_HOURS:
            if i < len(temps)   and temps[i]   is not None: game_temps.append(temps[i])
            if i < len(winds)   and winds[i]   is not None: game_winds.append(winds[i])
            if i < len(dirs)    and dirs[i]    is not None: game_dirs.append(dirs[i])
            if i < len(precips) and precips[i] is not None: game_precips.append(precips[i])

    if not game_winds:
        return _unknown_result()

    temp_f     = round(sum(game_temps) / len(game_temps)) if game_temps else 70
    wind_mph   = round(sum(game_winds) / len(game_winds), 1)
    # Use the most common wind direction (mode over window)
    wind_dir   = round(sum(game_dirs) / len(game_dirs)) % 360 if game_dirs else 0
    precip_pct = max(game_precips) if game_precips else 0

    label    = _wind_label(wind_mph, wind_dir, cf_bearing)
    bonus    = _score_bonus(temp_f, wind_mph, wind_dir, cf_bearing)
    summary  = _build_summary(temp_f, wind_mph, label, precip_pct)

    return {
        "temp_f":     temp_f,
        "wind_mph":   wind_mph,
        "wind_dir":   wind_dir,
        "wind_label": label,
        "precip_pct": int(precip_pct),
        "score_bonus": round(bonus, 2),
        "summary":    summary,
    }


def weather_score_bonus(cbs_home_team: str,
                        game_date: date | None = None) -> float:
    """Quick helper — just returns the score bonus (0.0 on error)."""
    try:
        return fetch_game_weather(cbs_home_team, game_date).get("score_bonus", 0.0)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

@lru_cache(maxsize=32)
def _fetch_hourly(lat: float, lng: float, date_str: str) -> dict:
    """Fetch hourly forecast from Open-Meteo. Cached per park per day."""
    params = {
        "latitude":           lat,
        "longitude":          lng,
        "hourly":             "temperature_2m,precipitation_probability,"
                              "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit":    "mph",
        "temperature_unit":   "fahrenheit",
        "timezone":           "auto",
        "start_date":         date_str,
        "end_date":           date_str,
        "forecast_days":      1,
    }
    r = requests.get(OPEN_METEO_URL, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _angular_diff(a: int, b: int) -> float:
    """Smallest angular distance between two compass bearings (0–180)."""
    diff = abs(a - b) % 360
    return diff if diff <= 180 else 360 - diff


def _wind_label(wind_mph: float, wind_dir: int, cf_bearing: int) -> str:
    """Describe wind direction relative to the ballpark's CF orientation."""
    if wind_mph < 5:
        return "calm"

    # Direction wind is blowing TOWARD (opposite of "coming from")
    wind_toward = (wind_dir + 180) % 360
    diff = _angular_diff(wind_toward, cf_bearing)

    # diff = 0 → dead blowing out to CF
    # diff = 180 → dead blowing in from CF
    if diff <= 35:
        return f"blowing out to CF ({wind_mph:.0f} mph)"
    if diff <= 70:
        return f"blowing out ({wind_mph:.0f} mph)"
    if diff >= 145:
        return f"blowing in from CF ({wind_mph:.0f} mph)"
    if diff >= 110:
        return f"blowing in ({wind_mph:.0f} mph)"
    return f"crosswind ({wind_mph:.0f} mph)"


def _score_bonus(temp_f: float, wind_mph: float,
                 wind_dir: int, cf_bearing: int) -> float:
    """
    Compute a hitting matchup score adjustment for weather conditions.

    Components:
      - Wind blowing out:  up to +6 at 20mph dead-on to CF
      - Wind blowing in:   up to -4 at 20mph dead-on from CF
      - Temperature:       +0.08 per °F above 65 (ball carries more in heat)
      - Precipitation:     handled separately (display only, not scored here)
    """
    bonus = 0.0

    # Wind component
    if wind_mph >= 5:
        wind_toward = (wind_dir + 180) % 360
        diff = _angular_diff(wind_toward, cf_bearing)
        # alignment: 1.0 = straight out, 0.0 = crosswind, -1.0 = straight in
        alignment = cos(radians(diff))
        # Scale: 20mph straight out → +6; 20mph straight in → -4
        if alignment > 0:
            bonus += alignment * wind_mph * 0.30
        else:
            bonus += alignment * wind_mph * 0.20

    # Temperature component: each 10°F above 65°F ≈ +0.8
    bonus += (temp_f - 65) * 0.08

    return round(bonus, 2)


def _build_summary(temp_f: int, wind_mph: float,
                   wind_label: str, precip_pct: int) -> str:
    parts = [f"{temp_f}°F", wind_label]
    if precip_pct >= 20:
        parts.append(f"rain {precip_pct}%")
    return ", ".join(parts)


def _dome_result() -> dict:
    return {
        "temp_f": 72, "wind_mph": 0.0, "wind_dir": 0,
        "wind_label": "dome (no weather effect)",
        "precip_pct": 0, "score_bonus": 0.0,
        "summary": "dome — weather irrelevant",
    }


def _unknown_result() -> dict:
    return {
        "temp_f": 70, "wind_mph": 0.0, "wind_dir": 0,
        "wind_label": "unknown",
        "precip_pct": 0, "score_bonus": 0.0,
        "summary": "weather unavailable",
    }
