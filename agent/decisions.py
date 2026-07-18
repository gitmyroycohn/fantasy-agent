"""
Decision engine -- runs per league and produces a list of recommended actions.
"""
import logging
from data.models import Team, Matchup
from mlb.teams import norm_name as _norm_name
from sports.baseball.streaming import rank_streaming_sps
from sports.baseball.categories import (
    analyze_matchup, priority_categories, summary_line,
    check_nl_eligibility, filter_nl_waiver_pool, validate_scoring_config,
)
from sports.baseball.drops import find_drop_candidates
from cbs.waivers import fetch_waiver_wire
from cbs.stats import fetch_matchup_stats
from cbs.auth import CBSAuth
from mlb.stats import enrich_players
from mlb.schedule import (
    two_start_pitchers, week_bounds, _today_et,
    teams_playing_today, probable_starters_today,
    schedule_weeks, back_to_back_two_starters,
)
from sports.baseball.lineup_optimizer import optimize_daily_lineup
from config.settings import FANTASYPROS_API_KEY
from fantasypros.client import FantasyProsClient, enrich_with_fp_projections
from closermonkey.client import CloserMonkeyClient
from savant.client import SavantClient, enrich_with_savant
from agent.tradevalue import analyze_roster_value
from cbs.standings import fetch_all_teams_stats
from agent.surplusmap import build_surplus_map, trade_leads_from_map, my_category_profile
from mlb.injuries import fetch_il_transactions, annotate_roster_injuries, format_transactions, fetch_active_il
from mlb.splits import fetch_recent_form as _fetch_recent_form
from config.periods import resolve_period

logger = logging.getLogger(__name__)

_fp_client  = FantasyProsClient(FANTASYPROS_API_KEY) if FANTASYPROS_API_KEY else None
_cm_client  = CloserMonkeyClient()
_sav_client = SavantClient()

# IL status keywords -- skip waiver players flagged with any of these
_IL_KEYWORDS = frozenset({"il", "dl", "injured", "10-day", "15-day", "60-day"})


def _fp_enrich(players, label=""):
    if not _fp_client or not players:
        return
    try:
        n = enrich_with_fp_projections(players, _fp_client)
        tag = f" ({label})" if label else ""
        print(f"  FP projections{tag}: {n}/{len(players)} matched")
    except Exception as e:
        tag = f" ({label})" if label else ""
        print(f"  FP projections{tag} failed: {e}")
        logger.warning("FP enrichment failed%s: %s", tag, e)


def _sav_enrich(players, label=""):
    if not players:
        return
    try:
        n = enrich_with_savant(players, _sav_client)
        tag = f" ({label})" if label else ""
        print(f"  Savant xStats{tag}: {n}/{len(players)} matched")
    except Exception as e:
        tag = f" ({label})" if label else ""
        print(f"  Savant xStats{tag} failed: {e}")


def run_decisions(auth, league_id, league_config, team, sport="baseball"):
    fmt = league_config.get("format")
    if fmt == "h2h_categories":
        return _h2h_decisions(auth, league_id, league_config, team, sport)
    elif fmt == "rotisserie":
        return _roto_decisions(auth, league_id, league_config, team, sport)
    else:
        raise ValueError(f"Unknown league format: {fmt}")


# -- H2H ------------------------------------------------------------------

def _h2h_decisions(auth, league_id, cfg, team, sport):
    actions = []

    raw_stats = fetch_matchup_stats(auth, league_id, sport)
    # BUG 6 fix: cross-check leagues.yaml's configured categories against
    # what CBS actually scores -- logs a WARNING on drift.
    validate_scoring_config(cfg.get("scoring", {}), raw_stats, cfg.get("name", league_id))
    matchup   = analyze_matchup(raw_stats, week=_resolve_week(raw_stats))
    losing    = priority_categories(matchup)

    actions.append({
        "type":          "matchup_summary",
        "summary":       summary_line(matchup, system="h2h"),
        "cats_winning":  matchup.cats_winning,
        "cats_losing":   matchup.cats_losing,
        "cats_tied":     matchup.cats_tied,
        "priority_cats": losing,
        # ENH 6: full category set (all scored categories, not just losing
        # ones), so output never obscures what the league actually scores.
        "category_standings": [
            {
                "category":  c.category,
                "my_value":  c.my_value,
                "opp_value": c.opp_value,
                "winning":   c.winning,
                "tied":      c.gap == 0,
            }
            for c in matchup.category_standings
        ],
    })

    waivers = fetch_waiver_wire(auth, league_id, sport, position="SP")
    try:
        enrich_players(waivers[:100])
    except Exception as e:
        logger.warning("SP enrichment failed: %s", e)
    _fp_enrich(waivers[:100], "SP wire")

    # 3-week schedule lookahead
    sched_weeks   = []
    two_start_now = {}
    bb_two_starters = set()   # back-to-back 2-starters (elite holds)
    try:
        sched_weeks     = schedule_weeks(n=3)
        two_start_now   = sched_weeks[0]["two_starters"] if sched_weeks else {}
        bb_two_starters = back_to_back_two_starters(sched_weeks, min_weeks=2)
        # BUG 5 item 5: label with the real period number and days, and
        # break out 3+ start SPs separately since extended periods (e.g. the
        # 14-day Period 16) can produce them.
        week_labels = [
            f"P{w['period']}({w['period_days']}d):{len(w['two_starters'])}x2+"
            + (f"/{len(w['multi_starters'])}x3+" if w.get("multi_starters") else "")
            for w in sched_weeks
        ]
        print(f"  Schedule (3 periods): {' | '.join(week_labels)}")
        if bb_two_starters:
            _norm = _norm_name
            bb_on_wire = [wp.player.name for wp in waivers
                          if _norm(wp.player.name) in bb_two_starters]
            if bb_on_wire:
                print(f"  Back-to-back 2-starters on wire: {', '.join(bb_on_wire[:8])}")
    except Exception as e:
        logger.warning("Schedule fetch failed: %s", e)

    cat_status = {c.category: {"winning": c.winning}
                  for c in matchup.category_standings}
    sp_recs = rank_streaming_sps(waivers, cat_status, two_starters=two_start_now)
    if sp_recs:
        # Annotate back-to-back 2-starters
        for r in sp_recs:
            if _norm_name(r["player"]) in bb_two_starters:
                r["back_to_back"] = True
        actions.append({
            "type":            "streaming_sp",
            "recommendations": sp_recs,
            "note":            "Submit adds before Monday scoring period lock.",
        })

    # Week 2 and 3 two-starters
    for week in sched_weeks[1:]:
        if week["two_starters"]:
            next_two_recs = rank_streaming_sps(waivers, cat_status,
                                               two_starters=week["two_starters"],
                                               max_results=5)
            if next_two_recs:
                offset = week["week_offset"]
                for r in next_two_recs:
                    if _norm_name(r["player"]) in bb_two_starters:
                        r["back_to_back"] = True
                actions.append({
                    "type":            "streaming_sp_next_week",
                    "recommendations": next_two_recs,
                    "week_offset":     offset,
                    "note":            f"Week +{offset} 2-starters -- add now before lock.",
                })

    if losing:
        all_waivers = fetch_waiver_wire(auth, league_id, sport,
                                        position="all", limit=200)
        try:
            enrich_players(all_waivers)
        except Exception as e:
            logger.warning("Waiver enrichment failed: %s", e)
        _fp_enrich(all_waivers, "all wire")
        _sav_enrich(all_waivers, "all wire")
        waiver_recs = _waiver_adds_for_cats(all_waivers, losing)
        if waiver_recs:
            actions.append({"type": "waiver_adds", "recommendations": waiver_recs})
        _add_drop_candidates(actions, team, all_waivers, nl_only=False,
                              stash_names=cfg.get("prospect_stash"))
    else:
        _add_drop_candidates(actions, team, [], nl_only=False,
                             stash_names=cfg.get("prospect_stash"))

    _add_closer_news(actions)
    _add_injury_report(actions, team)
    _add_trade_signals(actions, team, auth=auth, league_id=league_id,
                       my_tid=cfg.get("cbs_team_id"), sport=sport)
    _add_trade_leads(actions, auth, league_id, cfg, system="h2h")
    _add_lineup_advice(actions, team, no_bench=cfg.get("no_bench", False), league_cfg=cfg)

    league_name = cfg.get("name") or league_id
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


# -- Rotisserie -----------------------------------------------------------

def _roto_decisions(auth, league_id, cfg, team, sport):
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
    _sav_enrich(all_waivers, "roto wire")

    nl_waivers = filter_nl_waiver_pool(all_waivers, cfg)
    _FAKE = {"PS", "TS"}
    nl_waivers = [wp for wp in nl_waivers
                  if wp.player.positions
                  and not all(p in _FAKE for p in wp.player.positions)]

    if nl_waivers:
        # "S" (not "SV") is CBS's real saves category key for this league
        # too, confirmed live 2026-07-18 -- _waiver_adds_for_cats's
        # CAT_POSITIONS accepts both as aliases either way.
        waiver_recs = _waiver_adds_for_cats(
            nl_waivers, ["SB", "HR", "RBI", "K", "S", "ERA"])
        if not waiver_recs:
            waiver_recs = [
                {"player": wp.player.name, "team": wp.player.team,
                 "positions": wp.player.positions, "helps_cats": [],
                 "_stats": {}}
                for wp in nl_waivers[:5]
            ]
        actions.append({"type": "waiver_adds", "recommendations": waiver_recs})

    _add_drop_candidates(actions, team, nl_waivers, nl_only=True,
                         stash_names=cfg.get("prospect_stash"))

    try:
        raw_stats = fetch_matchup_stats(auth, league_id, sport)
        # BUG 6 fix: cross-check leagues.yaml's configured categories against
        # what CBS actually scores -- logs a WARNING on drift. casey_stengel's
        # categories are already correct, but this keeps them from silently
        # drifting in the future too.
        validate_scoring_config(cfg.get("scoring", {}), raw_stats, cfg.get("name", league_id))
        matchup   = analyze_matchup(raw_stats, week=_resolve_week(raw_stats))
        losing    = priority_categories(matchup)
        actions.append({
            "type":      "roto_summary",
            "summary":   summary_line(matchup, system="roto"),
            "weak_cats": losing[:5],
            # ENH 6: full category set with roto rank for every category.
            "category_standings": [
                {
                    "category": c.category,
                    "my_value": c.my_value,
                    "rank":     c.rank,
                    "rotopts":  c.rotopts,
                    "dif":      c.dif,
                    "winning":  c.winning,
                }
                for c in matchup.category_standings
            ],
        })
    except Exception as e:
        logger.warning("Roto scoring fetch failed: %s", e)

    _add_closer_news(actions)
    _add_injury_report(actions, team)
    _add_trade_signals(actions, team, auth=auth, league_id=league_id,
                       my_tid=cfg.get("cbs_team_id"), sport=sport)
    _add_trade_leads(actions, auth, league_id, cfg, system="roto")
    _add_lineup_advice(actions, team, no_bench=cfg.get("no_bench", False), league_cfg=cfg)

    league_name = cfg.get("name") or league_id
    return {
        "league":  league_name,
        "format":  "NL-Only Rotisserie",
        "actions": actions,
    }


# -- Helpers --------------------------------------------------------------

def _add_drop_candidates(actions, team, waiver_wire, nl_only=False, stash_names=None):
    try:
        drops = find_drop_candidates(
            team.roster, waiver_wire, nl_only=nl_only, stash_names=stash_names
        )
        if drops:
            actions.append({"type": "drop_candidates", "drops": drops})
    except Exception as e:
        logger.warning("Drop candidate analysis failed: %s", e)


_MUST_START_OPS = 0.850   # ENH 3: batters at or above this OPS are always active
_LINEUP_PITCHER_POS = {"SP", "RP", "P"}

def _add_lineup_advice(actions, team, no_bench=False, league_cfg=None):
    league_cfg = league_cfg or {}
    try:
        teams_today    = teams_playing_today()
        starters_today = probable_starters_today()
        _d = _today_et()
        today_str = f"{_d.strftime('%a %b')} {_d.day}"

        # BUG 5 fix: cross-reference the live MLB injured list so the lineup
        # optimizer never tells you to activate a player who is actually hurt.
        # Independent of the probable-starters feed (can lag a real IL move)
        # and independent of CBS's own roster slot (only moves to IL when the
        # commissioner/manager does it manually there).
        try:
            il_norms = set(fetch_active_il().keys())
        except Exception as e:
            logger.warning("fetch_active_il failed, IL cross-check skipped: %s", e)
            il_norms = set()

        # ENH 3: enrich roster batters with L/R platoon splits so the
        # optimizer can weight today's start/sit advice by handedness.
        try:
            from mlb.splits import enrich_with_splits
            enrich_with_splits(team.roster)
        except Exception as e:
            logger.warning("Splits enrichment failed, platoon weighting skipped: %s", e)

        # ENH 3: map each CBS team abbrev to the hand of the opposing
        # probable starter their batters face today.
        opp_hand_by_team: dict[str, str] = {}
        try:
            from mlb.schedule import todays_matchups
            for m in todays_matchups():
                if m.get("home_team") and m.get("away_starter_hand"):
                    opp_hand_by_team[m["home_team"].upper()] = m["away_starter_hand"]
                if m.get("away_team") and m.get("home_starter_hand"):
                    opp_hand_by_team[m["away_team"].upper()] = m["home_starter_hand"]
        except Exception as e:
            logger.warning("Today's matchups fetch failed, platoon weighting skipped: %s", e)

        # ENH 2: full CBS position eligibility per roster player (2B/SS, etc.)
        # plus any per-league static rule (pins_and_pills: all players
        # DH-eligible), so the optimizer/swap-finder can propose every
        # legal slot, not just the player's current slot tag.
        from cbs.players import apply_league_eligibility_rules

        # ENH 4/7: official posted lineups (confirmed/expected/not-in-lineup).
        try:
            from mlb.lineups import fetch_posted_lineups, lineup_status_for
            posted = fetch_posted_lineups()
        except Exception as e:
            logger.warning("Posted-lineup fetch failed, falling back to schedule-only: %s", e)
            posted = {"players": {}, "posted_teams": set()}
            lineup_status_for = lambda name, team, posted: "unknown"  # noqa: E731

        lineup_slots = [
            {
                "player_name":        rs.player.name,
                "team":               rs.player.team,
                "positions":          rs.player.positions,
                "eligible_positions": apply_league_eligibility_rules(
                                          rs.player.eligible_positions, league_cfg),
                "slot":               rs.slot,
                "is_starting":        rs.is_starting,
                "stats":              rs.player.stats or {},
                "lineup_status":      lineup_status_for(rs.player.name, rs.player.team, posted),
                "batting_order":      posted.get("players", {})
                                            .get(_norm_name(rs.player.name), {})
                                            .get("batting_order"),
            }
            for rs in team.roster
        ]

        advice = optimize_daily_lineup(lineup_slots, teams_today, starters_today,
                                       il_players=il_norms,
                                       opp_hand_by_team=opp_hand_by_team)

        # ENH 3: Must-start floor -- elite batters (season OPS >= .850) should
        # always be active regardless of park factor, L/R matchup, or schedule
        # ambiguity.  Override "bench" -> "ok" for these players so we never
        # accidentally tell the user to sit a stud on a "questionable" off day
        # -- this also overrides a platoon-driven down-rank for the same bats
        # (but never an ENH 4/7 "out_of_lineup" confirmed absence -- see
        # sports/baseball/lineup_optimizer.apply_must_start_floor).
        ops_by_norm = {
            _norm_name(rs.player.name): float((rs.player.stats or {}).get("OPS") or 0)
            for rs in team.roster
        }
        from sports.baseball.lineup_optimizer import apply_must_start_floor
        apply_must_start_floor(advice, ops_by_norm, floor=_MUST_START_OPS)

        # ENH 2: propose legal bench -> active swaps (every eligible slot
        # considered; never an illegal swap) for anyone advised to sit.
        swaps = []
        try:
            from sports.baseball.lineup_optimizer import find_legal_swaps
            swaps = find_legal_swaps(lineup_slots, advice)
        except Exception as e:
            logger.warning("Legal swap search failed: %s", e)

        if advice:
            actions.append({
                "type":              "daily_lineup",
                "today":             today_str,
                "teams_playing":     sorted(teams_today),
                "probable_starters": sorted(starters_today),
                "no_bench":          no_bench,
                "swaps":             swaps,
                "advice": [
                    {
                        "player":        a.player_name,
                        "team":          a.team,
                        "positions":     a.positions,
                        "slot":          a.slot,
                        "is_starting":   a.is_starting,
                        "advice":        a.advice,
                        "reason":        a.reason,
                        # ENH 4/7: confirmed / expected / not-in-lineup label
                        "lineup_status": a.lineup_status,
                        "lineup_label":  a.lineup_label,
                        "batting_order": a.batting_order,
                    }
                    for a in advice
                ],
            })
    except Exception as e:
        logger.warning("Daily lineup advice failed: %s", e)


def _add_closer_news(actions):
    try:
        reactions = _cm_client.rapid_reactions(limit=5)
        ledger    = _cm_client.leverage_ledger(limit=1)
        posts = reactions + [p for p in ledger if p not in reactions]
        if posts:
            actions.append({"type": "closer_news", "posts": posts})
    except Exception as e:
        logger.warning("CM news fetch failed: %s", e)


def get_filtered_waiver_adds(
    auth, league_id, league_cfg, sport="baseball",
    position_filter: str | None = None,
    playing_on=None,          # date object -- only players whose team plays that day
    min_batters: int = 2,
    limit: int = 10,
    week_offset: int = 0,     # 0 = current scoring week, 1 = next week, etc.
):
    """Direct waiver fetch with optional position, game-day, and week filters.

    Bypasses the full run_decisions pipeline so it's fast and composable.
    Called by the MCP waiver_recommendations tool.

    week_offset=1 looks ahead to the next CBS scoring period: 2-start SPs for
    that week get a score boost so they surface above equivalent one-starters.
    """
    from cbs.waivers import fetch_waiver_wire
    from mlb.stats import enrich_players
    from mlb.schedule import teams_playing_today, schedule_weeks, back_to_back_two_starters
    from cbs.stats import fetch_matchup_stats
    from sports.baseball.categories import analyze_matchup, priority_categories
    _norm = _norm_name

    # Fetch the full free-agent pool (no limit here -- CBS returns ~8400 players
    # in one call; filtering + enrichment happen below on the relevant subset).
    waivers = fetch_waiver_wire(auth, league_id, sport, position="all", limit=0)
    try:
        enrich_players(waivers)
    except Exception as e:
        logger.warning("Waiver enrichment failed: %s", e)
    _fp_enrich(waivers, "wire")
    _sav_enrich(waivers, "wire")

    # Enrich with recent form (last 14 days) -- hot streak component for scorer
    try:
        recent_data = _fetch_recent_form(14)
        for wp in waivers:
            p = wp.player
            key = _norm_name(p.name)
            if key in recent_data:
                if p.stats is None:
                    p.stats = {}
                for stat, val in recent_data[key].items():
                    p.stats[f"recent_{stat}"] = val
    except Exception as e:
        logger.warning("Recent-form enrichment failed: %s", e)

    # IL filter (Bug 2) -- skip players currently on the 10/15/60-day IL.
    # CBS player.status contains strings like "IL10", "IL15", "IL60", or "DL".
    pre_il = len(waivers)
    waivers = [
        wp for wp in waivers
        if not any(
            kw in (getattr(wp.player, "status", None) or "").lower()
            for kw in _IL_KEYWORDS
        )
    ]
    il_removed = pre_il - len(waivers)
    if il_removed:
        logger.info("IL filter: removed %d injured players from waiver pool", il_removed)

    # Recently-dropped filter (Bug 4) -- suppress players flagged "cut" 2+ times.
    # Reads logs/history.json so no extra API calls are needed.
    try:
        from agent.history import load_history
        _hist = load_history()
        _rd = _hist.get(str(league_id), {}).get("recently_dropped", {})
        if _rd:
            _rd_norm = {_norm(k) for k in _rd}
            pre_rd = len(waivers)
            waivers = [wp for wp in waivers
                       if _norm(wp.player.name) not in _rd_norm]
            rd_removed = pre_rd - len(waivers)
            if rd_removed:
                logger.info(
                    "recently_dropped filter: suppressed %d previously-dropped players",
                    rd_removed,
                )
    except Exception as e:
        logger.warning("recently_dropped filter failed: %s", e)

    # Position filter
    # CBS stores outfielders as LF/CF/RF, not OF -- expand the alias so
    # position="OF" matches all three CBS outfield tags.
    if position_filter:
        pos_up = position_filter.upper()
        if pos_up == "OF":
            pos_set = {"OF", "LF", "CF", "RF"}
        else:
            pos_set = {pos_up}
        waivers = [wp for wp in waivers
                   if pos_set & set(wp.player.positions or [])]

    # NL-only filter -- drop AL players from NL-only leagues (Casey Stengel)
    if league_cfg.get("nl_only") or league_cfg.get("roster_type") == "nl_only":
        try:
            waivers = filter_nl_waiver_pool(waivers, league_cfg)
        except Exception as e:
            logger.warning("NL filter failed: %s", e)

    # Game-day filter (only applies when explicitly requested via date=)
    if playing_on is not None:
        try:
            playing_teams = teams_playing_today(playing_on)
            waivers = [wp for wp in waivers
                       if (wp.player.team or "").upper() in playing_teams]
        except Exception as e:
            logger.warning("Game-day filter failed: %s", e)

    # Determine losing categories from current matchup
    try:
        raw_stats = fetch_matchup_stats(auth, league_id, sport)
        matchup   = analyze_matchup(raw_stats, week=_resolve_week(raw_stats))
        losing    = priority_categories(matchup)
    except Exception:
        # Fallback: treat all categories as targets
        scoring = league_cfg.get("scoring", {})
        losing = (list(scoring.get("hitting", [])) +
                  list(scoring.get("pitching", [])))

    # Two-start map for the target week
    # week_offset=0 -> this week's 2-starters, week_offset=1 -> next week's, etc.
    two_starters: dict[str, int] = {}
    bb_two_starters: set[str] = set()
    try:
        weeks = schedule_weeks(n=max(2, week_offset + 1))
        if week_offset < len(weeks):
            two_starters = weeks[week_offset].get("two_starters", {})
        if week_offset == 0:
            bb_two_starters = back_to_back_two_starters(weeks, min_weeks=2)
    except Exception as e:
        logger.warning("Schedule fetch for week_offset=%d failed: %s", week_offset, e)

    # If position is filtered to pitchers only, batter balance doesn't apply
    PITCHER_POS = {"SP", "RP", "P"}
    _is_pitcher_filter = (position_filter and
                          position_filter.upper() in PITCHER_POS and
                          position_filter.upper() != "OF")
    _min_batters = 0 if _is_pitcher_filter else min_batters

    recs = _waiver_adds_for_cats(
        waivers, losing,
        two_starters=two_starters,
        bb_two_starters=bb_two_starters,
        min_batters=_min_batters,
        limit=limit,
    )
    return recs


def _waiver_adds_for_cats(
    waivers, losing_cats, *,
    two_starters: dict | None = None,
    bb_two_starters: set | None = None,
    min_batters: int = 2,
    limit: int = 5,
):
    """Score and rank waiver wire pickups using a four-component composite score:

      1. Ownership %      -- crowd signal for overall value (strongest weight)
      2. YTD rate stats   -- per-game/per-IP rates, not raw counts
      3. FP ROS proj      -- rest-of-season projections override YTD rates when available
      4. Quality signals  -- OPS for batters, K9 for pitchers, Savant xwOBA/xERA/barrel%
    """
    # BUG (found in 2026-07-18 live run): CBS's REAL category keys for both
    # leagues are "S" (saves) and "BA" (batting average) -- confirmed by
    # validate_scoring_config()'s drift check, which caught casey_stengel
    # still configured with "AVG"/"SV" while CBS's live_scoring actually
    # returns "BA"/"S" (leagues.yaml corrected to match). losing_cats here
    # comes straight from those real CBS category names, so this dict (and
    # every score-boost check below) must recognize them. Both the CBS name
    # and the old assumed name are listed as aliases so this is robust
    # either way.
    CAT_POSITIONS = {
        "SB":  ["LF", "CF", "RF", "OF", "SS", "2B"],
        "HR":  ["1B", "LF", "CF", "RF", "OF", "3B"],
        "R":   ["LF", "CF", "RF", "OF", "SS", "2B"],
        "RBI": ["1B", "3B", "LF", "CF", "RF", "OF"],
        "AVG": ["LF", "CF", "RF", "OF", "1B", "2B", "SS", "3B", "C"],
        "BA":  ["LF", "CF", "RF", "OF", "1B", "2B", "SS", "3B", "C"],
        "K":   ["SP", "RP"],
        "SV":  ["RP"],
        "S":   ["RP"],
        "QS":  ["SP"],
        "W":   ["SP"],
        "ERA": ["SP"],
        "WHIP":["SP"],
    }

    BATTER_POS = {"C", "1B", "2B", "3B", "SS", "OF", "DH", "U", "CF", "LF", "RF"}
    PITCHER_POS = {"SP", "RP", "P"}

    def _rate(stats, count_key, opp_key, scale, min_opp=10):
        count = float(stats.get(count_key) or 0)
        opp   = float(stats.get(opp_key) or 0)
        if opp < min_opp:
            return 0.0
        return (count / opp) * scale

    def _fp(stats, key):
        v = stats.get(f"fp_{key.lower()}")
        return float(v) if v is not None else None

    recs = []
    for wp in waivers:
        helps = [c for c in losing_cats if any(
            pos in CAT_POSITIONS.get(c, []) for pos in wp.player.positions
        )]
        if not helps or not wp.player.positions:
            continue

        stats      = wp.player.stats or {}
        is_batter  = any(p in BATTER_POS  for p in wp.player.positions)
        is_pitcher = any(p in PITCHER_POS for p in wp.player.positions)
        g   = float(stats.get("G")  or 0)
        gs  = float(stats.get("GS") or 0)
        ip  = float(stats.get("IP") or 0)
        own = float(wp.ownership_pct or 0)

        score = own * 20.0

        if "SB" in helps:
            fp_v = _fp(stats, "SB")
            score += fp_v * 4.0 if fp_v is not None else _rate(stats, "SB", "G", 300)
        if "HR" in helps:
            fp_v = _fp(stats, "HR")
            score += fp_v * 3.0 if fp_v is not None else _rate(stats, "HR", "G", 600)
        if "R" in helps:
            fp_v = _fp(stats, "R")
            score += fp_v * 1.0 if fp_v is not None else _rate(stats, "R", "G", 150)
        if "RBI" in helps:
            fp_v = _fp(stats, "RBI")
            score += fp_v * 1.0 if fp_v is not None else _rate(stats, "RBI", "G", 150)
        if "AVG" in helps or "BA" in helps:
            fp_v = _fp(stats, "AVG")
            avg  = fp_v if fp_v is not None else float(stats.get("AVG") or 0)
            score += avg * 200
        if "K" in helps:
            fp_v = _fp(stats, "K")
            score += fp_v * 0.8 if fp_v is not None else _rate(stats, "K", "IP", 72, min_opp=10)
        if "SV" in helps or "S" in helps:
            fp_v = _fp(stats, "SV")
            score += fp_v * 4.0 if fp_v is not None else _rate(stats, "SV", "G", 300)
        if "W" in helps:
            fp_v = _fp(stats, "W")
            score += fp_v * 5.0 if fp_v is not None else _rate(stats, "W", "GS", 150, min_opp=5)
        if "QS" in helps:
            fp_v = _fp(stats, "QS")
            score += fp_v * 3.0 if fp_v is not None else _rate(stats, "QS", "GS", 150, min_opp=5)
        if "ERA" in helps:
            fp_v = _fp(stats, "ERA")
            era  = fp_v if fp_v is not None else float(stats.get("ERA") or 0)
            if era > 0:
                score -= era * 15
        if "WHIP" in helps:
            fp_v = _fp(stats, "WHIP")
            whip = fp_v if fp_v is not None else float(stats.get("WHIP") or 0)
            if whip > 0:
                score -= whip * 40

        if is_batter and g >= 20:
            ops = float(stats.get("OPS") or 0)
            if ops > 0.600:
                score += (ops - 0.600) * 100

        if is_pitcher and ip >= 10:
            k9 = float(stats.get("K9") or 0)
            score += k9 * 2

        if g >= 20 or ip >= 10:
            barrel = stats.get("sv_barrel_pct")
            if barrel is not None and any(c in helps for c in ("HR", "RBI", "SB")):
                score += max(0.0, barrel - 8.0) * 5.0
            xwoba = stats.get("sv_xwoba")
            if xwoba is not None:
                score += xwoba * 80.0
            xera = stats.get("sv_xera")
            if xera is not None and any(c in helps for c in ("K", "ERA", "WHIP", "W")):
                score -= xera * 5.0

        if is_batter:
            recent_games = int(stats.get("recent_games") or 0)
            if recent_games >= 5:
                recent_ops = float(stats.get("recent_ops") or 0)
                recent_hr  = int(stats.get("recent_hr") or 0)
                if recent_ops > 0.600:
                    score += (recent_ops - 0.700) * 30.0
                score += recent_hr * 3.0

        score += len(helps) * 3

        is_two_starter = False
        is_bb_two_starter = False
        if two_starters or bb_two_starters:
            _n = _norm_name(wp.player.name)
            if two_starters and _n in two_starters:
                n_starts = two_starters[_n]
                score += 20.0 * n_starts
                is_two_starter = True
            if bb_two_starters and _n in bb_two_starters:
                score += 10.0
                is_bb_two_starter = True

        stat_parts = []
        if own > 0:
            stat_parts.append(f"own={own:.0f}%")
        if g > 0:
            stat_parts.append(f"G={int(g)}")
        if is_batter:
            for k in ("SB", "HR", "R", "RBI", "AVG", "OPS"):
                v = stats.get(k)
                if v:
                    if k in ("AVG", "OPS"):
                        stat_parts.append(f"{k}={float(v):.3f}")
                    else:
                        stat_parts.append(f"{k}={int(v)}")
            rg = int(stats.get("recent_games") or 0)
            if rg >= 5:
                r_ops = stats.get("recent_ops")
                r_hr  = stats.get("recent_hr")
                if r_ops:
                    stat_parts.append(f"L14_OPS={float(r_ops):.3f}({rg}G)")
                if r_hr:
                    stat_parts.append(f"L14_HR={r_hr}")
        if is_pitcher:
            for k in ("K", "SV", "W", "QS", "ERA", "WHIP", "K9"):
                v = stats.get(k)
                if v:
                    if k in ("ERA", "WHIP", "K9"):
                        stat_parts.append(f"{k}={float(v):.2f}")
                    else:
                        stat_parts.append(f"{k}={int(v)}")

        rec = {
            "player":       wp.player.name,
            "team":         wp.player.team,
            "positions":    wp.player.positions,
            "ownership":    own,
            "helps_cats":   helps,
            "_score":       score,
            "_stats":       stats,
            "_stat_line":   " | ".join(stat_parts),
            "two_starter":  is_two_starter,
            "back_to_back": is_bb_two_starter,
        }

        if "SV" in helps or "S" in helps:
            try:
                cm = _cm_client.find_player(wp.player.name)
                if cm:
                    rec["cm_role"]      = cm["role"]
                    rec["cm_tendency"]  = cm["tendency"]
                    rec["cm_committee"] = cm["committee"]
            except Exception:
                pass

        recs.append(rec)

    recs.sort(key=lambda r: r["_score"], reverse=True)

    if min_batters > 0:
        def _is_batter(r):
            return any(p in BATTER_POS for p in (r.get("positions") or []))
        scored_batters = [r for r in recs if _is_batter(r) and r["_score"] > 0]
        pinned_ids     = {id(r) for r in scored_batters[:min_batters]}
        guaranteed     = scored_batters[:min_batters]
        remaining      = [r for r in recs if id(r) not in pinned_ids]
        final = guaranteed + remaining[:limit - len(guaranteed)]
        final.sort(key=lambda r: r["_score"], reverse=True)
    else:
        final = recs[:limit]

    for r in final:
        r.pop("_score", None)
    return final


def _add_trade_signals(actions: list, team, auth=None, league_id=None,
                       my_tid=None, sport="baseball") -> None:
    """Analyze roster for sell-high opportunities; scan opponent rosters for buy-low targets.

    SELL HIGH: own players whose stats lag projections (slump due to luck, not skill).
    BUY LOW:   opponent players whose stats lag projections -- targets to acquire cheap.

    Enriches rosters with FP ROS projections + Savant xStats before analysis.
    """
    try:
        _fp_enrich(team.roster, "roster")
        _sav_enrich(team.roster, "roster")
        my_signals = [s for s in analyze_roster_value(team.roster)
                      if s["signal"] == "sell_high"]

        buy_targets = []
        if auth and league_id:
            try:
                from cbs.roster import get_all_team_rosters
                all_rosters = get_all_team_rosters(auth, league_id, sport)
                tid_str = str(my_tid) if my_tid else ""
                for other_tid, info in all_rosters.items():
                    if str(other_tid) == tid_str:
                        continue
                    other_roster = info["roster"]
                    _fp_enrich(other_roster, f"team {other_tid}")
                    _sav_enrich(other_roster, f"team {other_tid}")
                    for s in analyze_roster_value(other_roster):
                        if s["signal"] == "buy_low":
                            buy_targets.append({**s, "_owner": info["name"]})
                buy_targets.sort(
                    key=lambda x: (0 if x.get("confidence") == "strong" else 1))
            except Exception as e:
                logger.warning("buy-low scan failed: %s", e)

        combined = my_signals + buy_targets
        if combined:
            actions.append({"type": "trade_signals", "signals": combined})
    except Exception as e:
        logger.warning("Trade signal analysis failed: %s", e)


def _add_trade_leads(actions: list, auth, league_id: str, cfg: dict,
                    system: str = "roto") -> None:
    """Fetch all teams' category stats and surface trade partner leads."""
    try:
        scoring = cfg.get("scoring", {})
        scoring_cats = list(scoring.get("hitting", [])) + list(scoring.get("pitching", []))
        my_team_id = str(cfg.get("cbs_team_id", ""))

        all_teams = fetch_all_teams_stats(auth, league_id,
                                          sport="baseball", system=system)
        if not all_teams:
            logger.warning("trade_leads: no team stats returned for %s", league_id)
            return

        surplus_map = build_surplus_map(all_teams, scoring_cats)
        profile     = my_category_profile(surplus_map, my_team_id)
        leads       = trade_leads_from_map(surplus_map, my_team_id, top_n=4)

        if profile or leads:
            actions.append({
                "type":    "trade_leads",
                "profile": profile,
                "leads":   leads,
                "n_teams": len(all_teams),
            })
    except Exception as e:
        logger.warning("Trade leads analysis failed: %s", e)


def _add_injury_report(actions: list, team) -> None:
    """Fetch recent IL transactions and cross-reference against roster."""
    try:
        roster_norms = {_norm_name(s.player.name) for s in team.roster}

        txns = fetch_il_transactions(lookback_days=7)
        roster_txns = [t for t in txns if t["norm"] in roster_norms]

        if txns or roster_txns:
            actions.append({
                "type":        "injury_report",
                "transactions": txns,
                "roster_hits":  roster_txns,
                "roster_norms": roster_norms,
            })
    except Exception as e:
        logger.warning("Injury report fetch failed: %s", e)


def _resolve_week(raw_stats: dict):
    """BUG 5 fix: resolve the true scoring period for `week=` in analyze_matchup.

    Previously _current_week() computed the period arithmetically from a
    hardcoded opening day, which was wrong on 81/166 days of the season
    (every Saturday/Sunday, since season_start isn't a Monday, plus the
    non-uniform 12-day Period 1 and 14-day Period 16).

    Now: raw_stats["period"] comes straight from CBS's authoritative
    league/scoring/live response (see cbs/stats.py) -- CBS's own period field
    is the single source of truth. config.periods.resolve_period cross-checks
    it against the local leagues.yaml table purely to log a WARNING on drift;
    CBS always wins.
    """
    cbs_period = raw_stats.get("period")
    try:
        resolved = resolve_period(_today_et(), cbs_period=cbs_period)
        return resolved["n"]
    except Exception as e:
        logger.warning("_resolve_week: could not resolve a period (%s) -- "
                       "using CBS's raw period value as-is", e)
        try:
            return int(str(cbs_period).strip())
        except (TypeError, ValueError):
            return cbs_period or "?"
