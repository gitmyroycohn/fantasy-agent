# CBS Fantasy Baseball Agent

Read-only agent that fetches your CBS Sports fantasy rosters, analyzes matchups,
and recommends streamers, waiver adds, drops, and trades. Runs automatically
every day via GitHub Actions and can also be invoked on demand from Claude
Desktop. **`DRY_RUN = True` always — the agent never submits anything to CBS.**

## Leagues
- **Pins & Pills** (`hemp`) — H2H 9-category (R/HR/RBI/SB/AVG/OPS/TB/XBH + W/SV/K/ERA/WHIP/QS/HLD/INNdGS/K_BB)
- **The Casey Stengel Amazin' Experience** (`baberuthdivingclubformen`) — NL-only Rotisserie, no bench

---

## What it does

| Capability | Module |
|---|---|
| Matchup / standings analysis | `sports/baseball/categories.py` |
| Waiver wire recommendations (cat-targeted) | `agent/decisions.py` |
| SP streaming, incl. 3-week schedule lookahead + back-to-back 2-starter flag | `sports/baseball/streaming.py`, `mlb/schedule.py` |
| Drop candidates (prospect-stash aware) | `sports/baseball/drops.py` |
| Daily lineup advice | `sports/baseball/lineup_optimizer.py` |
| Closer depth chart + news | `closermonkey/client.py` |
| Injury report — IL placements/activations, your roster flagged | `mlb/injuries.py` |
| Buy-low / sell-high trade signals | `agent/tradevalue.py` |
| League-wide surplus/deficit trade board (who to target) | `agent/surplusmap.py` |
| Specific trade evaluator (give vs. receive → verdict) | `agent/trade_eval.py` |
| Move-volume churn guard (caps recs when ≥4 moves) | `agent/main.py` |
| Decision memory (avoids repeating the same rec daily) | `agent/history.py` |

Enrichment layers feeding the above: **FantasyPros** (ROS projections, `fp_*` keys),
**Baseball Savant** (xStats, `sv_*` keys), **MLB Stats API** (schedule, IL,
probable starters — no auth needed).

---

## Setup (any machine with Python 3.11+)

### 1. Get the code
```
git clone https://github.com/gitmyroycohn/fantasy-agent.git
cd fantasy-agent
```

### 2. Install dependencies
```
pip install -r requirements.txt
pip install -r requirements-mcp.txt   # only if using the MCP server
```

### 3. Set credentials
```
cp .env.example .env
```
Edit `.env`:
- `CBS_COOKIE` — browser-captured session cookie (CBS login is JS-based, no API key).
  Log into cbssports.com → DevTools → Network tab → any request to cbssports.com →
  copy the full `Cookie:` header value. Lasts ~30–90 days; re-capture on auth errors.
- `FANTASYPROS_API_KEY` — from your FantasyPros account.

### 4. Leagues
`config/leagues.yaml` already has both leagues, team IDs, scoring categories, and
`prospect_stash` (players to never auto-flag as drop candidates).

---

## Running

```bash
# Both leagues
python -m agent.main --run daily --dry-run

# One league
python -m agent.main --run daily --dry-run --league hemp

# Verbose (DEBUG logs)
python -m agent.main --run daily --dry-run --verbose
```

Output is written to `logs/latest_output.md` and decision memory to `logs/history.json`.

---

## GitHub Actions (automatic daily run)

`.github/workflows/daily.yml` runs both leagues and commits `logs/` back to the
repo. Configured for `0 12 * * *` (noon UTC / 8am ET), but **GitHub's scheduler
runs low-traffic public repos late** — in practice it has been firing ~3 hours
after the configured time (~11am ET), not at 8am. This is expected GitHub
behavior, not a bug in this repo; the cron expression is a "no earlier than,"
not a guarantee.

Each run's header timestamp is generated with proper `zoneinfo` ET conversion
(fixed June 19, 2026 — it previously used naive `datetime.now()`, which returns
UTC on the runner, and mislabeled it "ET," making every timestamp read 4 hours
ahead of the real time).

Secrets required in the repo: `CBS_COOKIE`, `FANTASYPROS_API_KEY`.

---

## MCP server (on-demand, from Claude Desktop)

`mcp_server.py` exposes the agent as 7 tools via FastMCP:
`evaluate_trade_tool`, `get_roster`, `get_team_roster`, `list_league_teams`,
`waiver_recommendations`, `roster_value_signals`, `daily_decisions`.

`get_team_roster(league_id, team_name)` looks up **any** team in the league
by name (not just your own) — useful for trade research, e.g. "what does
Men of Steal have right now?" `list_league_teams(league_id)` lists all team
names/IDs if you don't know the exact name to pass in.

**Important: this uses stdio transport.** It only works inside the **Claude
Desktop app**, launched as a local subprocess — it is NOT reachable from a
claude.ai web Project, and the host PC must be on and Claude Desktop running.

Setup — create `%APPDATA%\Claude\claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "fantasy-baseball": {
      "command": "python",
      "args": ["C:\\Users\\guido\\fantasy-agent\\mcp_server.py"],
      "env": { "PYTHONPATH": "C:\\Users\\guido\\fantasy-agent" }
    }
  }
}
```
Restart Claude Desktop after editing. Tools then appear in any Desktop chat.

---

## Project structure
```
fantasy-agent/
├── mcp_server.py            # FastMCP server (Claude Desktop only)
├── run_agent.py
├── requirements.txt
├── requirements-mcp.txt
│
├── agent/
│   ├── main.py               # CLI entry point + output printer
│   ├── decisions.py          # recommendation engine (H2H + roto)
│   ├── tradevalue.py         # buy-low / sell-high signals
│   ├── trade_eval.py         # specific trade evaluator
│   ├── surplusmap.py         # league-wide surplus/deficit map
│   ├── history.py            # decision memory
│   └── summary.py            # TL;DR header + formatter
│
├── cbs/
│   ├── auth.py · roster.py · waivers.py · stats.py · lineup.py
│   └── standings.py          # all-teams category stats (for trade board)
│
├── mlb/
│   ├── stats.py               # player stat enrichment (free MLB Stats API)
│   ├── schedule.py            # 3-week lookahead, 2-starter detection
│   └── injuries.py            # IL transactions + active IL roster
│
├── sports/baseball/
│   ├── categories.py · drops.py · streaming.py · lineup_optimizer.py
│
├── fantasypros/client.py     # ROS projections
├── savant/client.py          # Baseball Savant xStats
├── closermonkey/client.py    # depth chart + news (scrape, no API)
│
├── config/
│   ├── settings.py            # DRY_RUN flag, thresholds
│   └── leagues.yaml           # leagues, team IDs, scoring, prospect_stash
│
├── data/models.py             # Player, RosterSlot, Team, Matchup
├── logs/
│   ├── latest_output.md       # committed by GitHub Actions each run
│   └── history.json           # decision memory
│
└── .github/workflows/daily.yml
```

---

## Known limits

- **CBS player stats unavailable at this subscription tier** — stats come from
  the free MLB Stats API instead (~90% match rate; recent callups may be missed).
- **Ownership %** — CBS returns 0 for all players; not used as a filter.
- **Write paths disabled** — `set_lineup()` / `claim_player()` are stubs.
  `DRY_RUN` must be explicitly flipped and the POST requests implemented before
  any submission path could ever be enabled — not currently planned.
- **Cron timing** — see GitHub Actions section above; expect ~11am ET, not 8am.
- **MCP server reach** — Claude Desktop only, PC must be on. See MCP section above.
