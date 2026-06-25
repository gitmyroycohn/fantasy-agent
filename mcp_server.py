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

def _load_leagues(path="config/leagues.yaml"):
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
        return format_trade_result(result)

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

        return "\n".join(out)

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
        return "\n".join(out)

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
        return "\n".join(out)

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

        out = []
        for league_cfg, sport in leagues:
            lid  = league_cfg["cbs_league_id"]
            name = league_cfg.get("name", lid)

            week_offset = 1 if next_week else 0

            recs = get_filtered_waiver_adds(
                auth, lid, league_cfg, sport,
                position_filter=position,
                playing_on=playing_on,
                min_batters=min_batters,
                limit=limit,
                week_offset=week_offset,
            )

            header_parts = [name]
            if next_week:
                header_parts.append("NEXT WEEK")
            if position:
                header_parts.append(f"position={position.upper()}")
            if playing_on:
                header_parts.append(f"playing={playing_on.isoformat()}")
            out.append(f"\n=== {' | '.join(header_parts)} -- Waiver Adds ===")

            if not recs:
                out.append("  No recommendations found matching these filters.")
                continue

            for r in recs:
                cats  = ", ".join(r.get("helps_cats", []))
                pos   = "/".join(r.get("positions", []))
                stats = r.get("_stats") or {}

                sav_parts = []
                if stats.get("sv_xwoba"):
                    sav_parts.append(f"xwOBA={stats['sv_xwoba']:.3f}")
                if stats.get("sv_barrel_pct") is not None:
                    sav_parts.append(f"Brl%={stats['sv_barrel_pct']:.1f}")
                if stats.get("sv_xera") is not None:
                    sav_parts.append(f"xERA={stats['sv_xera']:.2f}")
                sav_str = ("  [" + " | ".join(sav_parts) + "]") if sav_parts else ""

                cm_tag = ""
                if r.get("cm_role"):
                    cm_tag = f"  [CM: {r['cm_role']} | {r.get('cm_tendency','')}]"

                start_tag = ""
                if r.get("back_to_back"):
                    start_tag = "  ★★ 2-start back-to-back"
                elif r.get("two_starter"):
                    start_tag = "  ★ 2-start"

                out.append(f"  + {r['player']} ({r.get('team','?')}) [{pos}]"
                           f"  helps: {cats}{cm_tag}{sav_str}{start_tag}")

        return "\n".join(out) if out else "No waiver recommendations generated."

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
    Get buy-low / sell-high trade value signals for your current roster.

    Compares each player's season pace against FP rest-of-season projections.
    Players outpacing projections are sell-high candidates.
    Players underperforming projections are buy-low targets.

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
            roster = cbs_get_roster(auth, lid, tid, sport)
            try:
                enrich_roster(roster)
            except Exception:
                pass
            try:
                enrich_with_fp_projections(roster, fp_client)
            except Exception:
                pass
            try:
                enrich_with_savant(roster, sav_client)
            except Exception:
                pass

            signals = analyze_roster_value(roster)
            out.append(f"=== {name} -- Trade Value Signals ===")
            if not signals:
                out.append("  No signals generated (need FP projections + season stats).")
                continue

            sells = [s for s in signals if s["signal"] == "sell_high"]
            buys  = [s for s in signals if s["signal"] == "buy_low"]

            if sells:
                out.append(f"SELL HIGH ({len(sells)}) -- outpacing projections:")
                for s in sells:
                    pos = "/".join(s.get("positions", []))
                    out.append(f"  ~ {s['name']} ({s['team']}) [{pos}] [{s.get('confidence','')}]")
                    out.append(f"    {s['reason']}")
            if buys:
                out.append(f"BUY LOW ({len(buys)}) -- underperforming projections:")
                for s in buys:
                    pos = "/".join(s.get("positions", []))
                    out.append(f"  ~ {s['name']} ({s['team']}) [{pos}] [{s.get('confidence','')}]")
                    out.append(f"    {s['reason']}")
            out.append("")

        return "\n".join(out)

    except CBSAuthError as e:
        return f"CBS auth error: {e}"
    except Exception as e:
        logger.exception("roster_value_signals failed")
        return f"Error fetching roster value signals: {e}"


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

        return buf.getvalue() or "No output generated."

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
