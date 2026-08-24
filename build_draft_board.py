"""
Build a scored draft board for one or all 3 of Christopher's CBS football
leagues -- FantasyPros consensus ADP (realistic pick-availability) crossed
with each player's season projection, re-scored under THAT league's own
scoring rules (sports/football/scoring.py) instead of FantasyPros' generic
points_ppr aggregate. See fantasypros/draft_board.py's module docstring for
the full methodology and known limitations.

This replaces the two one-off scripts from earlier in the project
(build_hc_draft_board.py, build_sfflf_draft_board.py) with one committed,
config-driven tool covering all 3 leagues -- a 4th league or a scoring
change is a config/leagues.yaml + sports/football/scoring.py edit, not a
new script.

Usage (run from the repo root):
    python build_draft_board.py                  # all 3 leagues
    python build_draft_board.py hard_chargers     # just one league
    python build_draft_board.py f_league east_coast

Writes one CSV per league: <league_id>_draft_board.csv (repo root).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

import yaml

from config.settings import FANTASYPROS_API_KEY
from fantasypros.client import FantasyProsClient
from fantasypros.draft_board import build_draft_board, write_draft_board_csv


def _load_football_leagues():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "leagues.yaml")
    with open(path) as f:
        config = yaml.safe_load(f) or {}
    return config.get("football", []) or []


def main():
    if not FANTASYPROS_API_KEY:
        print("FANTASYPROS_API_KEY not set -- check .env")
        sys.exit(1)

    requested = set(sys.argv[1:]) or None  # None = all leagues
    leagues = _load_football_leagues()
    if requested:
        unknown = requested - {lg.get("id") for lg in leagues}
        if unknown:
            print(f"Unknown league id(s): {', '.join(sorted(unknown))}")
            print(f"Known: {', '.join(lg.get('id', '?') for lg in leagues)}")
            sys.exit(1)
        leagues = [lg for lg in leagues if lg.get("id") in requested]

    client = FantasyProsClient(FANTASYPROS_API_KEY)
    repo_root = os.path.dirname(os.path.abspath(__file__))

    for league_cfg in leagues:
        league_id = league_cfg.get("id", "?")
        name = league_cfg.get("name", league_id)
        profile = league_cfg.get("scoring_profile", "?")
        print(f"\n=== {name} ({league_id}) -- scoring_profile={profile} ===")

        rows = build_draft_board(league_cfg, client)
        scored = [r for r in rows if r["league_points"] is not None]
        print(f"Ranked: {len(rows)} players total, {len(scored)} scored (QB/RB/WR/TE with a projection)")

        out_path = os.path.join(repo_root, f"{league_id}_draft_board.csv")
        write_draft_board_csv(rows, out_path)
        print(f"Wrote {out_path}")

        boosted = sorted([r for r in scored if r["rank_delta"] and r["rank_delta"] > 0],
                         key=lambda r: -r["rank_delta"])[:10]
        if boosted:
            print(f"Top players boosted by {league_id}'s scoring vs generic consensus rank:")
            for r in boosted:
                print(f"  ECR #{r['ecr_rank']:>3}  ->  {league_id} #{r['league_rank']:>3}  "
                      f"(+{r['rank_delta']:>3})  {r['name']} ({r['position']}, {r['team']})")


if __name__ == "__main__":
    main()
