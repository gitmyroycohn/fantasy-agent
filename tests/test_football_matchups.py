"""
Tests for agent/football_matchups.py. cbs.stats.fetch_football_matchup is
mocked -- no live CBS network access in test runs (same convention as
tests/test_football_decisions.py mocking cbs.waivers.fetch_waiver_wire).
"""

from unittest.mock import patch

from cbs.stats import CBSFootballMatchupError
from agent.football_matchups import get_matchup_result


def test_available_matchup_passes_through_with_available_true():
    fake = {
        "period": "3", "my_team_name": "COWBOYS", "my_points": 105.4,
        "opponent": "Captain Jack", "opponent_id": "10", "opp_points": 98.2,
        "record": {"w": 2, "l": 1, "t": 0}, "home_away": "home",
    }
    with patch("agent.football_matchups.fetch_football_matchup", return_value=fake):
        result = get_matchup_result(auth=None, cbs_league_id="sfflf")

    assert result["available"] is True
    assert result["note"] is None
    assert result["my_points"] == 105.4
    assert result["opponent"] == "Captain Jack"


def test_bye_week_returns_unavailable_with_note():
    with patch("agent.football_matchups.fetch_football_matchup", return_value=None):
        result = get_matchup_result(auth=None, cbs_league_id="sfflf")

    assert result["available"] is False
    assert "bye week" in result["note"].lower()


def test_cbs_error_returns_unavailable_with_note():
    with patch("agent.football_matchups.fetch_football_matchup",
              side_effect=CBSFootballMatchupError("boom")):
        result = get_matchup_result(auth=None, cbs_league_id="sfflf")

    assert result["available"] is False
    assert "boom" in result["note"]


def test_week_mismatch_returns_explicit_unavailable_not_wrong_data():
    fake = {"period": "3", "my_team_name": "X", "my_points": 1, "opponent": "Y",
            "opponent_id": "2", "opp_points": 2, "record": {"w": 0, "l": 0, "t": 0},
            "home_away": ""}
    with patch("agent.football_matchups.fetch_football_matchup", return_value=fake):
        result = get_matchup_result(auth=None, cbs_league_id="sfflf", week=1)

    assert result["available"] is False
    assert result["period"] == "3"
    assert "period 3" in result["note"]


def test_matching_week_is_available():
    fake = {"period": "3", "my_team_name": "X", "my_points": 1, "opponent": "Y",
            "opponent_id": "2", "opp_points": 2, "record": {"w": 0, "l": 0, "t": 0},
            "home_away": ""}
    with patch("agent.football_matchups.fetch_football_matchup", return_value=fake):
        result = get_matchup_result(auth=None, cbs_league_id="sfflf", week=3)

    assert result["available"] is True
