"""
Quick test runner for get_filtered_waiver_adds.

Usage:
    python run_waiver_adds.py
    python run_waiver_adds.py --league casey_stengel
    python run_waiver_adds.py --league hemp --position SP
    python run_waiver_adds.py --league hemp --next-week
"""
import argparse
import os
import sys
import yaml
from datetime import datetime

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(__file__))

from config.settings import CBS_COOKIE
from cbs.auth import CBSAuth, CBSAuthError
from agent.decisions import get_filtered_waiver_adds


def load_leagues(path="config/leagues.yaml"):
    with open(path) as f:
        return yaml.safe_load(f) or {}


def fmt_rec(rec, i):
    p = rec.player
    name = getattr(p, "name", str(p))
    team = getattr(p, "team", "???")
    pos  = "/".join(getattr(p, "positions", []) or []) or "?"
    score = getattr(rec, "score", None)
    score_str = f"  score={score:.2f}" if score is not None else ""

    stats = getattr(p, "stats", None) or {}
    if stats:
        stat_str = "  " + "  ".join(f"{k}={v}" for k, v in list(stats.items())[:6])
    else:
        stat_str = ""

    return f"  {i:>2}. {name:<25} {pos:<8} {team:<5}{score_str}{stat_str}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--league", default=None,
                        help="League id from leagues.yaml (e.g. hemp, casey_stengel). "
                             "Omit to run all leagues.")
    parser.add_argument("--position", default=None,
                        help="Position filter: C, 1B, 2B, 3B, SS, OF, SP, RP")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--min-batters", type=int, default=2)
    parser.add_argument("--next-week", action="store_true",
                        help="Use week_offset=1 (next scoring period)")
    args = parser.parse_args()

    week_offset = 1 if args.next_week else 0
    label = "next week" if args.next_week else "current week"

    try:
        auth = CBSAuth()
    except CBSAuthError as e:
        print(f"AUTH ERROR: {e}")
        sys.exit(1)

    all_leagues = load_leagues()
    sport_map = {}
    for sport, leagues in all_leagues.items():
        for lg in leagues:
            sport_map[lg["id"]] = (sport, lg)

    if args.league:
        if args.league not in sport_map:
            print(f"Unknown league '{args.league}'. Available: {list(sport_map)}")
            sys.exit(1)
        targets = [args.league]
    else:
        targets = list(sport_map)

    os.makedirs("logs", exist_ok=True)
    out_lines = [f"# Waiver Add Recommendations — {datetime.now():%Y-%m-%d %H:%M}\n"]

    for lid in targets:
        sport, lg_cfg = sport_map[lid]
        league_id = lg_cfg["cbs_league_id"]
        name = lg_cfg.get("name", league_id)

        print(f"\n{'='*60}")
        print(f"League: {name}  ({label}, offset={week_offset})")
        if args.position:
            print(f"Position filter: {args.position}")
        print('='*60)

        try:
            recs = get_filtered_waiver_adds(
                auth,
                league_id,
                lg_cfg,
                sport=sport,
                position_filter=args.position,
                min_batters=args.min_batters,
                limit=args.limit,
                week_offset=week_offset,
            )
        except Exception as e:
            msg = f"  ERROR: {e}"
            print(msg)
            out_lines.append(f"\n## {name}\n{msg}\n")
            continue

        if not recs:
            print("  (no recommendations returned)")
            out_lines.append(f"\n## {name}\nNo recommendations.\n")
            continue

        section = [f"\n## {name} — Top {len(recs)} waiver adds ({label})\n"]
        for i, rec in enumerate(recs, 1):
            line = fmt_rec(rec, i)
            print(line)
            section.append(line)
        out_lines.extend(section)

    log_path = "logs/waiver_adds_output.md"
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines))
    print(f"\nOutput saved to {log_path}")


if __name__ == "__main__":
    main()
