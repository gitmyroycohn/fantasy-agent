"""
P1 bug fix (2026-08-15): roster_value_signals position/role mistagging.

Live incident: Joey Cantillo, a full-time SP, was shown tagged [RP] by
roster_value_signals (mcp_server.py -> agent.tradevalue.analyze_roster_value),
which distorts category-fit scoring and can generate bad drop
recommendations.

Root cause: analyze_roster_value() (and sports/baseball/drops.py's
find_drop_candidates(), same bug class) read Player.positions, which for a
ROSTER player (unlike a free agent) is derived from Player.position -- the
player's CURRENT CBS roster slot tag (see cbs/roster.py's ENH 2 comment),
not their true position. A full-time SP parked in an RP bench slot shows
positions=["RP"] even though CBS's own full position-eligibility index
(players/list, via cbs/players.py::fetch_position_eligibility_index) says
["SP"]. Player.eligible_positions already resolves this correctly --
preferring eligible_positions_override (the full-eligibility index) when
set -- and is already used for this exact purpose elsewhere (e.g.
sports/baseball/lineup_optimizer.py's slot-eligibility checks). The fix
is for analyze_roster_value()/find_drop_candidates() to use it too.
"""
from data.models import Player, RosterSlot
from agent.tradevalue import analyze_roster_value
from sports.baseball.drops import find_drop_candidates


def _cantillo_slot():
    # Rostered in an RP bench slot (position="RP" -- the current-slot tag),
    # but CBS's full eligibility index says he's SP-eligible.
    player = Player(
        id="cantillo", name="Joey Cantillo", position="RP", team="CLE",
        eligible_positions_override=["SP"],
        stats={
            "K": 90, "fp_k": 60,          # current pace far above ROS projection -> sell_high
            "ERA": 3.00, "fp_era": 4.20,
            "WHIP": 1.05, "fp_whip": 1.30,
            "IP": 90.0,
        },
    )
    return RosterSlot(player=player, slot="RP", is_starting=True)


def test_analyze_roster_value_reports_true_eligible_position_not_current_slot():
    signals = analyze_roster_value([_cantillo_slot()])
    assert len(signals) == 1
    sig = signals[0]
    assert sig["name"] == "Joey Cantillo"
    # The old bug: this would be ["RP"] (the current-slot tag).
    assert sig["positions"] == ["SP"]


def test_analyze_roster_value_still_works_for_a_real_rp_with_no_override():
    # A genuine RP with no eligible_positions_override (free-agent-style
    # data, or a roster player before the eligibility index was wired) must
    # still fall back to `positions` correctly -- this fix must not turn
    # every reliever into an SP.
    player = Player(
        id="realrp", name="Real Reliever", position="RP", team="NYM",
        stats={
            "SV": 20, "fp_sv": 12,
            "ERA": 2.50, "fp_era": 3.50,
            "WHIP": 1.00, "fp_whip": 1.25,
            "IP": 55.0,
        },
    )
    slot = RosterSlot(player=player, slot="RP", is_starting=True)
    signals = analyze_roster_value([slot])
    assert len(signals) == 1
    assert signals[0]["positions"] == ["RP"]


def test_find_drop_candidates_evaluates_true_position_not_current_slot():
    # A struggling pitcher parked in an RP slot but SP-eligible per the full
    # index must be evaluated (and reported) as an SP drop candidate, not RP.
    player = Player(
        id="strugglingsp", name="Struggling Starter", position="RP", team="MIA",
        eligible_positions_override=["SP"],
        stats={"ERA": 6.50, "WHIP": 1.70, "K": 10, "IP": 25.0},
    )
    slot = RosterSlot(player=player, slot="RP", is_starting=True)
    drops = find_drop_candidates([slot], waiver_wire=[])
    assert len(drops) == 1
    assert drops[0]["positions"] == ["SP"]
