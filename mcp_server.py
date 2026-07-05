"""
Fantasy Baseball Agent -- MCP Server

Exposes the agent's capabilities as tools for Claude Projects / Claude Desktop.

Tools:
  evaluate_trade          -- evaluate a specific trade offer
  daily_decisions         -- run full daily analysis for a league
  get_roster              -- your current roster for a league
  get_team_roster         -- ANY team's current roster, by name (trade research)
  list_league_teams       -- list team names in a league (helper for get_team_roster)
  waiver_recommendations  -- top waiver wire adds
  roster_value_signals    -- buy-low / sell-high signals

Setup (one-time):
  pip install mcp python-dotenv pyyaml requests beautifulsoup4

Add to Claude Desktop / Claude Project config:
  {
    "mcpServers": {
      "fantasy-baseball": {
        "command": "python",
        "args": ["C:/Users/guido/fantasy-agent/mcp_server.py"]
      }
    }
  }
"""
import io
import json
import logging
import sys
import os
import time

_SERVER_START = time.time()   # recorded at cold-start; used to detect warm-up window

# Bootstrap: add repo root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

import yaml
from mcp.server.fastmcp import FastMCP

from config.settings import CBS_COOKIE, FANTASYPROS_API_KEY, DRY_RUN
from cbs.auth import CBSAuth, CBSAuthError
from cbs.roster import get_roster as cbs_get_roster, get_all_team_rosters, resolve_team_id
from mlb.stats import enrich_roster
from fantasypros.client import FantasyProsClient
from savant.client import SavantClient
from agent.trade_eval import evaluate_trade, format_trade_result
from agent.tradevalue import analyze_roster_value
from agent.decisions import run_decisions, get_filtered_waiver_adds
from data.models import Team

logging.basicConfig(level=logging.WARNING,
                    format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Initialise shared clients (once at startup)
# ---------------------------------------------------------------------------

mcp = FastMCP("Fantasy Baseball Agent")

_COLD_START_WINDOW = 90   # seconds after startup considered a "cold start"

def _warmup_notice() -> str:
    """Return a friendly warm-up banner if the server just woke from a cold start."""
    elapsed = int(time.time() - _SERVER_START)
    if elapsed < _COLD_START_WINDOW:
        remaining = _COLD_START_WINDOW - elapsed
        return (
            f"🔄 **Server just woke up from a cold start** (started {elapsed}s ago).\n"
            f"   The first request after idle takes a little longer — "
            f"everything should be fully warm in ~{remaining}s.\n"
            f"   Your results are loading now...\n\n"
        )
    return ""


def _respond(body: str) -> str:
    """Prepend cold-start notice to any tool response when server just woke up."""
    notice = _warmup_notice()
    return notice + body if notice else body

def _load_leagues(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "config", "leagues.yaml")
    with open(path) as f:
        return yaml.safe_load(f) or {}

def _get_auth():
    return CBSAuth(CBS_COOKIE)

def _get_fp():
    if not FANTASYPROS_API_KEY:
        raise RuntimeError("FANTASYPROS_API_KEY not set in .env")
    return FantasyProsClient(FANTASYPROS_API_KEY)

def _get_sav():
    return SavantClient()

def _resolve_leagues(league_id: str) -> list[tuple[dict, str]]:
    """Return list of (league_cfg, sport) matching the requested league_id."""
    config = _load_leagues()
    results = []
    for sport, leagues in config.items():
        for league in (leagues or []):
            lid = league.get("id", league.get("cbs_league_id", ""))
            if league_id in ("all", lid):
                results.append((league, sport))
    return results


# ---------------------------------------------------------------------------
# Tool: evaluate_trade
# ---------------------------------------------------------------------------

@mcp.tool()
def evaluate_trade_tool(
    give: list[str],
    receive: list[str],
    league_id: str = "all",
) -> str:
    """
    Evaluate a fantasy baseball trade offer.

    Args:
        give:      List of player names you would give away.
                   e.g. ["Jarren Duran", "Hunter Brown"]
        receive:   List of player names you would receive.
                   e.g. ["Rafael Devers"]
        league_id: Which league to evaluate for (use league id from config,
                   or "all" to use the first configured league).

    Returns a verdict (ACCEPT / DECLINE / CLOSE) with per-category breakdown.
    """
    try:
        leagues = _resolve_leagues(league_id)
        if not leagues:
            return f"No league found matching '{league_id}'."

        league_cfg, _ = leagues[0]
        fp_client  = _get_fp()
        sav_client = _get_sav()

        result = evaluate_trade(
            give=give,
            receive=receive,
            league_cfg=league_cfg,
            fp_client=fp_client,
            sav_client=sav_client,
        )
        return _respond(format_trade_result(result))

    except Exception as e:
        logger.exception("evaluate_trade failed")
        return f"Error evaluating trade: {e}"


# ---------------------------------------------------------------------------
# Tool: get_roster
# ---------------------------------------------------------------------------

@mcp.tool()
def get_roster(league_id: str = "all") -> str:
    """
    Get your current fantasy roster for a league.

    Args:
        league_id: League id from config, or "all" for all leagues.

    Returns a formatted roster with player names, positions, and stats.
    """
    try:
        auth    = _get_auth()
        leagues = _resolve_leagues(league_id)
        if not leagues:
            return f"No league found matching '{league_id}'."

        out = []
        for league_cfg, sport in leagues:
            lid  = league_cfg["cbs_league_id"]
            tid  = str(league_cfg["cbs_team_id"])
            name = league_cfg.get("name", lid)
            roster = cbs_get_roster(auth, lid, tid, sport)
            try:
                enrich_roster(roster)
            except Exception:
                pass

            out.append(f"=== {name} ({sport}) ===")
            out.append(f"{'Slot':<6} {'Player':<24} {'Team':<5} {'Status'}")
            out.append("-" * 55)
            for rs in roster:
                p = rs.player
                status = p.status or ""
                out.append(f"{rs.slot:<6} {p.name:<24} {(p.team or '?'):<5} {status}")
            out.append("")

        return _respond("\n".join(out))

    except CBSAuthError as e:
        return f"CBS auth error: {e}"
    except Exception as e:
        logger.exception("get_roster failed")
        return f"Error fetching roster: {e}"


# ---------------------------------------------------------------------------
# Tool: get_team_roster -- ANY team in the league, not just your own
# ---------------------------------------------------------------------------

@mcp.tool()
def get_team_roster(league_id: str, team_name: str) -> str:
    """
    Get any team's current roster by name -- useful for trade research,
    scouting an opponent, or checking who's still rosters a player before
    proposing a deal. Not limited to your own team.

    Args:
        league_id: League id from config (e.g. "pins_and_pills" or
                   "casey_stengel" -- see config/leagues.yaml's "id" field,
                   NOT the CBS league id). "all" is not supported here since
                   a team name must be looked up within one league.
        team_name: Full or partial team name, case-insensitive
                   (e.g. "Men of Steal" or just "steal").
                   Call list_league_teams first if you don't know the exact name.

    Returns a formatted roster, or the list of valid team names if no match.
    """
    try:
        auth    = _get_auth()
        leagues = _resolve_leagues(league_id)
        if not leagues:
            return f"No league found matching '{league_id}'."
        if len(leagues) > 1:
            return ("Multiple leagues matched -- specify one league_id "
                     f"({', '.join(l[0].get('cbs_league_id','?') for l in leagues)}).")

        league_cfg, sport = leagues[0]
        lid = league_cfg["cbs_league_id"]
        all_rosters = get_all_team_rosters(auth, lid, sport)

        tid = resolve_team_id(all_rosters, team_name)
        if tid is None:
            names = ", ".join(info["name"] for info in all_rosters.values())
            return f"No team matching '{team_name}'. Teams in this league: {names}"

        info = all_rosters[tid]
        out = [f"=== {info['name']} ({sport}) -- {len(info['roster'])} players ==="]
        out.append(f"{'Slot':<6} {'Player':<24} {'Team':<5} {'Status'}")
        out.append("-" * 55)
        for rs in info["roster"]:
            p = rs.player
            out.append(f"{rs.slot:<6} {p.name:<24} {(p.team or '?'):<5} {p.status or ''}")
        return _respond("\n".join(out))

    except CBSAuthError as e:
        return f"CBS auth error: {e}"
    except Exception as e:
        logger.exception("get_team_roster failed")
        return f"Error fetching team roster: {e}"


# ---------------------------------------------------------------------------
# Tool: list_league_teams -- discovery helper for get_team_roster
# ---------------------------------------------------------------------------

@mcp.tool()
def list_league_teams(league_id: str) -> str:
    """
    List every team name in a league, with team IDs.

    Use this first if you don't know the exact team name to pass to
    get_team_roster.

    Args:
        league_id: League id from config (e.g. "pins_and_pills" or
                   "casey_stengel" -- see config/leagues.yaml's "id" field,
                   NOT the CBS league id), or "all" for every league.
    """
    try:
        auth    = _get_auth()
        leagues = _resolve_leagues(league_id)
        if not leagues:
            return f"No league found matching '{league_id}'."

        out = []
        for league_cfg, sport in leagues:
            lid = league_cfg["cbs_league_id"]
            all_rosters = get_all_team_rosters(auth, lid, sport)
            out.append(f"=== {league_cfg.get('name', lid)} ===")
            for tid, info in all_rosters.items():
                out.append(f"  {info['name']}  (id={tid}, {len(info['roster'])} players)")
        return _respond("\n".join(out))

    except CBSAuthError as e:
        return f"CBS auth error: {e}"
    except Exception as e:
        logger.exception("list_league_teams failed")
        return f"Error listing teams: {e}"


# ---------------------------------------------------------------------------
# Tool: waiver_recommendations
# ---------------------------------------------------------------------------

@mcp.tool()
def waiver_recommendations(
    league_id: str = "all",
    position: str | None = None,
    date: str | None = None,
    next_week: bool = False,
    min_batters: int = 2,
    limit: int = 10,
) -> str:
    """
    Get top waiver wire add recommendations for your league.

    Args:
        league_id:   League id from config, or "all" for all leagues.
        position:    Filter to a specific position, e.g. "SP", "RP", "OF",
                     "SS", "C", "1B", "3B". Leave blank for all positions.
        date:        Only show players whose team plays on this date.
                     "today", "tomorrow", or "YYYY-MM-DD". Leave blank for all.
        next_week:   If True, look ahead to the next CBS scoring period.
                     SPs with 2 starts next week are boosted to the top.
                     Back-to-back 2-starters (this week AND next) get an
                     additional boost. Use this on Friday/Saturday to plan
                     adds before the Monday scoring lock.
        min_batters: Minimum number of batter recommendations to include even
                     if pitcher categories are the priority. Default 2. Set to
                     0 to disable (useful when position="SP" or position="RP").
        limit:       Maximum number of recommendations to return. Default 10.

    Returns ranked waiver adds with category fit, Savant xStats, and CM closer tags.
    """
    from datetime import date as _date, timedelta
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")

    try:
        auth    = _get_auth()
        leagues = _resolve_leagues(league_id)
        if not leagues:
            return f"No league found matching '{league_id}'."

        # Parse date param
        playing_on = None
        if date:
            d_lower = date.strip().lower()
            today = _date.fromisoformat(
                __import__("datetime").datetime.now(_ET).date().isoformat()
            )
            if d_lower == "today":
                playing_on = today
            elif d_lower == "tomorrow":
                playing_on = today + timedelta(days=1)
            else:
                try:
                    playing_on = _date.fromisoformat(d_lower)
                except ValueError:
                    return f"Invalid date '{date}'. Use 'today', 'tomorrow', or 'YYYY-MM-DD'."

        from datetime import datetime as _dtnow
        from zoneinfo import ZoneInfo as _ZI
        _et_now   = _dtnow.now(_ZI("America/New_York"))
        _weekday  = _et_now.weekday()   # 0=Mon … 6=Sun

        out = []
        for league_cfg, sport in leagues:
            lid  = league_cfg["cbs_league_id"]
            name = league_cfg.get("name", lid)

            # Detect weekly pitcher lock leagues (sp_must_be_claimed_before_week).
            # Tue–Sun: pitcher adds only apply next week, so auto-shift the
            # 2-starter window and add a note to the output.
            weekly_lock = (league_cfg.get("constraints") or {}).get(
                "sp_must_be_claimed_before_week", False)
            pitcher_locked_midweek = weekly_lock and not next_week and _weekday > 0

            week_offset = 1 if (next_week or pitcher_locked_midweek) else 0

            recs = get_filtered_waiver_adds(
                auth, lid, league_cfg, sport,
                position_filter=position,
                playing_on=playing_on,
                min_batters=min_batters,
                limit=limit,
                week_offset=week_offset,
            )

            header_parts = [name]
            if next_week or pitcher_locked_midweek:
                header_parts.append("NEXT WEEK")
            if position:
                header_parts.append(f"position={position.upper()}")
            if playing_on:
                header_parts.append(f"playing={playing_on.isoformat()}")
            out.append(f"\n=== {' | '.join(header_parts)} -- Waiver Adds ===")

            if pitcher_locked_midweek:
                out.append("  ⚠️  Pitcher adds this week are locked — SP/RP recommendations "
                           "are for NEXT scoring period. Add hitters freely.")

            if not recs:
                out.append("  No recommendations found matching these filters.")
                continue

            for r in recs:
                cats      = ", ".join(r.get("helps_cats", []))
                pos       = "/".join(r.get("positions", []))
                stats     = r.get("_stats") or {}
                stat_line = r.get("_stat_line", "")

                sav_parts = []
                if stats.get("sv_xwoba"):
                    sav_parts.append(f"xwOBA={stats['sv_xwoba']:.3f}")
                if stats.get("sv_barrel_pct") is not None:
                    sav_parts.append(f"Brl%={stats['sv_barrel_pct']:.1f}")
                if stats.get("sv_xera") is not None:
                    sav_parts.append(f"xERA={stats['sv_xera']:.2f}")
                sav_str = (" [" + " | ".join(sav_parts) + "]") if sav_parts else ""

                cm_tag = ""
                if r.get("cm_role"):
                    cm_tag = f"  [CM: {r['cm_role']} | {r.get('cm_tendency','')}]"

                start_tag = ""
                if r.get("back_to_back"):
                    start_tag = "  ★★ 2-start back-to-back"
                elif r.get("two_starter"):
                    start_tag = "  ★ 2-start"

                header = (f"  + {r['player']} ({r.get('team','?')}) [{pos}]"
                          f"  helps: {cats}{cm_tag}{start_tag}")
                detail = ""
                if stat_line or sav_str:
                    detail = f"\n      {stat_line}{sav_str}"
                out.append(header + detail)

        return _respond("\n".join(out) if out else "No waiver recommendations generated.")

    except CBSAuthError as e:
        return f"CBS auth error: {e}"
    except Exception as e:
        logger.exception("waiver_recommendations failed")
        return f"Error fetching waiver recommendations: {e}"


# ---------------------------------------------------------------------------
# Tool: roster_value_signals
# ---------------------------------------------------------------------------

@mcp.tool()
def roster_value_signals(league_id: str = "all") -> str:
    """
    Get buy-low / sell-high trade value signals.

    SELL HIGH: your players outpacing their ROS projections -- trade them while
               their perceived value is inflated.

    BUY LOW:   players on OTHER teams underperforming their ROS projections --
               target them in trades; their owner may sell cheap.

    Args:
        league_id: League id from config, or "all" for all leagues.
    """
    try:
        from fantasypros.client import enrich_with_fp_projections
        from savant.client import enrich_with_savant

        auth       = _get_auth()
        fp_client  = _get_fp()
        sav_client = _get_sav()
        leagues    = _resolve_leagues(league_id)
        if not leagues:
            return f"No league found matching '{league_id}'."

        out = []
        for league_cfg, sport in leagues:
            lid  = league_cfg["cbs_league_id"]
            tid  = str(league_cfg["cbs_team_id"])
            name = league_cfg.get("name", lid)

            # --- SELL HIGH: analyze YOUR roster ---
            my_roster = cbs_get_roster(auth, lid, tid, sport)
            try:
                enrich_roster(my_roster)
            except Exception:
                pass
            try:
                enrich_with_fp_projections(my_roster, fp_client)
            except Exception:
                pass
            try:
                enrich_with_savant(my_roster, sav_client)
            except Exception:
                pass

            my_signals = analyze_roster_value(my_roster)
            sells = [s for s in my_signals if s["signal"] == "sell_high"]

            # --- BUY LOW: scan every OTHER team's roster for underperformers ---
            buy_targets = []
            try:
                all_rosters = get_all_team_rosters(auth, lid, sport)
                for other_tid, info in all_rosters.items():
                    if str(other_tid) == tid:
                        continue  # skip your own team
                    other_roster = info["roster"]
                    try:
                        enrich_roster(other_roster)
                    except Exception:
                        pass
                    try:
                        enrich_with_fp_projections(other_roster, fp_client)
                    except Exception:
                        pass
                    try:
                        enrich_with_savant(other_roster, sav_client)
                    except Exception:
                        pass
                    for s in analyze_roster_value(other_roster):
                        if s["signal"] == "buy_low":
                            buy_targets.append({**s, "_owner": info["name"]})
            except Exception as e:
                logger.warning("buy-low scan failed: %s", e)

            # Strongest signals first
            buy_targets.sort(key=lambda x: (0 if x.get("confidence") == "strong" else 1))

            out.append(f"=== {name} -- Trade Value Signals ===")

            if sells:
                out.append(f"SELL HIGH ({len(sells)}) -- your players outpacing projections:")
                for s in sells:
                    pos = "/".join(s.get("positions", []))
                    out.append(f"  ~ {s['name']} ({s['team']}) [{pos}] [{s.get('confidence','')}]")
                    out.append(f"    {s['reason']}")
            else:
                out.append("  SELL HIGH: no strong signals on your roster.")

            if buy_targets:
                out.append(f"\nBUY LOW ({len(buy_targets)}) -- underperformers on other teams to target in trades:")
                for s in buy_targets:
                    pos   = "/".join(s.get("positions", []))
                    owner = s.get("_owner", "?")
                    out.append(f"  + {s['name']} ({s['team']}) [{pos}] [{s.get('confidence','')}]  owned by: {owner}")
                    out.append(f"    {s['reason']}")
            else:
                out.append("\n  BUY LOW: no underperforming targets found on other teams.")
            out.append("")

        return _respond("\n".join(out))

    except CBSAuthError as e:
        return f"CBS auth error: {e}"
    except Exception as e:
        logger.exception("roster_value_signals failed")
        return f"Error fetching roster value signals: {e}"


# ---------------------------------------------------------------------------
# Tool: hitting_matchups
# ---------------------------------------------------------------------------

@mcp.tool()
def hitting_matchups(
    league_id: str = "all",
    date: str | None = None,
) -> str:
    """
    Score your roster batters' hitting matchups for today (or another date).

    For each batter, considers:
      • L/R split advantage — season OPS vs the probable starter's handedness
      • Park factor — how hitter-friendly the today's ballpark is
      • Hot streak — OPS and HR over the last 14 days

    Returns a ranked list from best to worst matchup, with a START / OK / SIT
    recommendation. Use this to set your daily lineup or pick the right bench
    player to activate.

    Args:
        league_id: League id from config, or "all" for all leagues.
        date:      Date to evaluate. "today" (default), "tomorrow", or "YYYY-MM-DD".
    """
    from datetime import date as _date, datetime as _dt, timedelta
    from zoneinfo import ZoneInfo
    from mlb.schedule import todays_matchups
    from mlb.splits  import fetch_batter_splits, fetch_recent_form
    from mlb.parks   import park_label, park_factor as _pf
    from mlb.teams   import norm_name as _norm

    _ET = ZoneInfo("America/New_York")

    try:
        # --- resolve date ---
        today = _dt.now(_ET).date()
        if not date or date.strip().lower() == "today":
            eval_date = today
        elif date.strip().lower() == "tomorrow":
            eval_date = today + timedelta(days=1)
        else:
            try:
                eval_date = _date.fromisoformat(date.strip())
            except ValueError:
                return f"Invalid date '{date}'. Use 'today', 'tomorrow', or 'YYYY-MM-DD'."

        # --- fetch matchup schedule ---
        matchups = todays_matchups(eval_date)
        if not matchups:
            return f"No MLB games found for {eval_date.isoformat()}."

        # Build fast lookup: cbs_team → matchup info
        # --- fetch weather per game (keyed by home_team) ---
        from mlb.weather import fetch_game_weather
        game_weather: dict[str, dict] = {}
        for m in matchups:
            ht = m.get("home_team", "")
            if ht and ht not in game_weather:
                try:
                    game_weather[ht] = fetch_game_weather(ht, eval_date)
                except Exception:
                    game_weather[ht] = {}

        # BUG 2 fix: use defensive .get() throughout so malformed matchup rows
        # (doubleheaders, rescheduled games, holiday slates) are skipped rather
        # than crashing with KeyError: 'home_team' / 'away_team'.
        team_to_matchup: dict[str, dict] = {}
        for m in matchups:
            try:
                ht = m.get("home_team") or ""
                at = m.get("away_team") or ""
                wx = game_weather.get(ht, {})
                # Batter on home team faces the AWAY starter
                if ht:
                    team_to_matchup[ht] = {
                        "opp_starter_hand": m.get("away_starter_hand"),
                        "opp_starter_name": m.get("away_starter_name"),
                        "home_team": ht,
                        "away_team": at,
                        "is_home": True,
                        "park_factor": m.get("park_factor", 100),
                        "park_factor_hr": m.get("park_factor_hr", 100),
                        "weather": wx,
                    }
                # Batter on away team faces the HOME starter
                if at:
                    team_to_matchup[at] = {
                        "opp_starter_hand": m.get("home_starter_hand"),
                        "opp_starter_name": m.get("home_starter_name"),
                        "home_team": ht,
                        "away_team": at,
                        "is_home": False,
                        "park_factor": m.get("park_factor", 100),
                        "park_factor_hr": m.get("park_factor_hr", 100),
                        "weather": wx,
                    }
            except Exception as _m_exc:
                logger.warning(
                    "hitting_matchups: skipping malformed matchup row: %s", _m_exc
                )
                continue

        # --- fetch split and recent-form data ---
        splits = fetch_batter_splits()
        recent = fetch_recent_form(14)

        auth    = _get_auth()
        leagues = _resolve_leagues(league_id)
        if not leagues:
            return f"No league found matching '{league_id}'."

        _PITCHER_POS = {"SP", "RP", "P"}
        out = []

        for league_cfg, sport in leagues:
            lid  = league_cfg["cbs_league_id"]
            tid  = str(league_cfg["cbs_team_id"])
            name = league_cfg.get("name", lid)
            roster = cbs_get_roster(auth, lid, tid, sport)
            try:
                enrich_roster(roster)
            except Exception:
                pass

            date_label = eval_date.isoformat()
            out.append(f"\n=== {name} | Hitting Matchups — {date_label} ===")

            scored: list[dict] = []
            no_game: list[str] = []
            pitchers_skipped  = 0

            for rs in roster:
                p = rs.player
                if not p.team:
                    continue
                if set(p.positions) & _PITCHER_POS and not (
                        set(p.positions) - _PITCHER_POS):
                    pitchers_skipped += 1
                    continue  # pure pitchers — skip

                cbs_team = (p.team or "").upper()
                m = team_to_matchup.get(cbs_team)
                if m is None:
                    no_game.append(p.name)
                    continue

                key = _norm(p.name)

                # --- L/R split ---
                hand = m["opp_starter_hand"]  # "L" / "R" / "S" / None
                split_key = None
                split_ops = None
                split_avg = None
                split_pa  = 0
                if hand in ("L", "S"):
                    split_key = "vs_l"
                elif hand == "R":
                    split_key = "vs_r"

                split_label = ""
                if key in splits and split_key:
                    sd = splits[key].get(split_key, {})
                    split_ops = sd.get("ops", 0.0)
                    split_avg = sd.get("avg", 0.0)
                    split_pa  = sd.get("pa", 0)
                    if split_pa >= 30:
                        split_label = (f"OPS vs {'LHP' if split_key=='vs_l' else 'RHP'}"
                                       f"={split_ops:.3f} ({split_pa} PA)")

                # --- recent form (last 14 days) ---
                rd = recent.get(key, {})
                recent_ops    = rd.get("ops", 0.0)
                recent_avg    = rd.get("avg", 0.0)
                recent_hr     = rd.get("hr", 0)
                recent_games  = rd.get("games", 0)
                hot_label = ""
                if recent_games >= 5:
                    hot_label = (f"L14: AVG={recent_avg:.3f} OPS={recent_ops:.3f}"
                                 f" HR={recent_hr} ({recent_games}G)")

                # --- park factor ---
                pf       = m["park_factor"]
                pf_label = park_label(pf)

                # --- weather ---
                wx           = m.get("weather", {})
                wx_bonus     = wx.get("score_bonus", 0.0)
                wx_summary   = wx.get("summary", "")
                wx_wind      = wx.get("wind_label", "")
                wx_precip    = wx.get("precip_pct", 0)
                wx_temp      = wx.get("temp_f", 70)

                # --- composite matchup score ---
                score = 0.0

                # Split component (OPS relative to .750 baseline)
                if split_ops and split_pa >= 30:
                    score += (split_ops - 0.750) * 40.0

                # Recent form component
                if recent_games >= 5:
                    score += (recent_ops - 0.720) * 20.0
                    score += recent_hr * 2.0

                # Park factor component
                score += (pf - 100) * 0.3

                # Weather component
                score += wx_bonus

                # Penalty: handedness unknown (starter TBD)
                if hand is None:
                    score -= 5.0

                # Must-start floor: elite players always START regardless of park/matchup.
                # Park factors and L/R splits should only differentiate borderline players,
                # not override a .900+ OPS bat. OPS threshold set at .850 (top ~15% of starters).
                ytd_ops = float((p.stats or {}).get("OPS") or 0)
                is_must_start = ytd_ops >= 0.850

                # Determine recommendation
                if is_must_start:
                    rec = "🟢 START"
                    score = max(score, 12.0)   # float to top even with bad park
                elif score >= 8:
                    rec = "🟢 START"
                elif score >= 2:
                    rec = "🟡 OK"
                elif score <= -5:
                    rec = "🔴 SIT"
                else:
                    rec = "🟡 OK"

                scored.append({
                    "name":         p.name,
                    "team":         cbs_team,
                    "positions":    p.positions,
                    "slot":         rs.slot,
                    "score":        score,
                    "rec":          rec,
                    "opp_hand":     hand,
                    "opp_starter":  m["opp_starter_name"],
                    "is_home":      m["is_home"],
                    "home_team":    m["home_team"],
                    "pf":           pf,
                    "pf_label":     pf_label,
                    "split_label":  split_label,
                    "hot_label":    hot_label,
                    "wx_summary":   wx_summary,
                    "wx_precip":    wx_precip,
                })

            # Sort by score descending
            scored.sort(key=lambda x: x["score"], reverse=True)

            for item in scored:
                pos      = "/".join(item["positions"])
                at_v     = "@" if not item["is_home"] else "vs"
                opp_team = item["away_team"] if item["is_home"] else item["home_team"]
                opp_str  = f"{at_v} {opp_team}"
                sp_str   = f" [{item['opp_starter'] or 'TBD'} {'('+item['opp_hand']+')' if item['opp_hand'] else ''}]"
                pf_str   = f" | park={item['pf']} ({item['pf_label']})"
                slot_str = f" [{item['slot']}]"
                out.append(
                    f"  {item['rec']}  {item['name']} ({item['team']}) [{pos}]{slot_str}"
                    f"  {opp_str}{sp_str}{pf_str}"
                )
                if item["split_label"]:
                    out.append(f"           {item['split_label']}")
                if item["hot_label"]:
                    out.append(f"           {item['hot_label']}")
                if item["wx_summary"] and "unavailable" not in item["wx_summary"] and "dome" not in item["wx_summary"]:
                    rain_str = f" ⛈ rain {item['wx_precip']}%" if item["wx_precip"] >= 20 else ""
                    out.append(f"           wx: {item['wx_summary']}{rain_str}")

            if no_game:
                out.append(f"\n  Off today: {', '.join(no_game)}")

        return _respond("\n".join(out) if out else "No matchup data generated.")

    except CBSAuthError as e:
        return f"CBS auth error: {e}"
    except Exception as e:
        logger.exception("hitting_matchups failed")
        return f"Error fetching hitting matchups: {e}"


# ---------------------------------------------------------------------------
# Tool: daily_decisions
# ---------------------------------------------------------------------------

@mcp.tool()
def daily_decisions(league_id: str = "all") -> str:
    """
    Run the full daily fantasy baseball analysis for your league(s).

    Returns the complete agent output: matchup summary, streaming SPs,
    waiver adds, drop candidates, trade signals, trade board, closer news,
    and daily lineup advice.

    Args:
        league_id: League id from config, or "all" for all leagues.
    """
    try:
        auth    = _get_auth()
        leagues = _resolve_leagues(league_id)
        if not leagues:
            return f"No league found matching '{league_id}'."

        buf = io.StringIO()
        original = sys.stdout
        sys.stdout = buf
        try:
            for league_cfg, sport in leagues:
                lid  = league_cfg["cbs_league_id"]
                tid  = str(league_cfg["cbs_team_id"])
                name = league_cfg.get("name", lid)
                roster = cbs_get_roster(auth, lid, tid, sport)
                try:
                    enrich_roster(roster)
                except Exception:
                    pass
                team   = Team(id=tid, name=name, roster=roster)
                result = run_decisions(auth, lid, league_cfg, team, sport)
                # Re-use main.py printer
                from agent.main import _print_decisions
                _print_decisions(result, dry_run=True)
        finally:
            sys.stdout = original

        return _respond(buf.getvalue() or "No output generated.")

    except CBSAuthError as e:
        return f"CBS auth error: {e}"
    except Exception as e:
        logger.exception("daily_decisions failed")
        return f"Error running daily decisions: {e}"


# ---------------------------------------------------------------------------
# Tool: get_baseball_image
# ---------------------------------------------------------------------------

@mcp.tool()
def get_baseball_image(
    subject: str | None = None,
    year: str | None = None,
    location: str | None = None,
) -> str:
    """
    Find and display a historical baseball image.

    Searches the Library of Congress Photographs collection first (excellent
    pre-1970 coverage), then Wikimedia Commons as a fallback for more modern
    subjects. All three parameters are combined into a single search query,
    so you can mix and match freely.

    Args:
        subject:  Player, team, or topic.
                  e.g. "Babe Ruth", "Satchel Paige", "Brooklyn Dodgers",
                  "Negro Leagues", "1927 Yankees", "World Series"
        year:     Year or decade to narrow results.
                  e.g. "1925", "1940", "1930s", "1950-1955"
        location: Venue, city, or ballpark.
                  e.g. "Yankee Stadium", "Birmingham", "Ebbets Field",
                  "Polo Grounds", "Fenway Park"

    Examples:
        subject="Babe Ruth", location="Yankee Stadium", year="1925"
        subject="Satchel Paige", location="Birmingham", year="1940"
        subject="Brooklyn Dodgers", year="1955"
        location="Ebbets Field", year="1940s"

    Leave all blank for a random historic baseball image.

    Returns an image with title, date, and source attribution.
    """
    import random as _random
    from mlb.images import search_player_images, random_historic_image

    try:
        # Build compound query from whichever params were provided
        parts = [p for p in (subject, location, year) if p]
        query = " ".join(parts) if parts else None

        if query:
            results = search_player_images(query, limit=6)
            if not results:
                return (f"No images found for '{query}'.\n"
                        "Try loosening the search — drop the year or location, "
                        "or use a broader subject like 'vintage pitcher 1950s'.")
            img = _random.choice(results[:3])   # pick from top 3 for variety
        else:
            img = random_historic_image()
            if not img:
                return "Could not fetch a random historic image right now — try naming a player or team."

        lines = [
            f"![{img['title']}]({img['url']})",
            "",
            f"**{img['title']}**",
        ]
        if img.get("date"):
            lines.append(f"📅 {img['date']}")
        if img.get("description"):
            lines.append(f"_{img['description']}_")
        lines.append(f"Source: [{img['source']}]({img['source_url']})")

        return "\n".join(lines)

    except Exception as e:
        logger.exception("get_baseball_image failed")
        return f"Image lookup failed: {e}"


# ---------------------------------------------------------------------------
# Tool: probe_schedule  [TEMPORARY — remove after CBS endpoint discovery]
# ---------------------------------------------------------------------------

@mcp.tool()
def probe_schedule(league_id: str = "all") -> str:
    """
    TEMPORARY diagnostic tool for Phase A of matchup_outlook development.

    Probes CBS API endpoints to discover which one exposes next-week matchup
    data. All probe results are logged at INFO level (look for [schedule probe]
    in Render logs). This tool returns a summary of what each endpoint returned.

    Remove this tool once the right CBS endpoint is identified.

    Args:
        league_id: League id from config, or "all" for all leagues.
    """
    try:
        from cbs.schedule import fetch_next_opponent

        auth    = _get_auth()
        leagues = _resolve_leagues(league_id)
        if not leagues:
            return f"No league found matching '{league_id}'."

        out = []
        for league_cfg, sport in leagues:
            lid  = league_cfg["cbs_league_id"]
            tid  = str(league_cfg["cbs_team_id"])
            name = league_cfg.get("name", lid)

            out.append(f"=== {name} — CBS Schedule Probe ===")
            out.append(f"league_id={lid}  team_id={tid}  sport={sport}")
            out.append("")

            for offset in (1, 0):
                label = "NEXT WEEK" if offset == 1 else "CURRENT WEEK"
                result = fetch_next_opponent(auth, lid, sport,
                                             my_team_id=tid, week_offset=offset)
                if result:
                    out.append(f"  {label} (week_offset={offset}):")
                    out.append(f"    opponent_id   = {result.get('opponent_id')}")
                    out.append(f"    opponent_name = {result.get('opponent_name')}")
                    out.append(f"    period        = {result.get('period')}")
                    out.append(f"    _source       = {result.get('_source')}")
                    out.append(f"    _fallback     = {result.get('_fallback', False)}")
                else:
                    out.append(f"  {label} (week_offset={offset}): NO RESULT — all probes failed")
                out.append("")

            out.append("Check Render logs for [schedule probe] lines to see full CBS responses.")
            out.append("")

        return _respond("\n".join(out))

    except CBSAuthError as e:
        return f"CBS auth error: {e}"
    except Exception as e:
        logger.exception("probe_schedule failed")
        return f"probe_schedule error: {e}"


# ---------------------------------------------------------------------------
# Entry point
#
# Two modes, switched by the MCP_TRANSPORT env var:
#   - "stdio" (default)  -- local use from Claude Desktop's config.json,
#                            which launches this file as a subprocess.
#   - "http"             -- standalone web service for cloud hosting
#                            (Render/Railway/etc), added to Claude as a
#                            custom connector by URL. Reachable from any
#                            device, independent of any one PC's state.
#
# The http mode is gated by a token (MCP_AUTH_TOKEN) since this server can
# reach your CBS fantasy data -- the URL alone must not be enough to call
# it. DNS-rebinding host-allowlisting is relaxed instead (we don't know
# the cloud host's domain at code-time, and the token is the real gate).
#
# Token is accepted two ways:
#   - Authorization: Bearer <token> header (for curl/PowerShell testing)
#   - ?token=<token> query param (for Claude's custom connector dialog,
#     which only supports OAuth or no-auth -- no plain bearer-token field.
#     Putting the token in the connector URL itself is the workaround;
#     Claude sends the URL as configured on every call, query string
#     included. Tradeoff: query-string tokens can end up in access logs,
#     unlike header-based tokens. Acceptable here since the worst case of
#     compromise is read-only access to fantasy baseball data.)
# ---------------------------------------------------------------------------

def _run_http():
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    auth_token = os.environ.get("MCP_AUTH_TOKEN")
    if not auth_token:
        raise RuntimeError(
            "MCP_AUTH_TOKEN must be set when running with MCP_TRANSPORT=http "
            "-- this server can reach your CBS fantasy data and must not be "
            "left reachable by anyone who finds the URL."
        )

    class BearerAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # Allow OAuth discovery + /health through unauthenticated
            # Required for Claude.ai connector to complete its sign-in flow.
            path = request.url.path
            if path.startswith("/.well-known/"):
                return await call_next(request)
            if path in ("/health", "/ping"):
                return JSONResponse({"status": "ok", "service": "fantasy-baseball-mcp"})
            header_val = request.headers.get("authorization", "")
            query_val  = request.query_params.get("token", "")
            ok = (header_val == f"Bearer {auth_token}") or (query_val == auth_token)
            if not ok:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    # Relax DNS-rebinding host allowlisting -- the cloud host's domain
    # isn't known at code-time, and BearerAuthMiddleware above is the
    # actual access gate.
    mcp.settings.transport_security.enable_dns_rebinding_protection = False

    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware)

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    if os.environ.get("MCP_TRANSPORT") == "http":
        _run_http()
    else:
        mcp.run()
