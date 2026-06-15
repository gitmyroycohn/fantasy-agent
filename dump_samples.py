"""
dump_samples.py — capture raw CBS data samples so roster/lineup/free-agent
parsing can be built against reality instead of guesses.

Run:  python dump_samples.py
Writes files into samples/ — share them back with Claude.
"""

import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

from cbs.auth import CBSAuth, CBSAPIError, league_base

LEAGUE = "hemp"
TEAM = "7"
SPORT = "baseball"

OUT = os.path.join(os.path.dirname(__file__), "samples")
os.makedirs(OUT, exist_ok=True)


def save(name: str, content: str):
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  wrote {path} ({len(content)} chars)")


def main():
    auth = CBSAuth()

    # 1. Full rosters JSON — to fix starter/bench detection
    print("[1] league/rosters JSON...")
    try:
        data = auth.api_get("league/rosters", LEAGUE, SPORT, team_id=TEAM)
        save("rosters.json", json.dumps(data, indent=2))
    except CBSAPIError as e:
        print(f"  FAILED: {e}")

    # 2. players/list API attempts — to fix free agents
    print("[2] players/list API variations...")
    for i, params in enumerate([
        {"free_agents": 1},
        {"status": "free_agents"},
        {"owned": 0},
        {},
    ]):
        try:
            data = auth.api_get("players/list", LEAGUE, SPORT, **params)
            body = json.dumps(data, indent=2)
            n = body.count('"fullname"') or body.count('"name"')
            save(f"players_list_{i}_{'_'.join(params.keys()) or 'noparams'}.json",
                 body[:30000])
            print(f"    params={params} -> ~{n} name fields")
        except CBSAPIError as e:
            print(f"    params={params} -> {e}")

    # 3. Candidate free-agent pages — record status, title, playerRow count
    print("[3] free-agent page hunt...")
    import re
    report = []
    for path in ["/players/add-drop", "/players", "/players/playersearch",
                 "/players/search", "/transactions/add-drop", "/players/all"]:
        url = f"{league_base(LEAGUE, SPORT)}{path}"
        try:
            r = auth.get_session().get(url, timeout=15, allow_redirects=True)
            title_m = re.search(r"<title>(.*?)</title>", r.text, re.S)
            title = (title_m.group(1).strip()[:80] if title_m else "?")
            rows = r.text.count("playerRow")
            line = (f"{path}: HTTP {r.status_code}, final={r.url}, "
                    f"title={title!r}, playerRow count={rows}")
            report.append(line)
            print(f"    {line}")
            if rows > 0 and "login" not in r.url.lower():
                save(f"fa_page{path.replace('/', '_')}.html", r.text[:60000])
        except Exception as e:
            report.append(f"{path}: ERROR {e}")
            print(f"    {path}: ERROR {e}")
    save("fa_page_report.txt", "\n".join(report))

    print("\nDone. Share the samples/ folder contents with Claude.")


if __name__ == "__main__":
    main()
