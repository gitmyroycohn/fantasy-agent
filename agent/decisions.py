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
from sports.baseball.drops import find_drop_candidates
from cbs.waivers import fetch_waiver_wire
from cbs.stats import fetch_matchup_stats
from cbs.auth import CBSAuth
from mlb.stats import enrich_players
from mlb.schedule import (
    two_start_pitchers, week_bounds, _today_et,
    teams_playing_today, probable_starters_today,
)
from sports.baseball.lineup_optimizer import optimize_daily_lineup
from config.settings import FANTASYPROS_API_KEY
from fantasypros.client import FantasyProsClient, enrich_with_fp_projections
from closermonkey.client import CloserMonkeyClient

logger = logging.getLogger(__name__)

# Module-level clients
_fp_client: FantasyProsClient | None = (
    FantasyProsClient(FANTASYPROS_API_KEY) if FANTASYPROS_API_KEY else None
)
_cm_client = CloserMonkeyClient()


def _fp_enrich(players: list, label: str = "") -> None:
    """Enrich players with FP ROS projections if client is available."""
    if not _fp_client or not players:
        return
    try:
        n = enrich_with_fp_projections(players, _fp_client)
        tag = f" ({label})" if label else ""
        print(f"  FP projections{tag}: {n}/{len(players)} matched")
    except Exception as e:
        tag = f" ({label})" if label else ""
        print(f"  FP projections{tag} failed: {e}")
        logger.warning("FP enrichment failed%s: %s",
                       f" ({label})" if label else "", e)


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
    _fp_enrich(waivers[:100], "SP wire")

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
        _fp_enrich(all_waivers, "all wire")
        waiver_recs = _waiver_adds_for_cats(all_waivers, losing)
        if waiver_recs:
            actions.append({"type": "waiver_adds", "recommendations": waiver_recs})
        _add_drop_candidates(actions, team, all_waivers, nl_only=False)
    else:
        _add_drop_candidates(actions, team, [], nl_only=False)

    _add_closer_news(actions)
    _add_lineup_advice(actions, team, no_bench=cfg.get("no_bench", False))

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
    _fp_enrich(all_waivers, "roto wire")

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

    _add_drop_candidates(actions, team, nl_waivers, nl_only=True)

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

    _add_closer_news(actions)
    _add_lineup_advice(actions, team, no_bench=cfg.get("no_bench", False))

    league_name = cfg.get("name") or cfg.get("display_name") or league_id
    return {
        "league":  league_name,
        "format":  "NL-Only Rotisserie",
        "actions": actions,
    }


# -- Helpers ------------------------------------------------------------------

def _add_drop_candidates(actions: list, team: Team,
                         waiver_wire: list, nl_only: bool = False) -> None:
    try:
        drops = find_drop_candidates(team.roster, waiver_wire, nl_only=nl_only)
        if drops:
            actions.append({"type": "drop_candidates", "drops": drops})
    except Exception as e:
        logger.warning("Drop candidate analysis failed: %s", e)


def _add_lineup_advice(actions: list, team: Team, no_bench: bool = False) -> None:
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
                "no_bench":          no_bench,
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


def _add_closer_news(actions: list) -> None:
    """Fetch CM rapid reactions and leverage ledger; append as closer_news action."""
    try:
        reactions = _cm_client.rapid_reactions(limit=5)
        ledger    = _cm_client.leverage_ledger(limit=1)
        posts = reactions + [p for p in ledger if p not in reactions]
        if posts:
            actions.append({"type": "closer_news", "posts": posts})
    except Exception as e:
        logger.warning("CM news fetch failed: %s", e)


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
    # (stat_key, lower_is_better)
    CAT_SORT_STAT = {
        "SB":  ("sb",   False),
        "HR":  ("hr",   False),
        "R":   ("r",    False),
        "RBI": ("rbi",  False),
        "AVG": ("avg",  False),
        "K":   ("k",    False),
        "SV":  ("sv",   False),
        "QS":  ("QS",   False),
        "W":   ("w",    False),
        "ERA": ("era",  True),
        "WHIP":("whip", True),
    }

    def _stat_val(player_stats: dict, base_key: str, lower: bool) -> float:
        """Check fp_ prefixed key first, then uppercase, then lowercase."""
        if not player_stats:
            return 0.0
        for key in (f"fp_{base_key}", base_key.upper(), base_key.lower(), base_key):
            v = player_stats.get(key)
            if v is not None:
                return float(v)
        return 0.0

    recs = []
    for wp in waivers:
        helps = [c for c in losing_cats if any(
            pos in CAT_POSITIONS.get(c, []) for pos in wp.player.positions
        )]
        if helps and wp.player.positions:
            sort_score = 0.0
            for cat in helps:
                base_key, lower = CAT_SORT_STAT.get(cat, (None, False))
                if base_key:
                    val = _stat_val(wp.player.stats, base_key, lower)
                    sort_score += (-val if lower else val)
            rec = {
                "player":     wp.player.name,
                "team":       wp.player.team,
                "positions":  wp.player.positions,
                "ownership":  wp.ownership_pct,
                "helps_cats": helps,
                "_score":     sort_score,
            }
            # Annotate RP/SV picks with Closer Monkey role
            if "SV" in helps:
                try:
                    cm = _cm_client.find_player(wp.player.name)
                    if cm:
                        rec["cm_role"]      = cm["role"]
                        rec["cm_tendency"]  = cm["tendency"]
                        rec["cm_committee"] = cm["committee"]
                except Exception:
                    pass
            recs.append(rec)

    recs.sort(key=lambda r: r.pop("_score"), reverse=True)
    return recs[:5]


def _current_week() -> int:
    from datetime import date
    opening_day = date(2026, 3, 26)
    delta = (date.today() - opening_day).days
    return max(1, delta // 7 + 1)
