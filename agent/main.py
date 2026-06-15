import argparse
import logging
import sys
import yaml

from config.settings import CBS_COOKIE, DRY_RUN
from cbs.auth import CBSAuth, CBSAuthError, CBSCookieExpiredError
from cbs.roster import get_roster
from cbs.waivers import get_available_players
from cbs.lineup import get_current_lineup
from mlb.stats import enrich_roster, enrich_players
from agent.decisions import run_decisions
from data.models import Team

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.WARNING,
                    format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def load_leagues(path="config/leagues.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def run_league(auth: CBSAuth, league: dict, sport: str,
               run_type: str, dry_run: bool):
    lid  = league["cbs_league_id"]
    tid  = str(league["cbs_team_id"])
    name = league.get("name", lid)
    print(f"\n=== {name} ({sport}) ===")

    if lid == "FILL_IN" or tid == "FILL_IN":
        print("  League/team ID not configured -- skipping.")
        return

    roster = get_roster(auth, lid, tid, sport)
    print(f"  Roster: {len(roster)} players")
    for rs in roster[:5]:
        print(f"    {rs.slot:>4}  {rs.player.name} ({rs.player.team})")
    if len(roster) > 5:
        print(f"    ... and {len(roster) - 5} more")

    try:
        enrich_roster(roster)
        enriched = sum(1 for rs in roster if rs.player.stats)
        print(f"  Stats enriched: {enriched}/{len(roster)} roster players")
    except Exception as e:
        print(f"  Stats enrichment: unavailable ({e})")

    lineup = get_current_lineup(auth, lid, tid, sport)
    starting = sum(1 for s in lineup if s["is_starting"])
    print(f"  Lineup: {starting}/{len(lineup)} starting")

    if run_type in ("daily", "waivers"):
        try:
            available = get_available_players(auth, lid, sport)
            print(f"  Free agents visible: {len(available)}")
            try:
                enrich_players(available[:200])
            except Exception:
                pass
        except Exception as e:
            print(f"  Free agents: unavailable ({e})")

    print()
    if run_type in ("daily", "weekly", "waivers"):
        try:
            team = Team(id=tid, name=name, roster=roster)
            result = run_decisions(auth, lid, league, team, sport)
            _print_decisions(result, dry_run)
        except Exception as e:
            print(f"  Decisions unavailable: {e}")
            logger.exception("run_decisions failed for %s", lid)
    else:
        print(f"  DRY_RUN={dry_run} -- no submissions will be made.")


def _print_decisions(result: dict, dry_run: bool):
    fmt = result.get("format", "")
    print(f"  Format: {fmt}")

    for action in result.get("actions", []):
        atype = action.get("type", "")

        if atype == "matchup_summary":
            print(f"  Matchup: {action.get('summary', '')}")
            pri = action.get("priority_cats", [])
            if pri:
                print(f"  Priority categories (losing, easiest first): {', '.join(pri)}")

        elif atype == "roto_summary":
            print(f"  Standings: {action.get('summary', '')}")
            weak = action.get("weak_cats", [])
            if weak:
                print(f"  Weakest categories: {', '.join(weak)}")

        elif atype == "nl_eligibility_warnings":
            for w in action.get("warnings", []):
                print(f"  !! {w['warning']}")

        elif atype in ("streaming_sp", "streaming_sp_next_week"):
            recs = action.get("recommendations", [])
            label = ("  Next week 2-starters" if atype == "streaming_sp_next_week"
                     else "  Streaming SP")
            if recs:
                print(f"{label} ({action.get('note', '')}):")
                for r in recs:
                    tag = " [2-START]" if r.get("starts", 1) >= 2 else ""
                    print(f"    + {r['player']} ({r['team']}){tag}  "
                          f"score={r['score']}  {r.get('reason', '')}")
            else:
                print(f"{label}: no candidates above threshold")

        elif atype == "waiver_adds":
            recs = action.get("recommendations", [])
            if recs:
                print(f"  Waiver adds ({len(recs)} suggestions):")
                for r in recs:
                    cats = r.get("helps_cats") or []
                    pos  = "/".join(r.get("positions", []))
                    print(f"    + {r['player']} ({r.get('team','?')}) "
                          f"[{pos}]  helps: {', '.join(cats)}")

        elif atype == "daily_lineup":
            today_str = action.get("today", "")
            advice    = action.get("advice", [])
            teams_ct  = len(action.get("teams_playing", []))

            print(f"\n  --- Daily Lineup ({today_str}, {teams_ct} MLB teams playing) ---")

            sp_starting = [a for a in advice
                           if "SP" in a["positions"] and a["advice"] in ("start", "ok")]
            sp_bench    = [a for a in advice
                           if "SP" in a["positions"] and a["advice"] == "bench_pitcher"]
            bat_off     = [a for a in advice
                           if "SP" not in a["positions"] and "RP" not in a["positions"]
                           and a["advice"] == "bench"]
            bat_active  = [a for a in advice
                           if "SP" not in a["positions"] and "RP" not in a["positions"]
                           and a["advice"] in ("start", "ok")]

            if sp_starting:
                print(f"  SPs starting today ({len(sp_starting)}):")
                for a in sp_starting:
                    mark = "active" if a["is_starting"] else "BENCH - move to active!"
                    print(f"    [{mark:>24}] {a['player']} ({a['team']})")
            else:
                print("  SPs starting today: none confirmed yet")

            if sp_bench:
                print(f"  SPs NOT starting today ({len(sp_bench)}):")
                for a in sp_bench:
                    mark = "ACTIVE - bench!" if a["is_starting"] else "already benched"
                    print(f"    [{mark:>20}] {a['player']} ({a['team']})  {a['reason']}")

            if bat_off:
                print(f"  Batters with off days - bench these ({len(bat_off)}):")
                for a in bat_off:
                    mark = "ACTIVE - bench!" if a["is_starting"] else "already benched"
                    pos  = "/".join(a["positions"])
                    print(f"    [{mark:>20}] {a['player']} ({a['team']}) [{pos}]")

            print(f"  Batters with games today: {len(bat_active)}")

    if dry_run:
        print(f"\n  DRY_RUN=True -- no submissions made.")


def main():
    parser = argparse.ArgumentParser(description="CBS Fantasy Agent")
    parser.add_argument("--run", choices=["daily", "weekly", "waivers", "lineup"],
                        default="daily")
    parser.add_argument("--league", default="all",
                        help="league id from leagues.yaml, or 'all'")
    parser.add_argument("--sport", default="all",
                        help="baseball, football, or 'all'")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    dry = args.dry_run or DRY_RUN
    print(f"CBS Fantasy Agent -- run={args.run}, league={args.league}, "
          f"sport={args.sport}, dry_run={dry}")

    try:
        auth = CBSAuth(CBS_COOKIE)
        auth.get_session()
    except CBSAuthError as e:
        print(f"\nAuth setup failed:\n{e}")
        return 1

    config = load_leagues()
    ran = 0
    for sport, leagues in config.items():
        if args.sport not in ("all", sport):
            continue
        for league in leagues or []:
            if args.league not in ("all", league.get("id")):
                continue
            try:
                run_league(auth, league, sport, args.run, dry)
                ran += 1
            except CBSCookieExpiredError as e:
                print(f"\nSession expired:\n{e}")
                return 1
            except Exception as e:
                print(f"  ERROR in {league.get('name', '?')}: {e}")
                logger.exception("run_league failed")

    if ran == 0:
        print("No leagues matched the --league/--sport filters.")
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
