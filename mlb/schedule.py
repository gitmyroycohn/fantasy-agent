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
from datetime import date, timedelta
from collections import defaultdict
from functools import lru_cache

import requests

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
    """
    if d is None:
        d = date.today()
    monday = d - timedelta(days=d.weekday())   # most recent Monday
    if next_week:
        monday += timedelta(weeks=1)
    sunday = monday + timedelta(days=6)
    return monday, sunday


def two_start_pitchers(d: date = None, next_week: bool = False) -> dict[str, int]:
    """Return {norm_name: num_starts} for pitchers with 2+ scheduled starts.

    Defaults to the current CBS scoring week. Pass next_week=True to look
    at next week instead (useful Thu–Sun when planning future adds).

    norm_name matches the same normalization used in mlb/stats.py so you can
    cross-reference with player stats.
    """
    start, end = week_bounds(d, next_week)
    counts = _fetch_start_counts(start.isoformat(), end.isoformat())
    return {name: n for name, n in counts.items() if n >= 2}


def is_two_starter(player_name: str, two_starters: dict[str, int]) -> bool:
    """Check if a player (by name) is a 2-starter this week."""
    return _norm(player_name) in two_starters


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
