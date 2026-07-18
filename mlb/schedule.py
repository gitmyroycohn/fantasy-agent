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
from datetime import date, timedelta
from collections import defaultdict
from functools import lru_cache

import requests

from mlb.teams import mlb_to_cbs, norm_name
from mlb.clock import today_et as _today_et  # noqa: F401 -- re-exported; see mlb/clock.py

logger = logging.getLogger(__name__)

MLB_API  = "https://statsapi.mlb.com/api/v1"
TIMEOUT  = 20


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def week_bounds(d: date = None, next_week: bool = False):
    """Return (start, end) for the real CBS scoring PERIOD containing d.

    BUG 5 fix: CBS scoring periods are NOT uniform 7-day Monday-Sunday weeks
    (Period 1 is 12 days, Period 16 is 14 days for the All-Star break, and
    season_start is not a Monday). This now resolves the true period bounds
    from config/leagues.yaml's periods table (see config/periods.py) instead
    of computing a Monday + 7-day window arithmetically.

    If next_week=True, return the bounds of the NEXT period (not d + 7 days --
    during an extended period that would still be inside the current period).
    Defaults to today in US Eastern time.
    """
    from config.periods import period_for_date, period_offset

    if d is None:
        d = _today_et()

    if next_week:
        p = period_offset(d, 1)
    else:
        p = period_for_date(d)

    if p is None:
        # Table doesn't cover this date (shouldn't happen in-season) --
        # degrade to the old Mon-Sun approximation rather than crashing, but
        # log loudly since this means leagues.yaml's periods table needs
        # updating (e.g. a new season with no table entries yet).
        logger.error(
            "No period found in config/leagues.yaml's periods table for %s -- "
            "falling back to Mon-Sun approximation. Update the periods table.",
            d.isoformat(),
        )
        monday = d - timedelta(days=d.weekday())
        if next_week:
            monday += timedelta(weeks=1)
        return monday, monday + timedelta(days=6)

    return p["start"], p["end"]


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
    """Return per-period start counts for the next n real CBS scoring periods.

    Returns a list of up to n dicts:
      [
        {"week_offset": 0, "period": 16, "monday": date, "sunday": date,
         "period_days": 14, "two_starters": {norm_name: start_count},
         "multi_starters": {norm_name: start_count}},
        ...
      ]

    BUG 5 fix: week_offset=N now means "N real periods ahead" (via
    config.periods.period_offset), not "N*7 days ahead". Previously, during
    the 14-day Period 16, week_offset=1 resolved to date+7 days -- still
    inside Period 16 -- so "next period" streaming advice pointed at the
    current period instead of the next one.

    "monday"/"sunday" keys are kept for backward compatibility with existing
    callers (agent/matchup_proj.py) -- they now hold the period's true start
    /end dates, which are not always Monday/Sunday-aligned in principle
    (though in this league's calendar they happen to be).

    two_starters (2+ starts) is kept for backward-compat callers.
    multi_starters (3+ starts) is new (BUG 5 item 5): in a 14-day period SPs
    can make 3-4 starts, and the old hardcoded 7-day fetch window physically
    could not see more than half of an extended period, so 3-start arms were
    silently invisible. Since the fetch window below now spans the period's
    *real* length, 3+ start pitchers are naturally included in two_starters
    (their count is >= 2) and are additionally broken out in multi_starters
    so callers can flag them distinctly.
    """
    from config.periods import period_offset

    if d is None:
        d = _today_et()
    weeks = []
    for offset in range(n):
        p = period_offset(d, offset)
        if p is None:
            logger.warning(
                "schedule_weeks: no period at offset=%d from %s -- stopping "
                "early (%d of %d requested periods returned)",
                offset, d.isoformat(), len(weeks), n,
            )
            break
        start, end = p["start"], p["end"]
        period_days = (end - start).days + 1
        counts = _fetch_start_counts(start.isoformat(), end.isoformat())
        weeks.append({
            "week_offset":    offset,
            "period":         p["n"],
            "monday":         start,
            "sunday":         end,
            "period_days":    period_days,
            "two_starters":   {name: c for name, c in counts.items() if c >= 2},
            "multi_starters": {name: c for name, c in counts.items() if c >= 3},
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
                    logger.debug("Team abbrev mapped: %s \u2192 %s", abbr, cbs_abbr)
                teams.add(cbs_abbr)
    logger.info("teams_playing_today %s: %d teams \u2014 %s",
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


def _fetch_today_games(date_str: str) -> list:
    """Fetch all games for a single date from the MLB Stats API.

    Not cached intentionally: probable pitchers are announced throughout
    the morning and the lineup optimizer must always reflect the latest
    data within a session.
    """
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
        logger.error("MLB schedule API error (%s \u2013 %s): %s", start_date, end_date, e)
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
    logger.info("Schedule %s\u2013%s: %d games, %d probable starters",
                start_date, end_date, total_games, len(counts))
    return dict(counts)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    """Canonical player-name normalizer \u2014 delegates to mlb.teams.norm_name."""
    return norm_name(name)
