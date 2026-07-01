"""
Per-week category projection engine for H2H fantasy baseball matchups.

Public API:
    project_matchup(auth, league_cfg, sport, fp_client, week_offset=1)
        -> full projection dict for my team vs next opponent

    project_team_output(roster_slots, scoring_cats, two_starters, games_by_team)
        -> {cat: float} projected team totals for the week

Design notes:
    * FP ROS totals are divided by remaining_weeks to get a per-week rate.
    * SP with 2 starts this week (in two_starters) gets a 2× multiplier on
      counting stats (K, W) and IP weight; ERA/WHIP remain unchanged (they
      are already per-IP rates and the IP-weighted average naturally handles
      the extra starts).
    * Hitter counting stats are scaled by (team_games_this_week / 6.0) where
      6 is the typical MLB week length.
    * ERA, WHIP: IP-weighted average across all pitchers with valid FP data.
    * AVG, OPS:  R-weighted average across all hitters with valid FP data
      (per-week projected R is a reasonable proxy for plate appearances).
    * Categories not available from FP (TB, XBH, QS, HLD, INNdGS, K_BB)
      project to 0.0.
"""

import logging
from collections import defaultdict
from datetime import date, timedelta
from functools import lru_cache

import requests

from mlb.teams import mlb_to_cbs, norm_name as _norm

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MLB_API = "https://statsapi.mlb.com/api/v1"
_TIMEOUT = 20

# Pitcher position tags
_PITCH_POS = {"SP", "RP", "P"}

# FP key map for counting stats
_HIT_CAT_KEYS = {
    "HR":  "fp_hr",
    "R":   "fp_r",
    "RBI": "fp_rbi",
    "SB":  "fp_sb",
}
_PITCH_CAT_KEYS = {
    "K":  "fp_k",
    "W":  "fp_w",
    "SV": "fp_sv",
}

# Roster statuses to skip (injured list, minor leagues)
_SKIP_STATUS = {"I", "IL", "DL", "ML"}

# Approximate last Sunday of CBS baseball season (updated each year if needed)
_SEASON_END = (9, 27)   # (month, day) — September 27


# ---------------------------------------------------------------------------
# Season-length helpers
# ---------------------------------------------------------------------------

def _remaining_weeks(today: date = None) -> int:
    """Estimate remaining CBS scoring weeks from today.

    Counts Monday-aligned weeks from the current CBS week through the final
    week ending around September 27.  Returns at least 1.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if today is None:
        today = datetime.now(ZoneInfo("America/New_York")).date()

    mm, dd = _SEASON_END
    season_end    = date(today.year, mm, dd)
    cur_monday    = today      - timedelta(days=today.weekday())
    last_monday   = season_end - timedelta(days=season_end.weekday())
    weeks         = max(1, (last_monday - cur_monday).days // 7 + 1)
    logger.debug("_remaining_weeks: cur=%s last=%s → %d weeks",
                 cur_monday, last_monday, weeks)
    return weeks


# ---------------------------------------------------------------------------
# Games-per-team for an arbitrary date range
# ---------------------------------------------------------------------------

@lru_cache(maxsize=14)
def _fetch_games_in_range(start_date: str, end_date: str) -> tuple:
    """Fetch all regular-season games in [start_date, end_date].

    Returns a tuple of raw MLB Stats API game dicts (tuple for lru_cache
    hashability).  Cached per date range string pair.
    """
    url    = f"{_MLB_API}/schedule"
    params = {
        "sportId":   1,
        "startDate": start_date,
        "endDate":   end_date,
        "hydrate":   "team",
        "gameType":  "R",
    }
    try:
        r = requests.get(url, params=params, timeout=_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("MLB schedule API error (%s – %s): %s",
                       start_date, end_date, e)
        return ()

    games = []
    for date_entry in data.get("dates", []):
        games.extend(date_entry.get("games", []))
    logger.info("_fetch_games_in_range %s–%s: %d games",
                start_date, end_date, len(games))
    return tuple(games)


def _games_per_team(monday: date, sunday: date) -> dict[str, int]:
    """Return {cbs_team_abbr: game_count} for all MLB teams in [monday, sunday]."""
    raw    = _fetch_games_in_range(monday.isoformat(), sunday.isoformat())
    counts: dict[str, int] = defaultdict(int)
    for game in raw:
        for side in ("home", "away"):
            abbr = (game.get("teams", {})
                        .get(side, {})
                        .get("team", {})
                        .get("abbreviation", ""))
            if abbr:
                counts[mlb_to_cbs(abbr.upper())] += 1
    return dict(counts)


# ---------------------------------------------------------------------------
# Core projection engine
# ---------------------------------------------------------------------------

def project_team_output(
    roster_slots: list,
    scoring_cats: list[str],
    two_starters: dict[str, int],
    games_by_team: dict[str, int],
    remaining_weeks: int = None,
) -> dict[str, float]:
    """Project a team's weekly category totals from FP ROS projections.

    Args:
        roster_slots:    list[RosterSlot] already enriched with fp_* keys on
                         player.stats via enrich_with_fp_projections().
        scoring_cats:    list of category strings (e.g. ["HR", "R", "ERA"]).
        two_starters:    {norm_name: start_count} from schedule_weeks for
                         the target week.  SPs absent from this dict get
                         start_count=1.
        games_by_team:   {cbs_team_abbr: game_count} for the target week.
                         Hitter counting stats are scaled by games / 6.0.
        remaining_weeks: Weeks left in the CBS season.  Auto-computed if None.

    Returns:
        {cat: float}  All requested categories present; rate stats (ERA,
        WHIP, AVG, OPS) are weighted averages.  Categories not derivable
        from FP data (TB, XBH, QS, HLD, INNdGS, K_BB) return 0.0.
    """
    if remaining_weeks is None:
        remaining_weeks = _remaining_weeks()
    remaining_weeks = max(1, remaining_weeks)

    # Pre-initialise all requested categories to 0.0
    totals: dict[str, float] = {cat: 0.0 for cat in scoring_cats}

    # Accumulators for rate stats that need weighted averages
    era_pairs:  list[tuple[float, float]] = []   # (era,  ip_weight)
    whip_pairs: list[tuple[float, float]] = []   # (whip, ip_weight)
    avg_pairs:  list[tuple[float, float]] = []   # (avg,  r_weight)
    ops_pairs:  list[tuple[float, float]] = []   # (ops,  r_weight)

    for rs in roster_slots:
        # Accept both RosterSlot (has .player) and bare Player objects
        p      = getattr(rs, "player", rs)
        stats  = p.stats or {}
        status = (p.status or "").upper()

        if status in _SKIP_STATUS:
            continue

        pos_set    = set(p.positions)
        is_pitcher = bool(pos_set & _PITCH_POS)
        is_hitter  = (not is_pitcher) or bool(pos_set - _PITCH_POS)

        # ----------------------------------------------------------------
        # PITCHER — counting stats + ERA/WHIP
        # ----------------------------------------------------------------
        if is_pitcher:
            fp_ip       = float(stats.get("fp_ip") or 0)
            ip_per_week = fp_ip / remaining_weeks

            # Two-starter multiplier for SPs
            if "SP" in pos_set:
                pname       = _norm(p.name)
                start_count = two_starters.get(pname, 1)
            else:
                start_count = 1       # RP: no start-count adjustment

            ip_this_week = ip_per_week * start_count

            # K, W, SV
            for cat, fp_key in _PITCH_CAT_KEYS.items():
                if cat not in scoring_cats:
                    continue
                fp_val       = float(stats.get(fp_key) or 0)
                per_week     = (fp_val / remaining_weeks) * start_count
                totals[cat] += per_week

            # ERA / WHIP: collect (rate, ip_weight) for IP-weighted average
            if ip_this_week > 0:
                fp_era  = float(stats.get("fp_era")  or 0)
                fp_whip = float(stats.get("fp_whip") or 0)
                if "ERA"  in scoring_cats and fp_era  > 0:
                    era_pairs.append((fp_era,  ip_this_week))
                if "WHIP" in scoring_cats and fp_whip > 0:
                    whip_pairs.append((fp_whip, ip_this_week))

        # ----------------------------------------------------------------
        # HITTER — counting stats + AVG/OPS
        # ----------------------------------------------------------------
        if is_hitter:
            team   = (p.team or "").upper()
            games  = games_by_team.get(team, 6)
            g_mult = games / 6.0

            # HR, R, RBI, SB
            for cat, fp_key in _HIT_CAT_KEYS.items():
                if cat not in scoring_cats:
                    continue
                fp_val       = float(stats.get(fp_key) or 0)
                per_week     = (fp_val / remaining_weeks) * g_mult
                totals[cat] += per_week

            # AVG / OPS: collect (rate, r_weight) for R-weighted average
            fp_r     = float(stats.get("fp_r") or 0)
            r_weekly = (fp_r / remaining_weeks) * g_mult
            weight   = r_weekly if r_weekly > 0 else 0.1    # fallback: tiny equal weight

            fp_avg = float(stats.get("fp_avg") or 0)
            fp_ops = float(stats.get("fp_ops") or 0)
            if "AVG" in scoring_cats and fp_avg > 0:
                avg_pairs.append((fp_avg, weight))
            if "OPS" in scoring_cats and fp_ops > 0:
                ops_pairs.append((fp_ops, weight))

    # ---- Resolve rate stats (weighted averages) -------------------------

    if era_pairs and "ERA" in totals:
        total_ip    = sum(ip for _, ip in era_pairs)
        totals["ERA"] = (sum(e * ip for e, ip in era_pairs) / total_ip
                         if total_ip > 0 else 0.0)

    if whip_pairs and "WHIP" in totals:
        total_ip     = sum(ip for _, ip in whip_pairs)
        totals["WHIP"] = (sum(w * ip for w, ip in whip_pairs) / total_ip
                          if total_ip > 0 else 0.0)

    if avg_pairs and "AVG" in totals:
        total_w    = sum(w for _, w in avg_pairs)
        totals["AVG"] = (sum(a * w for a, w in avg_pairs) / total_w
                         if total_w > 0 else 0.0)

    if ops_pairs and "OPS" in totals:
        total_w    = sum(w for _, w in ops_pairs)
        totals["OPS"] = (sum(o * w for o, w in ops_pairs) / total_w
                         if total_w > 0 else 0.0)

    return totals


# ---------------------------------------------------------------------------
# High-level orchestrator
# ---------------------------------------------------------------------------

def project_matchup(
    auth,
    league_cfg: dict,
    sport: str = "baseball",
    fp_client=None,
    week_offset: int = 1,
) -> dict:
    """Project per-category weekly totals for my team vs next week's opponent.

    Args:
        auth:        CBSAuth instance.
        league_cfg:  One league dict from config/leagues.yaml (not the whole
                     file — one entry only).
        sport:       "baseball" (default).
        fp_client:   FantasyProsClient.  If None, created from FANTASYPROS_API_KEY
                     in environment.
        week_offset: 0 = current week, 1 = next week (default).

    Returns a dict with:
        opponent_name    str   opponent's team name
        opponent_id      str   CBS team id
        period           int   CBS scoring period number
        week_offset      int   as supplied
        scoring_cats     list  all scoring categories for this league
        my_proj          dict  {cat: float}  my team projections
        opp_proj         dict  {cat: float}  opponent projections
        _warnings        list  non-fatal diagnostic messages
        _two_starters    dict  {norm_name: start_count} for target week
        _games_by_team   dict  {cbs_abbr: game_count} for target week
        _remaining_weeks int   weeks used for ROS → per-week scaling

    On error (roto league, no opponent, etc.) returns {"error": str, ...}.
    """
    from cbs.roster   import get_roster, get_all_team_rosters
    from cbs.schedule import fetch_next_opponent
    from mlb.schedule import schedule_weeks
    from fantasypros.client import FantasyProsClient, enrich_with_fp_projections

    warnings: list[str] = []
    lid = league_cfg["cbs_league_id"]
    tid = str(league_cfg["cbs_team_id"])

    # Scoring categories (hitting then pitching, order preserved)
    scoring    = league_cfg.get("scoring", {})
    hit_cats   = list(scoring.get("hitting",  []) or [])
    pitch_cats = list(scoring.get("pitching", []) or [])
    scoring_cats = hit_cats + pitch_cats

    # ------------------------------------------------------------------
    # My roster
    # ------------------------------------------------------------------
    my_slots = get_roster(auth, lid, tid, sport)

    # ------------------------------------------------------------------
    # Opponent for target week
    # ------------------------------------------------------------------
    opp_info = fetch_next_opponent(
        auth, lid, sport, my_team_id=tid, week_offset=week_offset
    )
    if not opp_info:
        return {
            "error":       ("Could not determine opponent — only H2H leagues "
                            "are supported"),
            "week_offset": week_offset,
            "scoring_cats": scoring_cats,
            "_warnings":   ["fetch_next_opponent returned None"],
        }

    opp_id   = str(opp_info.get("opponent_id", ""))
    opp_name = opp_info.get("opponent_name", "Unknown")
    period   = int(opp_info.get("period") or 0)

    # ------------------------------------------------------------------
    # Opponent's roster
    # ------------------------------------------------------------------
    all_rosters  = get_all_team_rosters(auth, lid, sport)
    opp_roster_r = all_rosters.get(opp_id) or all_rosters.get(str(opp_id))
    if opp_roster_r:
        opp_slots = opp_roster_r["roster"]
    else:
        warnings.append(
            f"Opponent roster not found (id={opp_id}) — "
            "opponent projection will be empty"
        )
        opp_slots = []

    # ------------------------------------------------------------------
    # FP enrichment
    # ------------------------------------------------------------------
    if fp_client is None:
        try:
            from config.settings import FANTASYPROS_API_KEY
            if FANTASYPROS_API_KEY:
                fp_client = FantasyProsClient(FANTASYPROS_API_KEY)
            else:
                warnings.append(
                    "FANTASYPROS_API_KEY not set — all projections will be 0"
                )
        except Exception as e:
            warnings.append(f"Could not create FP client: {e}")

    if fp_client is not None:
        try:
            matched_mine = enrich_with_fp_projections(my_slots,  fp_client)
            matched_opp  = enrich_with_fp_projections(opp_slots, fp_client)
            logger.info(
                "FP enrichment: %d/%d my players, %d/%d opp players matched",
                matched_mine, len(my_slots), matched_opp, len(opp_slots)
            )
        except Exception as e:
            warnings.append(f"FP enrichment error: {e}")

    # ------------------------------------------------------------------
    # Schedule data for target week
    # ------------------------------------------------------------------
    wk_data = schedule_weeks(n=max(week_offset + 1, 2))
    target  = wk_data[week_offset] if week_offset < len(wk_data) else wk_data[-1]

    two_starters = target["two_starters"]
    monday       = target["monday"]
    sunday       = target["sunday"]

    try:
        g_by_team = _games_per_team(monday, sunday)
    except Exception as e:
        warnings.append(f"games_per_team failed ({e}) — defaulting to 6 games/team")
        g_by_team = {}

    remaining_wks = _remaining_weeks()

    # ------------------------------------------------------------------
    # Project both rosters
    # ------------------------------------------------------------------
    my_proj = project_team_output(
        my_slots, scoring_cats, two_starters, g_by_team,
        remaining_weeks=remaining_wks,
    )
    opp_proj = project_team_output(
        opp_slots, scoring_cats, two_starters, g_by_team,
        remaining_weeks=remaining_wks,
    )

    return {
        "opponent_name":    opp_name,
        "opponent_id":      opp_id,
        "period":           period,
        "week_offset":      week_offset,
        "scoring_cats":     scoring_cats,
        "my_proj":          my_proj,
        "opp_proj":         opp_proj,
        "_warnings":        warnings,
        "_two_starters":    {k: v for k, v in two_starters.items()},
        "_games_by_team":   g_by_team,
        "_remaining_weeks": remaining_wks,
    }
