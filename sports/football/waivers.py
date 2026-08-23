"""
Waiver-wire filtering for Christopher's 3 CBS football leagues.

Roster-fit filtering (whether a free agent could legally fill one of the
team's open starting slots, or would breach a league's roster-size /
position-cap limits) is pure roster-fit logic (sports/football/roster_rules.py)
and has always been real -- this module wraps it into waiver-candidate
filtering.

Performance-based ranking on top of that filtering is now REAL for two of
the three leagues, not invented from raw stats: hard_chargers and
east_coast are both real per-play PPR formats, and FantasyPros' own
season-long PPR points projection (/nfl/{season}/projections,
stats.points_ppr field -- verified live against a real API response via
fp_probe.py on 2026-08-23, see project memory) is a legitimate value
signal for them specifically. sfflf is deliberately excluded: its scoring
is non-PPR tiered/position-diff, a format FantasyPros doesn't project for,
so ranking its free agents by points_ppr would silently misrank them
against a scoring system that isn't theirs -- sfflf always falls back to
ownership_pct-only sorting (see _PPR_PROJECTION_LEAGUES below), the same
"under-owned = worth grabbing" signal sports/baseball/streaming.py uses via
MIN_SP_OWNERSHIP_DROP, mirrored here without baseball's additional
stat-floor scoring layer. This is still not a full lineup optimizer or
start/sit tool -- it only ranks candidates for open starting slots.
"""

from __future__ import annotations

from sports.football.roster_rules import (
    LEAGUE_ROSTER_LIMITS,
    LEAGUE_STARTING_SLOTS,
    open_slots,
)

# Leagues where FantasyPros' points_ppr season-long projection is a valid
# ranking signal for waiver candidates -- both are real per-play PPR
# formats FP's PPR projection actually represents. sfflf is intentionally
# absent: its non-PPR tiered/position-diff scoring isn't what points_ppr
# measures, so it always uses ownership_pct-only sorting instead, whether
# or not a projections map is passed in.
_PPR_PROJECTION_LEAGUES = {"hard_chargers", "east_coast"}


def _norm(name: str) -> str:
    return name.strip().lower()


def eligible_for_slot(player, slot_label: str, league_id: str) -> bool:
    """Would this free agent be eligible to fill the given open starting slot?"""
    slots = {s.label: s for s in LEAGUE_STARTING_SLOTS[league_id]}
    if slot_label not in slots:
        raise ValueError(f"Unknown slot {slot_label!r} for league {league_id!r}")
    return bool(set(player.eligible_positions) & slots[slot_label].eligible_positions)


def find_waiver_candidates_for_open_slots(roster: list, waiver_wire: list,
                                           league_id: str,
                                           projections: dict[str, float] | None = None
                                           ) -> dict[str, list]:
    """For each currently-unfilled starting slot, return the free agents on
    the waiver wire eligible to fill it.

    projections: optional {normalized_player_name: points_ppr} map (see
    agent/football_decisions.py::_fp_nfl_projections_by_name()). Only used
    when league_id is in _PPR_PROJECTION_LEAGUES (hard_chargers,
    east_coast) -- passing it for sfflf has no effect, since points_ppr
    isn't a signal that fits sfflf's non-PPR tiered scoring.

    When applied: candidates with a projection are sorted first, highest
    points_ppr first; candidates with no projection match follow, sorted
    by ownership_pct ascending (same fallback as always). When not
    applied (sfflf, or no projections supplied): every candidate is
    sorted by ownership_pct ascending, same as before this signal existed.

    Returns {slot_label: [WaiverPlayer, ...]}. An empty dict means every
    starting slot is already filled (nothing to target this way -- doesn't
    mean the roster has no upgrade candidates, just that this function only
    looks for slot gaps, not swap-upgrades).
    """
    slots_needed = open_slots(roster, league_id)
    use_projections = bool(projections) and league_id in _PPR_PROJECTION_LEAGUES
    result = {}
    for label in slots_needed:
        candidates = [wp for wp in waiver_wire if eligible_for_slot(wp.player, label, league_id)]
        if use_projections:
            def _sort_key(wp, _proj=projections):
                pts = _proj.get(_norm(wp.player.name))
                # projected candidates first (best points_ppr first), then
                # unprojected candidates by ownership_pct ascending
                return (0, -pts) if pts is not None else (1, wp.ownership_pct)
            candidates.sort(key=_sort_key)
        else:
            candidates.sort(key=lambda wp: wp.ownership_pct)
        result[label] = candidates
    return result


def roster_has_room_for_add(roster: list, league_id: str) -> bool:
    """True if adding one more player wouldn't push the roster past its
    total-player limit. False means the caller must drop someone first --
    this function doesn't pick who."""
    limits = LEAGUE_ROSTER_LIMITS[league_id]
    return len(roster) < limits["total_max"]


def would_exceed_position_limit(roster: list, incoming_player, league_id: str) -> list[tuple]:
    """Return [(position, count_after_add, max_allowed), ...] for every
    league-specific max-per-position cap (e.g. f_league's 3-QB max) that
    adding incoming_player would breach. Empty list = no violation."""
    limits = LEAGUE_ROSTER_LIMITS[league_id].get("max_per_position", {})
    violations = []
    for pos, max_count in limits.items():
        if pos not in incoming_player.eligible_positions:
            continue
        current = sum(1 for rs in roster if pos in rs.player.eligible_positions)
        if current + 1 > max_count:
            violations.append((pos, current + 1, max_count))
    return violations
