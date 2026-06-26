"""
MLB schedule integration — identifies pitchers with 2+ starts in a CBS scoring week.

Uses the free MLB Stats API (no auth required).

Usage:
    from mlb.schedule import two_start_pitchers, week_bounds
    starters = two_start_pitchers()          # current CBS week
    starters = two_start_pitchers(next=True) # next week
    # returns {norm_name: start_count}
"""

import logging
from datetime import date, datetime, timedelta
from collections import defaultdict
from functools import lru_cache
from zoneinfo import ZoneInfo

import requests

from mlb.teams import mlb_to_cbs, norm_name

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
        next_monday, _ = week_bounds(d, next_week=True)  # (monday, sunday)
        start = next_monday
        end   = start + timedelta(days=lookahead_days - 1)
    else:
        start = _today_et() if d is None else d
        end   = start + timedelta(days=lookahead_days - 1)
    counts = _fetch_start_counts(start.isoformat(), end.isoformat())
    return {name: n for name, n in counts.items() if n >= 2}


def is_two_starter(player_name: str, two_starters: dict[str, int]) -> bool:
    """Check if a player (by name) is a 2-starter this week."""
    return _norm(player_name) in two_starters


def schedule_weeks(n: int = 3, d: date = None) -> list[dict]:
    """Return per-week start counts for the next n CBS scoring weeks.

    Returns a list of n dicts:
      [
        {"week_offset": 0, "monday": date, "sunday": date,
         "two_starters": {norm_name: start_count}},
        ...
      ]

    week_offset=0 is the current week, 1 is next week, etc.
    Only SPs with 2+ starts in a week appear in two_starters.
    """
    if d is None:
        d = _today_et()
    weeks = []
    for offset in range(n):
        is_next = offset > 0
        monday, sunday = week_bounds(d, next_week=False)
        # Shift by offset weeks
        monday += timedelta(weeks=offset)
        sunday += timedelta(weeks=offset)
        counts = _fetch_start_counts(monday.isoformat(), sunday.isoformat())
        weeks.append({
            "week_offset": offset,
            "monday":      monday,
            "sunday":      sunday,
            "two_starters": {name: n for name, n in counts.items() if n >= 2},
        })
    return weeks


def back_to_back_two_starters(weeks_data: list[dict],
                               min_weeks: int = 2) -> set[str]:
    """Return norm names that are 2-starters in at least min_weeks of the schedule.

    Use this to flag SP holds that are especially valuable and should not be
    traded or dropped ("back-to-back 2-starter — elite hold").
    """
    from collections import Counter
    counts: Counter = Counter()
    for week in weeks_data:
        for name in week["two_starters"]:
            counts[name] += 1
    return {name for name, cnt in counts.items() if cnt >= min_weeks}


# ---------------------------------------------------------------------------
# Daily schedule
# ---------------------------------------------------------------------------

# Team abbreviation mapping is now in mlb/teams.py (mlb_to_cbs / norm_name).


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
                cbs_abbr = mlb_to_cbs(abbr)
                if abbr != cbs_abbr:
                    logger.debug("Team abbrev mapped: %s → %s", abbr, cbs_abbr)
                teams.add(cbs_abbr)
    logger.info("teams_playing_today %s: %d teams — %s",
                d.isoformat(), len(teams), sorted(teams))
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


def todays_matchups(d: date = None) -> list[dict]:
    """
    Return matchup data for every game today.

    Each dict:
        home_team        str  CBS team abbrev (home)
        away_team        str  CBS team abbrev (away)
        home_starter     str | None  norm_name of home probable starter
        away_starter     str | None  norm_name of away probable starter
        home_starter_hand str | None "L" | "R" | "S" (switch)
        away_starter_hand str | None
        home_starter_name str | None full name
        away_starter_name str | None
        park_factor      int  park factor for the HOME team's park (runs)
        park_factor_hr   int  park factor for the HOME team's park (HR)

    Returns [] if API is unreachable or schedule is empty.
    """
    from mlb.parks import park_factor as _pf
    if d is None:
        d = _today_et()
    games = _fetch_today_games(d.isoformat())
    result = []
    for game in games:
        teams = game.get("teams", {})
        home_info = teams.get("home", {})
        away_info = teams.get("away", {})

        home_team_raw = (home_info.get("team", {}).get("abbreviation", "")
                         or home_info.get("team", {}).get("teamCode", ""))
        away_team_raw = (away_info.get("team", {}).get("abbreviation", "")
                         or away_info.get("team", {}).get("teamCode", ""))

        home_team = mlb_to_cbs(home_team_raw.upper()) if home_team_raw else ""
        away_team = mlb_to_cbs(away_team_raw.upper()) if away_team_raw else ""

        def _starter_info(side_info: dict) -> tuple:
            """(norm_name | None, hand | None, full_name | None)"""
            pitcher = side_info.get("probablePitcher")
            if not pitcher:
                return None, None, None
            full = pitcher.get("fullName", "")
            hand = pitcher.get("pitchHand", {}).get("code")  # "L" / "R" / "S"
            return (norm_name(full) if full else None), hand, (full or None)

        h_norm, h_hand, h_full = _starter_info(home_info)
        a_norm, a_hand, a_full = _starter_info(away_info)

        result.append({
            "home_team":          home_team,
            "away_team":          away_team,
            "home_starter":       h_norm,
            "away_starter":       a_norm,
            "home_starter_hand":  h_hand,
            "away_starter_hand":  a_hand,
            "home_starter_name":  h_full,
            "away_starter_name":  a_full,
            "park_factor":        _pf(home_team, "runs"),
            "park_factor_hr":     _pf(home_team, "hr"),
        })

    logger.info("todays_matchups %s: %d games", d.isoformat(), len(result))
    return result


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
    """Canonical player-name normalizer — delegates to mlb.teams.norm_name."""
    return norm_name(name)
