"""
Roster and starting-lineup legality rules for Christopher's 3 CBS football
leagues.

Starting-lineup slot definitions below are transcribed directly from each
league's CBS /rules "ROSTER LIMITS" table (see the football scoring-rules
memory captured 2026-07-31), cross-checked against the literal CBS
"ROSTER WARNINGS" text pulled from a live (empty, preseason) f_league
roster page on the same date:

    You have 0 active players. You must have 9.
    You have 0 reserve players. You must have 8.
    You have 0 active Quarterbacks. You must have 1.
    You have 0 active Running Backs. You must have 1.
    You have 0 active Wide Receivers. You must have 1.
    You have 0 active Tight Ends. You must have 1.
    You have 0 active Flex RB/WRs. You must have 2.
    You have 0 active Flex WR/TEs. You must have 1.
    You have 0 active Kickers. You must have 1.
    You have 0 active Defense/STs. You must have 1.
    You have 0 total players. Between 17 and 27 are allowed.

That confirms f_league's slot breakdown exactly (issue messages below
mirror this exact CBS phrasing). hard_chargers' breakdown is derived from
its /rules "ACTIVE MIN/MAX" ranges (dedicated QB/RB/WR/TE slots plus a
combined 3-slot RB/WR/TE flex -- the MAX values are exactly
dedicated-slot-count + flex-slot-count, e.g. RB max 4 = 1 dedicated + 3
flex, which is what confirms this reading). east_coast's breakdown is taken
directly from its /rules COMMISH MESSAGE, which spells out "Starting
Rosters (10 Starters)" explicitly rather than needing to be inferred.

IMPORTANT CAVEAT: CBS's own slot-TAG strings for football rosters (what a
real roster API response actually labels each starting slot as -- e.g.
whether f_league's RB/WR flex shows up as "RB-WR", "FLEX", or something
else) are NOT yet confirmed. No live roster has been fetched for any of
these leagues -- all 3 show "A ROSTER HAS NOT BEEN LOADED FOR THIS TEAM" as
of 2026-07-31 (preseason, draft not until 8/28/26 for f_league and not yet
scheduled for the other two). Rather than guess CBS's exact tag strings,
legality here is checked structurally: each required slot is defined by
the set of positions eligible to fill it, and validate_roster() solves
this as a matching problem (a starting player can fill any slot their
position is eligible for) using data.models.Player.eligible_positions,
NOT data.models.RosterSlot.slot. Sanity-check this against a real fetched
roster once one exists, the same way ENH 2 in sports/baseball validated
CBS's actual OF-tag behavior against live data before trusting it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SlotRequirement:
    label: str                    # human-readable, matches CBS's own wording where possible
    eligible_positions: frozenset
    count: int


# Starting-lineup slot breakdown per league, ORDER MATTERS: most-restrictive
# (fewest eligible positions) slots must come first so the greedy matcher in
# _match_slots() below gives them first pick of eligible starters before
# broader flex slots absorb them. This is safe (not just a heuristic) here
# specifically because each league's eligibility sets are "nested" --
# every flex slot's eligible set is a superset of the dedicated slots that
# precede it in the list -- which is the one structural condition under
# which greedy-by-restrictiveness matching is guaranteed optimal.
LEAGUE_STARTING_SLOTS: dict[str, list[SlotRequirement]] = {
    "f_league": [
        SlotRequirement("Quarterback", frozenset({"QB"}), 1),
        SlotRequirement("Running Back", frozenset({"RB"}), 1),
        SlotRequirement("Wide Receiver", frozenset({"WR"}), 1),
        SlotRequirement("Tight End", frozenset({"TE"}), 1),
        SlotRequirement("Kicker", frozenset({"K"}), 1),
        SlotRequirement("Defense/ST", frozenset({"DST"}), 1),
        SlotRequirement("Flex WR/TE", frozenset({"WR", "TE"}), 1),
        SlotRequirement("Flex RB/WR", frozenset({"RB", "WR"}), 2),
    ],
    "hard_chargers": [
        SlotRequirement("Quarterback", frozenset({"QB"}), 1),
        SlotRequirement("Running Back", frozenset({"RB"}), 1),
        SlotRequirement("Wide Receiver", frozenset({"WR"}), 1),
        SlotRequirement("Tight End", frozenset({"TE"}), 1),
        SlotRequirement("Kicker", frozenset({"K"}), 1),
        SlotRequirement("Defense/ST", frozenset({"DST"}), 1),
        SlotRequirement("Flex RB/WR/TE", frozenset({"RB", "WR", "TE"}), 3),
    ],
    "east_coast": [
        SlotRequirement("Quarterback", frozenset({"QB"}), 1),
        SlotRequirement("Tight End", frozenset({"TE"}), 1),
        SlotRequirement("Kicker", frozenset({"K"}), 1),
        SlotRequirement("Defense/ST", frozenset({"DST"}), 1),
        SlotRequirement("Running Back", frozenset({"RB"}), 2),
        SlotRequirement("Wide Receiver", frozenset({"WR"}), 3),
        SlotRequirement("Flex RB/WR/TE", frozenset({"RB", "WR", "TE"}), 1),
    ],
}

# Roster-wide (not just starting-lineup) limits.
LEAGUE_ROSTER_LIMITS: dict[str, dict] = {
    "f_league": {
        "bench": 8, "total_min": 17, "total_max": 27,
        "max_per_position": {"QB": 3},
    },
    "hard_chargers": {
        "bench": 6, "total_min": 15, "total_max": 15,  # fixed, no IR/practice slots
        "max_per_position": {},
    },
    "east_coast": {
        "bench": 9, "total_min": 0, "total_max": 19,  # 1 of the 9 bench spots is a Wild Card (any position)
        "max_per_position": {},
    },
}


def starters_required(league_id: str) -> int:
    return sum(s.count for s in LEAGUE_STARTING_SLOTS[league_id])


@dataclass
class RosterValidation:
    legal: bool
    starters_required: int
    starters_present: int
    issues: list = field(default_factory=list)
    unfilled_slots: list = field(default_factory=list)  # slot labels CBS would reject a lineup over


def _match_slots(starters: list, slots: list[SlotRequirement]):
    """Greedy matching of starters to slots, most-restrictive slot first
    (see the ordering note on LEAGUE_STARTING_SLOTS above for why this is
    safe rather than just approximate).

    Returns (assigned: dict[label, [player_name,...]], unfilled: [(label, short_by), ...])
    """
    remaining: dict[str, int] = {s.label: s.count for s in slots}
    assigned: dict[str, list[str]] = {s.label: [] for s in slots}
    used_ids: set[int] = set()

    for slot in slots:
        for rs in starters:
            if remaining[slot.label] <= 0:
                break
            if id(rs) in used_ids:
                continue
            if set(rs.player.eligible_positions) & slot.eligible_positions:
                assigned[slot.label].append(rs.player.name)
                used_ids.add(id(rs))
                remaining[slot.label] -= 1

    unfilled = [(label, n) for label, n in remaining.items() if n > 0]
    return assigned, unfilled


def validate_roster(roster: list, league_id: str) -> RosterValidation:
    """Check starting-lineup legality and roster-size limits for one team.

    roster: list of data.models.RosterSlot. Uses rs.is_starting and
    rs.player.eligible_positions -- NOT rs.slot (see module docstring for
    why: CBS's real football slot-tag strings aren't confirmed yet).
    """
    if league_id not in LEAGUE_STARTING_SLOTS:
        raise ValueError(f"Unknown football league_id: {league_id!r}")

    slots  = LEAGUE_STARTING_SLOTS[league_id]
    limits = LEAGUE_ROSTER_LIMITS[league_id]

    starters = [rs for rs in roster if rs.is_starting]
    bench    = [rs for rs in roster if not rs.is_starting]
    required = starters_required(league_id)

    issues: list[str] = []

    if len(starters) != required:
        issues.append(f"You have {len(starters)} active players. You must have {required}.")

    _, unfilled = _match_slots(starters, slots)
    for label, short_by in unfilled:
        issues.append(f"You have an unfilled {label} slot ({short_by} short).")

    if len(bench) > limits["bench"]:
        issues.append(
            f"You have {len(bench)} reserve players. You must have at most {limits['bench']}."
        )

    total = len(roster)
    if not (limits["total_min"] <= total <= limits["total_max"]):
        issues.append(
            f"You have {total} total players. Between {limits['total_min']} "
            f"and {limits['total_max']} are allowed."
        )

    for pos, max_count in limits.get("max_per_position", {}).items():
        count = sum(1 for rs in roster if pos in rs.player.eligible_positions)
        if count > max_count:
            issues.append(f"You have {count} {pos}s on your roster. Maximum allowed is {max_count}.")

    return RosterValidation(
        legal=not issues,
        starters_required=required,
        starters_present=len(starters),
        issues=issues,
        unfilled_slots=[label for label, _ in unfilled],
    )


def open_slots(roster: list, league_id: str) -> list[str]:
    """Labels of starting slots not currently filled -- used by
    sports/football/waivers.py to target free-agent adds at real gaps."""
    starters = [rs for rs in roster if rs.is_starting]
    _, unfilled = _match_slots(starters, LEAGUE_STARTING_SLOTS[league_id])
    return [label for label, _ in unfilled]


def slot_occupants(roster: list, league_id: str) -> dict[str, list]:
    """Which RosterSlot(s) currently fill each starting slot, using the same
    greedy most-restrictive-first matching as validate_roster()/
    open_slots() (see the ordering note on LEAGUE_STARTING_SLOTS for why
    that's safe here). A slot with an empty list is unfilled -- same
    information open_slots() reports, just keyed to who's actually there
    instead of just who's missing.

    Added for sports/football/waivers.py::find_upgrade_candidates() -- a
    fully-legal, fully-started roster (the normal case once the season's
    under way) has no open_slots() gaps at all, but that doesn't mean
    there's nothing worth grabbing off waivers; this is what lets that
    comparison happen against whoever's actually starting."""
    starters = [rs for rs in roster if rs.is_starting]
    slots = LEAGUE_STARTING_SLOTS[league_id]
    remaining: dict[str, int] = {s.label: s.count for s in slots}
    assigned: dict[str, list] = {s.label: [] for s in slots}
    used_ids: set[int] = set()
    for slot in slots:
        for rs in starters:
            if remaining[slot.label] <= 0:
                break
            if id(rs) in used_ids:
                continue
            if set(rs.player.eligible_positions) & slot.eligible_positions:
                assigned[slot.label].append(rs)
                used_ids.add(id(rs))
                remaining[slot.label] -= 1
    return assigned
