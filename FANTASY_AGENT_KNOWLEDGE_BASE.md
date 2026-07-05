# Fantasy Baseball Agent — CBS Sports
## Automated agent for managing fantasy baseball teams on CBS Sports Fantasy.

---

## Overview

This agent monitors, analyzes, and recommends actions for two CBS Sports fantasy baseball leagues. It is **read-only** (`DRY_RUN = True`) — it fetches data and makes recommendations but does not submit anything to CBS.

**Run it:** GitHub Actions → `gitmyroycohn/fantasy-agent` → Actions → Fantasy Agent → Run workflow. Runs automatically daily at 8am ET.

---

## Leagues

### Pins and Pills (`hemp`)
- Format: H2H 9-category
- CBS league ID: `hemp`
- My team ID: 7
- Scoring: weekly head-to-head, 9 categories (H, HR, OPS, R, RBI, SB, ERA, K, W, S, WHIP, INNdGS, QS)

### The Casey Stengel Amazin' Experience (`baberuthdivingclubformen`)
- Format: NL-only Rotisserie
- CBS league ID: `baberuthdivingclubformen`
- My team ID: 2
- Scoring: rotisserie, NL players only — AL players are ineligible

---

## Authentication

CBS Sports uses JavaScript-based login. The agent authenticates via a **browser-captured session cookie** stored in the `CBS_COOKIE` environment variable (GitHub Actions secret, or `.env` file locally).

- Cookie lasts ~30–90 days
- When expired: log into cbssports.com in Chrome, open DevTools → Network, navigate to a fantasy league page, copy the full `Cookie:` request header value, update the GitHub secret
- Per-league API tokens are extracted fresh each run from the league's JavaScript

---

## Data Sources

### CBS Sports API (`api.cbssports.com/fantasy/`)
- **Roster**: `players/list` + `transactions/roster` — returns all players with slot assignments
- **Free agents**: `players/list` filtered to unowned players (~8100–8200 players)
- **Live scoring**: `league/scoring/live` — returns matchup data, category standings, roto ranks
- **Player stats**: NOT available at this subscription level (returns `exceptions`)

### MLB Stats API (`statsapi.mlb.com/api/v1/stats`) — free, no auth
- Season stats for all MLB players (pitching + hitting)
- Used to enrich roster and waiver players with real stats
- ~90% match rate; minor leaguers/recent callups may be missed
- Cached per process run (lru_cache)

---

## What the Agent Produces

### H2H (Pins and Pills)
- **Matchup summary**: current week score (W-L-T by category)
- **Priority categories**: losing cats sorted by gap (easiest to flip first)
- **Streaming SP recommendations**: available SPs scoring ERA ≤ 4.00, K/9 ≥ 7.5, IP ≥ 10, ranked by composite score
- **Waiver adds**: available players sorted by relevant stats for losing categories

### Rotisserie (Casey Stengel)
- **Roto standings**: total roto points, cats winning/losing
- **Weakest categories**: bottom-ranked cats with most room to gain
- **Waiver adds**: NL-only players sorted by relevant stats (AL teams filtered out, including ATH = Athletics)
- **NL eligibility warnings**: flags any AL-team player on the roster

---

## Configuration

### `config/leagues.yaml`
League and team IDs. Add football leagues here when ready.

### `config/settings.py`
Key thresholds:
- `DRY_RUN = True` — never flip to False until write paths are validated
- `MAX_ERA_STREAMER = 4.00` — SP streaming ERA ceiling
- `MIN_K9_STREAMER = 7.5` — SP streaming K/9 floor
- `MIN_SP_OWNERSHIP_DROP = 50.0` — ownership % ceiling for streamers (CBS returns 0 for all, so this passes everything)

---

## Project Structure

```
fantasy-agent/
├── agent/
│   ├── main.py         # CLI entry point, --run daily/weekly/waivers/lineup
│   └── decisions.py    # recommendation engine (H2H + roto paths)
├── cbs/
│   ├── auth.py         # CBS cookie auth + per-league JS token extraction
│   ├── roster.py       # roster fetch (JSON API primary, HTML fallback)
│   ├── waivers.py      # free agent list + fetch_waiver_wire alias
│   ├── stats.py        # live scoring via league/scoring/live
│   └── lineup.py       # stub — write path not yet implemented
├── mlb/
│   └── stats.py        # MLB Stats API integration, enrich_roster/enrich_players
├── sports/baseball/
│   ├── categories.py   # analyze_matchup, priority_categories, NL eligibility
│   └── streaming.py    # rank_streaming_sps — scores available SPs
├── data/
│   └── models.py       # Player, RosterSlot, Team, Matchup, CategoryStanding, WaiverPlayer
├── config/
│   ├── settings.py     # DRY_RUN, thresholds
│   └── leagues.yaml    # league + team IDs
├── .github/workflows/
│   └── daily.yml       # GitHub Actions — manual trigger + 8am ET daily cron
├── .env                # CBS_COOKIE (gitignored, local only)
├── .env.example        # template
└── requirements.txt    # requests, beautifulsoup4, lxml, python-dotenv, pyyaml
```

---

## Running

```bash
# Both leagues, dry run
python -m agent.main --run daily --dry-run

# One league
python -m agent.main --run daily --dry-run --league hemp

# Verbose logging
python -m agent.main --run daily --dry-run --verbose
```

Via GitHub: Actions tab → Fantasy Agent → Run workflow.

---

## Known Limits & Pending Work

- **Write paths disabled**: `set_lineup()` and `claim_player()` are stubs. To enable, capture the POST requests from Chrome DevTools during a real lineup move or waiver claim, then implement and test before setting `DRY_RUN = False`.
- **Ownership % always 0**: CBS API returns 0 for all players. SP streaming ownership filter passes everything.
- **Football leagues**: not yet added to `leagues.yaml`. Need league IDs and team IDs for 3 CBS football leagues.
- **H2H category detail**: early-week matchups show 0-0-12 (all tied) until stats accumulate. This is correct behavior.
- **Cookie expiry**: ~30–90 days. Refresh by copying new cookie from Chrome DevTools and updating the GitHub Actions secret `CBS_COOKIE`.

---

## Phase B Changes (feature/bugs-and-enhancements — Jul 2026)

### BUG 1 — RP spot-starter detection (sports/baseball/lineup_optimizer.py)

**Problem:** `is_probable_starter` was always `None` for RP-designated players.
`format_lineup_advice` checked `"SP" in positions and is_probable_starter`, so any
reliever called up as a spot starter was invisible to the lineup advisor.

**Fix:** Both SP and RP blocks now check `norm_name in probable_starters` (the set
returned by `mlb.schedule.probable_starters_today()`). An RP match sets
`is_probable_starter=True`. `format_lineup_advice` now uses
`a.is_probable_starter is True` with no position filter, so spot starters surface
regardless of CBS eligibility tag.

---

### BUG 2 — hitting_matchups KeyError on doubleheader/holiday slates (mcp_server.py)

**Problem:** `hitting_matchups` crashed with `KeyError: 'home_team'` or
`'away_team'` when the matchup API returned malformed rows (e.g. doubleheaders,
rescheduled games, or short holiday slates where some fields are absent).

**Fix:** The `team_to_matchup` loop now wraps each row in a `try/except`, uses
`m.get("home_team") or ""` / `m.get("away_team") or ""`, and calls `m.get()`
for all field access. Malformed rows are logged at WARNING and skipped via
`continue`. `park_factor` / `park_factor_hr` default to 100 if absent.

---

### BUG 3 — Drop evaluator mislabels hot bats (sports/baseball/drops.py)

**Problem:** A player slashing .487/.1.472 OPS over 14 trailing days was flagged
as a "borderline drop" candidate because his season counting stats (early slump)
tripped the fail-threshold check.

**Fix:** Added a recency gate at the top of `_evaluate_batter`. If
`recent_games >= 10` AND `recent_ops >= 0.800` (thresholds:
`_HOT_MIN_GAMES = 10`, `_HOT_OPS_FLOOR = 0.800`), the function returns `None`
immediately. `recent_games` / `recent_ops` are populated by
`mlb.splits.fetch_recent_form(14)` in the waiver enrichment path; if absent the
gate is silently skipped.

---

### BUG 4 + ENH 2 — OF eligibility / multi-position normalization

#### data/models.py
Added module-level constant:
```python
_CBS_OF_MAP = {"LF": "OF", "CF": "OF", "RF": "OF"}
```
Added `eligible_positions` property to `Player` dataclass: iterates
`self.positions`, maps each through `_CBS_OF_MAP`, and deduplicates. Consumers
that need slot-matching (CBS uses LF/CF/RF tags, slots say OF) should use
`eligible_positions` instead of `positions`.

#### cbs/waivers.py
Added `_CBS_OF_NORM` dict and `_norm_pos(p)` helper (same LF/CF/RF -> OF mapping).
The `_available_from_api` position filter now normalizes both the query position
and each player's CBS position tags before comparing, so
`get_available_players(position="OF")` returns LF/CF/RF-tagged players correctly.

---

### ENH 3 — Must-start floor for elite batters (agent/decisions.py)

Added two module-level constants:
- `_MUST_START_OPS = 0.850`
- `_LINEUP_PITCHER_POS = {"SP", "RP", "P"}`

After `optimize_daily_lineup` returns, `_add_lineup_advice` now iterates the
advice list and overrides any `advice == "bench"` entry for a non-pitcher whose
season OPS >= .850 to `advice = "ok"` with a "Must-start floor" prefix on the
reason string. This prevents the agent from ever recommending the user sit an
elite bat due to schedule ambiguity or park-factor nudges.

OPS is looked up from `rs.player.stats` via `_norm_name` key for fuzzy matching.

---

### ENH 5 — FantasyPros API key wiring (.env.example)

Added placeholder to `.env.example`:
```
FANTASYPROS_API_KEY=your_fantasypros_api_key_here
```
The key is already consumed by `fantasypros/client.py` via
`_fp_client = FantasyProsClient(FANTASYPROS_API_KEY) if FANTASYPROS_API_KEY else None`
(graceful degradation when absent) and is already present as a secret in
`.github/workflows/daily.yml`. This commit just adds the documentation entry so
local developers know to set it.
