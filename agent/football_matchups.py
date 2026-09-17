"""
Matchup results for football leagues -- the enhancement order's (filed
2026-09-16) acceptance criterion 1: "'How did my team do last week?'
returns an actual score/result for all 3 football leagues, not a
roster-legality report."

Data source: CBS's own league/scoring/live endpoint, confirmed live for
football 2026-09-17 (football_scoring_live_probe.py) -- the SAME
authenticated JSON API already used for baseball in cbs/stats.py, with
sport="football" passed through (cbs/auth.py::api_get() is fully
sport-generic). No new external dependency for this piece.

KNOWN LIMITATION (confirmed 2026-09-17, not yet solved): this endpoint
only ever returns the CURRENT scoring period -- there's no confirmed
parameter for a past week's final result. get_matchup_result() below
reports the current period honestly and says so explicitly when a caller
asks for a different week; it does not guess or silently substitute the
wrong week's data. Whoever picks this up next should probe CBS for a
historical-period param before this can answer "how did I do two weeks
ago."
"""

from __future__ import annotations

import logging

from cbs.stats import fetch_football_matchup, CBSFootballMatchupError

logger = logging.getLogger(__name__)


def get_matchup_result(auth, cbs_league_id: str, week: int | None = None) -> dict:
    """One league's current matchup result. `week`, if given, is compared
    against CBS's own reported period -- a mismatch returns an explicit
    "unavailable for that week" note rather than silently returning the
    wrong week's score (see this module's docstring on the historical-week
    gap).

    Returns:
        {"available": bool, "period": str, "my_team_name": str,
         "opponent": str, "opponent_id": str, "my_points": float,
         "opp_points": float, "home_away": str,
         "record": {"w": int, "l": int, "t": int}, "note": str | None}
    "available": False means either CBS's connector failed (see `note`),
    there's no current matchup (bye week), or the requested week doesn't
    match the current period.
    """
    try:
        matchup = fetch_football_matchup(auth, cbs_league_id)
    except CBSFootballMatchupError as e:
        logger.warning("get_matchup_result: fetch failed for %s: %s", cbs_league_id, e)
        return {"available": False, "note": f"CBS matchup data unavailable: {e}"}

    if matchup is None:
        return {"available": False,
               "note": "No current matchup found (bye week, or CBS returned no opponent)."}

    if week is not None and str(week) != str(matchup["period"]):
        return {
            "available": False,
            "period": matchup["period"],
            "note": (f"Requested week {week}, but CBS's live-scoring endpoint only "
                     f"exposes the CURRENT period (period {matchup['period']} right "
                     "now) -- a past week's result isn't available through this tool "
                     "yet (see agent/football_matchups.py's module docstring)."),
        }

    matchup["available"] = True
    matchup["note"] = None
    return matchup
