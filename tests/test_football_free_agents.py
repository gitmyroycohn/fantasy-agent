"""
Tests for agent/football_free_agents.py -- the CBS-first, FantasyPros-
fallback free-agent pool added 2026-09-02 after Christopher hit the
"connector may be down" message live even with cbs/players_cache.py's
retry/backoff in place, and asked whether FantasyPros could stand in.
"""

from unittest.mock import patch

from data.models import Player, RosterSlot, WaiverPlayer
from cbs.players_cache import CBSConnectorUnavailable
import agent.football_free_agents as ffa


class _FakeFPClient:
    def __init__(self, projections):
        self._projections = projections

    def nfl_projections(self, position="ALL", scoring="PPR"):
        return self._projections


def _rs(name, position):
    return RosterSlot(player=Player(id=name, name=name, position=position),
                      slot="STARTER", is_starting=True)


def test_cbs_success_returns_cbs_live_and_skips_fantasypros():
    fake_wire = [WaiverPlayer(player=Player(id="1", name="A QB", position="QB"))]
    with patch.object(ffa, "fetch_waiver_wire", return_value=fake_wire) as mock_fetch, \
         patch.object(ffa, "get_all_team_rosters") as mock_rosters:
        pool, source = ffa.get_football_free_agents(auth=None, league_id="sfflf",
                                                     sport="football", fp_client=object())
    assert source == "cbs_live"
    assert pool == fake_wire
    mock_fetch.assert_called_once()
    mock_rosters.assert_not_called()  # never falls through to the FP path on success


def test_cbs_connector_unavailable_falls_back_to_fantasypros():
    fake_projections = [
        {"name": "Free WR", "position_id": "WR", "team_id": "KC",
         "stats": {"points_ppr": 100.0}},
        {"name": "Rostered WR", "position_id": "WR", "team_id": "SF",
         "stats": {"points_ppr": 90.0}},
    ]
    rosters = {"1": {"name": "Team A", "roster": [_rs("Rostered WR", "WR")]}}

    fp_client = _FakeFPClient(fake_projections)
    with patch.object(ffa, "fetch_waiver_wire",
                      side_effect=CBSConnectorUnavailable("timed out")), \
         patch.object(ffa, "get_all_team_rosters", return_value=rosters):
        pool, source = ffa.get_football_free_agents(
            auth=None, league_id="sfflf", sport="football", fp_client=fp_client)

    assert source == "fantasypros_fallback"
    names = [wp.player.name for wp in pool]
    assert "Free WR" in names
    assert "Rostered WR" not in names  # excluded -- already on a roster
    assert all(wp.ownership_pct == 0.0 for wp in pool)  # honestly unavailable, not guessed


def test_both_sources_failing_reports_unavailable_not_empty_success():
    with patch.object(ffa, "fetch_waiver_wire",
                      side_effect=CBSConnectorUnavailable("timed out")):
        pool, source = ffa.get_football_free_agents(
            auth=None, league_id="sfflf", sport="football", fp_client=None)
    assert pool == []
    assert source == "unavailable"


def test_fantasypros_fallback_refuses_to_guess_without_roster_data():
    # If we can't confirm who's actually rostered, don't present a
    # "free agent" list that might include someone's own starter.
    fake_projections = [{"name": "Some WR", "position_id": "WR", "team_id": "KC",
                        "stats": {"points_ppr": 100.0}}]
    fp_client = _FakeFPClient(fake_projections)
    with patch.object(ffa, "fetch_waiver_wire",
                      side_effect=CBSConnectorUnavailable("timed out")), \
         patch.object(ffa, "get_all_team_rosters", side_effect=RuntimeError("also down")):
        pool, source = ffa.get_football_free_agents(
            auth=None, league_id="sfflf", sport="football", fp_client=fp_client)
    assert pool == []
    assert source == "unavailable"


def test_unexpected_cbs_exception_also_falls_back_rather_than_propagating():
    fake_projections = [{"name": "Free WR", "position_id": "WR", "team_id": "KC",
                        "stats": {"points_ppr": 100.0}}]
    fp_client = _FakeFPClient(fake_projections)
    with patch.object(ffa, "fetch_waiver_wire", side_effect=RuntimeError("weird error")), \
         patch.object(ffa, "get_all_team_rosters", return_value={}):
        pool, source = ffa.get_football_free_agents(
            auth=None, league_id="sfflf", sport="football", fp_client=fp_client)
    assert source == "fantasypros_fallback"
    assert len(pool) == 1
