"""
Posted-lineup awareness (ENH 4/7).

The agent previously assumed any rostered hitter on a team with a game today
was actually in that day's starting lineup. Real MLB lineups regularly bench
platoon bats, rest players, etc., so "team is playing" is not the same as
"this player is starting."

Primary source (free, official, no scraping, no auth): the MLB Stats API
schedule endpoint's `lineups` hydration -- the same statsapi.mlb.com host
already used everywhere else in this codebase (mlb/schedule.py, mlb/stats.py,
mlb/splits.py). Official lineups typically post 1-2 hours before first
pitch; before that, a game simply has no `lineups` data yet in this feed.

NOT validated against a live response in this sandbox (no network egress to
statsapi.mlb.com available while writing this). The `lineups` hydration shape
-- homePlayers/awayPlayers lists of players in batting-order sequence -- is
documented community knowledge for this API, consistent with the
probablePitcher/team hydrations already relied on in mlb/schedule.py. If a
live run shows a different shape, add a probe here the same way
cbs_probe.py probes CBS endpoints, and fix this module before trusting it
further in production.

Public API
----------
fetch_posted_lineups(d=None) -> {
    "players": {norm_name: {"team": cbs_abbr, "batting_order": int}},
    "posted_teams": {cbs_abbr, ...},   # teams whose lineup IS posted
}

lineup_status_for(name, team, posted) -> "confirmed" | "not_in_lineup" | "unknown"
    "confirmed":      player is in THEIR OWN team's posted lineup (name AND
                      team must match -- see bug-fix note on the function).
    "not_in_lineup":  that team's lineup IS posted and this player is NOT in it.
    "unknown":        that team's lineup has not posted yet -- caller should
                      treat this as "expected" (based on schedule), never as
                      a confirmed absence.

batting_order_for(name, team, posted) -> int | None
    Batting-order slot for a hitter confirmed in their own team's posted
    lineup today, else None. Same name+team scoping as lineup_status_for().
"""

import logging
from datetime import date

import requests

from mlb.teams import mlb_to_cbs, norm_name

logger = logging.getLogger(__name__)

MLB_API = "https://statsapi.mlb.com/api/v1"
TIMEOUT = 20


def fetch_posted_lineups(d: date = None) -> dict:
    """Fetch today's official posted lineups from the MLB Stats API.

    Returns {"players": {norm_name: {"team","batting_order"}}, "posted_teams": set()}.
    Never raises -- returns empty structures on any API failure so callers
    degrade to "unknown" (expected-based-on-schedule) rather than crashing.
    """
    if d is None:
        from mlb.schedule import _today_et
        d = _today_et()

    url = f"{MLB_API}/schedule"
    params = {
        "sportId":  1,
        "date":     d.isoformat(),
        "hydrate":  "lineups,team",
        "gameType": "R",
    }
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error("MLB posted-lineups API error %s: %s", d.isoformat(), e)
        return {"players": {}, "posted_teams": set()}

    players: dict[str, dict] = {}
    posted_teams: set[str] = set()

    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            lineups = game.get("lineups") or {}
            teams = game.get("teams", {})
            home_abbr = _team_abbr(teams.get("home", {}))
            away_abbr = _team_abbr(teams.get("away", {}))

            for side_key, team_abbr in (("homePlayers", home_abbr), ("awayPlayers", away_abbr)):
                side_players = lineups.get(side_key) or []
                if not side_players or not team_abbr:
                    continue
                posted_teams.add(team_abbr)
                for i, p in enumerate(side_players):
                    name = p.get("fullName", "")
                    if not name:
                        continue
                    players[norm_name(name)] = {
                        "team":          team_abbr,
                        "batting_order": p.get("battingOrder") or (i + 1),
                    }

    logger.info(
        "Posted lineups %s: %d hitters confirmed across %d teams with lineups posted",
        d.isoformat(), len(players), len(posted_teams),
    )
    return {"players": players, "posted_teams": posted_teams}


def lineup_status_for(name: str, team: str, posted: dict) -> str:
    """Return "confirmed" / "not_in_lineup" / "unknown" for a rostered hitter.

    Bug fix (2026-08-16): this used to return "confirmed" on a normalized-
    name match alone, ignoring the `team` argument entirely -- `players` is
    a flat dict of every hitter in ANY team's posted lineup that day, keyed
    only by name. That let a bench/minor-league player who merely shares a
    normalized name with someone genuinely starting elsewhere get marked
    "confirmed," which find_legal_swaps() (sports/baseball/lineup_optimizer.
    py) then proposed as a legitimate swap-in. Reported live 2026-08-13:
    Franklin Arias, a Double-A prospect never called up to MLB, surfaced as
    a "confirmed playing" swap-in candidate.

    Fix: only return "confirmed" when the stored entry's team matches the
    player's own team. `team` is still checked here now, not just in the
    not_in_lineup fallback below.
    """
    norm = norm_name(name)
    team_up = (team or "").upper()
    entry = (posted or {}).get("players", {}).get(norm)
    if entry is not None and (entry.get("team") or "").upper() == team_up:
        return "confirmed"
    if team_up in (posted or {}).get("posted_teams", set()):
        return "not_in_lineup"
    return "unknown"


def batting_order_for(name: str, team: str, posted: dict) -> int | None:
    """Return the batting-order slot for a rostered hitter, but only if
    they're confirmed in THEIR OWN team's posted lineup today -- else None.

    Same team-scoping bug as lineup_status_for() above applied here too:
    the original call site (agent/decisions.py) looked this up by
    normalized name alone, so a bench/minor-league player could get a real
    batting-order number attached from an unrelated same-named player on a
    different team, reinforcing the same false "confirmed" impression.
    """
    norm = norm_name(name)
    team_up = (team or "").upper()
    entry = (posted or {}).get("players", {}).get(norm)
    if entry is not None and (entry.get("team") or "").upper() == team_up:
        return entry.get("batting_order")
    return None


def _team_abbr(side_info: dict) -> str:
    raw = (side_info.get("team", {}).get("abbreviation", "")
           or side_info.get("team", {}).get("teamCode", ""))
    return mlb_to_cbs(raw.upper()) if raw else ""
