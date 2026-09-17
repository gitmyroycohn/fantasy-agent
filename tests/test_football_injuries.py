"""
Tests for agent/football_injuries.py. FantasyPros' client and Sleeper's
get_players() are both mocked/faked -- no live network access in test
runs (same convention as tests/test_football_decisions.py).
"""

from unittest.mock import patch

from data.models import Player, RosterSlot
from sleeper.client import SleeperUnavailable

from agent.football_injuries import get_injury_report


def _rs(name, position):
    return RosterSlot(player=Player(id=name, name=name, position=position),
                      slot="STARTER", is_starting=True)


class _FakeFPClient:
    def __init__(self, injuries):
        self._injuries = injuries

    def nfl_injuries(self, week, season):
        return self._injuries


def test_both_sources_agree_no_disagreement_flag():
    roster = [_rs("Jonathan Taylor", "RB")]
    fp_client = _FakeFPClient([
        {"name": "Jonathan Taylor", "status": "Questionable", "injury_type": "Ankle",
         "practice_1": "DNP", "practice_2": "Limited", "practice_3": "Full",
         "probability_of_playing": "75%"},
    ])
    sleeper_players = {"1": {"full_name": "Jonathan Taylor", "injury_status": "Questionable"}}

    with patch("agent.football_injuries.get_players", return_value=sleeper_players):
        report = get_injury_report(roster, fp_client, season=2026, week=2)

    assert report["available"] is True
    assert len(report["players"]) == 1
    p = report["players"][0]
    assert p["fp_status"] == "Questionable"
    assert p["sleeper_status"] == "Questionable"
    assert p["disagreement"] is False
    assert p["fp_practice"] == ["DNP", "Limited", "Full"]


def test_sources_disagree_flags_it():
    roster = [_rs("Saquon Barkley", "RB")]
    fp_client = _FakeFPClient([
        {"name": "Saquon Barkley", "status": "Out", "injury_type": "Knee"},
    ])
    sleeper_players = {"1": {"full_name": "Saquon Barkley", "injury_status": "Questionable"}}

    with patch("agent.football_injuries.get_players", return_value=sleeper_players):
        report = get_injury_report(roster, fp_client, season=2026, week=2)

    assert report["players"][0]["disagreement"] is True


def test_player_with_no_status_from_either_source_is_not_reported():
    roster = [_rs("Healthy Guy", "WR")]
    fp_client = _FakeFPClient([])
    sleeper_players = {"1": {"full_name": "Healthy Guy"}}  # no injury_status key

    with patch("agent.football_injuries.get_players", return_value=sleeper_players):
        report = get_injury_report(roster, fp_client, season=2026, week=2)

    assert report["available"] is True
    assert report["players"] == []


def test_no_fp_client_degrades_to_sleeper_only_with_note():
    roster = [_rs("Brock Bowers", "TE")]
    sleeper_players = {"1": {"full_name": "Brock Bowers", "injury_status": "Probable"}}

    with patch("agent.football_injuries.get_players", return_value=sleeper_players):
        report = get_injury_report(roster, fp_client=None, season=2026, week=2)

    assert report["available"] is True
    assert report["note"] is not None
    assert "fantasypros" in report["note"].lower()
    assert report["players"][0]["sleeper_status"] == "Probable"
    assert report["players"][0]["fp_status"] is None


def test_both_sources_succeed_but_empty_is_still_available_with_no_players():
    # Neither source failed -- FantasyPros just had no injuries to report
    # and Sleeper's index (mocked here) has no rostered player with a
    # status. This is the common, ordinary case (most of a roster is
    # healthy most weeks) and must NOT be reported as an outage.
    roster = [_rs("Nobody", "WR")]
    fp_client = _FakeFPClient([])

    with patch("agent.football_injuries.get_players", return_value={}):
        report = get_injury_report(roster, fp_client, season=2026, week=2)

    assert report["available"] is True
    assert report["note"] is None
    assert report["players"] == []


def test_both_sources_actually_failing_returns_unavailable():
    # The real "both sources down" case: no fp_client configured AND
    # Sleeper's connector itself is unreachable (every retry failed).
    roster = [_rs("Nobody", "WR")]

    with patch("agent.football_injuries.get_players", side_effect=SleeperUnavailable("down")):
        report = get_injury_report(roster, fp_client=None, season=2026, week=2)

    assert report["available"] is False
    assert report["players"] == []
    assert "fantasypros" in report["note"].lower()
    assert "down" in report["note"]
