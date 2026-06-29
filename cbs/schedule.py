"""
CBS Fantasy schedule probe -- discovers which endpoint exposes next week's matchup.

Probes in this order:
  1. league/schedule          -- full season schedule (most likely)
  2. league/matchups          -- alternative naming
  3. league/schedule/season   -- another variant
  4. league/scoring/live      -- current week, then retried with period=N+1 param
  5. league/scoring/live      -- fallback: return current week's opponent unchanged

All probe results are logged at INFO level so they appear in Render logs.
Look for lines starting with "[schedule probe]" to see what CBS returns.

Public API:
    fetch_next_opponent(auth, league_id, sport, my_team_id, week_offset=1)
    -> {"opponent_id", "opponent_name", "period", "_source", "_fallback"} | None
"""

import logging
from cbs.auth import CBSAuth, CBSAPIError

logger = logging.getLogger(__name__)

# Ordered list of CBS endpoints to probe for schedule/matchup data.
# Each is tried in turn; first one that returns parseable matchup data wins.
_PROBE_ENDPOINTS = [
    "league/schedule",
    "league/matchups",
    "league/schedule/season",
    "league/scoring/schedule",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_next_opponent(
    auth: CBSAuth,
    league_id: str,
    sport: str = "baseball",
    my_team_id: str = "",
    week_offset: int = 1,
) -> dict | None:
    """Return the opponent for the scoring period (current + week_offset).

    week_offset=1 means next week (default).
    week_offset=0 means current week (same as live scoring already provides).

    Returns:
        {
            "opponent_id":   str,
            "opponent_name": str,
            "period":        int,
            "_source":       str,   # which endpoint/strategy worked
            "_fallback":     bool,  # True if we fell back to current week
        }
        or None if all probes fail.
    """
    tid = str(my_team_id)

    # Step 1: try dedicated schedule endpoints
    for endpoint in _PROBE_ENDPOINTS:
        result = _probe_endpoint(auth, league_id, sport, tid, endpoint, week_offset)
        if result:
            return result

    # Step 2: try live scoring with explicit period param
    result = _probe_period_param(auth, league_id, sport, tid, week_offset)
    if result:
        return result

    # Step 3: fall back to current week's opponent from live scoring
    result = _current_week_fallback(auth, league_id, sport, tid)
    if result:
        result["_fallback"] = True
        logger.warning(
            "[schedule probe] All endpoints failed for %s -- returning current "
            "week opponent as fallback (week_offset ignored)", league_id
        )
        return result

    logger.warning("[schedule probe] Complete failure for %s -- no opponent found", league_id)
    return None


# ---------------------------------------------------------------------------
# Probe strategies
# ---------------------------------------------------------------------------

def _probe_endpoint(auth, league_id, sport, my_team_id, endpoint, week_offset):
    """Try a single CBS endpoint and log everything it returns."""
    try:
        data = auth.api_get(endpoint, league_id, sport)
        body = data.get("body") or {}

        logger.warning("[schedule probe] %s -> HTTP 200, body keys: %s",
                    endpoint, list(body.keys()))

        # Walk every top-level key and log its type/length
        for key, val in body.items():
            if isinstance(val, list):
                logger.warning("[schedule probe]   %s[%s] = list(%d items)", endpoint, key, len(val))
                if val and isinstance(val[0], dict):
                    logger.warning("[schedule probe]     first item keys: %s", list(val[0].keys()))
            elif isinstance(val, dict):
                logger.warning("[schedule probe]   %s[%s] = dict, keys: %s",
                            endpoint, key, list(val.keys()))
            else:
                logger.warning("[schedule probe]   %s[%s] = %s: %r",
                            endpoint, key, type(val).__name__, str(val)[:80])

        # Try to parse matchup/opponent data out of whatever shape this is
        result = _parse_any_schedule(body, my_team_id, week_offset, source=endpoint)
        if result:
            logger.warning("[schedule probe] SUCCESS via %s: %s", endpoint, result)
        else:
            logger.warning("[schedule probe] %s returned data but no parseable matchup found", endpoint)
        return result

    except CBSAPIError as e:
        logger.warning("[schedule probe] %s -> CBS error: %s", endpoint, e)
        return None
    except Exception as e:
        logger.warning("[schedule probe] %s -> unexpected error: %s", endpoint, e)
        return None


def _probe_period_param(auth, league_id, sport, my_team_id, week_offset):
    """Try league/scoring/live with an explicit period=N+1 query param."""
    try:
        # First get current period number
        data = auth.api_get("league/scoring/live", league_id, sport)
        live = (data.get("body") or {}).get("live_scoring") or {}
        current_period = int(live.get("period", 0))
        if not current_period:
            logger.warning("[schedule probe] period param: could not determine current period")
            return None

        target_period = current_period + week_offset
        logger.warning("[schedule probe] Trying league/scoring/live?period=%d (current=%d, offset=%d)",
                    target_period, current_period, week_offset)

        data2 = auth.api_get("league/scoring/live", league_id, sport, period=target_period)
        live2 = (data2.get("body") or {}).get("live_scoring") or {}

        logger.warning("[schedule probe] period=%d response keys: %s", target_period, list(live2.keys()))
        returned_period = live2.get("period")
        logger.warning("[schedule probe] period=%d response.period = %r", target_period, returned_period)

        if str(returned_period) != str(target_period):
            logger.warning("[schedule probe] period param ignored by CBS (returned period %r, wanted %d)",
                        returned_period, target_period)
            return None

        # Parse the response -- same structure as current live scoring
        live_my_id = str(live2.get("my_team_id", ""))
        teams = live2.get("teams", [])
        for t in teams:
            tid = str(t.get("id") or t.get("team_id") or "")
            if tid and (tid == live_my_id or tid == my_team_id):
                matchups = t.get("matchups") or []
                if matchups:
                    m = matchups[0]
                    opp_id   = str(m.get("opp_team_id") or m.get("opponent_id") or "")
                    opp_name = m.get("opponent_team") or m.get("opponent_name") or ""
                    if opp_id:
                        return {
                            "opponent_id":   opp_id,
                            "opponent_name": opp_name,
                            "period":        target_period,
                            "_source":       f"league/scoring/live?period={target_period}",
                            "_fallback":     False,
                        }

        logger.warning("[schedule probe] period=%d: found response but could not extract opponent", target_period)
        return None

    except CBSAPIError as e:
        logger.warning("[schedule probe] period param probe failed: %s", e)
        return None
    except Exception as e:
        logger.warning("[schedule probe] period param probe unexpected error: %s", e)
        return None


def _current_week_fallback(auth, league_id, sport, my_team_id):
    """Last resort: extract current week's opponent from live scoring."""
    try:
        data = auth.api_get("league/scoring/live", league_id, sport)
        live = (data.get("body") or {}).get("live_scoring") or {}
        live_my_id = str(live.get("my_team_id", ""))
        current_period = int(live.get("period", 0))
        teams = live.get("teams", [])

        for t in teams:
            tid = str(t.get("id") or t.get("team_id") or "")
            if tid == live_my_id or (my_team_id and tid == my_team_id):
                matchups = t.get("matchups") or []
                if matchups:
                    m = matchups[0]
                    opp_id   = str(m.get("opp_team_id") or "")
                    opp_name = m.get("opponent_team") or ""
                    if opp_id:
                        return {
                            "opponent_id":   opp_id,
                            "opponent_name": opp_name,
                            "period":        current_period,
                            "_source":       "league/scoring/live (current week)",
                            "_fallback":     False,
                        }
    except Exception as e:
        logger.warning("[schedule probe] current week fallback failed: %s", e)
    return None


# ---------------------------------------------------------------------------
# Response parsers (schedule endpoint shapes)
# ---------------------------------------------------------------------------

def _parse_any_schedule(body: dict, my_team_id: str, week_offset: int, source: str) -> dict | None:
    """Try to extract opponent info from any schedule-shaped response body.

    CBS may use various key names and nesting depths -- we try them all.
    """
    # Collect all list values as candidate schedule arrays
    candidates = []
    for key, val in body.items():
        if isinstance(val, list) and val:
            candidates.append((key, val))
        elif isinstance(val, dict):
            for subkey, subval in val.items():
                if isinstance(subval, list) and subval:
                    candidates.append((f"{key}.{subkey}", subval))

    for key, items in candidates:
        result = _parse_schedule_list(items, my_team_id, week_offset, source, key)
        if result:
            return result

    return None


def _parse_schedule_list(items: list, my_team_id: str, week_offset: int,
                          source: str, list_key: str) -> dict | None:
    """Parse a list of schedule/matchup items.

    Handles shapes like:
      [{period, home_team_id, away_team_id, home_team_name, away_team_name}, ...]
      [{week, matchups: [{home_id, away_id}, ...]}, ...]
      [{period_id, teams: [{id, name}, {id, name}]}, ...]
    """
    # Find current + target period from items if possible
    # First pass: collect all periods that involve my team
    my_matchups = []
    for item in items:
        if not isinstance(item, dict):
            continue

        period = (item.get("period") or item.get("week") or
                  item.get("period_id") or item.get("scoring_period"))

        # Flat matchup shape: {period, home_team_id, away_team_id, ...}
        home_id  = str(item.get("home_team_id") or item.get("home_id") or "")
        away_id  = str(item.get("away_team_id") or item.get("away_id") or "")
        home_name = item.get("home_team_name") or item.get("home_name") or ""
        away_name = item.get("away_team_name") or item.get("away_name") or ""

        if my_team_id and my_team_id in (home_id, away_id):
            opp_id   = away_id   if home_id   == my_team_id else home_id
            opp_name = away_name if home_id   == my_team_id else home_name
            my_matchups.append({
                "period":   period,
                "opp_id":   opp_id,
                "opp_name": opp_name,
            })
            continue

        # Nested matchups shape: {week, matchups: [{...}, ...]}
        nested = item.get("matchups") or item.get("games") or item.get("periods") or []
        if isinstance(nested, list):
            for sub in nested:
                if not isinstance(sub, dict):
                    continue
                sub_period = period or sub.get("period") or sub.get("week")
                h_id  = str(sub.get("home_team_id") or sub.get("home_id") or "")
                a_id  = str(sub.get("away_team_id") or sub.get("away_id") or "")
                h_name = sub.get("home_team_name") or sub.get("home_name") or ""
                a_name = sub.get("away_team_name") or sub.get("away_name") or ""
                if my_team_id and my_team_id in (h_id, a_id):
                    opp_id   = a_id   if h_id   == my_team_id else h_id
                    opp_name = a_name if h_id   == my_team_id else h_name
                    my_matchups.append({
                        "period":   sub_period,
                        "opp_id":   opp_id,
                        "opp_name": opp_name,
                    })

    if not my_matchups:
        return None

    logger.warning("[schedule probe] %s[%s]: found %d matchups involving my team",
                source, list_key, len(my_matchups))
    for m in my_matchups[:5]:
        logger.warning("[schedule probe]   period=%s opp_id=%s opp_name=%s",
                    m["period"], m["opp_id"], m["opp_name"])

    # Find the current period by finding the smallest period number with data
    # (or use the one matching week_offset if periods are numbered)
    periods_with_nums = [(m, int(m["period"])) for m in my_matchups if m["period"] is not None]
    if not periods_with_nums:
        # No period numbers -- can't determine offset, return first
        m = my_matchups[0]
        return {
            "opponent_id":   m["opp_id"],
            "opponent_name": m["opp_name"] or f"Team {m['opp_id']}",
            "period":        0,
            "_source":       f"{source}[{list_key}]",
            "_fallback":     False,
        }

    # Sort by period number, find current week (assume lowest active period)
    periods_with_nums.sort(key=lambda x: x[1])
    # Current period is the one closest to the middle of the season
    # Simple heuristic: pick the period at index (week_offset) if enough data
    if week_offset < len(periods_with_nums):
        m, p = periods_with_nums[week_offset]
    else:
        m, p = periods_with_nums[-1]

    return {
        "opponent_id":   m["opp_id"],
        "opponent_name": m["opp_name"] or f"Team {m['opp_id']}",
        "period":        p,
        "_source":       f"{source}[{list_key}]",
        "_fallback":     False,
    }
