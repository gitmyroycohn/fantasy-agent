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
| **Hitting matchup expert** — L/R splits, park factors, hot streak, weather | `mcp_server.py` → `hitting_matchups` tool |
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
probable starters, L/R splits, recent form — no auth needed),
**Open-Meteo** (game-time weather forecast — no API key needed),
**FanGraphs park factors** (5-year baked-in table, all 30 parks).

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
- **Option A (recommended):** `CBS_USERNAME` + `CBS_PASSWORD` — auto-login on startup,
  transparent cookie refresh on expiry. No manual steps ever needed.
- **Option B (manual):** `CBS_COOKIE` — browser-captured session cookie.
  Log into cbssports.com → DevTools → Network tab → any request → copy the full
  `Cookie:` header value. Lasts ~30–90 days; re-capture on auth errors.
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

Secrets required in the repo: `CBS_COOKIE` (or `CBS_USERNAME`+`CBS_PASSWORD`), `FANTASYPROS_API_KEY`.

---

## MCP server (on-demand, from Claude Desktop)

`mcp_server.py` exposes the agent as 8 tools via FastMCP:
`evaluate_trade_tool`, `get_roster`, `get_team_roster`, `list_league_teams`,
`waiver_recommendations`, `roster_value_signals`, `daily_decisions`,
`hitting_matchups`.

`get_team_roster(league_id, team_name)` looks up **any** team in the league
by name (not just your own) — useful for trade research, e.g. "what does
Men of Steal have right now?" `list_league_teams(league_id)` lists all team
names/IDs if you don't know the exact name to pass in.

**Important: this uses stdio transport.** It only works inside the **Claude
Desktop app**, launched as a local subprocess — it is NOT reachable from a
claude.ai web Project, and the host PC must be on and Claude Desktop running.

### Local (stdio) -- Claude Desktop only, PC must be on

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

**Note (June 2026):** this Claude Desktop build's Connectors panel only
supports remote connectors added by URL — it does not surface local
stdio servers from `claude_desktop_config.json` at all. The stdio setup
above may not actually work depending on your Desktop version; the cloud
deployment below is the confirmed-working path.

### Cloud (HTTP) -- reachable from any device, no PC required

`mcp_server.py` can also run as a standalone web service (`MCP_TRANSPORT=http`),
gated by a bearer token (`MCP_AUTH_TOKEN`) since the URL is publicly
reachable and can query your CBS fantasy data.

Deploy to Render (free tier — cold starts after ~15 min idle, 30–60s to
wake on the next request). A GitHub Actions keep-alive pings `/health`
every 10 minutes and polls until the server confirms warm, preventing
cold starts during active hours. If a cold start does occur, all tool
responses include a friendly notice explaining the delay.
1. Render dashboard → **New** → **Blueprint** → point at this repo (uses `render.yaml`).
2. When prompted, set secrets: `CBS_COOKIE`, `FANTASYPROS_API_KEY`, `MCP_AUTH_TOKEN` (make up any long random string for the token).
3. Once deployed, your server URL is `https://<service-name>.onrender.com/mcp`.
4. In Claude, go to **Settings → Connectors → +**, add it as a custom connector with that URL, and supply the bearer token when prompted for auth.

Local testing before deploying:
```bash
MCP_TRANSPORT=http MCP_AUTH_TOKEN=test-secret python mcp_server.py
# then: curl -H "Authorization: Bearer test-secret" http://127.0.0.1:8000/mcp
```

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
│   ├── schedule.py            # 3-week lookahead, 2-starter detection, today's matchups
│   ├── injuries.py            # IL transactions + active IL roster
│   ├── splits.py              # L/R batter splits + recent form (last 14 days)
│   ├── parks.py               # FanGraphs 5-year park factors, all 30 parks
│   └── weather.py             # Open-Meteo game-time weather (wind/temp/precip)
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
└── .github/workflows/
    ├── daily.yml              # 8am ET daily run, commits logs/ back to repo
    └── keep-alive.yml         # pings /health every 10 min to prevent Render cold starts
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
