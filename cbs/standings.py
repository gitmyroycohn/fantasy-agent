"""
Fetch all teams' category stats from CBS for the league-wide surplus/deficit map.

Roto:   league/scoring/live already has all teams + per-category values.
H2H:    tries league/standings (season category totals), falls back to
        league/scoring/season, then to current-week live stats as a proxy.

Return shape (list of team dicts):
  [
    {
      "team_id":   "3",
      "team_name": "Sluggers",
      "cats": {
          "HR":  {"value": 120, "rank": 2},
          "SB":  {"value": 45,  "rank": 7},
          ...
      }
    },
    ...
  ]
"""
import logging
from cbs.auth import CBSAuth, CBSAPIError

logger = logging.getLogger(__name__)

# Categories where lower is better (for rank direction sanity checks)
_LOWER_IS_BETTER = {"ERA", "WHIP", "L", "BB"}


def fetch_all_teams_stats(auth: CBSAuth, league_id: str,
                          sport: str = "baseball",
                          system: str = "roto") -> list[dict]:
    """Return per-team category stats for every team in the league.

    system: 'roto' or 'h2h' (controls which endpoint strategy to use).
    """
    if system == "roto":
        return _from_roto_live(auth, league_id, sport)
    else:
        return _from_h2h(auth, league_id, sport)


# ---------------------------------------------------------------------------
# Roto: extract all teams from the live scoring endpoint
# ---------------------------------------------------------------------------

def _from_roto_live(auth: CBSAuth, league_id: str, sport: str) -> list[dict]:
    """For roto leagues the live endpoint has every team + categories array."""
    try:
        data = auth.api_get("league/scoring/live", league_id, sport)
        live = (data.get("body") or {}).get("live_scoring") or {}
        teams = live.get("teams", [])
        if not teams:
            logger.warning("standings: no teams in roto live_scoring for %s", league_id)
            return []
        result = []
        for t in teams:
            team_id   = str(t.get("id", t.get("team_id", "")))
            team_name = t.get("name", t.get("team_name", f"Team {team_id}"))
            cats = {}
            for c in t.get("categories", []):
                name = c.get("name", "")
                if not name:
                    continue
                try:
                    value = float(c.get("value", 0) or 0)
                    rank  = int(c.get("rank",  0) or 0)
                except (TypeError, ValueError):
                    value, rank = 0.0, 0
                cats[name] = {"value": value, "rank": rank}
            if cats:
                result.append({"team_id": team_id, "team_name": team_name,
                                "cats": cats})
        logger.info("standings (roto live): %d teams extracted for %s",
                    len(result), league_id)
        return result
    except CBSAPIError as e:
        logger.warning("standings roto live failed for %s: %s", league_id, e)
        return []


# ---------------------------------------------------------------------------
# H2H: try standings endpoint, fall back to season scoring
# ---------------------------------------------------------------------------

def _from_h2h(auth: CBSAuth, league_id: str, sport: str) -> list[dict]:
    """For H2H leagues, try several endpoints to get season category totals."""
    # Attempt 1: league/standings (may have season stats)
    for endpoint in ("league/standings", "league/scoring/season",
                     "league/scoring"):
        try:
            data = auth.api_get(endpoint, league_id, sport)
            result = _parse_standings_response(data)
            if result:
                logger.info("standings (h2h %s): %d teams for %s",
                            endpoint, len(result), league_id)
                return result
        except CBSAPIError as e:
            logger.debug("standings endpoint %s failed: %s", endpoint, e)

    # Attempt 2: fall back to live scoring — extracts current-week stats,
    # which is a weaker signal but better than nothing
    logger.warning("standings: falling back to live scoring proxy for H2H %s",
                   league_id)
    return _from_h2h_live(auth, league_id, sport)


def _parse_standings_response(data: dict) -> list[dict]:
    """Try to extract team category stats from a CBS standings/scoring response."""
    body = data.get("body") or {}
    # Common CBS shapes
    for key in ("standings", "teams", "scoring"):
        teams_raw = body.get(key)
        if isinstance(teams_raw, dict):
            teams_raw = teams_raw.get("teams", [])
        if isinstance(teams_raw, list) and teams_raw:
            result = _extract_teams(teams_raw)
            if result:
                return result
    # Nested live_scoring shape
    live = body.get("live_scoring") or {}
    teams_raw = live.get("teams", [])
    if teams_raw:
        return _extract_teams(teams_raw)
    return []


def _extract_teams(teams: list) -> list[dict]:
    result = []
    for t in teams:
        team_id   = str(t.get("id", t.get("team_id", "")))
        team_name = t.get("name", t.get("team_name", f"Team {team_id}"))
        cats = {}
        for c in t.get("categories", []):
            name = c.get("name", "")
            if not name:
                continue
            try:
                value = float(c.get("value", 0) or 0)
                rank  = int(c.get("rank",  0) or 0)
            except (TypeError, ValueError):
                value, rank = 0.0, 0
            cats[name] = {"value": value, "rank": rank}
        if cats:
            result.append({"team_id": team_id, "team_name": team_name,
                            "cats": cats})
    return result


def _from_h2h_live(auth: CBSAuth, league_id: str, sport: str) -> list[dict]:
    """Last-resort: use current-week live stats for all H2H teams."""
    try:
        data = auth.api_get("league/scoring/live", league_id, sport)
        live = (data.get("body") or {}).get("live_scoring") or {}
        teams = live.get("teams", [])
        return _extract_teams(teams)
    except CBSAPIError as e:
        logger.warning("standings live fallback failed for %s: %s", league_id, e)
        return []
