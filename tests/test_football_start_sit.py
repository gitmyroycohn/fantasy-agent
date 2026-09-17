"""
Tests for agent/football_start_sit.py. sports.football.actuals.
actual_points_for_roster is mocked -- no live Sleeper network access in
test runs (same convention as tests/test_football_decisions.py). A small
custom LEAGUE_STARTING_SLOTS is patched in so these tests don't depend on
the real leagues' exact slot shapes -- only the contested/uncontested
logic and ranking behavior this module owns.
"""

from unittest.mock import patch

from data.models import Player, RosterSlot
from sports.football.roster_rules import SlotRequirement
from sleeper.client import SleeperUnavailable

from agent.football_start_sit import football_start_sit


def _rs(name, position, starting=True):
    return RosterSlot(player=Player(id=name, name=name, position=position),
                      slot="STARTER" if starting else "BENCH", is_starting=starting)


_TEST_SLOTS = {
    "test_league": [
        SlotRequirement("Running Back", frozenset({"RB"}), 1),
        SlotRequirement("Wide Receiver", frozenset({"WR"}), 1),
    ]
}


def test_uncontested_slot_is_left_out_of_result():
    # Exactly 1 WR-eligible player for a 1-slot requirement -- nothing to rank.
    roster = [_rs("RB1", "RB"), _rs("RB2", "RB", starting=False), _rs("WR1", "WR")]
    fake_actuals = {
        "RB1": {"points": 10.0, "snap_share": 0.8, "position": "RB", "sleeper_matched": True},
        "RB2": {"points": 15.0, "snap_share": 0.6, "position": "RB", "sleeper_matched": True},
        "WR1": {"points": 5.0, "snap_share": 0.5, "position": "WR", "sleeper_matched": True},
    }
    with patch("agent.football_start_sit.actual_points_for_roster", return_value=fake_actuals), \
         patch("agent.football_start_sit.LEAGUE_STARTING_SLOTS", _TEST_SLOTS):
        result = football_start_sit(roster, "test_league", "standard_ppr", 2026, 2)

    assert result["available"] is True
    assert "Wide Receiver" not in result["by_slot"]  # only 1 eligible WR -- uncontested
    assert "Running Back" in result["by_slot"]        # 2 eligible RBs for 1 slot -- contested


def test_contested_slot_ranks_best_points_first():
    roster = [_rs("RB1", "RB"), _rs("RB2", "RB", starting=False), _rs("WR1", "WR")]
    fake_actuals = {
        "RB1": {"points": 10.0, "snap_share": 0.8, "position": "RB", "sleeper_matched": True},
        "RB2": {"points": 15.0, "snap_share": 0.6, "position": "RB", "sleeper_matched": True},
        "WR1": {"points": 5.0, "snap_share": 0.5, "position": "WR", "sleeper_matched": True},
    }
    with patch("agent.football_start_sit.actual_points_for_roster", return_value=fake_actuals), \
         patch("agent.football_start_sit.LEAGUE_STARTING_SLOTS", _TEST_SLOTS):
        result = football_start_sit(roster, "test_league", "standard_ppr", 2026, 2)

    names_in_order = [e["name"] for e in result["by_slot"]["Running Back"]]
    assert names_in_order == ["RB2", "RB1"]  # 15.0 > 10.0


def test_unmatched_players_sort_last_but_are_not_dropped():
    roster = [_rs("RB1", "RB"), _rs("RB2", "RB", starting=False)]
    fake_actuals = {
        "RB1": {"points": 10.0, "snap_share": 0.8, "position": "RB", "sleeper_matched": True},
        "RB2": {"points": None, "snap_share": None, "position": "RB", "sleeper_matched": False},
    }
    with patch("agent.football_start_sit.actual_points_for_roster", return_value=fake_actuals), \
         patch("agent.football_start_sit.LEAGUE_STARTING_SLOTS", _TEST_SLOTS):
        result = football_start_sit(roster, "test_league", "standard_ppr", 2026, 2)

    entries = result["by_slot"]["Running Back"]
    assert [e["name"] for e in entries] == ["RB1", "RB2"]
    assert entries[1]["sleeper_matched"] is False


def test_injury_status_and_current_starter_flow_through():
    roster = [_rs("RB1", "RB", starting=True), _rs("RB2", "RB", starting=False)]
    fake_actuals = {
        "RB1": {"points": 10.0, "snap_share": 0.8, "position": "RB", "sleeper_matched": True},
        "RB2": {"points": 12.0, "snap_share": 0.7, "position": "RB", "sleeper_matched": True},
    }
    injury_by_name = {"rb1": "Questionable"}
    with patch("agent.football_start_sit.actual_points_for_roster", return_value=fake_actuals), \
         patch("agent.football_start_sit.LEAGUE_STARTING_SLOTS", _TEST_SLOTS):
        result = football_start_sit(roster, "test_league", "standard_ppr", 2026, 2,
                                    injury_by_name=injury_by_name)

    by_name = {e["name"]: e for e in result["by_slot"]["Running Back"]}
    assert by_name["RB1"]["injury_status"] == "Questionable"
    assert by_name["RB1"]["is_current_starter"] is True
    assert by_name["RB2"]["injury_status"] is None
    assert by_name["RB2"]["is_current_starter"] is False


def test_position_filter_restricts_to_matching_slots():
    roster = [_rs("RB1", "RB"), _rs("RB2", "RB", starting=False), _rs("WR1", "WR"), _rs("WR2", "WR", starting=False)]
    fake_actuals = {n: {"points": 1.0, "snap_share": 0.5, "position": p, "sleeper_matched": True}
                    for n, p in [("RB1", "RB"), ("RB2", "RB"), ("WR1", "WR"), ("WR2", "WR")]}
    with patch("agent.football_start_sit.actual_points_for_roster", return_value=fake_actuals), \
         patch("agent.football_start_sit.LEAGUE_STARTING_SLOTS", _TEST_SLOTS):
        result = football_start_sit(roster, "test_league", "standard_ppr", 2026, 2, position="RB")

    assert set(result["by_slot"].keys()) == {"Running Back"}


def test_sleeper_unavailable_degrades_honestly():
    roster = [_rs("RB1", "RB")]
    with patch("agent.football_start_sit.actual_points_for_roster",
              side_effect=SleeperUnavailable("down")), \
         patch("agent.football_start_sit.LEAGUE_STARTING_SLOTS", _TEST_SLOTS):
        result = football_start_sit(roster, "test_league", "standard_ppr", 2026, 2)

    assert result["available"] is False
    assert "down" in result["note"]
    assert result["by_slot"] == {}
