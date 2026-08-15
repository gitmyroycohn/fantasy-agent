"""
MLB Stats API integration (statsapi.mlb.com).
Free, no auth required. Fetches current-season stats for all players.

Pitching stats populated: ERA, WHIP, K9, W, SV, HLD, K, QS, INNdGS, K_BB, IP, GS, G
Hitting stats populated:  AVG, OPS, HR, R, RBI, SB, TB, XBH, H, AB, G

Usage:
    from mlb.stats import enrich_roster, enrich_players
    enrich_roster(roster_slots, season=2026)
    enrich_players(waiver_players, season=2026)

Both functions work in-place: they set player.stats on each Player object.
Stats are cached per process — only one API call per group per season.
"""

import re
import logging
import requests
from functools import lru_cache

from mlb.teams import mlb_to_cbs, norm_name

logger = logging.getLogger(__name__)

MLB_API = "https://statsapi.mlb.com/api/v1"
TIMEOUT = 20

# Pitcher positions as CBS encodes them
_PITCHER_POSITIONS = {"SP", "RP", "P"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enrich_roster(slots: list, season: int = 2026) -> None:
    """Add MLB season stats to player.stats for each RosterSlot, in-place."""
    p_db = _fetch_stats("pitching", season)
    h_db = _fetch_stats("hitting", season)
    found = 0
    for slot in slots:
        stats = _lookup(slot.player, p_db, h_db)
        if stats:
            slot.player.stats = stats
            found += 1
    logger.info("enrich_roster: %d/%d players enriched", found, len(slots))


def enrich_players(waiver_players: list, season: int = 2026) -> None:
    """Add MLB season stats to player.stats for each WaiverPlayer, in-place."""
    p_db = _fetch_stats("pitching", season)
    h_db = _fetch_stats("hitting", season)
    found = 0
    for wp in waiver_players:
        stats = _lookup(wp.player, p_db, h_db)
        if stats:
            wp.player.stats = stats
            found += 1
    logger.info("enrich_players: %d/%d players enriched", found, len(waiver_players))


# ---------------------------------------------------------------------------
# Internal: fetch and cache
# ---------------------------------------------------------------------------

@lru_cache(maxsize=8)
def _fetch_stats(group: str, season: int) -> dict[str, dict]:
    """Fetch all player stats for one group from the MLB Stats API.

    Returns a lookup dict keyed two ways per player for flexible matching:
      - "{norm_name}_{team_lower}"  (preferred: name + CBS team abbrev)
      - "{norm_name}"               (fallback: name only -- ONLY when the
        normalized name is unambiguous this season, i.e. maps to exactly
        one team; see bug-fix note below)

    Bug fix (2026-08-15, P1 -- same risk class as the Franklin Arias
    incident in mlb.lineups.lineup_status_for(), 2026-08-13): the name-only
    fallback used to be populated unconditionally via `result.setdefault(norm,
    parsed)` -- "first name-collision writer wins" (the old comment said
    "last writer wins", which was itself wrong: setdefault() never
    overwrites). For a name shared by two different players in the same
    stat group this season, _lookup()'s name-only fallback could silently
    attribute an arbitrary one of those two players' stat line to the
    other's namesake on a different team, if the precise "{norm}_{team}" key
    ever misses (e.g. a stale/mismatched team on the caller's side). Now the
    name-only fallback is only populated for names that map to exactly one
    team this season; a genuine collision is left out of the name-only
    index entirely, so _lookup() falls through to {} (unknown) rather than
    risk a wrong match. Flagged as an unconfirmed, same-risk-class item in
    the bug tracker's "Also worth knowing" section since 2026-08-13.
    """
    url = f"{MLB_API}/stats"
    params = {
        "stats":      "season",
        "group":      group,
        "season":     season,
        "playerPool": "ALL",
        "limit":      2000,   # covers the full MLB player pool
        "offset":     0,
    }
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error("MLB Stats API error (%s %d): %s", group, season, e)
        return {}

    result: dict[str, dict] = {}
    teams_by_name: dict[str, set] = {}
    for stat_group in data.get("stats", []):
        for split in stat_group.get("splits", []):
            player_info = split.get("player", {})
            team_info   = split.get("team", {})
            raw_stat    = split.get("stat", {})

            full_name  = player_info.get("fullName", "")
            mlb_team   = team_info.get("abbreviation", "")
            cbs_team   = mlb_to_cbs(mlb_team)

            parsed = _parse_stat(raw_stat, group)

            norm        = norm_name(full_name)
            team_lower  = cbs_team.lower()
            key_precise = f"{norm}_{team_lower}"

            result[key_precise] = parsed
            teams_by_name.setdefault(norm, set()).add(team_lower)

    # Name-only fallback: only for names that resolved to exactly one team
    # this season. A name shared by 2+ players (different teams) is
    # ambiguous -- deliberately NOT added to the name-only index, so a
    # lookup that misses the precise key falls through to {} instead of
    # risking a match to the wrong player.
    for norm, teams in teams_by_name.items():
        if len(teams) == 1:
            team_lower = next(iter(teams))
            result[norm] = result[f"{norm}_{team_lower}"]

    logger.info("MLB Stats API: %d %s entries loaded for %d",
                len(result), group, season)
    return result


# ---------------------------------------------------------------------------
# Internal: stat parsing
# ---------------------------------------------------------------------------

def _parse_stat(raw: dict, group: str) -> dict:
    """Normalise one MLB API stat block to our internal stat key names."""
    if group == "pitching":
        ip  = _parse_ip(raw.get("inningsPitched", "0"))
        gs  = int(raw.get("gamesStarted", 0))
        k   = int(raw.get("strikeOuts", 0))
        bb  = int(raw.get("baseOnBalls", 0))
        return {
            "ERA":    _f(raw.get("era")),
            "WHIP":   _f(raw.get("whip")),
            "K9":     _f(raw.get("strikeoutsPer9Inn")) or
                      (round(k / ip * 9, 2) if ip else 0.0),
            "W":      int(raw.get("wins", 0)),
            "SV":     int(raw.get("saves", 0)),
            "HLD":    int(raw.get("holds", 0)),
            "K":      k,
            "QS":     int(raw.get("qualityStarts", 0)),
            "INNdGS": round(ip / gs, 2) if gs else 0.0,
            "K_BB":   round(k / bb, 2) if bb else float(k),
            "IP":     ip,
            "GS":     gs,
            "G":      int(raw.get("gamesPlayed", 0)),
        }
    else:
        doubles = int(raw.get("doubles", 0))
        triples = int(raw.get("triples", 0))
        hr      = int(raw.get("homeRuns", 0))
        hits    = int(raw.get("hits", 0))
        # MLB API may omit totalBases — compute from component stats
        tb_raw  = raw.get("totalBases")
        if tb_raw is not None:
            tb = int(tb_raw)
        else:
            singles = hits - doubles - triples - hr
            tb = singles + 2 * doubles + 3 * triples + 4 * hr
        return {
            "AVG":  _f(raw.get("avg")),
            "OPS":  _f(raw.get("ops")),
            "HR":   hr,
            "R":    int(raw.get("runs", 0)),
            "RBI":  int(raw.get("rbi", 0)),
            "SB":   int(raw.get("stolenBases", 0)),
            "TB":   tb,
            "XBH":  doubles + triples + hr,
            "H":    hits,
            "AB":   int(raw.get("atBats", 0)),
            "G":    int(raw.get("gamesPlayed", 0)),
        }


# ---------------------------------------------------------------------------
# Internal: player lookup
# ---------------------------------------------------------------------------

def _lookup(player, p_db: dict, h_db: dict) -> dict:
    """Return the stat dict for a player, or {} if not found."""
    team = (player.team or "").upper()
    norm = norm_name(player.name)
    key  = f"{norm}_{team.lower()}"

    is_pitcher = any(pos in _PITCHER_POSITIONS for pos in player.positions)
    primary_db, fallback_db = (p_db, h_db) if is_pitcher else (h_db, p_db)

    return (primary_db.get(key)
            or primary_db.get(norm)
            or fallback_db.get(key)
            or fallback_db.get(norm)
            or {})


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _f(val) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _parse_ip(ip_str) -> float:
    """Convert MLB '95.1' IP notation to decimal (95.333...)."""
    try:
        parts = str(ip_str).split(".")
        return int(parts[0]) + (int(parts[1]) / 3 if len(parts) > 1 else 0)
    except Exception:
        return _f(ip_str)
