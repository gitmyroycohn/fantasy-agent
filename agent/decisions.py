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
    fmt = league_config.get("format")
    if fmt == "h2h_categories":
        return _h2h_decisions(auth, league_id, league_config, team, sport)
    elif fmt == "rotisserie":
        return _roto_decisions(auth, league_id, league_config, team, sport)
    else:
        raise ValueError(f"Unknown league format: {fmt}")


# -- H2H (Pins & Pills) -------------------------------------------------------

def _h2h_decisions(auth: CBSAuth, league_id: str,
                   cfg: dict, team: Team, sport: str) -> dict:
    actions = []

    raw_stats = fetch_matchup_stats(auth, league_id, sport)
    matchup   = analyze_matchup(raw_stats, week=_current_week())
    losing    = priority_categories(matchup)

    actions.append({
        "type":          "matchup_summary",
        "summary":       summary_line(matchup, system="h2h"),
        "cats_winning":  matchup.cats_winning,
        "cats_losing":   matchup.cats_losing,
        "cats_tied":     matchup.cats_tied,
        "priority_cats": losing,
    })

    waivers = fetch_waiver_wire(auth, league_id, sport, position="SP")
    try:
        enrich_players(waivers[:100])
    except Exception as e:
        logger.warning("SP enrichment failed: %s", e)

    two_start_now  = {}
    two_start_next = {}
    try:
        two_start_now = two_start_pitchers()
        if _today_et().weekday() >= 3:
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

    cat_status = {c.category: {"winning": c.winning}
                  for c in matchup.category_standings}
    sp_recs = rank_streaming_sps(waivers, cat_status, two_starters=two_start_now)
    if sp_recs:
        actions.append({
            "type":            "streaming_sp",
            "recommendations": sp_recs,
            "note":            "Submit adds before Monday scoring period lock.",
        })

    if two_start_next:
        next_two_recs = rank_streaming_sps(waivers, cat_status,
                                           two_starters=two_start_next,
                                           max_results=5)
        if next_two_recs:
            actions.append({
                "type":            "streaming_sp_next_week",
                "recommendations": next_two_recs,
                "note":            "2-starters for NEXT week -- add now before lock.",
            })

    if losing:
        all_waivers = fetch_waiver_wire(auth, league_id, sport,
                                        position="all", limit=200)
        try:
            enrich_players(all_waivers)
        except Exception as e:
            logger.warning("Waiver enrichment failed: %s", e)
        waiver_recs = _waiver_adds_for_cats(all_waivers, losing)
        if waiver_recs:
            actions.append({"type": "waiver_adds", "recommendations": waiver_recs})

    _add_lineup_advice(actions, team)

    league_name = cfg.get("name") or cfg.get("display_name") or league_id
    return {
        "league": league_name,
        "format": "H2H Categories",
        "matchup": {
            "week":        matchup.week,
            "score":       f"{matchup.cats_winning}-{matchup.cats_losing}-{matchup.cats_tied}",
            "losing_cats": losing,
        },
        "actions": actions,
    }


# -- Rotisserie (Casey Stengel) -----------------------------------------------

def _roto_decisions(auth: CBSAuth, league_id: str,
                    cfg: dict, team: Team, sport: str) -> dict:
    actions = []

    warnings = check_nl_eligibility(team.players())
    if warnings:
        actions.append({"type": "nl_eligibility_warnings", "warnings": warnings})

    all_waivers = fetch_waiver_wire(auth, league_id, sport,
                                    position="all", limit=200)
    try:
        enrich_players(all_waivers)
    except Exception as e:
        logger.warning("Waiver enrichment failed: %s", e)

    nl_waivers = filter_nl_waiver_pool(all_waivers, cfg)
    _FAKE = {"PS", "TS"}
    nl_waivers = [wp for wp in nl_waivers
                  if wp.player.positions
                  and not all(p in _FAKE for p in wp.player.positions)]

    if nl_waivers:
        waiver_recs = _waiver_adds_for_cats(
            nl_waivers, ["SB", "HR", "RBI", "K", "SV", "ERA"])
        if not waiver_recs:
            waiver_recs = [
                {"player": wp.player.name, "team": wp.player.team,
                 "positions": wp.player.positions, "helps_cats": []}
                for wp in nl_waivers[:5]
            ]
        actions.append({"type": "waiver_adds", "recommendations": waiver_recs})

    try:
        raw_stats = fetch_matchup_stats(auth, league_id, sport)
        matchup   = analyze_matchup(raw_stats, week=_current_week())
        losing    = priority_categories(matchup)
        actions.append({
            "type":      "roto_summary",
            "summary":   summary_line(matchup, system="roto"),
            "weak_cats": losing[:5],
        })
    except Exception as e:
        logger.warning("Roto scoring fetch failed: %s", e)

    _add_lineup_advice(actions, team)

    league_name = cfg.get("name") or cfg.get("display_name") or league_id
    return {
        "league":  league_name,
        "format":  "NL-Only Rotisserie",
        "actions": actions,
    }


# -- Helpers ------------------------------------------------------------------

def _add_lineup_advice(actions: list, team: Team) -> None:
    try:
        teams_today    = teams_playing_today()
        starters_today = probable_starters_today()
        today_str      = _today_et().strftime("%a %b %-d")

        lineup_slots = []
        for rs in team.roster:
            lineup_slots.append({
                "player_name": rs.player.name,
                "team":        rs.player.team,
                "positions":   rs.player.positions,
                "slot":        rs.slot,
                "is_starting": rs.is_starting,
            })

        advice = optimize_daily_lineup(lineup_slots, teams_today, starters_today)

        if advice:
            actions.append({
                "type":              "daily_lineup",
                "today":             today_str,
                "teams_playing":     sorted(teams_today),
                "probable_starters": sorted(starters_today),
                "advice": [
                    {
                        "player":      a.player_name,
                        "team":        a.team,
                        "positions":   a.positions,
                        "slot":        a.slot,
                        "is_starting": a.is_starting,
                        "advice":      a.advice,
                        "reason":      a.reason,
                    }
                    for a in advice
                ],
            })
    except Exception as e:
        logger.warning("Daily lineup advice failed: %s", e)


def _waiver_adds_for_cats(waivers, losing_cats: list) -> list:
    CAT_POSITIONS = {
        "SB":  ["OF", "SS", "2B"],
        "HR":  ["1B", "OF", "3B"],
        "R":   ["OF", "SS", "2B"],
        "RBI": ["1B", "3B", "OF"],
        "AVG": ["OF", "1B", "2B", "SS", "3B", "C"],
        "K":   ["SP", "RP"],
        "SV":  ["RP"],
        "QS":  ["SP"],
        "W":   ["SP"],
        "ERA": ["SP"],
        "WHIP":["SP"],
    }
    CAT_SORT_STAT = {
        "SB":  ("SB",   False),
        "HR":  ("HR",   False),
        "R":   ("R",    False),
        "RBI": ("RBI",  False),
        "AVG": ("AVG",  False),
        "K":   ("K",    False),
        "SV":  ("SV",   False),
        "QS":  ("QS",   False),
        "W":   ("W",    False),
        "ERA": ("ERA",  True),
        "WHIP":("WHIP", True),
    }

    recs = []
    for wp in waivers:
        helps = [c for c in losing_cats if any(
            pos in CAT_POSITIONS.get(c, []) for pos in wp.player.positions
        )]
        if helps and wp.player.positions:
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

    recs.sort(key=lambda r: r.pop("_score"), reverse=True)
    return recs[:5]


def _current_week() -> int:
    from datetime import date
    opening_day = date(2026, 3, 26)
    delta = (date.today() - opening_day).days
    return max(1, delta // 7 + 1)
