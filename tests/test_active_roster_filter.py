"""
Regression test for the "optioned/demoted players surfacing as waiver adds"
bug (found 2026-08-15): Kevin Alcantara (CHC) was recommended as the #1
waiver add in both leagues on a live run, 11 days after being optioned to
Triple-A Iowa on 8/4. The waiver pool builder scored him purely off
season-long MLB stats and never checked whether he was still on an active
MLB roster.

agent.decisions._filter_active_roster() is the fix: it cross-checks each
waiver candidate's normalized name against mlb.injuries.fetch_active_roster_
names() (the current 26-man active-roster set from the MLB Stats API) and
drops anyone not currently on an active roster.
"""
from unittest.mock import patch

from agent.decisions import _filter_active_roster
from data.models import Player, WaiverPlayer


def _wp(name, team="CHC", position="OF"):
    return WaiverPlayer(player=Player(id=name, name=name, position=position, team=team))


def test_optioned_player_is_removed_from_pool():
    """Alcantara has real season stats but was optioned -- must be dropped."""
    pool = [_wp("Kevin Alcantara"), _wp("Some Active Guy")]
    with patch("agent.decisions.fetch_active_roster_names",
               return_value=frozenset({"someactiveguy"})):
        result = _filter_active_roster(pool, "test")
    names = {wp.player.name for wp in result}
    assert names == {"Some Active Guy"}
    assert "Kevin Alcantara" not in names


def test_active_players_all_pass_through():
    pool = [_wp("Player One"), _wp("Player Two")]
    with patch("agent.decisions.fetch_active_roster_names",
               return_value=frozenset({"playerone", "playertwo"})):
        result = _filter_active_roster(pool, "test")
    assert len(result) == 2


def test_empty_active_set_skips_filter_instead_of_wiping_pool():
    """If the MLB Stats API call fails and returns {}, don't nuke the whole
    waiver pool -- fail open, not closed."""
    pool = [_wp("Player One"), _wp("Player Two")]
    with patch("agent.decisions.fetch_active_roster_names",
               return_value=frozenset()):
        result = _filter_active_roster(pool, "test")
    assert len(result) == 2


def test_active_roster_lookup_exception_skips_filter():
    pool = [_wp("Player One")]
    with patch("agent.decisions.fetch_active_roster_names",
               side_effect=RuntimeError("MLB API down")):
        result = _filter_active_roster(pool, "test")
    assert len(result) == 1


def test_empty_pool_returns_empty_without_calling_api():
    with patch("agent.decisions.fetch_active_roster_names") as mock_fetch:
        result = _filter_active_roster([], "test")
    assert result == []
    mock_fetch.assert_not_called()
