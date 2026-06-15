"""
Decision engine — runs per league and produces a list of recommended actions.
"""
import logging
from data.models import Team, Matchup
from sports.baseball.streaming import rank_streaming_sps
from sports.baseball.categories import (
    analyze_matchup, priority_categories, summary_line,
    check_nl_eligibility, filter_nl_waiver_pool,
)
from cbs.waivers import fetch_waiver_wire
from cbs.stats import fetch_matchup_stats
from cbs.auth import CBSAuth
from mlb.stats import enrich_players
from mlb.schedule import two_start_pitchers, week_bounds, _today_et

logger = logging.getLogger(__name__)


def run_decisions(auth: CBSAuth, league_id: str,
                  league_config: dict, team: Team, sport: str = "baseball") -> dict:
    """
    Main decision function for a single league.
    Returns a structured dict of recommendations.
    """
    fmt = league_config.get("format")

    if fmt == "h2h_categories":
        return _h2h_decisions(auth, league_id, league_config, team, sport)
    elif fmt == "rotisserie":
        return _roto_decisions(auth, league_id, league_config, team, sport)
    else:
        raise ValueError(f"Unknown league format: {fmt}")


# ── H2H (Pins & Pills) ──────────────────────────────────────────

def _h2h_decisions(auth: CBSAuth, league_id: str,
                   cfg: dict, team: Team, sport: str) -> dict:
    actions = []

    # 1. Matchup analysis
    raw_stats = fetch_matchup_stats(auth, league_id, sport)
    matchup   = analyze_matchup(raw_stats, week=_current_week())
    losing    = priority_categories(matchup)

    actions.append({
        "type":         "matchup_summary",
        "summary":      summary_line(matchup, system="h2h"),
        "cats_winning": matchup.cats_winning,
        "cats_losing":  matchup.cats_losing,
        "cats_tied":    matchup.cats_tied,
        "priority_cats": losing,
    })

    # 2. Streaming SP recommendations (before Monday lock)
    waivers = fetch_waiver_wire(auth, league_id, sport, position="SP")
    # Enrich SP candidates — lru_cache makes this cheap (MLB data already fetched)
    try:
        enrich_players(waivers[:100])
    except Exception as e:
        logger.warning("SP enrichment failed: %s", e)
    # Fetch 2-start pitchers for current week (and next if Thu–Sun)
    two_start_now  = {}
    two_start_next = {}
    try:
        two_start_now  = two_start_pitchers()
        if _today_et().weekday() >= 3:   # Thursday or later ET — also show next week
            two_start_next = two_start_pitchers(next_week=True)
    except Exception as e:
        logger.warning("2-start fetch failed: %s", e)
    print(f"  2-start pitchers detected this week: {len(two_start_now)}")
    if two_start_now:
        # Show which available SPs are 2-starters (regardless of stat threshold)
        import re as _re
        _norm = lambda n: _re.sub(r"[^a-z0-9]", "", n.lower())
        two_on_wire = [wp.player.name for wp in waivers
                       if _norm(wp.player.name) in two_start_now]
        if two_on_wire:
            print(f"  2-starters on waiver wire: {', '.join(two_on_wire[:10])}")
    # rank_streaming_sps expects {cat: {"winning": bool}} — build from matchup
    cat_status = {c.category: {"winning": c.winning}
                  for c in matchup.category_standings}
    sp_recs = rank_streaming_sps(waivers, cat_status, two_starters=two_start_now)

    if sp_recs:
        actions.append({
            "type": "streaming_sp",
            "recommendations": sp_recs,
            "note": "Submit adds before Monday scoring period lock.",
        })

    # Surface next week's 2-starters on waivers (Thu–Sun planning)
    if two_start_next:
        next_two_recs = rank_streaming_sps(waivers, cat_status,
                                           two_starters=two_start_next,
                                           max_results=5)
        if next_two_recs:
            actions.append({
                "type": "streaming_sp_next_week",
                "recommendations": next_two_recs,
                "note": "2-starters available for NEXT week — add now before lock.",
            })

    # 3. General waiver adds for losing categories
    if losing:
        all_waivers = fetch_waiver_wire(auth, league_id, sport,
                                        position="all", limit=200)
        try:
            enrich_players(all_waivers)
        except Exception as e:
            logger.warning("Waiver enrichment failed: %s", e)
        waiver_recs = _waiver_adds_for_cats(all_waivers, losing)
        if waiver_recs:
            actions.append({
                "type": "waiver_adds",
                "recommendations": waiver_recs,
            })

    league_name = cfg.get("name") or cfg.get("display_name") or league_id
    return {
        "league": league_name,
        "format": "H2H Categories",
        "matchup": {
            "week": matchup.week,
            "score": f"{matchup.cats_winning}-{matchup.cats_losing}-{matchup.cats_tied}",
            "losing_cats": losing,
        },
        "actions": actions,
    }


# ── Rotisserie (Casey Stengel) ───────────────────────────────────

def _roto_decisions(auth: CBSAuth, league_id: str,
                    cfg: dict, team: Team, sport: str) -> dict:
    actions = []

    # 1. NL eligibility check
    warnings = check_nl_eligibility(team.players())
    if warnings:
        actions.append({
            "type": "nl_eligibility_warnings",
            "warnings": warnings,
        })

    # 2. Waiver adds — NL only
    all_waivers = fetch_waiver_wire(auth, league_id, sport,
                                    position="all", limit=200)
    try:
        enrich_players(all_waivers)
    except Exception as e:
        logger.warning("Waiver enrichment failed: %s", e)
    nl_waivers  = filter_nl_waiver_pool(all_waivers, cfg)
    # Drop CBS team/staff placeholder entries (no real position)
    _FAKE_POSITIONS = {"PS", "TS"}   # CBS "pitching staff" / "team" placeholders
    nl_waivers  = [wp for wp in nl_waivers
                   if wp.player.positions
                   and not all(p in _FAKE_POSITIONS for p in wp.player.positions)]

    if nl_waivers:
        # Rank by roto weak cats (populated after roto summary fetch below)
        # For now rank by add_rank (CBS order) — will improve after stats fetch
        waiver_recs = _waiver_adds_for_cats(nl_waivers, ["SB", "HR", "RBI", "K", "SV", "ERA"])
        if not waiver_recs:
            waiver_recs = [
                {"player": wp.player.name, "team": wp.player.team,
                 "positions": wp.player.positions, "helps_cats": []}
                for wp in nl_waivers[:5]
            ]
        actions.append({"type": "waiver_adds", "recommendations": waiver_recs})

    # 3. Roto standing summary
    try:
        raw_stats = fetch_matchup_stats(auth, league_id, sport)
        matchup   = analyze_matchup(raw_stats, week=_current_week())
        losing    = priority_categories(matchup)
        actions.append({
            "type":    "roto_summary",
            "summary": summary_line(matchup, system="roto"),
            "weak_cats": losing[:5],
        })
    except Exception as e:
        logger.warning("Roto scoring fetch failed: %s", e)

    league_name = cfg.get("name") or cfg.get("display_name") or league_id
    return {
        "league": league_name,
        "format": "NL-Only Rotisserie",
        "actions": actions,
    }


# ── Helpers ──────────────────────────────────────────────────────

def _waiver_adds_for_cats(waivers, losing_cats: list[str]) -> list[dict]:
    """Filter and rank available players who help losing categories."""
    CAT_POSITIONS = {
        "SB": ["OF", "SS", "2B"],
        "HR": ["1B", "OF", "3B"],
        "R":  ["OF", "SS", "2B"],
        "RBI":["1B", "3B", "OF"],
        "AVG":["OF", "1B", "2B", "SS", "3B", "C"],
        "K":  ["SP", "RP"],
        "SV": ["RP"],
        "QS": ["SP"],
        "W":  ["SP"],
        "ERA":["SP"],
        "WHIP":["SP"],
    }
    # Stat to sort by for each losing category (higher = better, except ERA/WHIP)
    CAT_SORT_STAT = {
        "SB": ("SB", False),   # (stat_key, lower_is_better)
        "HR": ("HR", False),
        "R":  ("R",  False),
        "RBI":("RBI",False),
        "AVG":("AVG",False),
        "K":  ("K",  False),
        "SV": ("SV", False),
        "QS": ("QS", False),
        "W":  ("W",  False),
        "ERA":("ERA",True),
        "WHIP":("WHIP",True),
    }

    relevant_positions = set()
    for cat in losing_cats:
        relevant_positions.update(CAT_POSITIONS.get(cat, []))

    recs = []
    for wp in waivers:
        helps = [c for c in losing_cats if any(
            pos in CAT_POSITIONS.get(c, []) for pos in wp.player.positions
        )]
        if helps and wp.player.positions:
            # Score: sum of z-score proxies for the cats this player helps
            sort_score = 0.0
            for cat in helps:
                stat_key, lower = CAT_SORT_STAT.get(cat, (None, False))
                if stat_key and wp.player.stats:
                    val = wp.player.stats.get(stat_key, 0.0) or 0.0
                    sort_score += (-val if lower else val)
            recs.append({
                "player":    wp.player.name,
                "team":      wp.player.team,
                "positions": wp.player.positions,
                "ownership": wp.ownership_pct,
                "helps_cats":helps,
                "_score":    sort_score,
            })

    # Sort by stat quality; players with no MLB stats sink to bottom
    recs.sort(key=lambda r: r.pop("_score"), reverse=True)
    return recs[:5]


def _current_week() -> int:
    from datetime import date
    # Rough MLB season week calculation from opening day
    opening_day = date(2026, 3, 26)
    delta = (date.today() - opening_day).days
    return max(1, delta // 7 + 1)
