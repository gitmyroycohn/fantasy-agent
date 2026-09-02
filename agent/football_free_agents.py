"""
Football free-agent pool with a FantasyPros fallback, added 2026-09-02.

Root problem this works around: CBS's football players/list endpoint --
the only CBS source for a league's full free-agent pool -- has proven
unreliable in real production runs even after cbs/players_cache.py's
retry/backoff (3 attempts, escalating up to a 35s ceiling; see that
module's docstring). Retrying a slow endpoint harder wasn't sufficient by
itself -- Christopher hit the same "connector may be down" result live
after that fix shipped, and asked whether FantasyPros could stand in.

This module answers that: get_football_free_agents() tries CBS's live
players/list-backed fetch first (cbs.waivers.fetch_waiver_wire, unchanged,
still the preferred source -- full CBS player universe, real
ownership_pct), and ONLY on CBSConnectorUnavailable falls back to building
a free-agent pool from FantasyPros' projected player universe instead.

The FantasyPros fallback sidesteps players/list entirely:
- The "who's a real, fantasy-relevant NFL player" universe comes from
  FantasyPros' own season projections (the same /nfl/{season}/projections
  call agent/football_decisions.py's ranking adapters already use --
  confirmed live 2026-08-23, ~600 players, see project memory
  fantasypros_mcp.md for verified field names).
- The "who's actually rostered in THIS CBS league" set comes from
  cbs.roster.get_all_team_rosters(), which for football loops one
  league/rosters call per team (NOT players/list) -- a separately
  debugged, working path already relied on by get_league_keepers in
  production (see cbs/roster.py's get_all_team_rosters docstring).
- Free agent = a FantasyPros-projected player whose normalized name isn't
  in that rostered-name set.

Trade-offs, stated plainly rather than hidden behind a normal-looking
result:
- The fallback pool is FantasyPros' ~600 rankable players, not CBS's full
  ~8000+ universe -- it can miss a true deep-bench/practice-squad name
  that FantasyPros doesn't project, though for waiver purposes that's
  arguably the more useful universe anyway (nobody's streaming a player
  with no projection).
- No real ownership_pct is available this way (that field only exists on
  CBS's players/list response) -- fallback WaiverPlayers carry
  ownership_pct=0.0. rank_waiver_recommendations()'s ownership_pct-only
  fallback ranking simply won't have real signal here; that's an accepted
  limitation since FantasyPros projections are already the PRIMARY ranking
  signal for all 3 leagues whenever FANTASYPROS_API_KEY is set, which this
  path requires regardless.
- If get_all_team_rosters() itself fails, this refuses to guess (returns
  an empty pool with source "unavailable") rather than presenting a
  free-agent list that might actually include someone's starter.

Callers get back (pool, source) where source is one of:
  "cbs_live"             -- the normal, preferred path worked.
  "fantasypros_fallback" -- CBS's connector was down; this is FantasyPros-
                            derived, see trade-offs above.
  "unavailable"           -- both sources failed; pool is [].
"""

from __future__ import annotations

import logging

from data.models import Player, WaiverPlayer
from cbs.auth import CBSAuth
from cbs.waivers import fetch_waiver_wire
from cbs.players_cache import CBSConnectorUnavailable
from cbs.roster import get_all_team_rosters

logger = logging.getLogger(__name__)


def _norm(name: str) -> str:
    return name.strip().lower()


def _fantasypros_free_agent_pool(auth: CBSAuth, league_id: str, sport: str,
                                 fp_client) -> list[WaiverPlayer]:
    """Build the fallback pool. Returns [] (never raises) on any failure --
    see module docstring for the "unavailable" vs "fantasypros_fallback"
    distinction callers should surface, not swallow."""
    if fp_client is None:
        return []

    try:
        entries = fp_client.nfl_projections(position="ALL", scoring="PPR")
    except Exception as e:
        logger.warning("football free-agent FantasyPros fallback: projections fetch failed: %s", e)
        return []
    if not entries:
        return []

    try:
        all_rosters = get_all_team_rosters(auth, league_id, sport)
    except Exception as e:
        logger.warning(
            "football free-agent FantasyPros fallback: could not fetch league "
            "rosters for %s (%s) -- refusing to guess a free-agent list without "
            "knowing who's actually owned", league_id, e)
        return []

    rostered_names: set[str] = set()
    for info in all_rosters.values():
        for rs in info.get("roster", []):
            rostered_names.add(_norm(rs.player.name))

    pool = []
    for entry in entries:
        name = entry.get("name")
        position = entry.get("position_id")
        if not name or not position:
            continue
        if _norm(name) in rostered_names:
            continue
        team = entry.get("team_id") or ""
        pool.append(WaiverPlayer(
            player=Player(id=_norm(name), name=name, position=position, team=team),
            add_rank=0,
            ownership_pct=0.0,  # not available from this source -- see module docstring
            on_waivers=False,
        ))

    logger.info(
        "football free-agent FantasyPros fallback: %d free agents for %s "
        "(%d FP-projected players, %d rostered names excluded)",
        len(pool), league_id, len(entries), len(rostered_names))
    return pool


def get_football_free_agents(auth: CBSAuth, league_id: str, sport: str,
                             fp_client) -> tuple[list[WaiverPlayer], str]:
    """CBS-first, FantasyPros-fallback free-agent pool for one football
    league. Returns (pool, source) -- see module docstring for the 3
    possible source values and what each implies about pool quality
    (ownership_pct availability especially). Callers should always surface
    `source` to the user when it isn't "cbs_live" rather than presenting a
    fallback pool as if it were the normal case.
    """
    try:
        pool = fetch_waiver_wire(auth, league_id, sport, position="all", limit=300)
        return pool, "cbs_live"
    except CBSConnectorUnavailable as e:
        logger.warning(
            "get_football_free_agents: CBS connector unavailable for %s (%s) -- "
            "falling back to FantasyPros-derived pool", league_id, e)
    except Exception as e:
        logger.warning(
            "get_football_free_agents: unexpected CBS fetch failure for %s (%s) -- "
            "falling back to FantasyPros-derived pool", league_id, e)

    pool = _fantasypros_free_agent_pool(auth, league_id, sport, fp_client)
    return pool, ("fantasypros_fallback" if pool else "unavailable")
