"""
Fantasy Baseball Agent -- MCP Server

Exposes the agent's capabilities as tools for Claude Projects / Claude Desktop.

Tools:
  evaluate_trade            -- evaluate a specific trade offer [baseball only]
  daily_decisions           -- run full daily analysis for a league [baseball + football]
  get_roster                -- your current roster for a league [baseball + football]
  get_team_roster           -- ANY team's current roster, by name (trade research) [baseball + football]
  list_league_teams         -- list team names in a league (helper for get_team_roster) [baseball + football]
  get_draft_board           -- live CBS draft order/results for a league [football only]
  get_fantasypros_draft_board -- FantasyPros value re-scored under each league's own rules [football only]
  get_league_keepers        -- keeper guidance for EVERY manager in a keeper league, not just yours [football only]
  waiver_recommendations    -- top waiver wire adds [baseball only]
  roster_value_signals      -- buy-low / sell-high signals [baseball only]

Football support (added 2026-08-01) is intentionally partial -- see
agent/football_decisions.py and sports/football/. Tools marked
[baseball only] above still hard-call sports.baseball.*/mlb.* with no
sport branching (streaming SPs, category analysis, Savant xStats, MLB
schedule/injury data -- none of which apply to football) and will not
resolve football leagues at all, by design (see _BASEBALL_ONLY_SPORTS /
_FOOTBALL_AWARE_SPORTS below _resolve_leagues()).

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
from cbs.draft import fetch_draft_board, my_picks as _my_draft_picks
from cbs.settings import fetch_roster_rules, format_roster_rules
from mlb.stats import enrich_roster
from fantasypros.client import FantasyProsClient
from fantasypros.draft_board import build_draft_board as _build_fp_draft_board
from savant.client import SavantClient
from agent.trade_eval import evaluate_trade, format_trade_result
from agent.tradevalue import analyze_roster_value
from agent.decisions import run_decisions, get_filtered_waiver_adds, trade_window_status
from agent.football_decisions import run_football_decisions, league_keeper_report, predicted_keepers
from data.models import Team
from mlb.clock import now_et, today_et

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


# Most tools here (waiver_recommendations, roster_value_signals,
# evaluate_trade_tool, hitting_matchups, probe_schedule) hard-import
# sports.baseball.* / mlb.* with no sport branching -- calling them against
# a football league would silently apply baseball logic (categories,
# streaming SPs, MLB schedule/injury data, Savant xStats...) to a sport
# none of that applies to. Those tools all call _resolve_leagues(league_id)
# with no `sports` arg, so they get this baseball-only default and never
# see football leagues.
#
# A few tools ARE football-safe and pass sports=_FOOTBALL_AWARE_SPORTS
# explicitly below: get_roster / list_league_teams / get_team_roster only
# ever fetch+print a roster generically (no baseball-specific analysis),
# and daily_decisions dispatches per-league to run_football_decisions()
# for football leagues instead of run_decisions() (see its docstring).
_BASEBALL_ONLY_SPORTS  = {"baseball"}
_FOOTBALL_AWARE_SPORTS = {"baseball", "football"}


def _resolve_leagues(league_id: str,
                     sports: set = _BASEBALL_ONLY_SPORTS) -> list[tuple[dict, str]]:
    """Return list of (league_cfg, sport) matching the requested league_id,
    restricted to `sports` (default: baseball only -- see the guidance
    above _BASEBALL_ONLY_SPORTS for which tools should pass a wider set)."""
    config = _load_leagues()
    results = []
    for sport, leagues in config.items():
        # leagues.yaml also carries top-level season_start/periods keys
        # (BUG 5 fix) that aren't sport -> [league, ...] entries.
        if not isinstance(leagues, list):
            continue
        if sport not in sports:
            continue
        for league in (leagues or []):
            # A real league entry always has cbs_league_id -- filters out
            # non-league list entries under a reserved key (leagues.yaml's
            # `periods:` table is itself a list of {n,start,end} dicts).
            if not isinstance(league, dict) or "cbs_league_id" not in league:
                continue
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

        # Trade deadline enhancement: evaluate_trade_tool stays callable
        # on request past the deadline (manual override), but must not
        # hand back an ACCEPT/DECLINE/CLOSE verdict once the league's
        # trade_deadline has passed -- per-league, independent of any
        # other league's deadline.
        window = trade_window_status(league_cfg)
        if window["status"] == "closed":
            league_name  = league_cfg.get("name", league_cfg.get("id", league_id))
            deadline_str = window["deadline"].isoformat() if window["deadline"] else "unknown"
            return _respond(
                f"Trade deadline has passed for {league_name} "
                f"(deadline was {deadline_str}). No ACCEPT/DECLINE/CLOSE verdict "
                "is available -- trades can no longer be made in this league "
                "for the rest of the season."
            )

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
        leagues = _resolve_leagues(league_id, sports=_FOOTBALL_AWARE_SPORTS)
        if not leagues:
            return f"No league found matching '{league_id}'."

        out = []
        for league_cfg, sport in leagues:
            lid  = league_cfg["cbs_league_id"]
            tid  = str(league_cfg["cbs_team_id"])
            name = league_cfg.get("name", lid)
            roster = cbs_get_roster(auth, lid, tid, sport)
            if sport == "baseball":  # mlb.stats enrichment is baseball-only
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
        leagues = _resolve_leagues(league_id, sports=_FOOTBALL_AWARE_SPORTS)
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
        leagues = _resolve_leagues(league_id, sports=_FOOTBALL_AWARE_SPORTS)
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
# Tool: get_draft_board -- draft order (pre-draft) and draft results
# (post-draft), for one league or all of them.
# ---------------------------------------------------------------------------

@mcp.tool()
def get_draft_board(league_id: str = "all") -> str:
    """
    Get the draft order/pick slots for a league, or the pick-by-pick draft
    results once that league's draft has started. Same underlying CBS page
    serves both -- pre-draft, player cells are empty and this just shows
    who picks when; once the draft starts/finishes, made picks show the
    player taken.

    Identifies your own team (from config/leagues.yaml's my_team_name) and
    calls out every pick that belongs to you, with the overall pick number,
    so you always know exactly when you're on the clock.

    Args:
        league_id: League id from config (e.g. "hard_chargers", "f_league",
                   "east_coast" -- see config/leagues.yaml's "id" field,
                   NOT the CBS league id), or "all" for every league.
                   For "all", output is a compact per-league summary; for a
                   single league_id, the full round-by-round board is shown.

    NOTE: CBS doesn't expose the draft board via the api.cbssports.com JSON
    API this repo otherwise uses (see cbs/draft.py docstring) -- this scrapes
    the rendered HTML at /draft/results instead. The exact text format of a
    made pick's player cell hasn't been confirmed against a real completed
    pick as of this tool's introduction (2026-08-24, all 3 leagues still
    pre-draft) -- treat player names in results as raw/unparsed until that's
    verified against a live draft.
    """
    try:
        auth    = _get_auth()
        leagues = _resolve_leagues(league_id, sports=_FOOTBALL_AWARE_SPORTS)
        if not leagues:
            return f"No league found matching '{league_id}'."

        out = []
        for league_cfg, sport in leagues:
            lid  = league_cfg["cbs_league_id"]
            name = league_cfg.get("name", lid)
            my_team = league_cfg.get("my_team_name")

            board = fetch_draft_board(auth, lid, sport)
            out.append(f"=== {name} ({board['status']}) ===")

            if board["status"] == "unknown":
                out.append("  Could not parse draft board -- CBS page layout "
                            "may have changed.")
                continue

            mine = _my_draft_picks(board, my_team) if my_team else []
            if my_team and not mine:
                out.append(f"  WARNING: no picks matched my_team_name="
                            f"'{my_team}' -- team may have been renamed on "
                            f"CBS. Round 1 order: "
                            f"{', '.join(board['team_order_round1'])}")

            if league_id == "all" or len(leagues) > 1:
                # Compact summary mode (used for "all", or any multi-match).
                pos = board["team_order_round1"].index(my_team) + 1 if my_team in board["team_order_round1"] else "?"
                out.append(f"  Pick position: {pos} of {len(board['team_order_round1'])}")
                if mine:
                    my_overalls = ", ".join(str(p["overall"]) for p in mine)
                    out.append(f"  Your picks (overall #): {my_overalls}")
            else:
                # Single-league mode: full round-by-round board.
                my_overalls = {p["overall"] for p in mine}
                for rnd in board["rounds"]:
                    out.append(f"  Round {rnd['round']}:")
                    for p in rnd["picks"]:
                        marker = " <-- YOU" if p["overall"] in my_overalls else ""
                        player = f" -- {p['player_raw']}" if p["player_raw"] else ""
                        out.append(f"    {p['overall']:>3} (R{rnd['round']}.{p['pick']:<2}) "
                                   f"{p['team']}{player}{marker}")
        return _respond("\n".join(out))

    except CBSAuthError as e:
        return f"CBS auth error: {e}"
    except Exception as e:
        logger.exception("get_draft_board failed")
        return f"Error fetching draft board: {e}"


# ---------------------------------------------------------------------------
# Tool: get_fantasypros_draft_board
# ---------------------------------------------------------------------------

@mcp.tool()
def get_fantasypros_draft_board(league_id: str = "all", top_n: int = 15) -> str:
    """
    Build a scored draft board for a football league: FantasyPros consensus
    ADP (realistic pick-availability order) cross-referenced with each
    player's season projection, re-scored under THAT league's own scoring
    rules (config/leagues.yaml's scoring_profile -- standard_ppr,
    standard_ppr_strict, or sfflf_tiered's tiered/position-dependent
    formula) instead of FantasyPros' generic points_ppr aggregate.

    This is the VALUE side of draft prep -- who's actually worth taking
    under this league's exact rules. Pair with get_draft_board (the pick
    ORDER/live results) to know both what to take and when you're on the
    clock. See fantasypros/draft_board.py's module docstring for the full
    methodology and known limitations (K/DST aren't scored here; 2-point
    conversion type isn't broken out by FantasyPros' projections).

    Keeper filtering: for a keeper league (f_league, east_coast), each
    manager's predicted keepers (get_league_keepers) are excluded from this
    board entirely before ranking -- a kept player doesn't re-enter the live
    draft, so leaving them in would misrepresent both who's available and
    everyone else's true league_rank. If the keeper lookup fails for a
    league (e.g. a CBS fetch error), that league's board falls back to
    unfiltered and says so, rather than silently guessing.

    Args:
        league_id: League id from config (e.g. "hard_chargers", "f_league",
                   "east_coast" -- see config/leagues.yaml's "id" field),
                   or "all" for every football league.
        top_n:     How many top-value players to list per league (default 15).

    Returns each league's top-N players by league-specific value, plus the
    biggest "boosts" (players this league's scoring likes more than the
    generic PPR market does) and "penalties" (players the market
    overvalues relative to this league's actual rules).
    """
    try:
        leagues = _resolve_leagues(league_id, sports={"football"})
        if not leagues:
            return f"No football league found matching '{league_id}'."

        fp_client = _get_fp()
        auth = _get_auth()
        out = []
        for league_cfg, _sport in leagues:
            name = league_cfg.get("name", league_cfg.get("id", league_id))
            profile = league_cfg.get("scoring_profile", "?")
            out.append(f"=== {name} (scoring_profile={profile}) ===")

            exclude = set()
            try:
                exclude = predicted_keepers(auth, league_cfg["cbs_league_id"], league_cfg)
            except Exception as e:
                out.append(f"  Keeper lookup failed ({e}) -- board built unfiltered.")

            rows = _build_fp_draft_board(league_cfg, fp_client, exclude_players=exclude)
            scored = [r for r in rows if r["league_points"] is not None]
            if exclude:
                out.append(f"  Excluded {len(exclude)} predicted keeper(s): "
                           f"{', '.join(sorted(exclude))}")
            if not scored:
                out.append("  No scored players -- FantasyPros data unavailable "
                            "or no projections matched.")
                out.append("")
                continue

            out.append(f"  Top {min(top_n, len(scored))} by {league_cfg.get('id')}-specific value:")
            for r in scored[:top_n]:
                delta = f" (ECR #{r['ecr_rank']}, {r['rank_delta']:+d})" if r["rank_delta"] else f" (ECR #{r['ecr_rank']})"
                out.append(f"    #{r['league_rank']:<3} {r['name']:<26} "
                           f"{r['position']:<3} {r['team']:<4} {r['league_points']:>6.1f} pts{delta}")

            boosted = sorted([r for r in scored if r["rank_delta"] and r["rank_delta"] > 0],
                             key=lambda r: -r["rank_delta"])[:8]
            if boosted:
                out.append("  Biggest boosts vs generic consensus:")
                for r in boosted:
                    out.append(f"    ECR #{r['ecr_rank']} -> #{r['league_rank']} "
                               f"(+{r['rank_delta']})  {r['name']} ({r['position']}, {r['team']})")

            penalized = sorted([r for r in scored if r["rank_delta"] and r["rank_delta"] < 0],
                               key=lambda r: r["rank_delta"])[:8]
            if penalized:
                out.append("  Biggest penalties vs generic consensus:")
                for r in penalized:
                    out.append(f"    ECR #{r['ecr_rank']} -> #{r['league_rank']} "
                               f"({r['rank_delta']})  {r['name']} ({r['position']}, {r['team']})")
            out.append("")

        return _respond("\n".join(out))

    except RuntimeError as e:
        return f"FantasyPros error: {e}"
    except Exception as e:
        logger.exception("get_fantasypros_draft_board failed")
        return f"Error building draft board: {e}"


# ---------------------------------------------------------------------------
# Tool: get_league_keepers
# ---------------------------------------------------------------------------

@mcp.tool()
def get_league_keepers(league_id: str = "all") -> str:
    """
    Keeper guidance for EVERY team in a football keeper league -- what each
    manager will likely keep, not just Christopher's own roster.

    Reuses the same per-team policy logic daily_decisions applies to
    Christopher's team (sports/football/keepers.py::keeper_guidance()) but
    loops it across every roster in the league via
    cbs.roster.get_all_team_rosters(), so you get a full league picture:
    which players each team is expected to protect, who else is eligible,
    and (east_coast only) which players are locked out because their
    3-season contract has expired (CBS CONTRACT column = 0).

    Ranking is by FantasyPros consensus ECR when FANTASYPROS_API_KEY is set
    (best value kept), degrading to "no ranking signal, can't guess who
    each manager keeps" if not -- this never fabricates a pick.

    IMPORTANT caveat by league:
      - east_coast: each manager decides their own keepers, so this is a
        genuine prediction of individually-rational choices (best 3 by ECR,
        contract-expired players excluded).
      - f_league: keepers are actually chosen by that league's
        COMMISSIONER, not each manager individually (Christopher isn't the
        commissioner there and the real selection criteria aren't published
        to him) -- treat f_league's output here as "who a manager would
        rationally keep by value," not a forecast of the commissioner's
        actual decision.
      - hard_chargers: not a keeper league; reported as such with no team
        breakdown.

    Args:
        league_id: League id from config (e.g. "hard_chargers", "f_league",
                   "east_coast" -- see config/leagues.yaml's "id" field),
                   or "all" for every football league.
    """
    try:
        auth    = _get_auth()
        leagues = _resolve_leagues(league_id, sports={"football"})
        if not leagues:
            return f"No football league found matching '{league_id}'."

        out = []
        for league_cfg, sport in leagues:
            lid  = league_cfg["cbs_league_id"]
            name = league_cfg.get("name", league_cfg.get("id", league_id))

            report = league_keeper_report(auth, lid, league_cfg, sport)
            out.append(f"=== {name} ===")

            if not report["is_keeper_league"]:
                out.append(f"  Not a keeper league. {report['policy_note']}")
                out.append("")
                continue

            out.append(f"  Max keepers: {report['max_keepers']} "
                       f"(decided by: {report['decided_by']})")
            out.append(f"  {report['policy_note']}")

            for tid, t in report["teams"].items():
                out.append(f"\n  -- {t['team_name']} --")
                if t["recommended_keeps"]:
                    src = f" (by {t['ranking_source']})" if t["ranking_source"] else ""
                    out.append(f"    Likely keeps{src}: {', '.join(t['recommended_keeps'])}")
                else:
                    out.append("    Likely keeps: none determined (no ranking signal)")
                if t["other_eligible"]:
                    out.append(f"    Other eligible: {', '.join(t['other_eligible'])}")
                if t["contract_expired"]:
                    out.append(f"    NOT keeper-eligible (contract expired): "
                               f"{', '.join(t['contract_expired'])}")
                if t["note"]:
                    out.append(f"    Note: {t['note']}")
            out.append("")

        return _respond("\n".join(out))

    except CBSAuthError as e:
        return f"CBS auth error: {e}"
    except Exception as e:
        logger.exception("get_league_keepers failed")
        return f"Error building league keeper report: {e}"


# ---------------------------------------------------------------------------
# Tool: get_roster_limits
# ---------------------------------------------------------------------------

@mcp.tool()
def get_roster_limits(league_id: str = "all") -> str:
    """
    Real roster position limits for a football league -- straight from
    CBS (league/rules JSON API), not guessed or left unconfirmed.

    Confirmed live 2026-08-24 against a real CBS "ROSTER LIMITS" settings
    screenshot for hard_chargers: exact match on every field. Prior to this
    tool, draft guides either guessed generic industry-standard position
    counts or flagged a league's exact starting-lineup shape (QB/RB/WR/TE/
    FLEX min-max counts, bench size) as unconfirmed -- this fetches CBS's
    actual settings directly (cbs/settings.py::fetch_roster_rules) so that
    never has to happen again.

    Returns each position's min/max ACTIVE (starting) slots -- note CBS
    models flexible ranges (e.g. RB: min 1, max 4) plus dedicated flex
    slots (e.g. RB-WR-TE: exactly 3), not fixed named slots -- along with
    the league-wide Starters/Bench/Total counts.

    Args:
        league_id: League id from config (e.g. "hard_chargers", "f_league",
                   "east_coast"), or "all" for every football league.
    """
    try:
        auth    = _get_auth()
        leagues = _resolve_leagues(league_id, sports={"football"})
        if not leagues:
            return f"No football league found matching '{league_id}'."

        out = []
        for league_cfg, sport in leagues:
            lid  = league_cfg["cbs_league_id"]
            name = league_cfg.get("name", league_cfg.get("id", league_id))
            rules = fetch_roster_rules(auth, lid, sport)
            out.append(format_roster_rules(rules, name))
            out.append("")

        return _respond("\n".join(out))

    except CBSAuthError as e:
        return f"CBS auth error: {e}"
    except Exception as e:
        logger.exception("get_roster_limits failed")
        return f"Error fetching roster limits: {e}"


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

    try:
        auth    = _get_auth()
        leagues = _resolve_leagues(league_id)
        if not leagues:
            return f"No league found matching '{league_id}'."

        # Parse date param
        playing_on = None
        if date:
            d_lower = date.strip().lower()
            today = today_et()
            if d_lower == "today":
                playing_on = today
            elif d_lower == "tomorrow":
                playing_on = today + timedelta(days=1)
            else:
                try:
                    playing_on = _date.fromisoformat(d_lower)
                except ValueError:
                    return f"Invalid date '{date}'. Use 'today', 'tomorrow', or 'YYYY-MM-DD'."

        _weekday  = now_et().weekday()   # 0=Mon … 6=Sun

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
    from datetime import date as _date, timedelta
    from mlb.schedule import todays_matchups
    from mlb.splits  import fetch_batter_splits, fetch_recent_form
    from mlb.parks   import park_label, park_factor as _pf
    from mlb.teams   import norm_name as _norm
    from mlb.injuries import fetch_active_il, annotate_roster_injuries

    try:
        # --- resolve date ---
        today = today_et()
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

        # P1 fix (2026-08-24): cross-reference the live MLB injured list so
        # this tool can never recommend START for a player who is actually
        # hurt (e.g. Juan Soto, confirmed 10-day IL since 7/25, was
        # recommended START here while daily_decisions correctly flagged
        # him -- the two tools didn't share an IL check). Routes through
        # mlb.injuries.annotate_roster_injuries(), the same team-safe check
        # daily_decisions uses (see its docstring) -- fetched once here,
        # cross-referenced per-league against each team's own roster below.
        try:
            active_il = fetch_active_il()
        except Exception as e:
            logger.warning("hitting_matchups: fetch_active_il failed, IL check skipped: %s", e)
            active_il = {}

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

            # P1 fix (2026-08-24): players confirmed on the active IL are
            # excluded from scoring below rather than silently scored (and
            # possibly floated to START by the must-start floor) as if
            # healthy. See annotate_roster_injuries()'s docstring for why
            # this is team-checked, not name-only.
            il_flags = annotate_roster_injuries(roster, active_il)
            il_norms = {_norm(f["player_name"]) for f in il_flags}
            il_display = {_norm(f["player_name"]): f for f in il_flags}

            scored: list[dict] = []
            no_game: list[str] = []
            on_il: list[str] = []
            pitchers_skipped  = 0

            for rs in roster:
                p = rs.player
                if not p.team:
                    continue
                if set(p.positions) & _PITCHER_POS and not (
                        set(p.positions) - _PITCHER_POS):
                    pitchers_skipped += 1
                    continue  # pure pitchers — skip

                if _norm(p.name) in il_norms:
                    il_type = il_display[_norm(p.name)].get("il_type", "IL")
                    on_il.append(f"{p.name} ({il_type})")
                    continue  # confirmed injured — never recommend START/SIT

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
                    "away_team":    m["away_team"],
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

            if on_il:
                out.append(f"\n  🚫 On IL (excluded): {', '.join(on_il)}")

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
    Run the full daily fantasy analysis for your league(s) -- baseball or
    football.

    Baseball leagues get the complete agent output: matchup summary,
    streaming SPs, waiver adds, drop candidates, trade signals, trade
    board, closer news, and daily lineup advice.

    Football leagues get a more limited report for now (see
    agent/football_decisions.py): starting-lineup/roster legality, if any
    starting slots are open, free-agent targets for those slots sorted by
    ownership%, and keeper guidance (real per-league policy -- east_coast
    has a confirmed 3-keeper cap with no cost mechanic and will get ranked
    keeper suggestions if FANTASYPROS_API_KEY is set; f_league is also a
    keeper league (confirmed 2-keeper cap, any position, no cost mechanic --
    but selection is made by that league's commissioner, not Christopher, so
    treat f_league keeper output here as a ranked recommendation, not what
    will actually be kept); hard_chargers isn't a keeper league at all).
    This tool only covers YOUR OWN roster -- use get_league_keepers for
    keeper guidance across every manager in the league. There is no
    performance-based scoring for football yet otherwise (no live NFL stat
    feed to calibrate against) -- don't expect start/sit advice or ranked
    waiver adds the way baseball gets them.

    Args:
        league_id: League id from config, or "all" for all leagues (baseball
                   AND football).
    """
    try:
        auth    = _get_auth()
        leagues = _resolve_leagues(league_id, sports=_FOOTBALL_AWARE_SPORTS)
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
                if sport == "baseball":  # mlb.stats enrichment is baseball-only
                    try:
                        enrich_roster(roster)
                    except Exception:
                        pass
                team = Team(id=tid, name=name, roster=roster)
                if sport == "football":
                    result = run_football_decisions(auth, lid, league_cfg, team, sport)
                else:
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
