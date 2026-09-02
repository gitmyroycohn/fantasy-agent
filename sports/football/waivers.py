"""
Waiver-wire filtering for Christopher's 3 CBS football leagues.

Roster-fit filtering (whether a free agent could legally fill one of the
team's open starting slots, or would breach a league's roster-size /
position-cap limits) is pure roster-fit logic (sports/football/roster_rules.py)
and has always been real -- this module wraps it into waiver-candidate
filtering.

Performance-based ranking on top of that filtering is now REAL for all
three leagues, not invented from raw stats -- but not from one shared
formula, since the leagues don't share a scoring format:
hard_chargers and east_coast are both real per-play PPR formats, and
FantasyPros' own season-long PPR points projection (/nfl/{season}/
projections, stats.points_ppr field -- verified live against a real API
response via fp_probe.py on 2026-08-23, see project memory) is a
legitimate value signal for them directly. sfflf's scoring is non-PPR
tiered/position-diff -- a format FantasyPros doesn't project for directly,
so ranking its free agents by points_ppr would silently misrank them
against a scoring system that isn't theirs -- but as of 2026-08-23 its
candidates ARE ranked by a real signal too: sfflf's own tiered/position-
dependent formula reimplemented against FantasyPros' raw per-category
projection stats (pass/rush/rec yards, TDs by type) instead of points_ppr
-- see sports/football/scoring.py::estimate_sfflf_points() for the method
and its one documented gap (no long-TD-yardage bonus, since season
projections don't carry per-TD yardage -- a systematic but uniform
underestimate, fine for ranking candidates against each other). Any
league falls back to ownership_pct-only sorting (see _PROJECTION_LEAGUES
below) whenever no projections map is supplied or FantasyPros' API is
unavailable -- the same "under-owned = worth grabbing" signal
sports/baseball/streaming.py uses via MIN_SP_OWNERSHIP_DROP, mirrored here
without baseball's additional stat-floor scoring layer. This is still not
a full lineup optimizer or start/sit tool -- it only ranks candidates for
open starting slots.
"""

from __future__ import annotations

from sports.football.roster_rules import (
    LEAGUE_ROSTER_LIMITS,
    LEAGUE_STARTING_SLOTS,
    open_slots,
    slot_occupants,
)

# Minimum projected-point edge a free agent must have over the CURRENT
# starter at a slot to be worth surfacing as an upgrade suggestion --
# avoids noise from trivial/within-projection-error point differences.
# Only ever applied when a real projections signal exists for both players
# (see find_upgrade_candidates()) -- never estimated off ownership_pct,
# since there's no honest way to say a free agent's ownership beats a
# rostered starter's actual production outlook.
UPGRADE_MIN_POINT_EDGE = 1.0

# Leagues where a real (non-ownership) FantasyPros-derived signal exists
# for ranking waiver candidates. The signal itself differs per league --
# hard_chargers/east_coast use FantasyPros' points_ppr projection directly
# (real per-play PPR formats, a fair match); f_league uses an ESTIMATE of
# its own tiered/position-dependent scoring computed from FantasyPros' raw
# stat projections (see sports/football/scoring.py::estimate_sfflf_points())
# -- which adapter the caller uses is agent/football_decisions.py's job,
# not this module's; this set only gates whether find_waiver_candidates_
# for_open_slots() trusts a supplied `projections` map at all for a given
# league, vs. always falling back to ownership_pct.
_PROJECTION_LEAGUES = {"hard_chargers", "east_coast", "f_league"}


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

    projections: optional {normalized_player_name: points} map -- for
    hard_chargers/east_coast this is FantasyPros' points_ppr projection
    (agent/football_decisions.py::_fp_nfl_projections_by_name()); for
    f_league it's an sfflf-scoring ESTIMATE derived from FantasyPros' raw
    stats (agent/football_decisions.py::_fp_sfflf_points_by_name(), which
    wraps sports/football/scoring.py::estimate_sfflf_points()). Only used
    when league_id is in _PROJECTION_LEAGUES; the values are NOT
    cross-league comparable (a hard_chargers points_ppr number and an
    f_league estimated-points number measure different scoring formats),
    but this function only sorts within one league's own candidate list,
    so that's never an issue here.

    When applied: candidates with a projection are sorted first, highest
    projected points first; candidates with no projection match follow,
    sorted by ownership_pct ascending (same fallback as always). When not
    applied (league not in _PROJECTION_LEAGUES, or no projections
    supplied): every candidate is sorted by ownership_pct ascending, same
    as before this signal existed.

    Returns {slot_label: [WaiverPlayer, ...]}. An empty dict means every
    starting slot is already filled (nothing to target this way -- doesn't
    mean the roster has no upgrade candidates, just that this function only
    looks for slot gaps, not swap-upgrades).
    """
    slots_needed = open_slots(roster, league_id)
    use_projections = bool(projections) and league_id in _PROJECTION_LEAGUES
    result = {}
    for label in slots_needed:
        candidates = [wp for wp in waiver_wire if eligible_for_slot(wp.player, label, league_id)]
        if use_projections:
            def _sort_key(wp, _proj=projections):
                pts = _proj.get(_norm(wp.player.name))
                # projected candidates first (best projected points first), then
                # unprojected candidates by ownership_pct ascending
                return (0, -pts) if pts is not None else (1, wp.ownership_pct)
            candidates.sort(key=_sort_key)
        else:
            candidates.sort(key=lambda wp: wp.ownership_pct)
        result[label] = candidates
    return result


def find_upgrade_candidates(roster: list, waiver_wire: list, league_id: str,
                           projections: dict[str, float] | None = None
                           ) -> dict[str, list[tuple]]:
    """Free agents who project AHEAD of whoever is currently STARTING at an
    eligible slot -- added 2026-09-02 after Christopher reported that a
    fully-legal, fully-started roster (9/9 starters, 8/8 bench -- the
    normal state once the season's under way, not the exception) got ZERO
    waiver recommendations from find_waiver_candidates_for_open_slots()'s
    gap-only filtering, which he correctly flagged as not how a waiver
    tool should behave. That function's open-slot filtering stays useful
    (a bye-week/injury gap is real), it just isn't the only case that
    matters -- most real usage is "is anyone out there better than my
    weakest starter," not "is a slot literally empty."

    Only produced when a real projections signal is available for BOTH the
    current starter and the free agent -- never estimated off
    ownership_pct, since there's no honest way to say a free agent's
    ownership beats a rostered starter's actual production outlook. A slot
    whose starter has no projection match is skipped rather than guessed.
    Requires at least UPGRADE_MIN_POINT_EDGE points of projected edge to
    filter out noise-level differences.

    Returns {slot_label: [(WaiverPlayer, occupant_RosterSlot, point_edge), ...]}
    sorted by point_edge descending, for slots that ARE currently filled
    (an unfilled slot is open_slots()/find_waiver_candidates_for_open_slots()'s
    job, not this one's -- no overlap between the two).
    """
    if not projections or league_id not in _PROJECTION_LEAGUES:
        return {}

    occupants = slot_occupants(roster, league_id)
    slots_def = {s.label: s for s in LEAGUE_STARTING_SLOTS[league_id]}

    result: dict[str, list[tuple]] = {}
    for slot_label, occupant_slots in occupants.items():
        if not occupant_slots:
            continue  # unfilled -- not this function's concern
        eligible = slots_def[slot_label].eligible_positions
        candidates = []
        for occ in occupant_slots:
            occ_pts = projections.get(_norm(occ.player.name))
            if occ_pts is None:
                continue
            for wp in waiver_wire:
                if not (set(wp.player.eligible_positions) & eligible):
                    continue
                fa_pts = projections.get(_norm(wp.player.name))
                if fa_pts is None:
                    continue
                edge = fa_pts - occ_pts
                if edge >= UPGRADE_MIN_POINT_EDGE:
                    candidates.append((wp, occ, edge))
        if candidates:
            candidates.sort(key=lambda t: -t[2])
            result[slot_label] = candidates
    return result


def rank_waiver_recommendations(roster: list, waiver_wire: list, league_id: str,
                                projections: dict[str, float] | None = None,
                                position: str | None = None,
                                limit: int = 10) -> list[dict]:
    """Flat, ranked waiver-recommendation list for the football_waiver_
    recommendations MCP tool (2026-08-31 enhancement order, extended
    2026-09-02 to cover upgrade candidates -- see find_upgrade_candidates()
    docstring for why gap-filling alone wasn't enough).

    Merges two tiers, both scoring-format-aware via the same projections
    map: (1) find_waiver_candidates_for_open_slots() for slots that are
    currently EMPTY, and (2) find_upgrade_candidates() for free agents who
    project ahead of whoever's currently STARTING at a filled slot. A
    player showing up in both (rare -- would need to be both slot-eligible
    for an open gap AND a projected upgrade over a different filled slot)
    is deduped to one entry carrying whichever detail applies.

    Args mirror find_waiver_candidates_for_open_slots(); position/limit
    are this function's own filter/cap on the flattened, merged result.

    Returns a list of dicts (already sorted, already limited):
        {"player": WaiverPlayer, "slots": [slot_label, ...],
         "projected_points": float | None,
         "upgrade_over": (occupant_name, point_edge) | None}
    projected_points/upgrade_over are None wherever no real signal is
    available -- never fabricated, same rule
    _fp_nfl_projections_by_name()/_fp_sfflf_points_by_name() already follow.
    """
    by_slot = find_waiver_candidates_for_open_slots(
        roster, waiver_wire, league_id, projections=projections)
    upgrades_by_slot = find_upgrade_candidates(
        roster, waiver_wire, league_id, projections=projections)

    use_projections = bool(projections) and league_id in _PROJECTION_LEAGUES
    req_pos = position.strip().upper() if position else None

    by_player: dict[str, dict] = {}
    order: list[str] = []

    def _entry(wp):
        pid = wp.player.id or wp.player.name
        if pid not in by_player:
            by_player[pid] = {
                "player": wp,
                "slots": [],
                "projected_points": (projections.get(_norm(wp.player.name))
                                      if use_projections else None),
                "upgrade_over": None,
            }
            order.append(pid)
        return by_player[pid]

    for slot_label, candidates in by_slot.items():
        for wp in candidates:
            if req_pos and req_pos not in wp.player.eligible_positions:
                continue
            entry = _entry(wp)
            if slot_label not in entry["slots"]:
                entry["slots"].append(slot_label)

    for slot_label, candidates in upgrades_by_slot.items():
        for wp, occ, edge in candidates:
            if req_pos and req_pos not in wp.player.eligible_positions:
                continue
            entry = _entry(wp)
            if slot_label not in entry["slots"]:
                entry["slots"].append(slot_label)
            if entry["upgrade_over"] is None or edge > entry["upgrade_over"][1]:
                entry["upgrade_over"] = (occ.player.name, edge)

    def _sort_key(pid):
        entry = by_player[pid]
        pts = entry["projected_points"]
        # projected candidates first (best points first), then unprojected
        # candidates by ownership_pct ascending -- same fallback rule as
        # find_waiver_candidates_for_open_slots().
        return (0, -pts) if pts is not None else (1, entry["player"].ownership_pct)

    order.sort(key=_sort_key)
    return [by_player[pid] for pid in order[:limit]]


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
