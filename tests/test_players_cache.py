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
    players_cache._failures.clear()
    # Don't actually sleep through the retry backoff in tests.
    monkeypatch.setattr(players_cache.time, "sleep", lambda _s: None)
    yield
    players_cache._cache.clear()
    players_cache._failures.clear()


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


def _err503():
    return CBSAPIError("players/list: HTTP 503, non-JSON response: ", http_status=503)


def test_503_is_retried_then_raised_as_connector_unavailable():
    # A gateway 503 is CBS being unhealthy, not CBS rejecting the request --
    # it must surface as CBSConnectorUnavailable so waivers.py skips the
    # unvalidated HTML scrape and football_free_agents.py uses its FP fallback.
    auth = _FakeAuth([requests.exceptions.ReadTimeout("t"),
                      requests.exceptions.ReadTimeout("t"), _err503()])
    with pytest.raises(CBSConnectorUnavailable):
        get_players_list(auth, "sfflf", "football")
    assert len(auth.calls) == 3


def test_4xx_json_error_still_not_retried():
    auth = _FakeAuth([CBSAPIError("API status 403", http_status=403)])
    with pytest.raises(CBSAPIError) as ei:
        get_players_list(auth, "sfflf", "football")
    assert not isinstance(ei.value, CBSConnectorUnavailable)
    assert len(auth.calls) == 1


def test_negative_cache_fails_fast_after_total_failure():
    auth = _FakeAuth([_err503()] * 3)
    with pytest.raises(CBSConnectorUnavailable):
        get_players_list(auth, "sfflf", "football")
    with pytest.raises(CBSConnectorUnavailable):
        get_players_list(auth, "sfflf", "football")
    assert len(auth.calls) == 3  # second call made no CBS requests


def test_negative_cache_expires_and_force_refresh_bypasses_it():
    auth = _FakeAuth([_err503()] * 3 + [_resp([{"id": "1"}])])
    with pytest.raises(CBSConnectorUnavailable):
        get_players_list(auth, "sfflf", "football")
    key = ("sfflf", "football")
    failed_at, msg = players_cache._failures[key]
    players_cache._failures[key] = (failed_at - 10_000, msg)
    assert get_players_list(auth, "sfflf", "football") == [{"id": "1"}]
    assert key not in players_cache._failures  # cleared by the success


def test_allow_stale_serves_expired_cache_only_when_opted_in():
    auth = _FakeAuth([_resp([{"id": "1"}])] + [_err503()] * 6)
    get_players_list(auth, "sfflf", "football", ttl_seconds=100)
    key = ("sfflf", "football")
    cached_at, raw = players_cache._cache[key]
    players_cache._cache[key] = (cached_at - 1000, raw)  # expired, not ancient
    with pytest.raises(CBSConnectorUnavailable):  # waivers-style caller
        get_players_list(auth, "sfflf", "football", ttl_seconds=100)
    assert get_players_list(auth, "sfflf", "football", ttl_seconds=100,
                            allow_stale=True) == [{"id": "1"}]  # eligibility-style


def test_stale_beyond_max_age_is_not_served():
    auth = _FakeAuth([_resp([{"id": "1"}])] + [_err503()] * 3)
    get_players_list(auth, "sfflf", "football", ttl_seconds=100)
    key = ("sfflf", "football")
    cached_at, raw = players_cache._cache[key]
    players_cache._cache[key] = (cached_at - players_cache.STALE_MAX_SECONDS - 60, raw)
    with pytest.raises(CBSConnectorUnavailable):
        get_players_list(auth, "sfflf", "football", ttl_seconds=100, allow_stale=True)


def _age_failure(key, seconds=10_000):
    failed_at, msg = players_cache._failures[key]
    players_cache._failures[key] = (failed_at - seconds, msg)


def test_expired_failure_sends_one_short_probe_not_the_full_ladder():
    auth = _FakeAuth([_err503()] * 3 + [_err503()])
    key = ("sfflf", "football")
    with pytest.raises(CBSConnectorUnavailable):
        get_players_list(auth, "sfflf", "football")
    assert len(auth.calls) == 3
    _age_failure(key)
    with pytest.raises(CBSConnectorUnavailable):
        get_players_list(auth, "sfflf", "football")
    assert len(auth.calls) == 4                       # exactly one probe
    assert auth.calls[-1]["timeout"] == players_cache._RETRY_TIMEOUTS_SECONDS[0]
    # the failed probe restarts the fail-fast window
    with pytest.raises(CBSConnectorUnavailable):
        get_players_list(auth, "sfflf", "football")
    assert len(auth.calls) == 4


def test_successful_probe_recovers_and_fills_cache():
    auth = _FakeAuth([_err503()] * 3 + [_resp([{"id": "1"}])])
    key = ("sfflf", "football")
    with pytest.raises(CBSConnectorUnavailable):
        get_players_list(auth, "sfflf", "football")
    _age_failure(key)
    assert get_players_list(auth, "sfflf", "football") == [{"id": "1"}]
    assert key not in players_cache._failures
    assert get_players_list(auth, "sfflf", "football") == [{"id": "1"}]
    assert len(auth.calls) == 4                       # second read was a cache hit


def test_force_refresh_after_expiry_still_runs_the_full_ladder():
    auth = _FakeAuth([_err503()] * 6)
    key = ("sfflf", "football")
    with pytest.raises(CBSConnectorUnavailable):
        get_players_list(auth, "sfflf", "football")
    _age_failure(key)
    with pytest.raises(CBSConnectorUnavailable):
        get_players_list(auth, "sfflf", "football", force_refresh=True)
    assert len(auth.calls) == 6                       # 3 + full ladder of 3
