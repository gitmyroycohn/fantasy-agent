# Fantasy Baseball Agent — CBS Sports
## Automated agent for managing fantasy baseball teams on CBS Sports Fantasy.

---

## Overview

This agent monitors, analyzes, and recommends actions for two CBS Sports fantasy baseball leagues. It is **read-only** (`DRY_RUN = True`) — it fetches data and makes recommendations but does not submit anything to CBS.

**Run it:** GitHub Actions → `gitmyroycohn/fantasy-agent` → Actions → Fantasy Agent → Run workflow. Configured 8am ET (`0 12 * * *`) but fires ~11am ET on this low-traffic public repo — expected GitHub scheduler behavior, not a malfunction. Don't flag an ~11am run timestamp as evidence something's broken.

---

## Leagues

### Pins and Pills (`hemp`)
- Format: H2H 12-category (corrected in Phase C — see BUG 6 below; the header
  previously said "9-category" and listed 13 categories including a
  nonexistent `QS`, which was itself wrong)
- CBS league ID: `hemp`
- My team ID: 7
- Scoring periods: NOT uniform 7-day Monday-Sunday weeks. Season starts
  3/25/26 (a Wednesday); Period 1 is 12 days; Period 16 (7/13-7/26) is 14
  days for the All-Star break. The real period calendar lives in
  `config/leagues.yaml`'s `periods:` table (see BUG 5 below); CBS's own
  `league/scoring/live` `period` field is always the runtime source of truth,
  with the table used for future-period lookahead.
- Scoring categories (exactly 12 — cross-checked at runtime against CBS's
  `league/scoring/live` categories, which logs a WARNING on any drift):
  - Hitting (6): `H`, `HR`, `OPS`, `R`, `RBI`, `SB`
  - Pitching (6): `ERA`, `INNdGS`, `K`, `S`, `W`, `WHIP`
  - NOT scored (despite being listed pre-Phase-C): `AVG`, `TB`, `XBH`, `QS`,
    `HLD`, `K_BB`
- Position eligibility: all players are DH-eligible in this league's settings;
  a player qualifies at a position with 162 games last season OR 1 game this
  season (see `config/leagues.yaml`'s `eligibility:` block).

### The Casey Stengel Amazin' Experience (`baberuthdivingclubformen`)
- Format: NL-only Rotisserie
- CBS league ID: `baberuthdivingclubformen`
- My team ID: 2
- Scoring: rotisserie, NL players only — AL players are ineligible
- Scoring categories (10, unchanged/already correct):
  - Hitting (5): `R`, `HR`, `RBI`, `SB`, `AVG`
  - Pitching (5): `W`, `SV`, `K`, `ERA`, `WHIP`
- Position eligibility: primary position + 20 games last season or 20 games
  this season (not all-DH-eligible, unlike Pins and Pills).

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

---

## Phase C Changes (phase-c/period-fix-categories-eligibility-platoon — Jul 2026)

### BUG 5 (P0) — Scoring-period awareness was fabricated

**Problem:** `agent/decisions.py`'s `_current_week()` computed the period
arithmetically from a hardcoded `opening_day = date(today.year, 3, 28)`, which
was wrong on 81/166 days of the season (49%) — every Saturday and Sunday,
since the real season start (3/25/26) is a Wednesday, not a Monday, plus the
formula assumed a flat 7-day cadence when Period 1 is 12 days and Period 16
(the All-Star break) is 14 days.

**Fix:**
- `_current_week()` is deleted. `config/leagues.yaml` gained a `season_start`
  and a `periods:` table (`n`/`start`/`end` per period, sourced from the
  actual 2026 CBS schedule). `config/periods.py` is a new loader
  (`period_for_date`, `period_offset`, `resolve_period`) that resolves a date
  to its real period bounds.
- `agent/decisions.py`'s three `analyze_matchup(raw_stats, week=...)` call
  sites now pass `week=_resolve_week(raw_stats)`, which reads CBS's own
  authoritative `period` field from `cbs.stats.fetch_matchup_stats` and only
  falls back to the local table if CBS didn't return one. `config.periods`
  logs a WARNING if the two disagree, but CBS always wins.
- `mlb/schedule.py`'s `week_bounds()` / `schedule_weeks()` now resolve real
  period boundaries from `config/periods.py` instead of `Monday + timedelta`.
  `week_offset=N` now means "N real periods ahead" (`period_offset`), not
  `N*7 days` — during the 14-day Period 16, the old code's `week_offset=1`
  landed on 7/20-7/26, still inside Period 16.
  `schedule_weeks()` also returns `period`, `period_days`, and a new
  `multi_starters` (3+ start SPs) alongside the existing `two_starters` (2+),
  so a 14-day period's 3-4-start arms are correctly surfaced instead of only
  ever seeing a 7-day slice of the period.
- `mlb/schedule.py::_today_et()` (already existed) is now also used by
  `mlb/weather.py`, replacing a UTC `date.today()` call.

### BUG 6 (P0) — Wrong scoring categories in config/leagues.yaml

**Problem:** `pins_and_pills` declared 17 categories including `AVG`, `TB`,
`XBH`, `QS`, `HLD`, `K_BB`, none of which the league actually scores, while
`H` — a real scored category — was missing entirely.

**Fix:** Corrected to the real 12: hitting `[H, HR, OPS, R, RBI, SB]` /
pitching `[ERA, INNdGS, K, S, W, WHIP]`. Added
`sports/baseball/categories.validate_scoring_config()`, called from both the
H2H and roto decision paths right after `fetch_matchup_stats`, which
cross-checks the configured category list against the categories CBS's
`league/scoring/live` actually returns and logs a WARNING on any mismatch —
so this class of drift can't silently recur. `casey_stengel`'s 10 categories
were already correct.

### ENH 2 (finish) — Multi-position eligibility

**Problem:** Phase B only normalized LF/CF/RF → OF. `Player.eligible_positions`
was still derived solely from the player's current roster slot tag, so a
2B/SS-eligible player rostered at 2B today never showed as SS-eligible.

**Fix:**
- `data/models.py`'s `Player` gained `eligible_positions_override`;
  `eligible_positions` uses it when set, else falls back to the old
  slot-derived behavior (free agents unaffected).
- New `cbs/players.py::fetch_position_eligibility_index()` builds a
  `{player_id: [positions]}` index from `players/list` (the same validated
  endpoint `cbs/waivers.py` already uses), and `cbs/roster.py` merges it into
  every roster player's `eligible_positions_override`. **Not validated
  against a live CBS response** (no `CBS_COOKIE` available while writing
  this) — mirrors the already-validated `players/list` field usage in
  `cbs/waivers.py`; extend `cbs_probe.py` to confirm on the next live run.
- `cbs/players.py::apply_league_eligibility_rules()` applies
  `config/leagues.yaml`'s new per-league `eligibility:` block — currently just
  the pins_and_pills "all players DH-eligible" static rule. The games-played
  thresholds documented there (162 last season OR 1 this season for
  pins_and_pills; primary position + 20 games for casey_stengel) are CBS's
  own configured settings, so `players/list`'s per-league eligibility is
  already threshold-correct; the block exists as living documentation/
  cross-check reference, not a games-log recomputation.
- `sports/baseball/lineup_optimizer.py::find_legal_swaps()` is new: proposes
  bench→active swaps using each player's full eligible-position list, and
  only into slots they're actually eligible for (OF-normalized; utility
  slots accept any batter). Wired into `agent/decisions.py::_add_lineup_advice`
  and printed in `agent/main.py` under "Legal Lineup Swaps".

### ENH 3 (finish) — Platoon weighting

**Problem:** `mlb/splits.py` already fetched `split_vs_l_ops` / `split_vs_r_ops`,
but nothing consumed them — `sports/baseball/lineup_optimizer.py` had no
handedness logic at all.

**Fix:** `agent/decisions.py::_add_lineup_advice` now enriches the roster with
splits (`mlb.splits.enrich_with_splits`) and builds `opp_hand_by_team` from
`mlb.schedule.todays_matchups()`'s probable-starter hand data.
`optimize_daily_lineup()` takes `opp_hand_by_team` and down-ranks a batter to
`advice="bench"` when their OPS split against today's hand is both
meaningfully worse than their split against the other hand
(`config.settings.PLATOON_OPS_GAP = 0.100`) and weak in absolute terms
(`PLATOON_FLOOR_OPS = 0.700`), surfacing the reason. The existing must-start
floor (season OPS >= .850) is now a standalone, testable
`apply_must_start_floor()` in `lineup_optimizer.py` and continues to override
platoon-driven benches for elite bats — but deliberately does NOT override
ENH 4/7's `out_of_lineup` status, since that's a confirmed real-world absence
rather than a heuristic guess.

### ENH 6 — Emit the full category set

**Problem:** Output only ever printed losing categories, obscuring what the
league actually scores.

**Fix:** `matchup_summary` / `roto_summary` actions in `agent/decisions.py`
now include a `category_standings` list covering every category from
`Matchup.category_standings` (W/L/T + values for H2H, rank/rotopts/dif for
roto). `agent/main.py` prints an "All categories" line before the
losing-categories summary. Sourced from the same `league/scoring/live` fetch
already used for the matchup summary — no extra API call.

### ENH 4 / ENH 7 — Posted-lineup awareness

**Problem:** The agent assumed any rostered hitter on a team with a game
today was actually starting; real lineups regularly platoon or rest players.

**Fix:** New `mlb/lineups.py::fetch_posted_lineups()` uses the MLB Stats API
schedule endpoint's `lineups` hydration (free, official, no scraping — the
same host already used throughout this codebase) to get today's official
starting lineups and batting order once posted (~1-2hr before first pitch).
`lineup_status_for()` resolves each hitter to `confirmed` / `not_in_lineup`
(lineup posted, player absent) / `unknown` (not posted yet). Wired into
`optimize_daily_lineup()`: a `not_in_lineup` hitter gets its own
`advice="out_of_lineup"` (distinct from the heuristic `"bench"`, so the
must-start floor never overrides a confirmed real-world absence), and every
`LineupAdvice` exposes a `.lineup_label` of `confirmed` / `expected` /
`not-in-lineup` for display. **Not validated against a live response** in
this environment (no network egress available while writing this) — the
`lineups` hydration shape is documented community knowledge for this API,
consistent with the `probablePitcher`/`team` hydrations already relied on in
`mlb/schedule.py`. Considered and declined: the optional RotoWire
early-projection layer (kept out of scope this pass — MLB's own feed already
covers the "confirmed" case cleanly, and RotoWire scraping adds ToS/rate-limit
surface for a "projected" layer that isn't strictly required by this phase's
done-criteria) and a third-party "Big Balls Sports Data" API suggested during
this work (its docs/OpenAPI spec have no lineup, starter, or probable-pitcher
endpoint at all — it's a season batting/pitching stats + gamelog API, refreshed
roughly a day after games finish, not a same-day lineup feed).

### Validation

No live CBS or MLB Stats API access was available while writing Phase C (no
`CBS_COOKIE`, no network egress to `statsapi.mlb.com`/`api.cbssports.com` from
the environment this was built in). Validated instead with a `tests/` pytest
suite (`tests/test_periods.py`, `tests/test_schedule.py`,
`tests/test_categories.py`, `tests/test_eligibility.py`,
`tests/test_lineup_optimizer.py`, `tests/test_lineups.py`) covering every
stated done-criterion against synthetic fixtures matching this codebase's
documented CBS/MLB JSON shapes, plus an end-to-end smoke test of
`agent.main._print_decisions` on synthetic action data. **Recommend running
the GitHub Actions workflow once on this branch** (where `CBS_COOKIE` is a
configured secret) to confirm the two live-data-dependent, unvalidated pieces
before merging: `cbs/players.py`'s `players/list`-based eligibility index, and
`mlb/lineups.py`'s `lineups` hydration shape.
