"""
MLB hitting splits: vs LHP, vs RHP, and recent hot streak.

Data source: MLB Stats API (free, no auth).

Public API
----------
fetch_batter_splits(season)
    → {norm_name: {"vs_l": {avg, ops, hr, pa}, "vs_r": {avg, ops, hr, pa}}}

fetch_recent_form(days=14, season)
    → {norm_name: {avg, ops, hr, sb, r, rbi, games}}

enrich_with_splits(players, season)
    Adds "split_vs_l_ops", "split_vs_r_ops", "split_vs_l_avg", "split_vs_r_avg",
    "recent_avg", "recent_ops", "recent_hr", "recent_sb", "recent_games"
    to player.stats in-place.
"""

import logging
from functools import lru_cache

import requests

from mlb.teams import norm_name

logger = logging.getLogger(__name__)

MLB_API = "https://statsapi.mlb.com/api/v1"
TIMEOUT = 20


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_batter_splits(season: int = 2026) -> dict[str, dict]:
    """
    Return L/R hitting splits for all batters.

    Returns {norm_name: {"vs_l": {...}, "vs_r": {...}}}
    Each split dict has: avg, ops, hr, pa, slg, obp
    """
    vs_l = _fetch_sit_stats("vl", season)
    vs_r = _fetch_sit_stats("vr", season)

    result: dict[str, dict] = {}
    all_names = set(vs_l.keys()) | set(vs_r.keys())
    for name in all_names:
        result[name] = {
            "vs_l": vs_l.get(name, {}),
            "vs_r": vs_r.get(name, {}),
        }
    return result


def fetch_recent_form(days: int = 14, season: int = 2026) -> dict[str, dict]:
    """
    Return hitting stats over the last N days for all batters.

    Returns {norm_name: {avg, ops, hr, sb, r, rbi, games}}
    """
    return _fetch_last_x_days(days, season)


def enrich_with_splits(players: list, season: int = 2026) -> int:
    """
    Add L/R split stats and recent form to player.stats in-place.

    Adds keys:
      split_vs_l_avg, split_vs_l_ops, split_vs_l_hr, split_vs_l_pa
      split_vs_r_avg, split_vs_r_ops, split_vs_r_hr, split_vs_r_pa
      recent_avg, recent_ops, recent_hr, recent_sb, recent_games

    Returns number of players enriched.
    """
    splits = fetch_batter_splits(season)
    recent = fetch_recent_form(14, season)
    found = 0

    for wp in players:
        # Handle both WaiverPlayer (wp.player) and RosterSlot (wp.player) objects
        player = getattr(wp, "player", wp)
        key = norm_name(player.name)

        if player.stats is None:
            player.stats = {}

        enriched = False

        if key in splits:
            s = splits[key]
            for hand, prefix in (("vs_l", "split_vs_l"), ("vs_r", "split_vs_r")):
                sd = s.get(hand, {})
                for stat in ("avg", "ops", "hr", "pa", "slg", "obp"):
                    v = sd.get(stat)
                    if v is not None:
                        player.stats[f"{prefix}_{stat}"] = v
            enriched = True

        if key in recent:
            rd = recent[key]
            for stat in ("avg", "ops", "hr", "sb", "r", "rbi", "games"):
                v = rd.get(stat)
                if v is not None:
                    player.stats[f"recent_{stat}"] = v
            enriched = True

        if enriched:
            found += 1

    logger.info("enrich_with_splits: %d/%d players enriched", found, len(players))
    return found


# ---------------------------------------------------------------------------
# Internal: MLB Stats API calls
# ---------------------------------------------------------------------------

@lru_cache(maxsize=4)
def _fetch_sit_stats(sit_code: str, season: int) -> dict[str, dict]:
    """
    Fetch split stats for all batters with a given situation code.

    sit_code: "vl" = vs LHP, "vr" = vs RHP
    Returns {norm_name: {avg, ops, hr, pa, slg, obp}}
    """
    url = f"{MLB_API}/stats"
    params = {
        "stats":      "statSplits",
        "group":      "hitting",
        "season":     season,
        "sitCodes":   sit_code,
        "playerPool": "ALL",
        "limit":      2000,
    }
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error("MLB splits API error (sitCode=%s, %d): %s", sit_code, season, e)
        return {}

    result: dict[str, dict] = {}
    for stat_group in data.get("stats", []):
        for split in stat_group.get("splits", []):
            player_info = split.get("player", {})
            name = player_info.get("fullName", "")
            if not name:
                continue
            raw = split.get("stat", {})
            key = norm_name(name)
            result[key] = {
                "avg": _f(raw.get("avg")),
                "ops": _f(raw.get("ops")),
                "slg": _f(raw.get("slg")),
                "obp": _f(raw.get("obp")),
                "hr":  int(raw.get("homeRuns", 0)),
                "pa":  int(raw.get("plateAppearances", 0)
                           or raw.get("atBats", 0)),
            }

    label = "vs LHP" if sit_code == "vl" else "vs RHP"
    logger.info("MLB splits %s %d: %d batters", label, season, len(result))
    return result


@lru_cache(maxsize=4)
def _fetch_last_x_days(days: int, season: int) -> dict[str, dict]:
    """
    Fetch hitting stats for the last N days.
    Returns {norm_name: {avg, ops, hr, sb, r, rbi, games}}
    """
    url = f"{MLB_API}/stats"
    params = {
        "stats":      "lastXDays",
        "numDays":    days,
        "group":      "hitting",
        "season":     season,
        "playerPool": "ALL",
        "limit":      2000,
    }
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error("MLB lastXDays API error (%d days, %d): %s", days, season, e)
        return {}

    result: dict[str, dict] = {}
    for stat_group in data.get("stats", []):
        for split in stat_group.get("splits", []):
            player_info = split.get("player", {})
            name = player_info.get("fullName", "")
            if not name:
                continue
            raw = split.get("stat", {})
            key = norm_name(name)
            result[key] = {
                "avg":   _f(raw.get("avg")),
                "ops":   _f(raw.get("ops")),
                "hr":    int(raw.get("homeRuns", 0)),
                "sb":    int(raw.get("stolenBases", 0)),
                "r":     int(raw.get("runs", 0)),
                "rbi":   int(raw.get("rbi", 0)),
                "games": int(raw.get("gamesPlayed", 0)),
            }

    logger.info("MLB last %d days %d: %d batters", days, season, len(result))
    return result


def _f(val) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0
