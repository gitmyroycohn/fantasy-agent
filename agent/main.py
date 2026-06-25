import argparse
import io
import logging
import os
import sys
import yaml

from config.settings import CBS_COOKIE, DRY_RUN
from cbs.auth import CBSAuth, CBSAuthError, CBSCookieExpiredError
from cbs.roster import get_roster
from cbs.waivers import get_available_players
from cbs.lineup import get_current_lineup
from mlb.stats import enrich_roster, enrich_players
from agent.decisions import run_decisions
from agent.summary import format_tldr
from agent.history import load_history, save_history, update_and_annotate, prune_history
from data.models import Team

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

OUTPUT_PATH  = "logs/latest_output.md"
HISTORY_PATH = "logs/history.json"


class _Tee:
    def __init__(self, a, b):
        self.a, self.b = a, b
    def write(self, s):
        self.a.write(s)
        self.b.write(s)
    def flush(self):
        self.a.flush()
        self.b.flush()


def load_leagues(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "config", "leagues.yaml")
    with open(path) as f:
        return yaml.safe_load(f) or {}


def run_league(auth, league, sport, run_type, dry_run, history=None):
    lid  = league["cbs_league_id"]
    tid  = str(league["cbs_team_id"])
    name = league.get("name", lid)
    print(f"\n=== {name} ({sport}) ===")

    if lid == "FILL_IN" or tid == "FILL_IN":
        print("  League/team ID not configured -- skipping.")
        return None

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

    lineup  = get_current_lineup(auth, lid, tid, sport)
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
            team   = Team(id=tid, name=name, roster=roster)
            result = run_decisions(auth, lid, league, team, sport)
            if history is not None:
                update_and_annotate(result, history, lid)
            _print_decisions(result, dry_run)
            return result
        except Exception as e:
            print(f"  Decisions unavailable: {e}")
            logger.exception("run_decisions failed for %s", lid)
            return None


_CHURN_THRESHOLD = 4   # total adds+drops above this triggers the guardrail
_PRIORITY_CAP    = 3   # show at most this many items per section when capped


def _move_volume(actions: list) -> int:
    """Count total recommended moves (adds + cuts) across all action types."""
    total = 0
    for action in actions:
        if action.get("type") == "waiver_adds":
            total += len(action.get("recommendations", []))
        elif action.get("type") == "drop_candidates":
            for severity in ("cut", "monitor"):
                total += sum(
                    1 for d in action.get("drops", [])
                    if d.get("severity") == severity
                )
    return total


def _print_decisions(result, dry_run):
    fmt = result.get("format", "")
    print(f"  Format: {fmt}")

    actions     = result.get("actions", [])
    total_moves = _move_volume(actions)
    capped      = total_moves >= _CHURN_THRESHOLD
    if capped:
        print(f"\n  ⚠  CHURN GUARD: {total_moves} moves recommended -- "
              f"showing top {_PRIORITY_CAP} per section. "
              f"Prioritize ruthlessly; avoid making all moves at once.")

    for action in actions:
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
            recs   = action.get("recommendations", [])
            offset = action.get("week_offset", 0)
            if atype == "streaming_sp_next_week":
                label = f"  Week +{offset} 2-starters"
            else:
                label = "  Streaming SP"
            if recs:
                print(f"{label} ({action.get('note', '')}):")
                for r in recs:
                    tag  = " [2-START]" if r.get("starts", 1) >= 2 else ""
                    bb   = " [BB-2START ⚡ elite hold]" if r.get("back_to_back") else ""
                    dtag = f"  [{r['_days']}]" if "_days" in r else ""
                    print(f"    + {r['player']} ({r['team']}){tag}{bb}{dtag}  "
                          f"score={r['score']}  {r.get('reason', '')}")
            else:
                print(f"{label}: no candidates above threshold")

        elif atype == "waiver_adds":
            recs = action.get("recommendations", [])
            if recs:
                shown = recs[:_PRIORITY_CAP] if capped else recs
                extra = len(recs) - len(shown)
                note  = f" (top {len(shown)} shown -- {extra} more suppressed)" if extra else ""
                print(f"  Waiver adds ({len(recs)} suggestions){note}:")
                for r in shown:
                    cats  = r.get("helps_cats") or []
                    pos   = "/".join(r.get("positions", []))
                    dtag  = f"  [{r['_days']}]" if "_days" in r else ""
                    stats = r.get("_stats") or {}

                    # Closer Monkey tag for SV picks
                    cm_tag = ""
                    if r.get("cm_role"):
                        role_map = {
                            "closer":        "CLOSER",
                            "first_in_line": "1st-in-line",
                            "second_in_line":"2nd-in-line",
                        }
                        lbl  = role_map.get(r["cm_role"], r["cm_role"])
                        comm = " [committee]" if r.get("cm_committee") else ""
                        cm_tag = f"  [CM: {lbl}{comm} | {r.get('cm_tendency','')}]"

                    # Savant snippet
                    sav_parts = []
                    if stats.get("sv_barrel_pct") is not None:
                        sav_parts.append(f"Brl%={stats['sv_barrel_pct']:.1f}")
                    if stats.get("sv_xwoba") is not None:
                        sav_parts.append(f"xwOBA={stats['sv_xwoba']:.3f}")
                    if stats.get("sv_xera") is not None:
                        sav_parts.append(f"xERA={stats['sv_xera']:.2f}")
                    sav_tag = ("  [" + " | ".join(sav_parts) + "]") if sav_parts else ""

                    print(f"    + {r['player']} ({r.get('team','?')}) "
                          f"[{pos}]{dtag}  helps: {', '.join(cats)}{cm_tag}{sav_tag}")

        elif atype == "drop_candidates":
            drops   = action.get("drops", [])
            cut     = [d for d in drops if d["severity"] == "cut"]
            monitor = [d for d in drops if d["severity"] == "monitor"]
            if drops:
                print(f"\n  --- Drop Candidates ---")
            if cut:
                shown_cut = cut[:_PRIORITY_CAP] if capped else cut
                extra_cut = len(cut) - len(shown_cut)
                note_cut  = f" (top {len(shown_cut)} -- {extra_cut} more)" if extra_cut else ""
                print(f"  CUT ({len(cut)}) -- below replacement level{note_cut}:")
                for d in shown_cut:
                    pos  = "/".join(d.get("positions", []))
                    rep  = d.get("replace_with")
                    dtag = f"  [{d['_days']}]" if "_days" in d else ""
                    mark = "active" if d.get("is_starting") else "bench"
                    rstr = f"  => add {rep}" if rep else ""
                    print(f"    DROP {d['player']} ({d['team']}) [{pos}] [{mark}]{dtag}")
                    print(f"         {d['reason']}{rstr}")
            if monitor:
                print(f"  MONITOR ({len(monitor)}) -- borderline:")
                for d in monitor:
                    pos  = "/".join(d.get("positions", []))
                    rep  = d.get("replace_with")
                    dtag = f"  [{d['_days']}]" if "_days" in d else ""
                    rstr = f"  => consider {rep}" if rep else ""
                    print(f"    WATCH {d['player']} ({d['team']}) [{pos}]{dtag}")
                    print(f"          {d['reason']}{rstr}")

        elif atype == "trade_signals":
            signals = action.get("signals", [])
            if signals:
                sells = [s for s in signals if s["signal"] == "sell_high"]
                buys  = [s for s in signals if s["signal"] == "buy_low"]
                print(f"\n  --- Trade Value Signals ---")
                if sells:
                    print(f"  SELL HIGH ({len(sells)}) -- outpacing projections:")
                    for s in sells[:4]:
                        pos  = "/".join(s.get("positions", []))
                        conf = s.get("confidence", "")
                        print(f"    ~ {s['name']} ({s['team']}) [{pos}] [{conf}]")
                        print(f"      {s['reason']}")
                if buys:
                    print(f"  BUY LOW ({len(buys)}) -- underperforming projections:")
                    for s in buys[:4]:
                        pos  = "/".join(s.get("positions", []))
                        conf = s.get("confidence", "")
                        print(f"    ~ {s['name']} ({s['team']}) [{pos}] [{conf}]")
                        print(f"      {s['reason']}")

        elif atype == "trade_leads":
            profile = action.get("profile", {})
            leads   = action.get("leads", [])
            n_teams = action.get("n_teams", 0)
            if profile or leads:
                print(f"\n  --- Trade Board ({n_teams} teams scanned) ---")
                if profile:
                    surplus = ", ".join(profile.get("surplus", [])) or "none"
                    deficit = ", ".join(profile.get("deficit", [])) or "none"
                    print(f"  My surplus (can sell): {surplus}")
                    print(f"  My deficit (need buy): {deficit}")
                if leads:
                    print(f"  Top trade targets:")
                    for lead in leads:
                        i_want    = ", ".join(lead.get("i_want",    [])) or "—"
                        they_want = ", ".join(lead.get("they_want", [])) or "—"
                        align     = lead.get("alignment", 0)
                        print(f"    >> {lead['team_name']}  [alignment={align}]")
                        print(f"       They have: {i_want}")
                        print(f"       They need: {they_want}")

        elif atype == "closer_news":
            posts = action.get("posts", [])
            if posts:
                print(f"\n  --- Closer Monkey News ---")
                for p in posts:
                    label = "RAPID REACTION" if "rapid" in p["title"].lower() else "LEDGER"
                    print(f"  [{label}] {p['title']}")
                    if p.get("summary"):
                        for line in p["summary"].split("\n")[:3]:
                            line = line.strip()
                            if line:
                                print(f"    {line}")
                    if p.get("link"):
                        print(f"    {p['link']}")

        elif atype == "daily_lineup":
            today_str = action.get("today", "")
            advice    = action.get("advice", [])
            teams_ct  = len(action.get("teams_playing", []))
            no_bench  = action.get("no_bench", False)

            print(f"\n  --- Daily Lineup ({today_str}, {teams_ct} MLB teams playing) ---")

            if no_bench:
                sp_on   = [a for a in advice if "SP" in a["positions"]
                           and a["advice"] in ("start", "ok")]
                sp_off  = [a for a in advice if "SP" in a["positions"]
                           and a["advice"] == "bench_pitcher"]
                bat_off = [a for a in advice if "SP" not in a["positions"]
                           and "RP" not in a["positions"] and a["advice"] == "bench"]
                bat_on  = [a for a in advice if "SP" not in a["positions"]
                           and "RP" not in a["positions"] and a["advice"] in ("start", "ok")]

                if sp_on:
                    print(f"  SPs pitching today ({len(sp_on)}):")
                    for a in sp_on:
                        print(f"    {a['player']} ({a['team']})")
                else:
                    print("  SPs pitching today: none confirmed yet")
                if sp_off:
                    print(f"  SPs NOT pitching today ({len(sp_off)}) [no bench -- FYI only]:")
                    for a in sp_off:
                        print(f"    {a['player']} ({a['team']})  {a['reason']}")
                if bat_off:
                    print(f"  Batters with no game today ({len(bat_off)}) [no bench -- FYI only]:")
                    for a in bat_off:
                        pos = "/".join(a["positions"])
                        print(f"    {a['player']} ({a['team']}) [{pos}]  -- 0 stats today")
                print(f"  Batters with games today: {len(bat_on)}")

            else:
                sp_on   = [a for a in advice if "SP" in a["positions"]
                           and a["advice"] in ("start", "ok")]
                sp_off  = [a for a in advice if "SP" in a["positions"]
                           and a["advice"] == "bench_pitcher"]
                bat_off = [a for a in advice if "SP" not in a["positions"]
                           and "RP" not in a["positions"] and a["advice"] == "bench"]
                bat_on  = [a for a in advice if "SP" not in a["positions"]
                           and "RP" not in a["positions"] and a["advice"] in ("start", "ok")]

                if sp_on:
                    print(f"  SPs starting today ({len(sp_on)}):")
                    for a in sp_on:
                        mark = "active" if a["is_starting"] else "BENCH - move to active!"
                        print(f"    [{mark:>24}] {a['player']} ({a['team']})")
                else:
                    print("  SPs starting today: none confirmed yet")
                if sp_off:
                    print(f"  SPs NOT starting today ({len(sp_off)}):")
                    for a in sp_off:
                        mark = "ACTIVE - bench!" if a["is_starting"] else "already benched"
                        print(f"    [{mark:>20}] {a['player']} ({a['team']})  {a['reason']}")
                if bat_off:
                    print(f"  Batters with off days - bench these ({len(bat_off)}):")
                    for a in bat_off:
                        mark = "ACTIVE - bench!" if a["is_starting"] else "already benched"
                        pos  = "/".join(a["positions"])
                        print(f"    [{mark:>20}] {a['player']} ({a['team']}) [{pos}]")
                print(f"  Batters with games today: {len(bat_on)}")

        elif atype == "injury_report":
            txns        = action.get("transactions", [])
            roster_hits = action.get("roster_hits", [])
            roster_norms = action.get("roster_norms", set())

            print(f"\n  --- Injury Report (last 7 days) ---")

            if roster_hits:
                print(f"  ★ YOUR ROSTER PLAYERS:")
                for t in roster_hits:
                    icon = "🚑" if t["type"] == "placed" else ("✅" if t["type"] == "activated" else "🔄")
                    date_str = t.get("date", "")
                    try:
                        from datetime import datetime
                        _dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
                        date_str = f"{_dt.month}/{_dt.day}"
                    except Exception:
                        pass
                    print(f"    {icon} {t['player']} ({t['team']}) — {t['type_desc']}  [{date_str}]")

            other_txns = [t for t in txns if t["norm"] not in roster_norms]
            if other_txns:
                placed    = [t for t in other_txns if t["type"] == "placed"]
                activated = [t for t in other_txns if t["type"] == "activated"]
                if placed:
                    print(f"  🚑 Placed ({len(placed)}):", ", ".join(
                        f"{t['player']} ({t['team']})" for t in placed[:8]))
                if activated:
                    print(f"  ✅ Activated ({len(activated)}):", ", ".join(
                        f"{t['player']} ({t['team']})" for t in activated[:8]))

            if not txns:
                print("  No IL moves in the last 7 days.")

    if dry_run:
        print(f"\n  DRY_RUN=True -- no submissions made.")


def _write_output(header, body):
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(header)
        f.write(body)


def main():
    parser = argparse.ArgumentParser(description="CBS Fantasy Agent")
    parser.add_argument("--run", choices=["daily", "weekly", "waivers", "lineup"],
                        default="daily")
    parser.add_argument("--league", default="all")
    parser.add_argument("--sport",  default="all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    dry = args.dry_run or DRY_RUN
    header_line = (f"CBS Fantasy Agent -- run={args.run}, league={args.league}, "
                   f"sport={args.sport}, dry_run={dry}")
    print(header_line)

    try:
        auth = CBSAuth(CBS_COOKIE)
        auth.get_session()
    except CBSAuthError as e:
        print(f"\nAuth setup failed:\n{e}")
        return 1

    history = load_history(HISTORY_PATH)
    prune_history(history)

    config  = load_leagues()
    results = []
    ran     = 0

    body_buf        = io.StringIO()
    original_stdout = sys.stdout
    sys.stdout      = _Tee(original_stdout, body_buf)

    try:
        print(header_line)

        for sport, leagues in config.items():
            if args.sport not in ("all", sport):
                continue
            for league in leagues or []:
                if args.league not in ("all", league.get("id")):
                    continue
                try:
                    result = run_league(auth, league, sport, args.run, dry,
                                        history=history)
                    if result:
                        results.append(result)
                    ran += 1
                except CBSCookieExpiredError as e:
                    print(f"\nSession expired:\n{e}")
                    sys.stdout = original_stdout
                    return 1
                except Exception as e:
                    print(f"  ERROR in {league.get('name', '?')}: {e}")
                    logger.exception("run_league failed")

        if ran == 0:
            print("No leagues matched the --league/--sport filters.")
        print("\nDone.")

    finally:
        sys.stdout = original_stdout

    save_history(history, HISTORY_PATH)

    body = body_buf.getvalue()
    if results:
        tldr = format_tldr(results)
        _write_output(tldr + "\n", body)
        print(f"\n[Output written to {OUTPUT_PATH}]")
    else:
        _write_output("", body)

    return 0


if __name__ == "__main__":
    sys.exit(main())
