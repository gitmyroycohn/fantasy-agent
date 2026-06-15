"""
MLB schedule integration — identifies pitchers with 2+ starts in a CBS scoring week.

Uses the free MLB Stats API (no auth required).

Usage:
    from mlb.schedule import two_start_pitchers, week_bounds
    starters = two_start_pitchers()          # current CBS week
    starters = two_start_pitchers(next=True) # next week
    # returns {norm_name: start_count}
"""

import re
import logging
from datetime import date, datetime, timedelta
from collections import defaultdict
from functools import lru_cache
from zoneinfo import ZoneInfo

import requests

_ET = ZoneInfo("America/New_York")


def _today_et() -> date:
    """Today's date in US Eastern time (handles UTC offset on GitHub Actions)."""
    return datetime.now(_ET).date()

logger = logging.getLogger(__name__)

MLB_API  = "https://statsapi.mlb.com/api/v1"
TIMEOUT  = 20


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def week_bounds(d: date = None, next_week: bool = False):
    """Return (monday, sunday) for the CBS scoring week containing d.

    CBS H2H weeks run Monday–Sunday.
    If next_week=True, return the following week's bounds.
    Defaults to today in US Eastern time.
    """
    if d is None:
        d = _today_et()
    monday = d - timedelta(days=d.weekday())   # most recent Monday
    if next_week:
        monday += timedelta(weeks=1)
    sunday = monday + timedelta(days=6)
    return monday, sunday


def two_start_pitchers(d: date = None, next_week: bool = False,
                       lookahead_days: int = 10) -> dict[str, int]:
    """Return {norm_name: num_starts} for pitchers with 2+ scheduled starts.

    Uses a rolling lookahead window (default 10 days from today) rather than
    a strict Mon-Sun CBS week, because probable pitchers for Thu-Sun aren't
    announced until mid-week. A 10-day window reliably catches 2-starters
    as soon as both starts are confirmed.

    Pass next_week=True to anchor the window at next Monday instead of today
    (useful Thu-Sun when planning adds for the following week).

    norm_name matches the same normalization used in mlb/stats.py.
    """
    if next_week:
        _, next_monday = week_bounds(d, next_week=True)
        start = next_monday - timedelta(days=6)   # next Monday
        end   = start + timedelta(days=lookahead_days - 1)
    else:
        start = _today_et() if d is None else d
        end   = start + timedelta(days=lookahead_days - 1)
    counts = _fetch_start_counts(start.isoformat(), end.isoformat())
    return {name: n for name, n in counts.items() if n >= 2}


def is_two_starter(player_name: str, two_starters: dict[str, int]) -> bool:
    """Check if a player (by name) is a 2-starter this week."""
    return _norm(player_name) in two_starters


# ---------------------------------------------------------------------------
# Daily schedule
# ---------------------------------------------------------------------------

# MLB API abbrev → CBS abbrev (same mapping as mlb/stats.py)
_MLB_TO_CBS: dict[str, str] = {
    "TB":  "TBR", "CHW": "CWS", "KC": "KCR",
    "SD":  "SDP", "SF":  "SFG",
}


def teams_playing_today(d: date = None) -> set[str]:
    """Return CBS-style team abbreviations for teams with a game today."""
    if d is None:
        d = _today_et()
    games = _fetch_today_games(d.isoformat())
    teams: set[str] = set()
    for game in games:
        for side in ("home", "away"):
            abbr = (game.get("teams", {})
                        .get(side, {})
                        .get("team", {})
                        .get("abbreviation", ""))
            if abbr:
                teams.add(_MLB_TO_CBS.get(abbr, abbr))
    return teams


def probable_starters_today(d: date = None) -> set[str]:
    """Return norm names of pitchers confirmed as probable starters today."""
    if d is None:
        d = _today_et()
    games = _fetch_today_games(d.isoformat())
    starters: set[str] = set()
    for game in games:
        for side in ("home", "away"):
            pitcher = (game.get("teams", {})
                           .get(side, {})
                           .get("probablePitcher"))
            if pitcher:
                name = pitcher.get("fullName", "")
                if name:
                    starters.add(_norm(name))
    return starters


@lru_cache(maxsize=7)
def _fetch_today_games(date_str: str) -> list:
    """Fetch all games for a single date. Returns list of game dicts."""
    url = f"{MLB_API}/schedule"
    params = {
        "sportId":  1,
        "date":     date_str,
        "hydrate":  "probablePitcher,team",
        "gameType": "R",
    }
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error("MLB schedule (daily) API error %s: %s", date_str, e)
        return []
    games = []
    for date_entry in data.get("dates", []):
        games.extend(date_entry.get("games", []))
    logger.info("Daily schedule %s: %d games", date_str, len(games))
    return games


# ---------------------------------------------------------------------------
# Internal: fetch and cache
# ---------------------------------------------------------------------------

@lru_cache(maxsize=14)
def _fetch_start_counts(start_date: str, end_date: str) -> dict[str, int]:
    """Fetch probable starters for [start_date, end_date].

    Returns {norm_name: start_count} for every pitcher with at least one
    probable start listed. Cached per (start_date, end_date) pair.
    """
    url = f"{MLB_API}/schedule"
    params = {
        "sportId":   1,
        "startDate": start_date,
        "endDate":   end_date,
        "hydrate":   "probablePitcher",
        "gameType":  "R",    # regular season only; skip spring/playoffs
    }
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error("MLB schedule API error (%s – %s): %s", start_date, end_date, e)
        return {}

    counts: dict[str, int] = defaultdict(int)
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            for side in ("home", "away"):
                pitcher = (game.get("teams", {})
                               .get(side, {})
                               .get("probablePitcher"))
                if pitcher:
                    full_name = pitcher.get("fullName", "")
                    if full_name:
                        counts[_norm(full_name)] += 1

    total_games = sum(len(d.get("games", [])) for d in data.get("dates", []))
    logger.info("Schedule %s–%s: %d games, %d probable starters",
                start_date, end_date, total_games, len(counts))
    return dict(counts)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    """Match the same normalization as mlb/stats.py for cross-referencing."""
    return re.sub(r"[^a-z0-9]", "", name.lower())
