"""
League roster-limit settings, fetched from CBS's JSON API -- confirmed live
2026-08-24 (roster_limits_diag2.py, run by Christopher against hard_chargers)
against a real CBS "ROSTER LIMITS" settings-page screenshot: exact match on
every field (QB 1/1, RB 1-4, WR 1-4, TE 1-4, RB-WR-TE 3/3, K 1/1, DST 1/1;
Active 9/9, Reserve 6/6, Total 15/15).

Endpoint discovery note: league/details (already used elsewhere in this repo
for token validation) does NOT carry this data -- it's season/scoring/keeper
metadata only (num_teams, is_ppr, uses_keepers, etc.). The roster-limit data
lives under league/rules -> body.rules.roster, a sibling endpoint that was
probed but never actually read for content prior to this. This module is
that missing piece: previously the agent had to guess generic replacement
baselines or leave a league's exact positional structure "unconfirmed" in
draft guides (see config/leagues.yaml's football section, which still has
some hand-guessed roster comments predating this) -- this fetches the real
thing directly from CBS instead.
"""

import logging
from cbs.auth import CBSAuth, CBSAPIError

logger = logging.getLogger(__name__)


def _to_int_or_none(v) -> int | None:
    """CBS returns these as strings, and 'max_total' is often the literal
    string "No Limit" rather than a number -- never crash on that, just
    treat it as None (uncapped)."""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def fetch_roster_rules(auth: CBSAuth, league_id: str,
                       sport: str = "football") -> dict:
    """
    Fetch a league's real roster position limits from CBS (league/rules
    JSON API), instead of guessing or leaving them unconfirmed.

    Returns:
        {
          "positions": [
              {"abbr": "QB", "min_active": 1, "max_active": 1, "max_total": None},
              {"abbr": "RB", "min_active": 1, "max_active": 4, "max_total": None},
              ...
          ],
          "starters": {"min": 9, "max": 9},   # CBS "Active Players"
          "bench":    {"min": 6, "max": 6},   # CBS "Reserve Players"
          "injured":  {"min": 0, "max": 0},   # CBS "Injured Players"
          "practice": {"min": 0, "max": 0},   # CBS "Practice Players"
          "total":    {"min": 15, "max": 15}, # CBS "Total Players"
        }

    Raises CBSAPIError if the endpoint itself fails (bad token, dead
    cookie, etc). Returns an empty dict (not a guess) if the response
    doesn't have the expected `rules.roster` shape -- callers should treat
    an empty dict as "still unconfirmed", never fabricate a fallback.
    """
    data = auth.api_get("league/rules", league_id, sport)
    roster = ((data.get("body", {}) or {}).get("rules", {}) or {}).get("roster")
    if not isinstance(roster, dict):
        logger.warning(
            "fetch_roster_rules: league/rules response for %s had no "
            "rules.roster section -- returning {} rather than guessing",
            league_id)
        return {}

    positions = []
    for p in roster.get("positions", []) or []:
        positions.append({
            "abbr": p.get("abbr", "?"),
            "min_active": _to_int_or_none(p.get("min_active")),
            "max_active": _to_int_or_none(p.get("max_active")),
            "max_total": _to_int_or_none(p.get("max_total")),
        })

    # CBS's "statuses" list uses free-text descriptions ("Active Players",
    # "Reserve Players", ...) rather than stable keys -- map defensively by
    # substring rather than assuming a fixed order/count.
    STATUS_MAP = {
        "active": "starters",
        "reserve": "bench",
        "injured": "injured",
        "practice": "practice",
        "total": "total",
    }
    result = {
        "positions": positions,
        "starters": {}, "bench": {}, "injured": {}, "practice": {}, "total": {},
    }
    for s in roster.get("statuses", []) or []:
        desc = str(s.get("description", "")).lower()
        key = next((v for k, v in STATUS_MAP.items() if k in desc), None)
        if key is None:
            logger.warning("fetch_roster_rules: unrecognized roster status "
                           "description %r -- skipping", s.get("description"))
            continue
        result[key] = {
            "min": _to_int_or_none(s.get("min")),
            "max": _to_int_or_none(s.get("max")),
        }

    logger.info("fetch_roster_rules: %d position rules fetched for %s",
                len(positions), league_id)
    return result


def format_roster_rules(rules: dict, league_name: str = "") -> str:
    """Human-readable summary, for MCP tool output."""
    if not rules or not rules.get("positions"):
        return (f"Roster limits for {league_name or 'this league'}: "
                "not available (CBS league/rules didn't return the expected "
                "data -- don't guess, treat as unconfirmed).")

    lines = [f"Roster limits for {league_name}:" if league_name else "Roster limits:"]
    for label, key in [("Starters", "starters"), ("Bench", "bench"),
                       ("Total", "total")]:
        s = rules.get(key) or {}
        if s.get("min") is not None:
            lines.append(f"  {label}: {s['min']}/{s['max']}")
    lines.append("  Positions (min active / max active):")
    for p in rules["positions"]:
        mn = p["min_active"] if p["min_active"] is not None else "?"
        mx = p["max_active"] if p["max_active"] is not None else "?"
        lines.append(f"    {p['abbr']}: {mn}/{mx}")
    return "\n".join(lines)
