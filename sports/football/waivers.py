"""
Waiver-wire filtering for Christopher's 3 CBS football leagues.

There is deliberately NO performance-based waiver ranking here yet, unlike
sports/baseball/streaming.py (SP streaming, scored against real ERA/K9
thresholds) and sports/baseball/drops.py (drop candidates, scored against
real _BAT_FLOOR/_PITCH_FLOOR thresholds calibrated on actual MLB stat
distributions). Football has no live stat feed, no MCP tool, and not even
a single drafted roster yet as of 2026-08-01 (all 3 leagues are preseason --
see project memory). Inventing NFL fantasy-point floors with no real season
data behind them would be worse than not having the feature: it would look
authoritative while being a guess. That piece gets built once real weekly
football stats exist to calibrate against, the same way baseball's floors
were tuned against real production data.

What IS buildable without live stats: whether a free agent could legally
fill one of the team's open starting slots, or would breach a league's
roster-size / position-cap limits if added. That's pure roster-fit logic
(sports/football/roster_rules.py) and is genuinely useful today -- this
module wraps it into waiver-candidate filtering, with ownership_pct
(already present in data.models.WaiverPlayer) as the only ranking signal,
mirroring the "under-owned = worth grabbing" logic
sports/baseball/streaming.py uses via MIN_SP_OWNERSHIP_DROP, just without
the additional stat-based scoring layer baseball has on top of it.
"""

from __future__ import annotations

from sports.football.roster_rules import (
    LEAGUE_ROSTER_LIMITS,
    LEAGUE_STARTING_SLOTS,
    open_slots,
)


def eligible_for_slot(player, slot_label: str, league_id: str) -> bool:
    """Would this free agent be eligible to fill the given open starting slot?"""
    slots = {s.label: s for s in LEAGUE_STARTING_SLOTS[league_id]}
    if slot_label not in slots:
        raise ValueError(f"Unknown slot {slot_label!r} for league {league_id!r}")
    return bool(set(player.eligible_positions) & slots[slot_label].eligible_positions)


def find_waiver_candidates_for_open_slots(roster: list, waiver_wire: list,
                                           league_id: str) -> dict[str, list]:
    """For each currently-unfilled starting slot, return the free agents on
    the waiver wire eligible to fill it, sorted by ownership_pct ascending
    (lower ownership = more available/under-the-radar).

    Returns {slot_label: [WaiverPlayer, ...]}. An empty dict means every
    starting slot is already filled (nothing to target this way -- doesn't
    mean the roster has no upgrade candidates, just that this function only
    looks for slot gaps, not swap-upgrades).
    """
    slots_needed = open_slots(roster, league_id)
    result = {}
    for label in slots_needed:
        candidates = [wp for wp in waiver_wire if eligible_for_slot(wp.player, label, league_id)]
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
