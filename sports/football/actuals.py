"""
Real weekly actual fantasy points for Christopher's 3 football leagues, from
raw per-player stats -- the enhancement order (filed 2026-09-16) that this
answers is: "actual weekly fantasy points scored under *that specific
league's* scoring rules ... a shared 'points' feed needs the same
re-scoring treatment get_fantasypros_draft_board already does for draft
value."

This is the ACTUALS counterpart to sports/football/scoring.py::
estimate_sfflf_points() (which does the same kind of re-scoring, but for
FantasyPros' season-long PROJECTIONS, not real weekly box scores). The raw
data source here is Sleeper (sleeper/client.py) -- confirmed live 2026-09-17
to carry the raw per-category stats (rush/rec/pass yards, TD counts + the
longest TD's yardage, receptions, fumbles lost, snap counts) that
score_player()/score_sfflf()/score_standard_ppr() in sports/football/
scoring.py already implement CBS's real per-league formulas against, once
FantasyPros' player-points endpoint turned out to be points-only (see the
football-stats-feed-scoping project memory).

Why Sleeper for ALL THREE leagues, not just f_league: FantasyPros'
player-points is scored under FantasyPros' own generic PPR ruleset, which
does NOT match east_coast's "stricter" penalty structure (see
sports/football/scoring.py::ECFC_PROFILE -- -2 pass_int, -2 fumble_lost,
-2 missed_xp, its own FG-bonus tiers, none of which are guaranteed to match
FantasyPros' own generic defaults) or hard_chargers' exact FG-bonus tiers
either. Since score_player() already implements each league's REAL rules
exactly from raw per-play stats, using Sleeper's raw stats + that existing
exact scorer for all 3 leagues is both simpler (one raw-stats source
instead of "FantasyPros for two leagues, Sleeper for the third") and more
accurate (byte-exact CBS rules instead of an approximation under
FantasyPros' own scoring config) than trusting FantasyPros' pre-scored
total for hard_chargers/east_coast.

KNOWN, DELIBERATE GAPS (do not silently assume these are complete -- same
spirit as every "KNOWN DATA GAPS" section elsewhere in this codebase):

  - K and DST are NOT supported here -- score_actual_points() returns None
    for both, same as estimate_sfflf_points() already does and for the
    identical reason: Sleeper's kicker (made-FG-by-distance) and
    team-defense (sacks, INTs, points allowed, special-teams TDs) field
    names were never probed against a real Sleeper response (none of the 6
    real test players in the 2026-09-17 probes were a K or DST). Guessing
    at those field names would be fabricating data, not estimating from
    it. Whoever picks this up next should run a live Sleeper probe against
    a real K and a real DST entry before extending this.
  - f_league's long-TD bonus (score_sfflf()'s _long_td_bonus(), 3-12 extra
    points depending on position/TD type/length) needs each individual
    TD's own yardage. Sleeper's weekly stats give a TD *count* plus only
    the LONGEST TD's yardage that game (e.g. rush_td=2, rush_td_lng=45) --
    not a per-TD breakdown. _td_yardage_list() below assumes the longest
    TD is real (gets the correct bonus) and every OTHER TD that game is
    treated as under the 10-yard bonus threshold (gets none). This is a
    systematic UNDERESTIMATE for any game with 2+ long TDs by the same
    player at the same position -- same direction, and same "fine for
    comparing, not a promise of an exact total" caveat already documented
    on estimate_sfflf_points(). hard_chargers/east_coast have no long-TD
    bonus at all (see score_standard_ppr()), so this gap doesn't affect
    them.
  - Two-point conversions and offensive-fumble-recovery TDs default to 0
    -- Sleeper's field names for these were never confirmed live (none of
    the 6 test players had one in the probed week). Under-counts by a
    small, rare amount when they occur; does not affect the vast majority
    of stat lines.
"""

from __future__ import annotations

import logging

from sleeper.client import get_players, get_weekly_stats, build_name_index, find_player_id
from sports.football.scoring import score_player

logger = logging.getLogger(__name__)


def _td_yardage_list(count, longest) -> list[float]:
    """Best-effort reconstruction of a per-TD yardage list from Sleeper's
    (count, longest) pair -- see this module's docstring for the exact gap
    this leaves. Returns [] for count<=0."""
    count = int(count or 0)
    if count <= 0:
        return []
    longest = float(longest or 0)
    return [longest] + [0.0] * (count - 1)


def sleeper_stats_to_cbs_shape(raw: dict, position: str, scoring_profile: str) -> dict:
    """Map one Sleeper weekly stat-line dict to the CBS-abbreviation shape
    sports/football/scoring.py's score_player()/score_sfflf()/
    score_standard_ppr() expect. position: "QB", "RB", "WR", or "TE" only
    (see this module's docstring on why K/DST aren't handled here). Sleeper
    key names confirmed live 2026-09-17 against real rostered players
    (RB/TE: Jonathan Taylor, Saquon Barkley, Brock Bowers; QB: Drake Maye,
    Dak Prescott, Justin Herbert)."""
    rec = raw.get("rec", 0) or 0
    rush_yd = raw.get("rush_yd", 0) or 0
    rec_yd = raw.get("rec_yd", 0) or 0
    pass_yd = raw.get("pass_yd", 0) or 0

    shaped: dict = {
        "Recpt": rec,
        "RuYd": rush_yd,
        "ReYd": rec_yd,
        "PaYd": pass_yd,
        "PaInt": raw.get("pass_int", 0) or 0,
        "FL": raw.get("fum_lost", 0) or 0,
        # Not yet confirmed live (no test player had one in the probed
        # week) -- defaults to 0, see this module's docstring.
        "Pa2P": raw.get("pass_2pt", 0) or 0,
        "Re2P": raw.get("rec_2pt", 0) or 0,
        "Ru2P": raw.get("rush_2pt", 0) or 0,
        "OFRTD": raw.get("fum_rec_td", 0) or 0,
    }

    if scoring_profile == "sfflf_tiered":
        # f_league's tiered formula needs per-TD yardage lists (for the
        # long-TD bonus) and combined-yardage keys, not flat TD counts.
        shaped["PaTD"] = _td_yardage_list(raw.get("pass_td", 0), raw.get("pass_td_lng"))
        shaped["RuTD"] = _td_yardage_list(raw.get("rush_td", 0), raw.get("rush_td_lng"))
        shaped["ReTD"] = _td_yardage_list(raw.get("rec_td", 0), raw.get("rec_td_lng"))
        if position == "QB":
            shaped["PaRuYd"] = pass_yd + rush_yd
        else:
            shaped["RuReYd"] = rush_yd + rec_yd
    else:
        # hard_chargers/east_coast: flat per-TD-count rate, no long bonus,
        # so a plain count is all score_standard_ppr() needs.
        shaped["PaTD"] = raw.get("pass_td", 0) or 0
        shaped["RuTD"] = raw.get("rush_td", 0) or 0
        shaped["ReTD"] = raw.get("rec_td", 0) or 0

    return shaped


def score_actual_points(raw: dict, position: str, scoring_profile: str) -> float | None:
    """One player's REAL weekly fantasy points, scored exactly under their
    league's actual rules (sports/football/scoring.py::score_player()) from
    Sleeper's raw weekly stats. Returns None for K/DST (unsupported, see
    module docstring) -- never a fabricated/guessed number for positions
    this hasn't been verified against."""
    if position not in ("QB", "RB", "WR", "TE"):
        return None
    shaped = sleeper_stats_to_cbs_shape(raw, position, scoring_profile)
    return score_player(shaped, position, scoring_profile)


def snap_share(raw: dict) -> float | None:
    """Offensive snap share (0.0-1.0) for this player that week -- the
    "recent snap share" signal the enhancement order asked for. Confirmed
    live 2026-09-17 (off_snp/tm_off_snp both present on real RB/TE/QB stat
    lines). Returns None if either field is missing/zero (bye week,
    inactive, or a position Sleeper doesn't track offensive snaps for --
    e.g. K/DST)."""
    off = raw.get("off_snp")
    team_off = raw.get("tm_off_snp")
    if not off or not team_off:
        return None
    try:
        return float(off) / float(team_off)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def actual_points_for_roster(roster: list, league_id: str, scoring_profile: str,
                             season: int, week: int) -> dict[str, dict]:
    """For every player on `roster` (list of data.models.RosterSlot), fetch
    their real actual points/snap share for one week.

    Returns {player_name: {"points": float|None, "snap_share": float|None,
    "position": str, "sleeper_matched": bool}}.

    Raises sleeper.client.SleeperUnavailable if Sleeper's connector itself
    is down (every retry failed) -- callers should catch this and degrade
    honestly, same pattern as a CBS/FantasyPros outage elsewhere in this
    codebase. A player Sleeper's index just has no name-match for is NOT
    an error (sleeper_matched=False, points/snap_share=None) -- see
    sleeper.client.find_player_id()'s docstring on the exact-match-only
    caveat this leaves for suffixes/nicknames.
    """
    players = get_players()  # may raise SleeperUnavailable
    name_index = build_name_index(players)
    week_stats = get_weekly_stats(season, week)  # may raise SleeperUnavailable

    result: dict[str, dict] = {}
    for rs in roster:
        name = rs.player.name
        position = rs.player.positions[0] if rs.player.positions else rs.player.position
        pid = find_player_id(name_index, name)
        if pid is None:
            result[name] = {"points": None, "snap_share": None,
                            "position": position, "sleeper_matched": False}
            continue
        raw = week_stats.get(pid)
        if raw is None:
            # Matched a real Sleeper player, but no stat line this week --
            # bye, inactive, or DNP. Distinct from "couldn't match at all".
            result[name] = {"points": None, "snap_share": None,
                            "position": position, "sleeper_matched": True}
            continue
        result[name] = {
            "points": score_actual_points(raw, position, scoring_profile),
            "snap_share": snap_share(raw),
            "position": position,
            "sleeper_matched": True,
        }
    return result
