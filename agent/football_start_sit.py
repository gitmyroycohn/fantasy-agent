"""
Start/sit recommendations for football rosters -- the enhancement order's
(filed 2026-09-16) acceptance criterion 2: "for every open/contested
roster slot, a ranked recommendation, not just a legality check."

Scope, stated honestly (mirrors agent/football_decisions.py's own "what
this does NOT do yet" convention):

  WHAT THIS DOES: for each starting slot where more eligible players exist
  than the slot needs (i.e. it's actually a decision, not a formality),
  ranks the eligible candidates by their REAL actual fantasy points from
  the last completed week (sports/football/actuals.py, scored exactly
  under that league's own rules), tagged with recent snap share and
  current injury status so the ranking's inputs are visible, not just its
  output.

  WHAT THIS DOES NOT DO (real, open gaps -- not yet solved):
    - Opponent-matchup quality (e.g. "this RB faces a bottom-5 run
      defense this week") is NOT factored in. The enhancement order asked
      for this explicitly; Sleeper's weekly stats endpoint carries no
      opponent-defense-quality data, and no source for it has been probed
      yet. Whoever picks this up next should look at a defense-vs-position
      feed (FantasyPros' consensus rankings carry some matchup context but
      it hasn't been verified as machine-readable/reliable for this).
    - This ranks by LAST week's real result, not a projection for the
      UPCOMING week -- it's "who's been playing well," not "who will score
      the most points this week." Recent-form is a reasonable start/sit
      signal on its own (the enhancement order asked for "recent
      snap/target/touch trend"), but it is not a projection and shouldn't
      be presented as one.
    - This is not a full lineup optimizer -- it ranks candidates within
      each contested slot independently; it doesn't solve the whole roster
      jointly (e.g. a flex-eligible player appearing as a candidate in two
      slots at once isn't reconciled here).
"""

from __future__ import annotations

import logging

from sleeper.client import SleeperUnavailable
from sports.football.actuals import actual_points_for_roster
from sports.football.roster_rules import LEAGUE_STARTING_SLOTS

logger = logging.getLogger(__name__)


def _norm(name: str) -> str:
    return name.strip().lower()


def football_start_sit(roster: list, league_id: str, scoring_profile: str,
                       season: int, week: int,
                       injury_by_name: dict[str, str] | None = None,
                       position: str | None = None) -> dict:
    """Ranked start/sit candidates for every CONTESTED starting slot in one
    league's roster (a slot is "contested" when more eligible players
    exist than the slot needs -- an uncontested slot has nothing to rank
    and is left out of the result entirely).

    `week` should be the last COMPLETED week's real actuals (callers
    resolve the current week and typically pass week-1 -- see
    mcp_server.py::football_start_sit()'s use of this function; a week
    still in progress doesn't have a final actual yet).

    `injury_by_name`, if given, should be {normalized_name: status_str}
    (see agent/football_injuries.py::get_injury_report()'s output) so the
    ranking can surface injury status alongside points -- this function
    does not fetch injuries itself to avoid a redundant fetch when a
    caller already has a report in hand.

    `position`, if given, restricts the result to slots whose eligible
    positions include it (e.g. "RB" only shows RB-eligible slots).

    Returns:
        {"available": bool, "note": str | None,
         "by_slot": {slot_label: [
             {"name": str, "points": float | None, "snap_share": float | None,
              "injury_status": str | None, "is_current_starter": bool,
              "sleeper_matched": bool},
             ...  # sorted best-to-worst; unscored players last, never dropped
         ]}}

    "available": False means Sleeper's connector itself failed (every
    retry) -- see sports/football/actuals.py::actual_points_for_roster().
    A player Sleeper just has no name-match for is NOT an error; they
    still appear in a slot's ranking, at the bottom, with points=None.
    """
    try:
        actuals = actual_points_for_roster(roster, league_id, scoring_profile, season, week)
    except SleeperUnavailable as e:
        logger.warning("football_start_sit: Sleeper unavailable for league %s: %s", league_id, e)
        return {"available": False,
                "note": f"Sleeper connector unavailable this run: {e}", "by_slot": {}}

    injury_by_name = injury_by_name or {}
    slot_defs = LEAGUE_STARTING_SLOTS.get(league_id, [])

    by_slot: dict[str, list[dict]] = {}
    for slot in slot_defs:
        if position is not None and position not in slot.eligible_positions:
            continue

        eligible_rs = [
            rs for rs in roster
            if set(rs.player.eligible_positions) & slot.eligible_positions
        ]
        if len(eligible_rs) <= slot.count:
            continue  # not contested -- nothing to rank

        entries = []
        for rs in eligible_rs:
            name = rs.player.name
            a = actuals.get(name, {})
            entries.append({
                "name": name,
                "points": a.get("points"),
                "snap_share": a.get("snap_share"),
                "injury_status": injury_by_name.get(_norm(name)),
                "is_current_starter": rs.is_starting,
                "sleeper_matched": a.get("sleeper_matched", False),
            })

        entries.sort(key=lambda e: (e["points"] is None, -(e["points"] or 0.0)))
        by_slot[slot.label] = entries

    return {"available": True, "note": None, "by_slot": by_slot}
