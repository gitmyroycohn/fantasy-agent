"""
contract_probe.py -- verify cbs.roster.fetch_contract_years() (the new
CONTRACT-column scraper added 2026-08-23) against the real live east_coast
roster page, and check the contract_years_to_acquired_seasons() conversion
against the known-good snapshot from 2026-08-18 (see project memory
"football keeper policies").

Same pattern as dump_samples.py/fp_probe.py: run locally (this machine has
real internet access; the Cowork sandbox that wrote this script does not),
then share the printed output back with Claude.

Run:  python contract_probe.py
Writes samples/contract_years_raw.json for the record.
"""

import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

from cbs.auth import CBSAuth, CBSAuthError
from cbs.roster import fetch_contract_years
from sports.football.keepers import contract_years_to_acquired_seasons, contract_status

LEAGUE_ID = "ecfc"
TEAM_ID = "5"          # Christopher's Hotlanta Hussies
CURRENT_SEASON = 2026

# The exact values observed on this same roster 2026-08-18 -- if these
# don't match today, either the roster has changed since (a trade,
# waiver add, or a season rollover) or something about the scrape is
# picking up the wrong column/page.
KNOWN_2026_08_18 = {
    "Jonathan Taylor": 0,
    "Saquon Barkley": 1,
    "Brock Bowers": 1,
}

OUT = os.path.join(os.path.dirname(__file__), "samples")
os.makedirs(OUT, exist_ok=True)


def main():
    try:
        auth = CBSAuth()
    except CBSAuthError as e:
        print(f"FAILED to build CBSAuth: {e}")
        sys.exit(1)

    print(f"[1] Fetching CONTRACT column for {LEAGUE_ID}/teams/{TEAM_ID} ...")
    try:
        raw = fetch_contract_years(auth, LEAGUE_ID, TEAM_ID, sport="football")
    except Exception as e:
        print(f"  FAILED: {e}")
        sys.exit(1)

    print(f"  Parsed {len(raw)} contract values:")
    for name, value in sorted(raw.items()):
        print(f"    {name!r}: {value}")

    with open(os.path.join(OUT, "contract_years_raw.json"), "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)
    print(f"  wrote samples/contract_years_raw.json")

    print(f"\n[2] Cross-checking against the known 2026-08-18 snapshot ...")
    any_mismatch = False
    for name, expected in KNOWN_2026_08_18.items():
        actual = raw.get(name)
        if actual is None:
            print(f"    {name}: NOT FOUND in today's scrape (roster changed, or name text differs -- check spelling/case in samples/contract_years_raw.json)")
            any_mismatch = True
        elif actual != expected:
            print(f"    {name}: MISMATCH -- expected {expected} (from 2026-08-18), got {actual} today. "
                  f"Could be legitimate (a season elapsed, or he was traded) -- just don't assume the scraper is broken without checking.")
            any_mismatch = True
        else:
            print(f"    {name}: OK ({actual}, matches 2026-08-18)")
    if not any_mismatch:
        print("  All cross-checks passed.")

    print(f"\n[3] Converting to acquired_season (current_season={CURRENT_SEASON}) ...")
    acquired = contract_years_to_acquired_seasons(raw, CURRENT_SEASON)
    for name, season in sorted(acquired.items()):
        status = contract_status(name, season, CURRENT_SEASON)
        flag = "EXPIRED -- not keeper-eligible" if status.is_expired else \
               (f"last valid season ({status.expires_after_season})" if status.expires_after_season == CURRENT_SEASON
                else f"valid through {status.expires_after_season}")
        print(f"    {name}: acquired {season} -> {flag}")

    print("\nDone. Share the printed output above (and samples/contract_years_raw.json "
          "if anything looks off) back with Claude.")


if __name__ == "__main__":
    main()
