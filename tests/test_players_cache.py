"""
Unit tests for cbs/players_cache.py -- the shared retry/backoff/TTL cache
wrapper around CBS's players/list endpoint added for the 2026-08-31 football
waiver-recommendations enhancement order (root cause: football's call to
this endpoint has timed out on every real production run while baseball's
identical-shaped call has not -- see that module's docstring).
"""
import time

import pytest
import requests

import cbs.players_cache as players_cache
from cbs.players_cache import get_players_list, CBSConnectorUnavailable
from cbs.auth import CBSAPIError


class _FakeAuth:
    """Stands in for CBSAuth.api_get -- records calls, replays scripted
    responses/exceptions in order."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def api_get(self, endpoint, league_id, sport, timeout=None, **params):
        self.calls.append({"endpoint": endpoint, "league_id": league_id,
                           "sport": sport, "timeout": timeout})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def _clear_cache_and_sleep(monkeypatch):
    players_cache._cache.clear()
    # Don't actually sleep through the retry backoff in tests.
    monkeypatch.setattr(players_cache.time, "sleep", lambda _s: None)
    yield
    players_cache._cache.clear()


def _resp(players):
    return {"body": {"players": players}}


def test_successful_fetch_populates_cache_and_returns_players():
    auth = _FakeAuth([_resp([{"id": "1", "fullname": "Trevor Lawrence"}])])
    raw = get_players_list(auth, "sfflf", "football")
    assert raw == [{"id": "1", "fullname": "Trevor Lawrence"}]
    assert len(auth.calls) == 1


def test_cache_hit_avoids_a_second_cbs_call():
    auth = _FakeAuth([_resp([{"id": "1"}])])
    get_players_list(auth, "sfflf", "football")
    get_players_list(auth, "sfflf", "football")
    assert len(auth.calls) == 1  # second call served from cache


def test_expired_cache_triggers_a_fresh_fetch():
    # Backdate the cached entry directly rather than sleeping through a real
    # TTL -- time.sleep is patched to a no-op for the retry-backoff tests
    # below, and patching it back on just for this one test would be more
    # fragile than just moving the clock ourselves.
    auth = _FakeAuth([_resp([{"id": "1"}]), _resp([{"id": "2"}])])
    get_players_list(auth, "sfflf", "football", ttl_seconds=100)
    key = ("sfflf", "football")
    cached_at, raw = players_cache._cache[key]
    players_cache._cache[key] = (cached_at - 1000, raw)
    raw = get_players_list(auth, "sfflf", "football", ttl_seconds=100)
    assert raw == [{"id": "2"}]
    assert len(auth.calls) == 2


def test_force_refresh_bypasses_a_warm_cache():
    auth = _FakeAuth([_resp([{"id": "1"}]), _resp([{"id": "2"}])])
    get_players_list(auth, "sfflf", "football")
    raw = get_players_list(auth, "sfflf", "football", force_refresh=True)
    assert raw == [{"id": "2"}]
    assert len(auth.calls) == 2


def test_transient_timeout_recovers_on_retry():
    auth = _FakeAuth([
        requests.exceptions.ReadTimeout("timed out"),
        _resp([{"id": "1"}]),
    ])
    raw = get_players_list(auth, "sfflf", "football")
    assert raw == [{"id": "1"}]
    assert len(auth.calls) == 2
    # second attempt should use a longer timeout ceiling than the first
    assert auth.calls[1]["timeout"] > auth.calls[0]["timeout"]


def test_every_attempt_timing_out_raises_connector_unavailable():
    auth = _FakeAuth([
        requests.exceptions.ReadTimeout("t1"),
        requests.exceptions.ReadTimeout("t2"),
        requests.exceptions.ReadTimeout("t3"),
    ])
    with pytest.raises(CBSConnectorUnavailable):
        get_players_list(auth, "sfflf", "football")
    assert len(auth.calls) == 3  # never hangs past the last retry


def test_connector_unavailable_is_a_cbs_api_error_subclass():
    # so existing `except CBSAPIError` call sites (cbs/players.py) still
    # catch it without needing their own special-casing.
    assert issubclass(CBSConnectorUnavailable, CBSAPIError)


def test_explicit_cbs_error_is_not_retried():
    auth = _FakeAuth([CBSAPIError("bad token")])
    with pytest.raises(CBSAPIError):
        get_players_list(auth, "sfflf", "football")
    assert len(auth.calls) == 1  # a real API error isn't a connectivity problem
