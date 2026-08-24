"""
Baseball Savant client -- fetches public xStats CSV leaderboards.

No auth required. Three endpoints used:

  Expected stats (batters):
    https://baseballsavant.mlb.com/leaderboard/expected_statistics
      ?type=batter&year={year}&min=50&csv=true
    Columns: last_name+first_name, player_id, pa, ba, est_ba,
             est_ba_minus_ba_diff*, woba, est_woba, est_woba_minus_woba_diff*
    *Both diff columns are stored as (actual - expected), despite the name.
     Negative diff = player performing BELOW expectation = buy-low signal.

  Expected stats (pitchers):
    https://baseballsavant.mlb.com/leaderboard/expected_statistics
      ?type=pitcher&year={year}&min=20&csv=true
    Adds: era, xera, era_minus_xera_diff
    Positive era_minus_xera_diff means ERA > xERA = unlucky = buy-low signal.

  Statcast exit-velocity / barrel (batters):
    https://baseballsavant.mlb.com/leaderboard/statcast
      ?type=batter&year={year}&min=100&csv=true
    Columns: avg_hit_speed (EV), brl_percent (Barrel%), ev95percent (Hard Hit%)
"""

import csv
import io
import re
import urllib.request
import logging
from datetime import date

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (compatible; FantasyAgent/1.0)"
_YEAR = date.today().year

_EXPECT_URL = (
    "https://baseballsavant.mlb.com/leaderboard/expected_statistics"
    "?type={type}&year={year}&min={min_pa}&csv=true"
)
_EV_URL = (
    "https://baseballsavant.mlb.com/leaderboard/statcast"
    "?type=batter&year={year}&min={min_pa}&csv=true"
)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

from mlb.teams import norm_name as _norm


def _fetch_csv(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(raw))
    return list(reader)


def _parse_name(combined: str) -> str:
    """Convert 'Last, First' -> 'First Last'."""
    parts = combined.split(", ", 1)
    if len(parts) == 2:
        return f"{parts[1].strip()} {parts[0].strip()}"
    return combined.strip()


def _f(val) -> float | None:
    try:
        return float(val) if val not in (None, "", "null", " ") else None
    except (ValueError, TypeError):
        return None


def _collision_safe_index(rows: list[dict], build: "callable", label: str) -> dict:
    """Build a name-keyed lookup dict from a Savant leaderboard CSV, refusing
    to silently conflate two different real players who normalize to the
    same name.

    Bug fix (2026-08-24, P1 -- same risk class as the mlb/stats.py
    name-collision fix, 2026-08-15): every loader here used to do
    `result[key] = {...}` unconditionally per row -- last row wins on a
    name collision, with no signal anything went wrong. Confirmed live:
    waiver_recommendations surfaced a real Houston relief pitcher named
    "Julio Rodriguez" enriched with the Seattle star Julio Rodríguez's
    batter Statcast line (xwOBA, Barrel%) stapled onto his entry -- two
    different real players, same normalized name. The Aug-15 fix closed
    this exact pattern in mlb/stats.py's season-stats lookup, but this
    module's Savant enrichment (the free-agent waiver-pool enrichment path)
    was never touched and carried the same risk, unguarded.

    These leaderboard CSVs don't carry a team column, so disambiguation
    here uses Savant's own player_id (the MLB-authoritative person id,
    already parsed elsewhere as sv_mlb_id) instead of team. A name whose
    rows carry two or more distinct ids -- or any blank/missing id, which
    can't prove the rows are the same person -- is dropped from the index
    entirely; a lookup for that name falls through to {} (unknown) rather
    than risk attributing the wrong player's stats.
    """
    by_key: dict[str, dict] = {}
    ids_by_key: dict[str, set] = {}
    for i, row in enumerate(rows):
        name = _parse_name(row.get("last_name, first_name", ""))
        key  = _norm(name)
        pid  = (row.get("player_id") or "").strip()
        by_key[key] = build(row, name)
        # A blank id can't prove two rows are the same person -- give it a
        # unique placeholder so it still trips the ambiguity check below
        # against any other row (blank or not) sharing this name.
        ids_by_key.setdefault(key, set()).add(pid or f"__no_id_row_{i}__")

    result = {k: v for k, v in by_key.items() if len(ids_by_key[k]) <= 1}
    collisions = sorted(k for k, ids in ids_by_key.items() if len(ids) > 1)
    if collisions:
        logger.warning("Savant %s: %d name collision(s) excluded from lookup: %s",
                        label, len(collisions), collisions)
    return result


# ---------------------------------------------------------------------------
# SavantClient
# ---------------------------------------------------------------------------

class SavantClient:
    """Lazy-loading cache for Baseball Savant public CSV leaderboards."""

    def __init__(self, season: int = None):
        self.season = season or _YEAR
        # name_norm -> stats dict
        self._bat_xstats: dict | None = None
        self._pit_xstats: dict | None = None
        self._bat_ev: dict | None = None

    # -- loaders ------------------------------------------------------------

    def _load_bat_xstats(self) -> dict:
        if self._bat_xstats is not None:
            return self._bat_xstats
        url = _EXPECT_URL.format(type="batter", year=self.season, min_pa=50)
        try:
            rows = _fetch_csv(url)
            result = _collision_safe_index(
                rows,
                build=lambda row, name: {
                    "sv_name":       name,
                    "sv_mlb_id":     row.get("player_id", ""),
                    "sv_pa":         _f(row.get("pa")),
                    "sv_ba":         _f(row.get("ba")),
                    "sv_xba":        _f(row.get("est_ba")),
                    "sv_xba_diff":   _f(row.get("est_ba_minus_ba_diff")),
                    "sv_woba":       _f(row.get("woba")),
                    "sv_xwoba":      _f(row.get("est_woba")),
                    "sv_xwoba_diff": _f(row.get("est_woba_minus_woba_diff")),
                },
                label="batter xStats",
            )
            self._bat_xstats = result
            print(f"  Savant batter xStats: {len(result)} players")
        except Exception as e:
            logger.warning("Savant batter xStats: %s", e)
            print(f"  Savant batter xStats: unavailable ({e})")
            self._bat_xstats = {}
        return self._bat_xstats

    def _load_pit_xstats(self) -> dict:
        if self._pit_xstats is not None:
            return self._pit_xstats
        url = _EXPECT_URL.format(type="pitcher", year=self.season, min_pa=20)
        try:
            rows = _fetch_csv(url)
            result = _collision_safe_index(
                rows,
                build=lambda row, name: {
                    "sv_name":         name,
                    "sv_mlb_id":       row.get("player_id", ""),
                    "sv_pa_faced":     _f(row.get("pa")),
                    "sv_xwoba_ag":     _f(row.get("est_woba")),
                    "sv_era":          _f(row.get("era")),
                    "sv_xera":         _f(row.get("xera")),
                    # positive = ERA worse than expected = unlucky = buy low
                    "sv_era_diff":     _f(row.get("era_minus_xera_diff")),
                },
                label="pitcher xStats",
            )
            self._pit_xstats = result
            print(f"  Savant pitcher xStats: {len(result)} players")
        except Exception as e:
            logger.warning("Savant pitcher xStats: %s", e)
            print(f"  Savant pitcher xStats: unavailable ({e})")
            self._pit_xstats = {}
        return self._pit_xstats

    def _load_bat_ev(self) -> dict:
        if self._bat_ev is not None:
            return self._bat_ev
        url = _EV_URL.format(year=self.season, min_pa=50)
        try:
            rows = _fetch_csv(url)
            result = _collision_safe_index(
                rows,
                build=lambda row, name: {
                    "sv_ev":          _f(row.get("avg_hit_speed")),
                    "sv_barrel_pct":  _f(row.get("brl_percent")),
                    "sv_hard_hit_pct": _f(row.get("ev95percent")),
                    "sv_sweet_pct":   _f(row.get("anglesweetspotpercent")),
                },
                label="EV/Barrel",
            )
            self._bat_ev = result
            print(f"  Savant EV/Barrel: {len(result)} players")
        except Exception as e:
            logger.warning("Savant EV/Barrel: %s", e)
            print(f"  Savant EV/Barrel: unavailable ({e})")
            self._bat_ev = {}
        return self._bat_ev

    # -- public API ---------------------------------------------------------

    def batter_data(self, player_name: str) -> dict:
        """Merged xStats + EV data for a batter. Returns {} if not found."""
        key = _norm(player_name)
        out = {}
        bat = self._load_bat_xstats().get(key, {})
        ev  = self._load_bat_ev().get(key, {})
        out.update(bat)
        out.update(ev)
        return out

    def pitcher_data(self, player_name: str) -> dict:
        """xStats for a pitcher. Returns {} if not found."""
        key = _norm(player_name)
        return self._load_pit_xstats().get(key, {})

    def preload_all(self) -> None:
        """Eagerly fetch all three endpoints (call once at startup)."""
        self._load_bat_xstats()
        self._load_bat_ev()
        self._load_pit_xstats()


# ---------------------------------------------------------------------------
# Enrichment helper
# ---------------------------------------------------------------------------

_PITCHER_POSITIONS = {"SP", "RP"}


def enrich_with_savant(players: list, client: SavantClient) -> int:
    """Add sv_* keys to player.stats for each player in the list.

    Works on any iterable of objects with .player.name and .player.positions.
    Returns the number of players successfully matched.
    """
    matched = 0
    for wp in players:
        p = wp.player
        is_pitcher = bool(_PITCHER_POSITIONS & set(p.positions or []))
        data = client.pitcher_data(p.name) if is_pitcher else client.batter_data(p.name)
        if data:
            if p.stats is None:
                p.stats = {}
            p.stats.update(data)
            matched += 1
    return matched
