"""
contract_diag.py -- one-off diagnostic to capture the RAW roster-page HTML
around the CONTRACT column, since contract_probe.py's column-detection
guess (2026-08-23) was wrong (landed on an ownership-% column instead).
Same pattern as dump_samples.py -- run locally, share samples/ back.

Run:  python contract_diag.py
"""

import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

from bs4 import BeautifulSoup
from cbs.auth import CBSAuth

LEAGUE_ID = "ecfc"
TEAM_ID = "5"
SPORT = "football"

OUT = os.path.join(os.path.dirname(__file__), "samples")
os.makedirs(OUT, exist_ok=True)


def main():
    auth = CBSAuth()
    r = auth.fetch_league_page(LEAGUE_ID, SPORT, f"/teams/{TEAM_ID}")

    # 1. Save the full raw HTML for direct inspection.
    raw_path = os.path.join(OUT, "ecfc_team5_raw.html")
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(r.text)
    print(f"Wrote {raw_path} ({len(r.text)} chars)")

    soup = BeautifulSoup(r.text, "html.parser")
    rows = soup.select("tr.playerRow")
    print(f"Found {len(rows)} tr.playerRow elements")

    if not rows:
        print("No player rows found -- nothing more to inspect.")
        return

    # 2. How many DISTINCT tables contain playerRow rows? (active vs
    # reserve sections might be separate tables with separate headers.)
    tables = []
    seen_ids = set()
    for row in rows:
        t = row.find_parent("table")
        if t is not None and id(t) not in seen_ids:
            seen_ids.add(id(t))
            tables.append(t)
    print(f"Player rows belong to {len(tables)} distinct <table> element(s)")

    # 3. For each such table, print every header-ish row's cell texts
    # with index, and search anywhere in the table (not just the first
    # row) for a cell whose text contains "contract".
    for ti, table in enumerate(tables):
        print(f"\n--- Table {ti} ---")
        all_trs = table.find_all("tr")
        print(f"  {len(all_trs)} <tr> total in this table")
        for ri, tr in enumerate(all_trs[:3]):
            cells = tr.find_all(["th", "td"])
            texts = [c.get_text(strip=True) for c in cells]
            print(f"  tr[{ri}] ({len(cells)} cells): {texts}")

        # Search the whole table for the literal word "contract" anywhere
        # in a cell's text, case-insensitive -- wherever it is.
        found_any = False
        for ri, tr in enumerate(all_trs):
            cells = tr.find_all(["th", "td"])
            for ci, c in enumerate(cells):
                txt = c.get_text(strip=True)
                if "contract" in txt.lower():
                    print(f"  MATCH: tr[{ri}] td[{ci}] = {txt!r}")
                    found_any = True
        if not found_any:
            print('  No cell anywhere in this table contains the text "contract"')

    # 4. Print one full real player row (all cells, with index) for the
    # first non-empty playerRow, so the actual column layout is visible.
    for row in rows:
        if "empty" in row.get("class", []):
            continue
        cells = row.find_all("td")
        link = row.select_one("a.playerLink") or row.select_one("a[aria-label]")
        name = (link.get("aria-label") or link.text).strip() if link else "?"
        print(f"\n--- Full row for {name!r} ({len(cells)} <td> cells) ---")
        for i, c in enumerate(cells):
            print(f"  td[{i}]: {c.get_text(strip=True)!r}")
        break

    print("\nDone. Share this printed output AND samples/ecfc_team5_raw.html back with Claude.")


if __name__ == "__main__":
    main()
