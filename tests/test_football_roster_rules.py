"""
Tests for sports/football/roster_rules.py and sports/football/waivers.py.

Synthetic Player/RosterSlot/WaiverPlayer fixtures (no live CBS football
roster data exists yet -- all 3 leagues are preseason as of 2026-08-01, see
project memory). These tests verify the slot-matching and roster-limit
logic itself, not CBS's real slot-tag strings (which remain unconfirmed --
see the module docstring in sports/football/roster_rules.py).
"""

from data.models import Player, RosterSlot, WaiverPlayer

from sports.football.roster_rules import (
    starters_required,
    validate_roster,
    open_slots,
    LEAGUE_STARTING_SLOTS,
)
from sports.football.waivers import (
    eligible_for_slot,
    find_waiver_candidates_for_open_slots,
    roster_has_room_for_add,
    would_exceed_position_limit,
)


def _p(name, position):
    return Player(id=name.lower().replace(" ", "_"), name=name, position=position)


def _starter(name, position):
    return RosterSlot(player=_p(name, position), slot="STARTER", is_starting=True)


def _bench(name, position):
    return RosterSlot(player=_p(name, position), slot="BENCH", is_starting=False)


# ---------------------------------------------------------------------------
# starters_required
# ---------------------------------------------------------------------------

def test_starters_required_matches_each_leagues_confirmed_lineup_size():
    assert starters_required("f_league") == 9
    assert starters_required("hard_chargers") == 9
    assert starters_required("east_coast") == 10


def test_unknown_league_raises():
    import pytest
    with pytest.raises(KeyError):
        starters_required("not_a_real_league")


# ---------------------------------------------------------------------------
# f_league: QB, RB, WR, TE, K, DST, Flex WR/TE(1), Flex RB/WR(2) = 9 starters
# ---------------------------------------------------------------------------

def _legal_f_league_roster():
    starters = [
        _starter("QB1", "QB"),
        _starter("RB1", "RB"),
        _starter("WR1", "WR"),
        _starter("TE1", "TE"),
        _starter("K1", "K"),
        _starter("DST1", "DST"),
        _starter("FlexTE", "TE"),     # fills Flex WR/TE
        _starter("FlexRB1", "RB"),    # fills one of the Flex RB/WR slots
        _starter("FlexWR1", "WR"),    # fills the other Flex RB/WR slot
    ]
    bench = [_bench(f"Bench{i}", "WR") for i in range(8)]
    return starters + bench


def test_f_league_legal_roster_passes():
    roster = _legal_f_league_roster()
    result = validate_roster(roster, "f_league")
    assert result.legal, result.issues
    assert result.starters_present == 9
    assert result.unfilled_slots == []


def test_f_league_missing_qb_is_illegal():
    roster = _legal_f_league_roster()
    # Replace the QB starter with another WR -- no one left eligible for QB slot
    roster[0] = _starter("NotAQB", "WR")
    result = validate_roster(roster, "f_league")
    assert not result.legal
    assert "Quarterback" in result.unfilled_slots


def test_f_league_max_3_qb_enforced():
    roster = _legal_f_league_roster()
    # Swap 2 existing bench spots for QBs (3 total QBs on roster: fine, exactly at cap)
    roster[-1] = _bench("QB2", "QB")
    roster[-2] = _bench("QB3", "QB")
    result = validate_roster(roster, "f_league")
    assert result.legal, result.issues

    # A 4th QB (swapping in for another bench spot, bench count unchanged) breaches the cap
    roster[-3] = _bench("QB4", "QB")
    result = validate_roster(roster, "f_league")
    assert not result.legal
    assert any("QB" in issue and "Maximum allowed is 3" in issue for issue in result.issues)


def test_f_league_bench_too_large_is_illegal():
    roster = _legal_f_league_roster() + [_bench("Extra", "WR")]
    result = validate_roster(roster, "f_league")
    assert not result.legal
    assert any("reserve players" in issue for issue in result.issues)


def test_f_league_open_slots_reports_unfilled_flex():
    roster = _legal_f_league_roster()
    # Drop one of the two Flex RB/WR starters down to bench
    roster[8].is_starting = False
    result = validate_roster(roster, "f_league")
    assert not result.legal
    assert "Flex RB/WR" in open_slots(roster, "f_league")


# ---------------------------------------------------------------------------
# hard_chargers: QB, RB, WR, TE, K, DST, Flex RB/WR/TE(3) = 9 starters
# ---------------------------------------------------------------------------

def _legal_hard_chargers_roster():
    starters = [
        _starter("QB1", "QB"),
        _starter("RB1", "RB"),
        _starter("WR1", "WR"),
        _starter("TE1", "TE"),
        _starter("K1", "K"),
        _starter("DST1", "DST"),
        _starter("Flex1", "RB"),
        _starter("Flex2", "WR"),
        _starter("Flex3", "TE"),
    ]
    bench = [_bench(f"Bench{i}", "WR") for i in range(6)]
    return starters + bench


def test_hard_chargers_legal_roster_passes():
    roster = _legal_hard_chargers_roster()
    result = validate_roster(roster, "hard_chargers")
    assert result.legal, result.issues


def test_hard_chargers_total_roster_fixed_at_15():
    roster = _legal_hard_chargers_roster()
    assert len(roster) == 15
    roster.append(_bench("Extra", "WR"))
    result = validate_roster(roster, "hard_chargers")
    assert not result.legal
    assert any("total players" in issue for issue in result.issues)


# ---------------------------------------------------------------------------
# east_coast: QB1, RB2, WR3, TE1, Flex RB/WR/TE(1), K1, DST1 = 10 starters
# ---------------------------------------------------------------------------

def _legal_east_coast_roster():
    starters = [
        _starter("QB1", "QB"),
        _starter("RB1", "RB"),
        _starter("RB2", "RB"),
        _starter("WR1", "WR"),
        _starter("WR2", "WR"),
        _starter("WR3", "WR"),
        _starter("TE1", "TE"),
        _starter("K1", "K"),
        _starter("DST1", "DST"),
        _starter("Flex1", "RB"),
    ]
    bench = [_bench(f"Bench{i}", "WR") for i in range(9)]
    return starters + bench


def test_east_coast_legal_roster_passes():
    roster = _legal_east_coast_roster()
    result = validate_roster(roster, "east_coast")
    assert result.legal, result.issues
    assert len(roster) == 19


def test_east_coast_missing_3rd_wr_is_illegal():
    roster = _legal_east_coast_roster()
    roster[5] = _starter("NotAWR", "TE")  # was WR3
    result = validate_roster(roster, "east_coast")
    assert not result.legal
    assert "Wide Receiver" in result.unfilled_slots


# ---------------------------------------------------------------------------
# waivers.py
# ---------------------------------------------------------------------------

def test_eligible_for_slot_respects_flex_positions():
    rb = _p("Some RB", "RB")
    qb = _p("Some QB", "QB")
    assert eligible_for_slot(rb, "Flex RB/WR", "f_league") is True
    assert eligible_for_slot(qb, "Flex RB/WR", "f_league") is False


def test_eligible_for_slot_unknown_slot_raises():
    import pytest
    with pytest.raises(ValueError):
        eligible_for_slot(_p("X", "WR"), "Not A Real Slot", "f_league")


def test_find_waiver_candidates_targets_open_slots_and_sorts_by_ownership():
    roster = _legal_f_league_roster()
    roster[0] = _starter("NotAQB", "WR")  # opens the Quarterback slot

    wire = [
        WaiverPlayer(player=_p("Popular QB", "QB"), ownership_pct=80.0),
        WaiverPlayer(player=_p("Sleeper QB", "QB"), ownership_pct=5.0),
        WaiverPlayer(player=_p("Irrelevant WR", "WR"), ownership_pct=1.0),
    ]

    candidates = find_waiver_candidates_for_open_slots(roster, wire, "f_league")
    assert "Quarterback" in candidates
    names = [wp.player.name for wp in candidates["Quarterback"]]
    assert names == ["Sleeper QB", "Popular QB"]  # sorted by ownership ascending
    assert "Irrelevant WR" not in names


def test_roster_has_room_for_add_respects_total_max():
    roster = _legal_hard_chargers_roster()  # exactly 15, at hard_chargers' fixed cap
    assert roster_has_room_for_add(roster, "hard_chargers") is False

    roster_f = _legal_f_league_roster()  # 17 of 17-27 allowed
    assert roster_has_room_for_add(roster_f, "f_league") is True


def test_would_exceed_position_limit_f_league_qb_cap():
    roster = _legal_f_league_roster() + [_bench("QB2", "QB"), _bench("QB3", "QB")]
    incoming = _p("QB4", "QB")
    violations = would_exceed_position_limit(roster, incoming, "f_league")
    assert violations == [("QB", 4, 3)]

    # A non-QB add never trips this league's only cap
    assert would_exceed_position_limit(roster, _p("WR9", "WR"), "f_league") == []
