"""
Full CBS player position-eligibility lookup (ENH 2 fix).

league/rosters' `position` field (consumed in cbs/roster.py) reflects only a
player's CURRENT roster slot -- Phase B derived Player.eligible_positions
from that field, so a 2B/SS-eligible player rostered at 2B today only
surfaced as 2B-eligible, and the lineup optimizer could never propose moving
him to SS.

players/list -- already validated in cbs/waivers.py._available_from_api --
returns each player's full CBS position-eligibility string (e.g. "2B/SS"),
which is exactly what free-agent Player objects already use for
eligible_positions via data.models.Player. This module builds an
{player_id: [positions]} index from players/list so roster-building code
(cbs/roster.py) can populate the real full eligibility for ROSTERED players
too, instead of falling back to the current slot tag.

NOTE: this has not been validated against a live CBS response in this
environment (no CBS_COOKIE available to the agent that wrote it). It mirrors
the already-validated players/list `position` field usage in cbs/waivers.py.
If a live run shows this index doesn't actually carry full eligibility for
rostered players (e.g. CBS returns a different shape for owned vs. free
players), extend cbs_probe.py to confirm before trusting it further.
"""

import logging

from cbs.auth import CBSAuth, CBSAPIError

logger = logging.getLogger(__name__)

# CBS tags outfielders as LF/CF/RF -- normalize to OF, same convention as
# data/models.py's _CBS_OF_MAP and cbs/waivers.py's _CBS_OF_NORM.
_CBS_OF_MAP = {"LF": "OF", "CF": "OF", "RF": "OF"}

# Process-local cache: (league_id, sport) -> {player_id: [positions]}.
# players/list returns the league's full ~8000+ player universe in one call
# (see cbs/waivers.py) -- caching avoids re-fetching it once per roster call
# within the same run (my roster + every opponent roster for trade scans).
_cache: dict[tuple, dict[str, list[str]]] = {}


def _norm_pos(p: str) -> str:
    return _CBS_OF_MAP.get(p.strip().upper(), p.strip().upper())


def fetch_position_eligibility_index(auth: CBSAuth, league_id: str,
                                     sport: str = "baseball") -> dict[str, list[str]]:
    """Return {player_id: [eligible_positions, CBS-OF-normalized]} for every
    player in the league's player universe (players/list).

    Returns {} (never raises) on API failure -- callers should treat a
    missing index entry as "fall back to the player's current roster slot",
    exactly like before ENH 2.
    """
    key = (league_id, sport)
    if key in _cache:
        return _cache[key]

    try:
        data = auth.api_get("players/list", league_id, sport)
    except CBSAPIError as e:
        logger.warning(
            "fetch_position_eligibility_index: players/list failed for %s (%s) -- "
            "roster players will fall back to their current slot tag only",
            league_id, e,
        )
        return {}

    raw = (data.get("body", {}) or {}).get("players", []) or []
    index: dict[str, list[str]] = {}
    for p in raw:
        pid = str(p.get("id", ""))
        if not pid:
            continue
        pos_str = p.get("position", "")
        positions = []
        seen = set()
        for tok in pos_str.split("/"):
            tok = tok.strip()
            if not tok:
                continue
            mapped = _norm_pos(tok)
            if mapped not in seen:
                seen.add(mapped)
                positions.append(mapped)
        if positions:
            index[pid] = positions

    logger.info("Position eligibility index: %d players indexed from %s players/list",
               len(index), league_id)
    _cache[key] = index
    return index


def clear_cache() -> None:
    """Drop the cached index (useful in tests / long-lived processes)."""
    _cache.clear()


def apply_league_eligibility_rules(positions: list[str], league_cfg: dict) -> list[str]:
    """Apply per-league eligibility overrides from config/leagues.yaml's
    `eligibility:` block on top of a player's CBS-derived positions.

    Currently: pins_and_pills declares all_players_dh_eligible: true (every
    rostered player is DH-eligible in that league's settings), so DH is added
    to every player's list regardless of what CBS returned. This is a static
    per-league rule, not something that needs games-played data -- CBS's own
    players/list eligibility (via fetch_position_eligibility_index) is
    already the authoritative source for the games-played thresholds
    documented in leagues.yaml's eligibility block, since those thresholds
    are the league's actual CBS settings.
    """
    elig_cfg = (league_cfg or {}).get("eligibility") or {}
    result = list(positions)
    if elig_cfg.get("all_players_dh_eligible") and "DH" not in result:
        result = result + ["DH"]
    return result
