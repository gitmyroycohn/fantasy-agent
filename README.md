# CBS Fantasy Baseball Agent

Read-only agent that fetches your CBS Sports fantasy rosters, analyzes matchups, and recommends streamers and waiver adds. Does not submit anything to CBS (`DRY_RUN = True` by default).

## Leagues
- **Pins & Pills** (`hemp`) — H2H 9-category
- **The Casey Stengel Amazin' Experience** (`baberuthdivingclubformen`) — NL-only Rotisserie

---

## Setup (any machine with Python 3.11+)

### 1. Get the code
```
git clone <repo-url> fantasy-agent
cd fantasy-agent
```
Or just unzip/copy the folder.

### 2. Install dependencies
```
pip install -r requirements.txt
```

### 3. Set your CBS cookie
CBS login is a JavaScript flow — the agent uses a browser-captured session cookie instead of username/password.

**How to get your cookie (one-time, ~5 min):**
1. Log into [cbssports.com](https://cbssports.com) in Chrome
2. Open DevTools → **Network** tab (F12)
3. Navigate to one of your fantasy league pages
4. Click any request to `cbssports.com` → Headers → Request Headers
5. Copy the entire value of the `Cookie:` header (one long line)

Then:
```
cp .env.example .env
# edit .env and paste your cookie value after CBS_COOKIE=
```

The cookie lasts ~30–90 days. When it expires you'll see an auth error and need to repeat the steps above.

### 4. Verify leagues are configured
`config/leagues.yaml` already has the two leagues and team IDs. No changes needed unless you add football leagues.

---

## Running

```bash
# Both leagues
python -m agent.main --run daily --dry-run

# One league
python -m agent.main --run daily --dry-run --league hemp

# Verbose (shows DEBUG logs)
python -m agent.main --run daily --dry-run --verbose
```

Sample output:
```
=== Pins and Pills (baseball) ===
  Roster: 34 players  |  Stats enriched: 29/34
  Matchup: Week 12 vs Captain Jack: 7-4-1
  Priority categories (losing): H, W, S, SB, K
  Streaming SP: Andrew Alvarez (WAS) score=6.51  ERA 3.7, K/9 9.62
  Waiver adds: Andrew Abbott (CIN) [SP]  helps: W, K
```

---

## Run modes

| Flag | What it does |
|------|--------------|
| `--run daily` | Full analysis: matchup, streamers, waiver adds |
| `--run weekly` | Same as daily |
| `--run waivers` | Waiver analysis only |
| `--run lineup` | (future) Lineup optimization |
| `--dry-run` | Always set — no CBS submissions |
| `--league <id>` | Run one league; `all` = both |
| `--sport <name>` | `baseball`, `football`, or `all` |

---

## Project structure
```
fantasy-agent/
├── agent/
│   ├── main.py         # CLI entry point
│   └── decisions.py    # recommendation engine
├── cbs/
│   ├── auth.py         # CBS cookie auth + per-league token extraction
│   ├── roster.py       # roster fetch (JSON API + HTML fallback)
│   ├── waivers.py      # free agent list
│   ├── stats.py        # live scoring / matchup stats
│   └── lineup.py       # stub (write path not yet implemented)
├── mlb/
│   └── stats.py        # free MLB Stats API for player stat enrichment
├── sports/
│   └── baseball/
│       ├── categories.py   # H2H + roto category analysis
│       └── streaming.py    # SP streaming scorer
├── data/
│   └── models.py       # Player, RosterSlot, Team, Matchup dataclasses
├── config/
│   ├── settings.py     # DRY_RUN flag, thresholds
│   └── leagues.yaml    # league + team IDs
├── .env                # your CBS_COOKIE (gitignored)
├── .env.example        # template
└── requirements.txt
```

---

## Known limits

- **CBS player stats unavailable** — CBS API doesn't expose per-player stats at this subscription level. Stats come from the free [MLB Stats API](https://statsapi.mlb.com) instead (~90% match rate; minor leaguers/recent callups may be missed).
- **Ownership %** — CBS returns 0 for all players. Streaming filter uses `MIN_SP_OWNERSHIP_DROP = 50` which passes everything; tweak in `config/settings.py` if CBS starts returning real values.
- **Write paths disabled** — `set_lineup()` and `claim_player()` are stubs. To enable, capture the relevant POST requests from Chrome DevTools and implement them.
