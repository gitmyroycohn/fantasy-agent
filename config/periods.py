"""
Real CBS scoring-period calendar (BUG 5 fix).

CBS scoring periods are NOT uniform 7-day Monday-Sunday weeks. This module
resolves a date to its true period number and (start, end) bounds using the
authoritative `season_start` / `periods:` table in config/leagues.yaml, rather
than computing it arithmetically from a hardcoded opening day (the old
agent.decisions._current_week(), which was wrong on 81/166 days of the 2026
season -- every Saturday and Sunday, since season_start is a Wednesday, plus
the extended 12-day Period 1 and 14-day Period 16).

CBS's own league/scoring/live `period` field remains the single source of
truth for the *current* period at runtime (see resolve_period()) -- this
table's job is to let the agent resolve periods CBS's live endpoint doesn't
cover directly: future-period lookahead (week_offset), 2-start SP windows,
and schedule streaming.

Public API
----------
load_periods(path=None) -> {"season_start": date|None, "periods": [{"n","start","end"}, ...]}
period_for_date(d, path=None) -> {"n","start","end"} | None
period_bounds(n, path=None) -> (start, end) | None
period_offset(d, offset, path=None) -> {"n","start","end"} | None
    offset=0 -> the period containing d; offset=1 -> the NEXT period (not
    d + 7 days -- BUG 5 item 4: week_offset must mean "N periods ahead").
resolve_period(d, cbs_period=None, path=None) -> {"n","start","end","source"}
    Prefers cbs_period when it disagrees with the local table; logs a
    WARNING on mismatch (BUG 5 item 6).
"""

import logging
import os
from datetime import date, datetime

import yaml

logger = logging.getLogger(__name__)

_LEAGUES_YAML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leagues.yaml")


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def load_periods(path: str | None = None) -> dict:
    """Load the season_start/periods table from config/leagues.yaml.

    Returns {"season_start": date | None, "periods": [{"n": int, "start": date, "end": date}, ...]}
    sorted by period number.
    """
    p = path or _LEAGUES_YAML
    with open(p) as f:
        raw = yaml.safe_load(f) or {}

    season_start_raw = raw.get("season_start")
    periods_raw = raw.get("periods") or []

    periods = []
    for entry in periods_raw:
        try:
            periods.append({
                "n":     int(entry["n"]),
                "start": _parse_date(str(entry["start"])),
                "end":   _parse_date(str(entry["end"])),
            })
        except (KeyError, ValueError) as e:
            logger.error("Malformed periods entry in leagues.yaml: %s (%s)", entry, e)

    periods.sort(key=lambda x: x["n"])

    if not periods:
        logger.error(
            "config/leagues.yaml has no usable `periods:` table -- period "
            "resolution will fail. See config/periods.py."
        )

    return {
        "season_start": _parse_date(str(season_start_raw)) if season_start_raw else None,
        "periods": periods,
    }


_cache: dict | None = None


def _get(path: str | None = None) -> dict:
    global _cache
    if path is not None:
        return load_periods(path)
    if _cache is None:
        _cache = load_periods()
    return _cache


def clear_cache() -> None:
    """Force the next _get() call to reload leagues.yaml (useful in tests)."""
    global _cache
    _cache = None


def period_for_date(d: date, path: str | None = None) -> dict | None:
    """Return {"n","start","end"} for the period containing date d.

    If d falls before the first configured period, returns the first period.
    If d falls after the last configured period, returns the last period
    (rather than None) so lookahead code degrades gracefully at the edges of
    the table instead of crashing.
    """
    data = _get(path)
    periods = data["periods"]
    if not periods:
        return None
    for p in periods:
        if p["start"] <= d <= p["end"]:
            return p
    if d < periods[0]["start"]:
        return periods[0]
    if d > periods[-1]["end"]:
        return periods[-1]
    return None


def period_bounds(n: int, path: str | None = None) -> tuple[date, date] | None:
    """Return (start, end) for period number n, or None if not in the table."""
    data = _get(path)
    for p in data["periods"]:
        if p["n"] == n:
            return p["start"], p["end"]
    return None


def period_offset(d: date, offset: int, path: str | None = None) -> dict | None:
    """Return the period `offset` periods ahead of the period containing d.

    offset=0 -> current period, offset=1 -> the NEXT period regardless of how
    many days long the current period is (BUG 5 item 4). This is the fix for
    week_offset=1 resolving to "current date + 7 days" -- which, during a
    14-day period, is still inside the *current* period and so pointed
    streaming/waiver "next period" advice at the wrong window.
    """
    data = _get(path)
    periods = data["periods"]
    if not periods:
        return None
    cur = period_for_date(d, path)
    if cur is None:
        return None
    idx_by_n = {p["n"]: i for i, p in enumerate(periods)}
    idx = idx_by_n.get(cur["n"])
    if idx is None:
        return None
    target_idx = idx + offset
    if 0 <= target_idx < len(periods):
        return periods[target_idx]
    return None


def resolve_period(d: date, cbs_period=None, path: str | None = None) -> dict:
    """Resolve the true period for date d.

    cbs_period: the `period` field from cbs.stats.fetch_matchup_stats's raw
    live_scoring response (str/int/None). When present and it disagrees with
    the local table, CBS wins (it's the live authoritative source) and a
    WARNING is logged so table drift can't silently recur (BUG 5 item 6).

    Returns {"n": int, "start": date|None, "end": date|None, "source": "cbs"|"table"}.
    Raises ValueError if neither the table nor cbs_period can resolve a period.
    """
    local = period_for_date(d, path)

    cbs_n = None
    if cbs_period not in (None, "", "?"):
        try:
            cbs_n = int(str(cbs_period).strip())
        except ValueError:
            logger.warning("Non-numeric CBS period value %r -- ignoring", cbs_period)
            cbs_n = None

    if cbs_n is not None:
        if local is None or local["n"] != cbs_n:
            logger.warning(
                "Period mismatch for %s: leagues.yaml table says period %s, "
                "CBS live_scoring reports period %s -- using CBS (authoritative). "
                "If this fires repeatedly, the periods: table in config/leagues.yaml "
                "is out of date and should be corrected.",
                d.isoformat(), local["n"] if local else "?", cbs_n,
            )
        bounds = period_bounds(cbs_n, path)
        if bounds:
            return {"n": cbs_n, "start": bounds[0], "end": bounds[1], "source": "cbs"}
        return {"n": cbs_n, "start": None, "end": None, "source": "cbs"}

    if local:
        return {**local, "source": "table"}

    raise ValueError(
        f"Could not resolve a scoring period for {d} -- no CBS period supplied "
        "and config/leagues.yaml's periods table doesn't cover this date."
    )
