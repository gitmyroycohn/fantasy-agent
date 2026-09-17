"""
Tests for sleeper/client.py -- the retry/cache discipline mirrored from
cbs/players_cache.py, plus the name-matching helpers actuals/injuries
depend on.

requests.get is mocked throughout -- there is no live network access in
CI/test runs (same reasoning as tests/test_football_decisions.py mocking
cbs.waivers.fetch_waiver_wire).
"""

import requests
from unittest.mock import patch, MagicMock

import pytest

import sleeper.client as sc


def _ok_response(payload):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def setup_function(_):
    # Every test starts from a clean process-wide cache -- these are module
    # globals, same caveat as cbs/players_cache.py's own module cache.
    sc._players_cache = None
    sc._stats_cache = {}


# ---------------------------------------------------------------------------
# _get() retry ladder
# ---------------------------------------------------------------------------

def test_get_succeeds_on_first_attempt_without_sleeping():
    with patch("sleeper.client.requests.get", return_value=_ok_response({"a": 1})) as mock_get, \
         patch("sleeper.client.time.sleep") as mock_sleep:
        result = sc._get("/state/nfl")
    assert result == {"a": 1}
    mock_get.assert_called_once()
    mock_sleep.assert_not_called()  # first attempt has 0 backoff


def test_get_retries_then_succeeds():
    with patch("sleeper.client.requests.get",
              side_effect=[requests.exceptions.ConnectionError("down"), _ok_response({"ok": True})]) as mock_get, \
         patch("sleeper.client.time.sleep"):
        result = sc._get("/state/nfl")
    assert result == {"ok": True}
    assert mock_get.call_count == 2


def test_get_raises_sleeper_unavailable_after_all_retries_fail():
    with patch("sleeper.client.requests.get",
              side_effect=requests.exceptions.Timeout("slow")) as mock_get, \
         patch("sleeper.client.time.sleep"):
        with pytest.raises(sc.SleeperUnavailable):
            sc._get("/state/nfl")
    assert mock_get.call_count == len(sc._RETRY_TIMEOUTS_SECONDS)


# ---------------------------------------------------------------------------
# get_players() / get_weekly_stats() caching
# ---------------------------------------------------------------------------

def test_get_players_caches_within_ttl():
    payload = {"123": {"full_name": "Test Player"}}
    with patch("sleeper.client.requests.get", return_value=_ok_response(payload)) as mock_get, \
         patch("sleeper.client.time.sleep"):
        first = sc.get_players()
        second = sc.get_players()
    assert first == payload
    assert second == payload
    mock_get.assert_called_once()  # second call hit the cache


def test_get_players_force_refresh_bypasses_cache():
    payload = {"123": {"full_name": "Test Player"}}
    with patch("sleeper.client.requests.get", return_value=_ok_response(payload)) as mock_get, \
         patch("sleeper.client.time.sleep"):
        sc.get_players()
        sc.get_players(force_refresh=True)
    assert mock_get.call_count == 2


def test_get_weekly_stats_cache_keyed_by_season_and_week():
    week2 = {"123": {"pts": 10}}
    week3 = {"123": {"pts": 20}}
    with patch("sleeper.client.requests.get", side_effect=[_ok_response(week2), _ok_response(week3)]), \
         patch("sleeper.client.time.sleep"):
        got2 = sc.get_weekly_stats(2026, 2)
        got3 = sc.get_weekly_stats(2026, 3)
    assert got2 == week2
    assert got3 == week3


def test_get_players_raises_on_unexpected_shape():
    with patch("sleeper.client.requests.get", return_value=_ok_response(["not", "a", "dict"])), \
         patch("sleeper.client.time.sleep"):
        with pytest.raises(sc.SleeperUnavailable):
            sc.get_players()


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------

def test_build_name_index_uses_full_name_when_present():
    players = {"1": {"full_name": "Jonathan Taylor"}}
    index = sc.build_name_index(players)
    assert index == {"jonathan taylor": "1"}


def test_build_name_index_falls_back_to_first_last_name():
    players = {"1": {"first_name": "Drake", "last_name": "Maye"}}
    index = sc.build_name_index(players)
    assert index == {"drake maye": "1"}


def test_find_player_id_exact_match_only():
    index = {"saquon barkley": "2"}
    assert sc.find_player_id(index, "Saquon Barkley") == "2"
    assert sc.find_player_id(index, "  SAQUON BARKLEY  ") == "2"
    assert sc.find_player_id(index, "Saquon Barkley Jr.") is None
