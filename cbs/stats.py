"""
CBS live scoring via league/scoring/live (validated endpoint).

Returns a MatchupData dict consumed by sports/baseball/categories.analyze_matchup.

H2H shape:
{
    "system": "h2h",
    "period": "11",
    "opponent": "Captain Jack",
    "opponent_id": "10",
    "my_score": "8-4-0",
    "categories": {
        "ERA":  {"mine": 3.375, "opp": 4.12},
        "HR":   {"mine": 12,    "opp": 9},
        ...
    }
}

Roto shape:
{
    "system": "roto",
    "period": "12",
    "num_teams": 8,
    "categories": {
        "BA":  {"mine": 0.274, "rank": 4, "rotopts": 5, "dif": -1},
        "HR":  {"mine": 104,   "rank": 2, "rotopts": 7, "dif":  0},
        ...
    }
}
"""

import re
import logging
from cbs.auth import CBSAuth, CBSAPIError

logger = logging.getLogger(__name__)

# Stats labels to skip when parsing raw stats strings —
# these are counting/context fields that aren't fantasy scoring categories.
_SKIP_LABELS = {
    "AB", "PA", "H", "HA", "PC", "BBI", "BB",
    "2B", "3B", "CS", "ER", "L",
}


def fetch_matchup_stats(auth: CBSAuth, league_id: str,
                        sport: str = "baseball") -> dict:
    """Fetch current period scoring from CBS league/scoring/live.
    Returns a MatchupData dict (see module docstring)."""
    data = auth.api_get("league/scoring/live", league_id, sport)
    live = (data.get("body") or {}).get("live_scoring") or {}

    system = live.get("system", "").lower()
    period = str(live.get("period", ""))

    if system == "roto":
        return _parse_roto(live, period)
    elif system == "h2h":
        return _parse_h2h(live, period)
    else:
        logger.warning("Unknown scoring system '%s' for league %s — returning empty",
                       system, league_id)
        return {"system": system, "period": period, "categories": {}}


# ---------------------------------------------------------------------------
# Roto parser
# ---------------------------------------------------------------------------

def _parse_roto(live: dict, period: str) -> dict:
    """Parse rotisserie live_scoring using each team's categories array."""
    my_team_id = str(live.get("my_team_id", ""))
    teams = live.get("teams", [])
    num_teams = len(teams)

    my_team = _find_team(teams, my_team_id)
    if my_team is None:
        raise CBSAPIError(
            f"My team (id={my_team_id}) not found in roto live_scoring "
            f"(saw ids: {[str(t.get('id', t.get('team_id', '?'))) for t in teams]})")

    cats = {}
    for c in my_team.get("categories", []):
        name = c.get("name", "")
        if not name:
            continue
        try:
            value = float(c.get("value", 0))
        except (TypeError, ValueError):
            value = 0.0
        cats[name] = {
            "mine":    value,
            "rank":    int(c.get("rank", 0)),
            "rotopts": int(c.get("rotopts", 0)),
            "dif":     int(c.get("dif", 0)),
        }

    if not cats:
        # Fallback: parse the active_live_stats string
        stats_str = my_team.get("active_live_stats", "")
        parsed = _parse_stats_string(stats_str)
        cats = {k: {"mine": v, "rank": 0, "rotopts": 0, "dif": 0}
                for k, v in parsed.items()}
        logger.warning(
            "Roto categories array empty for league %s period %s — "
            "fell back to stats string parsing (%d cats found)",
            live.get("league_id", "?"), period, len(cats))

    logger.info("Roto: %d categories parsed, period=%s, num_teams=%d",
                len(cats), period, num_teams)
    return {
        "system":    "roto",
        "period":    period,
        "num_teams": num_teams,
        "categories": cats,
    }


# ---------------------------------------------------------------------------
# H2H parser
# ---------------------------------------------------------------------------

def _parse_h2h(live: dict, period: str) -> dict:
    """Parse H2H live_scoring by comparing my team vs opponent."""
    my_team_id = str(live.get("my_team_id", ""))
    teams = live.get("teams", [])

    my_team = _find_team(teams, my_team_id)
    if my_team is None:
        raise CBSAPIError(
            f"My team (id={my_team_id}) not found in H2H live_scoring")

    opp_team_id = str(my_team.get("opp_team_id", ""))
    opp_team = _find_team(teams, opp_team_id) if opp_team_id else None

    matchup_info = (my_team.get("matchups") or [{}])[0]
    my_score   = matchup_info.get("pts", "")
    opponent   = matchup_info.get("opponent_team", "Unknown")

    cats = {}

    # Path 1: structured categories array (present on some CBS league configs)
    my_cats  = my_team.get("categories", [])
    opp_cats = opp_team.get("categories", []) if opp_team else []

    if my_cats and opp_cats:
        opp_by_name = {c.get("name"): c for c in opp_cats}
        for c in my_cats:
            name = c.get("name", "")
            if not name:
                continue
            try:
                mine   = float(c.get("value", 0))
                theirs = float((opp_by_name.get(name) or {}).get("value", 0))
            except (TypeError, ValueError):
                mine = theirs = 0.0
            cats[name] = {"mine": mine, "opp": theirs}
        logger.info("H2H: %d categories from structured array", len(cats))

    else:
        # Path 2: parse active_live_stats strings from both teams
        my_stats  = _parse_stats_string(my_team.get("active_live_stats", ""))
        opp_stats = (
            _parse_stats_string(opp_team.get("active_live_stats", ""))
            if opp_team else {}
        )
        for name, mine in my_stats.items():
            cats[name] = {"mine": mine, "opp": opp_stats.get(name, 0.0)}
        logger.warning(
            "H2H: no categories array — used stats string parsing (%d cats). "
            "If this fires every run, extend the probe to find the categories field.",
            len(cats))

    logger.info("H2H: %d categories, score=%s, opponent=%s",
                len(cats), my_score, opponent)
    return {
        "system":      "h2h",
        "period":      period,
        "opponent":    opponent,
        "opponent_id": opp_team_id,
        "my_score":    my_score,
        "categories":  cats,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_team(teams: list, team_id: str) -> dict | None:
    """Find a team in the live_scoring teams list by id."""
    for t in teams:
        tid = str(t.get("id", t.get("team_id", "")))
        if tid == team_id:
            return t
    return None


def _parse_stats_string(s: str) -> dict[str, float]:
    """Parse a CBS stats string like '12 HR, 3.375 ERA, 0.285 AVG ...'
    into {label: float_value}.  Skips labels in _SKIP_LABELS."""
    result: dict[str, float] = {}
    if not s:
        return result
    # CBS separates hitting/pitching sections with " - "
    for part in s.split(" - "):
        for m in re.finditer(r'(\d+(?:\.\d+)?)\s+([A-Z][A-Z0-9_/]+)', part):
            val_str, label = m.group(1), m.group(2)
            if label in _SKIP_LABELS:
                continue
            try:
                result[label] = float(val_str)
            except ValueError:
                pass
    return result
