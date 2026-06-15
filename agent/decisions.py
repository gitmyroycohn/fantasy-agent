"""
Decision engine -- runs per league and produces a list of recommended actions.
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
from mlb.schedule import (
    two_start_pitchers, week_bounds, _today_et,
    teams_playing_today, probable_starters_today,
)
from sports.baseball.lineup_optimizer import optimize_daily_lineup

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


# -- H2H (Pins & Pills) ------------------------------------------------------

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
    # Enrich SP candidates -- lru_cache makes this cheap
    try:
        enrich_players(waivers[:100])
    except Exception as e:
        logger.warning("SP enrichment failed: %s", e)

    # Fetch 2-start pitchers for current week (and next if Thu-Sun)
    two_start_now  = {}
    two_start_next = {}
    try:
        two_start_now  = two_start_pitchers()
        if _today_et().weekday() >= 3:   # Thursday or later ET
            two_start_next = two_start_pitchers(next_week=True)
    except Exception as e:
        logger.warning("2-start fetch failed: %s", e)

    print(f"  2-start pitchers detected this week: {len(two_start_now)}")
    if two_start_now:
        import re as _re
        _norm = lambda n: _re.sub(r"[^a-z0-9]", "", n.lower())
        two_on_wire = [wp.player.name for wp in waivers
                       if _norm(wp.player.name) in two_start_now]
        if two_on_wire:
            print(f"  2-starters on waiver wire: {', '.join(two_on_wire[:10])}")

    # rank_streaming_sps expects {cat: {"winning": bool}} -- build from matchup
    cat_status = {c.category: {"winning": c.winning}
                  for c in matchup.category_standings}
    sp_recs = rank_streaming_sps(waivers, cat_status, two_starters=two_start_now)

    if sp_recs:
        actions.append({
            "type": "streaming_sp",
            "recommendations": sp_recs,
            "note": "Submit adds before Monday scoring period lock.",
        })

    # Surface next week's 2-starters on waivers (Thu-Sun planning)
    if two_start_next:
        next_two_recs = rank_streaming_sps(waivers, cat_status,
                                           two_starters=two_start_next,
                                           max_results=5)
        if next_two_recs:
            actions.append({
                "type": "streaming_sp_next_week",
                "recommendations": next_two_recs,
                "note": "2-starters available for NEXT week -- add now before lock.",
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

    # 4. Daily lineup advice
    _add_lineup_advice(actions, team)

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


# -- Rotisserie (Casey Stengel) -----------------------------------------------

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

    # 2. Waiver adds -- NL 