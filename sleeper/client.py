"""
Sleeper API client -- free, read-only, no API key.

Added for the football lineup-data enhancement order (filed 2026-09-16):
FantasyPros' player-points endpoint (fantasypros/client.py::nfl_player_points)
turned out to be points-only, not raw per-category stats (confirmed live via
fp_nfl_stats_probe.py, 2026-09-17) -- f_league's tiered, non-PPR, position-
dependent scoring (sports/football/scoring.py::score_sfflf()) needs raw stat
categories to re-score real weekly performance, which FantasyPros can't
supply here. Sleeper's weekly stats endpoint has exactly that: confirmed
live 2026-09-17 (sleeper_nfl_stats_probe.py, sleeper_qb_stats_probe.py)
against real rostered players across all three of Christopher's leagues
(RB/TE via Jonathan Taylor/Saquon Barkley/Brock Bowers, QB via Drake
Maye/Dak Prescott/Justin Herbert) -- see the football-stats-feed-scoping
project memory for the full probe results.

Two endpoints used, both public/unauthenticated:
  GET /players/nfl                       -- full player index (~14MB,
                                             fetch at most once/day per
                                             Sleeper's own guidance)
  GET /stats/nfl/regular/{season}/{week} -- one week's raw per-player
                                             stats, keyed by Sleeper's own
                                             player_id (NOT the same ID
                                             system as CBS or FantasyPros --
                                             matched by normalized
                                             full_name, see find_player_id())
  GET /state/nfl                         -- current season/week pointer,
                                             tiny/cheap, no caching needed

UNDOCUMENTED ENDPOINT CAVEAT: the weekly-stats endpoint isn't in Sleeper's
official docs (which only mention stats appearing inside league/roster
matchup data) -- it's a widely-used, reliable-but-unofficial shape.
Confirmed working 2026-09-17; if Sleeper ever changes or removes it,
get_weekly_stats() raises SleeperUnavailable the same as any other fetch
failure, so callers degrade the same honest way as a CBS/FantasyPros
outage.

Same retry/cache discipline as cbs/players_cache.py (mirrored deliberately):
escalating timeout ceiling across a few attempts, then an explicit
"unavailable" exception rather than hanging or silently returning stale
data past its TTL.
"""

from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)

BASE = "https://api.sleeper.app/v1"

_RETRY_TIMEOUTS_SECONDS = (15, 25, 35)
_RETRY_BACKOFF_SECONDS = (0, 2, 5)

# Sleeper's own guidance: the ~14MB player index should be fetched "at most
# once a day". Weekly stats get a much shorter TTL -- a week still in
# progress (games ongoing) has genuinely changing numbers; this is a
# compromise between "don't hammer Sleeper every call" and "don't serve a
# stale in-progress score too long". Once a week is final the data stops
# changing regardless of TTL, so a cache hit past that point is never
# wrong, just an occasional needless re-fetch.
DEFAULT_PLAYERS_TTL_SECONDS = 24 * 60 * 60
DEFAULT_STATS_TTL_SECONDS = 15 * 60


class SleeperUnavailable(Exception):
    """Sleeper did not answer after every retry -- may be down or unusually
    slow right now. Distinct from a plain request exception so callers can
    degrade honestly (same pattern as cbs.players_cache.CBSConnectorUnavailable)
    instead of silently returning nothing or stale data."""


# Process-wide caches -- same shape/intent as cbs/players_cache.py's module
# cache, just two of them (one per endpoint).
_players_cache: tuple[float, dict] | None = None
_stats_cache: dict[tuple[int, int], tuple[float, dict]] = {}


def _get(path: str):
    """GET one Sleeper endpoint with the same escalating-timeout retry
    ladder as cbs/players_cache.py. Raises SleeperUnavailable if every
    attempt fails to connect/times out or returns an HTTP error."""
    url = f"{BASE}{path}"
    last_err: Exception | None = None
    attempts = len(_RETRY_TIMEOUTS_SECONDS)
    for attempt, (timeout, wait) in enumerate(
            zip(_RETRY_TIMEOUTS_SECONDS, _RETRY_BACKOFF_SECONDS), start=1):
        if wait:
            time.sleep(wait)
        t0 = time.monotonic()
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            elapsed = time.monotonic() - t0
            logger.info("Sleeper %s: HTTP %s in %.1fs (attempt %d/%d)",
                       path, r.status_code, elapsed, attempt, attempts)
            return r.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                requests.exceptions.HTTPError) as e:
            elapsed = time.monotonic() - t0
            logger.warning(
                "Sleeper %s attempt %d/%d: failed after %.1fs (ceiling=%ds): %s",
                path, attempt, attempts, elapsed, timeout, e)
            last_err = e
            continue

    raise SleeperUnavailable(
        f"Sleeper {path}: did not respond after {attempts} attempts "
        f"(timeouts up to {_RETRY_TIMEOUTS_SECONDS[-1]}s). Last error: {last_err}")


def get_players(ttl_seconds: float = DEFAULT_PLAYERS_TTL_SECONDS,
                force_refresh: bool = False) -> dict[str, dict]:
    """Full Sleeper NFL player index, keyed by Sleeper player_id. Cached
    process-wide for ttl_seconds (default 24h, per Sleeper's own "at most
    once a day" guidance for this ~14MB endpoint)."""
    global _players_cache
    now = time.monotonic()
    if not force_refresh and _players_cache is not None:
        cached_at, data = _players_cache
        if now - cached_at < ttl_seconds:
            return data
    data = _get("/players/nfl")
    if not isinstance(data, dict):
        raise SleeperUnavailable(f"Sleeper /players/nfl returned unexpected shape: {type(data)}")
    _players_cache = (now, data)
    return data


def get_weekly_stats(season: int, week: int,
                     ttl_seconds: float = DEFAULT_STATS_TTL_SECONDS,
                     force_refresh: bool = False) -> dict[str, dict]:
    """One week's raw per-player stats, keyed by Sleeper player_id.
    Confirmed shape 2026-09-17 via sleeper_nfl_stats_probe.py /
    sleeper_qb_stats_probe.py -- see this module's docstring."""
    key = (season, week)
    now = time.monotonic()
    if not force_refresh and key in _stats_cache:
        cached_at, data = _stats_cache[key]
        if now - cached_at < ttl_seconds:
            return data
    data = _get(f"/stats/nfl/regular/{season}/{week}")
    if not isinstance(data, dict):
        raise SleeperUnavailable(f"Sleeper weekly stats returned unexpected shape: {type(data)}")
    _stats_cache[key] = (now, data)
    return data


def get_state() -> dict:
    """Sleeper's own current-week/season pointer -- no cache (tiny, cheap
    call). Used as a source for "what NFL week is it right now" (see
    mcp_server.py::_current_nfl_week())."""
    data = _get("/state/nfl")
    if not isinstance(data, dict):
        raise SleeperUnavailable(f"Sleeper /state/nfl returned unexpected shape: {type(data)}")
    return data


def _norm(name: str) -> str:
    return name.strip().lower()


def build_name_index(players: dict[str, dict]) -> dict[str, str]:
    """{normalized_full_name: sleeper_player_id}, built from get_players()'s
    result. Exact-match only -- see find_player_id()'s docstring for the
    fuzzy-matching gap this leaves."""
    index: dict[str, str] = {}
    for pid, p in players.items():
        full = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        if full:
            index[_norm(full)] = pid
    return index


def find_player_id(name_index: dict[str, str], name: str) -> str | None:
    """Look up a Sleeper player_id by a CBS/roster player name.

    Exact normalized-full-name match only -- the same lookup that hit 6/6
    real test players in the 2026-09-17 probe scripts, but that was 6
    players, not a full ~19-27 player roster. A name Sleeper spells
    differently (suffix handling, nicknames, punctuation) returns None
    here rather than guess -- callers should treat None as "no Sleeper
    data for this player this run", not an error, same safe-degrade
    pattern as the rest of this codebase. Team-defense (DST) entries
    generally won't match here either (Sleeper indexes those under a team
    name/abbreviation, not a person's full_name, and that mapping has
    never been probed live -- see sports/football/actuals.py's module
    docstring on why K/DST scoring isn't supported yet)."""
    return name_index.get(_norm(name))
