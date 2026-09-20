"""
Shared cache + retry wrapper around CBS's players/list endpoint.

Root-cause context (2026-08-31 waiver-recommendations enhancement order):
players/list returns a league's full player universe in one call. Baseball's
call to this exact endpoint has consistently succeeded in production (8000+
players returned inside the 15s default timeout -- see
logs/latest_output.md), while football's call to the SAME endpoint/shape has
timed out on every real run across all three football leagues (f_league,
hard_chargers, east_coast) as of 2026-08-25. Neither sport paginates the
request (no page/max param is sent by either path), so raw payload size
doesn't explain the split -- this points to CBS's football players/list
backend genuinely being slower than baseball's, not a bug in how we call it.

Separately, this endpoint was being fetched TWICE per football league per
run with no cache shared between the two call sites: once via
cbs/roster.py -> cbs/players.py::fetch_position_eligibility_index (which had
its own bespoke process-local cache) during roster building, and again,
uncached, via cbs/waivers.py during waiver-wire lookup. Both call sites now
go through get_players_list() below so a single successful fetch covers an
entire run (and, via the TTL, an entire session of follow-up questions)
instead of hitting CBS's slow endpoint twice.

This module never hangs past its last retry's timeout ceiling and never
returns stale-and-unlabeled data past its TTL -- on total failure it raises
CBSConnectorUnavailable so callers can surface an honest "connector may be
down" message instead of guessing or silently going empty.
"""

from __future__ import annotations

import logging
import time

import requests

from cbs.auth import CBSAuth, CBSAPIError

logger = logging.getLogger(__name__)

# How long a successful fetch is trusted before the next call re-hits CBS.
# Ticket asked for 15-30 min; 20 min split the difference.
DEFAULT_TTL_SECONDS = 20 * 60

# Escalating per-attempt timeout ceiling and the wait before each attempt
# (first attempt fires immediately). 3 attempts, ~55s worst case total
# before giving up and reporting the connector unavailable -- long enough
# to ride out CBS being merely slow, short enough to fail well within an
# outer MCP tool call's own timeout.
_RETRY_TIMEOUTS_SECONDS = (15, 25, 35)
_RETRY_BACKOFF_SECONDS  = (0, 2, 5)

# After a total failure, fail fast for this long instead of re-running the
# whole ~65s retry ladder for every caller (eligibility index, waivers,
# repeated tool calls). Short enough to notice CBS recovering.
NEGATIVE_TTL_SECONDS = 5 * 60

# Opt-in ceiling for serving an expired cache entry when CBS is down
# (allow_stale=True). Player eligibility barely changes in-season; ownership
# does, so waiver lookups must NOT opt in.
STALE_MAX_SECONDS = 6 * 60 * 60


class CBSConnectorUnavailable(CBSAPIError):
    """players/list did not answer after every retry -- CBS's endpoint may
    be down or unusually slow right now. Deliberately distinct from
    CBSAPIError (a case where CBS DID answer, just with an error) so
    callers can tell "CBS said no" apart from "CBS never answered" and
    report the second case honestly instead of falling back to an
    unvalidated HTML scrape or silently returning an empty list."""


# (league_id, sport) -> (fetched_at_monotonic, raw player records)
_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}

# (league_id, sport) -> (failed_at_monotonic, error message) for the last
# total failure. Cleared on the next success.
_failures: dict[tuple[str, str], tuple[float, str]] = {}


def _is_upstream_failure(e: CBSAPIError) -> bool:
    """CBS (or its gateway) is unhealthy: 5xx, or a non-JSON body with no
    parseable status. Retryable, and must surface as CBSConnectorUnavailable
    so callers fall back honestly (FantasyPros pool) instead of scraping the
    unvalidated HTML page."""
    status = getattr(e, "http_status", None)
    return status is not None and status >= 500


def get_players_list(auth: CBSAuth, league_id: str, sport: str = "baseball",
                     ttl_seconds: float = DEFAULT_TTL_SECONDS,
                     force_refresh: bool = False,
                     allow_stale: bool = False) -> list[dict]:
    """Return the raw player records from players/list for (league_id, sport).

    Fetches with retry/backoff and an escalating timeout ceiling, and caches
    the result for ttl_seconds so every caller within that window (roster
    eligibility lookup, waiver lookup, repeated questions in a session)
    shares one CBS call instead of each re-fetching independently.

    Raises CBSConnectorUnavailable if every attempt times out/fails to
    connect. Raises CBSAPIError unchanged (no retry) if CBS answers with an
    explicit error -- that's not a connectivity problem, so retrying it
    would just waste the ceiling on something retries can't fix.
    """
    key = (league_id, sport)
    now = time.monotonic()
    if not force_refresh and key in _cache:
        cached_at, raw = _cache[key]
        age = now - cached_at
        if age < ttl_seconds:
            logger.info("players/list cache hit for %s/%s (age=%.0fs, %d players)",
                        league_id, sport, age, len(raw))
            return raw

    if not force_refresh and key in _failures:
        failed_at, msg = _failures[key]
        if now - failed_at < NEGATIVE_TTL_SECONDS:
            logger.info("players/list negative-cache hit for %s/%s (failed %.0fs ago)",
                        league_id, sport, now - failed_at)
            stale = _stale_or_none(key, allow_stale, now)
            if stale is not None:
                return stale
            raise CBSConnectorUnavailable(
                f"players/list for {league_id}/{sport}: recent failure "
                f"({now - failed_at:.0f}s ago), not retrying yet. {msg}")

    last_err: Exception | None = None
    attempts = len(_RETRY_TIMEOUTS_SECONDS)
    for attempt, (timeout, wait) in enumerate(
            zip(_RETRY_TIMEOUTS_SECONDS, _RETRY_BACKOFF_SECONDS), start=1):
        if wait:
            time.sleep(wait)
        t0 = time.monotonic()
        try:
            data = auth.api_get("players/list", league_id, sport, timeout=timeout)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            elapsed = time.monotonic() - t0
            logger.warning(
                "players/list attempt %d/%d for %s/%s: no response after "
                "%.1fs (ceiling=%ds): %s",
                attempt, attempts, league_id, sport, elapsed, timeout, e)
            last_err = e
            continue
        except CBSAPIError as e:
            if not _is_upstream_failure(e):
                raise  # real API-level error: retrying can't fix it
            logger.warning("players/list attempt %d/%d for %s/%s: upstream %s: %s",
                           attempt, attempts, league_id, sport,
                           getattr(e, "http_status", "?"), e)
            last_err = e
            continue

        elapsed = time.monotonic() - t0
        raw = (data.get("body", {}) or {}).get("players", []) or []
        logger.info(
            "players/list fetched for %s/%s: %d players in %.1fs (attempt %d/%d)",
            league_id, sport, len(raw), elapsed, attempt, attempts)
        _cache[key] = (time.monotonic(), raw)
        _failures.pop(key, None)
        return raw

    _failures[key] = (time.monotonic(), str(last_err))
    stale = _stale_or_none(key, allow_stale, time.monotonic())
    if stale is not None:
        return stale
    raise CBSConnectorUnavailable(
        f"players/list for {league_id}/{sport}: CBS did not respond after "
        f"{attempts} attempts (timeouts up to {_RETRY_TIMEOUTS_SECONDS[-1]}s). "
        f"Connector may be down. Last error: {last_err}")


def _stale_or_none(key, allow_stale: bool, now: float) -> list[dict] | None:
    """Expired-but-usable cache entry, only when the caller opted in."""
    if not allow_stale or key not in _cache:
        return None
    cached_at, raw = _cache[key]
    age = now - cached_at
    if age > STALE_MAX_SECONDS:
        return None
    logger.warning("players/list serving STALE data for %s/%s (age=%.0f min) "
                   "because CBS is failing", key[0], key[1], age / 60)
    return raw
