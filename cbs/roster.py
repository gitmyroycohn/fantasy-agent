"""
Roster fetching via the CBS Fantasy JSON API (validated by cbs_probe.py),
with HTML scraping of the league subdomain as a fallback.
"""

import logging
from bs4 import BeautifulSoup
from data.models import Player, RosterSlot
from cbs.auth import CBSAuth, CBSAPIError
from cbs.players import fetch_position_eligibility_index

logger = logging.getLogger(__name__)


def get_roster(auth: CBSAuth, league_id: str, team_id: str,
               sport: str = "baseball") -> list[RosterSlot]:
    """Fetch a team's roster. JSON API first, HTML fallback."""
    try:
        return _roster_from_api(auth, league_id, team_id, sport)
    except CBSAPIError as e:
        logger.warning("JSON API roster failed (%s) — falling back to HTML", e)
        return _roster_from_html(auth, league_id, team_id, sport)


# ---------------------------------------------------------------------------
# JSON API (primary)
# ---------------------------------------------------------------------------
def _roster_from_api(auth: CBSAuth, league_id: str, team_id: str,
                     sport: str) -> list[RosterSlot]:
    data = auth.api_get("league/rosters", league_id, sport, team_id=team_id)
    teams = (data.get("body", {}) or {}).get("rosters", {}).get("teams", [])

    team = None
    for t in teams:
        if str(t.get("id", "")) == str(team_id):
            team = t
            break
    if team is None and len(teams) == 1:
        team = teams[0]
    if team is None:
        raise CBSAPIError(
            f"team_id {team_id} not in rosters response "
            f"(got ids: {[t.get('id') for t in teams]})")

    # ENH 2 fix: `position` on this payload is only the player's CURRENT
    # roster slot, not their full CBS eligibility (a 2B/SS player rostered at
    # 2B today would otherwise only ever show as 2B-eligible). Look up full
    # eligibility from players/list (best-effort -- falls back silently to
    # the slot tag if the index can't be fetched).
    try:
        pos_index = fetch_position_eligibility_index(auth, league_id, sport)
    except Exception as e:
        logger.warning("Position eligibility index unavailable (%s) -- "
                       "roster players will use their current slot only", e)
        pos_index = {}

    slots = _slots_from_team_payload(team, pos_index)
    logger.info("API roster: %d players for team %s in %s",
                len(slots), team_id, league_id)
    return slots


# ---------------------------------------------------------------------------
# HTML scraping (fallback) — selector validated by cbs_probe.py: tr.playerRow
# ---------------------------------------------------------------------------
def _roster_from_html(auth: CBSAuth, league_id: str, team_id: str,
                      sport: str) -> list[RosterSlot]:
    r = auth.fetch_league_page(league_id, sport, f"/teams/{team_id}")
    soup = BeautifulSoup(r.text, "html.parser")

    slots = []
    for row in soup.select("tr.playerRow"):
        classes = row.get("class", [])
        if "empty" in classes:
            continue
        pos_el = row.select_one("td.playerPosition")
        pos = pos_el.text.strip() if pos_el else ""
        link = row.select_one("a.playerLink") or row.select_one("a[aria-label]")
        if link is None:
            continue
        name = (link.get("aria-label") or link.text).strip()
        href = link.get("href", "")
        pid = href.rstrip("/").split("/")[-1] if href else ""
        slots.append(RosterSlot(
            player=Player(id=pid, name=name, position=pos),
            slot=pos,
        ))
    logger.info("HTML roster: %d players for team %s in %s",
                len(slots), team_id, league_id)
    return slots


# ---------------------------------------------------------------------------
# All teams (on-demand lookup of any team in the league, not just your own)
# ---------------------------------------------------------------------------

def _slots_from_team_payload(team: dict, pos_index: dict | None = None) -> list[RosterSlot]:
    """Shared parser: CBS team dict -> list[RosterSlot]. Same logic as
    _roster_from_api's inner loop, factored out so get_all_team_rosters
    can reuse it for every team in a single API response.

    pos_index: optional {player_id: [eligible_positions]} from
    cbs.players.fetch_position_eligibility_index (ENH 2). When a player's id
    is present, their full CBS eligibility is used instead of just the
    current roster slot tag.
    """
    pos_index = pos_index or {}
    slots = []
    for p in team.get("players", []) or []:
        roster_status = str(p.get("roster_status", "")).upper()
        pid = str(p.get("id", ""))
        player = Player(
            id=pid,
            name=p.get("fullname") or p.get("name", "Unknown"),
            position=p.get("position", ""),
            team=p.get("pro_team", ""),
            status=roster_status or "A",
            eligible_positions_override=pos_index.get(pid),
        )
        slots.append(RosterSlot(
            player=player,
            slot=p.get("roster_pos") or player.position,
            is_starting=roster_status == "A",
        ))
    return slots


def get_all_team_rosters(auth: CBSAuth, league_id: str,
                         sport: str = "baseball") -> dict[str, dict]:
    """
    Fetch every team's roster in the league with a single API call.

    The league/rosters endpoint already returns all teams -- get_roster()
    just filters it down to one. This skips the filter and keeps everyone,
    so callers can look up any team on demand (e.g. for trade research)
    without one API round-trip per team.

    Returns {team_id: {"name": team_name, "roster": [RosterSlot, ...]}}
    """
    data = auth.api_get("league/rosters", league_id, sport)
    teams = (data.get("body", {}) or {}).get("rosters", {}).get("teams", [])

    # ENH 2: same full-eligibility lookup as get_roster(), shared across
    # every team in this response (single players/list call, cached).
    try:
        pos_index = fetch_position_eligibility_index(auth, league_id, sport)
    except Exception as e:
        logger.warning("Position eligibility index unavailable (%s) -- "
                       "all teams' players will use their current slot only", e)
        pos_index = {}

    result = {}
    for t in teams:
        team_id   = str(t.get("id", ""))
        team_name = t.get("name") or t.get("nickname") or f"Team {team_id}"
        result[team_id] = {
            "name":   team_name,
            "roster": _slots_from_team_payload(t, pos_index),
        }
    logger.info("get_all_team_rosters: %d teams fetched for %s",
                len(result), league_id)
    return result


def resolve_team_id(all_rosters: dict, query: str) -> str | None:
    """
    Resolve a team name (exact or partial, case-insensitive) to a team_id.

    Tries exact match first, then substring match. Returns None if no team
    in the league matches.
    """
    q = query.strip().lower()
    for tid, info in all_rosters.items():
        if info["name"].strip().lower() == q:
            return tid
    for tid, info in all_rosters.items():
        if q in info["name"].strip().lower():
            return tid
    return None


# ---------------------------------------------------------------------------
# Contract-years scraping (east_coast football keeper house rule)
# ---------------------------------------------------------------------------

def fetch_contract_years(auth: CBSAuth, league_id: str, team_id: str,
                         sport: str = "football") -> dict[str, int]:
    """Scrape CBS's read-only CONTRACT column from a team's live roster
    page (/teams/<team_id>) -- confirmed present on east_coast (ecfc) via
    Christopher's Salary/Contracts commissioner feature, live-verified
    2026-08-18 (see project memory "football keeper policies"). This is
    the plain HTML table cell, NOT the commissioner-only "Edit Salary &
    Contracts" JS widget -- no interaction needed, just page-text parsing,
    same fetch path as _roster_from_html's fallback above.

    Returns {player_name: contract_value} using the same name-extraction
    logic as _roster_from_html (a.playerLink / aria-label text) -- NOT
    normalized, so this may not exact-match names from _roster_from_api's
    JSON-derived roster (a different CBS data source). Callers that need
    to match this against an already-fetched roster should normalize both
    sides rather than assume identical strings -- see
    agent/football_decisions.py's east_coast wiring for that.

    contract_value is CBS's displayed count of seasons of validity
    remaining, counting the current season (0=already expired, 1=this is
    the final valid season, 2=this plus one more after) -- see
    sports/football/keepers.py::contract_years_to_acquired_seasons() for
    the conversion into keeper_guidance()'s expected acquired_season shape
    (this function only reports what CBS displays, no interpretation).

    Column position: this table actually has THREE header-ish rows above
    the player rows -- a 1-cell title row ("Players... Set Your Default
    View"), a grouped-column row (e.g. a "Contracts" cell spanning
    several real columns), and the real per-column header row (which has
    its own standalone "contract" cell). Live-inspected 2026-08-23 via
    contract_diag.py (see project memory "football keeper policies") --
    an earlier version of this function took the table's FIRST <tr>,
    which is that 1-cell title row, found no "contract" match, and
    silently fell back to a wrong hardcoded index. This version instead
    scans every <tr> in the table for one containing a cell whose exact
    (case-insensitive, stripped) text equals "contract" -- an exact
    match, not a substring, so the grouped "Contracts" row (plural) does
    not false-match -- and uses that cell's index. Falls back to the
    confirmed-live 0-based index 12 with a WARNING log only if no row
    matches at all, rather than silently guessing wrong or crashing.
    Skips (and logs) any row whose contract cell isn't parseable as an
    integer -- never fabricates a value. Returns {} (not a partial/wrong
    guess) if no player rows are found at all.
    """
    r = auth.fetch_league_page(league_id, sport, f"/teams/{team_id}")
    soup = BeautifulSoup(r.text, "html.parser")

    rows = soup.select("tr.playerRow")
    if not rows:
        logger.warning("fetch_contract_years: no tr.playerRow found on "
                       "%s/teams/%s -- returning empty", league_id, team_id)
        return {}

    contract_idx = None
    table = rows[0].find_parent("table")
    if table is not None:
        for header_row in table.find_all("tr"):
            for i, cell in enumerate(header_row.find_all(["th", "td"])):
                if cell.get_text(strip=True).lower() == "contract":
                    contract_idx = i
                    break
            if contract_idx is not None:
                break
    if contract_idx is None:
        logger.warning(
            "fetch_contract_years: could not locate a 'CONTRACT' header "
            "cell on %s/teams/%s -- falling back to the index (12) "
            "confirmed live 2026-08-23. If parsed values look wrong, "
            "CBS's table layout has changed and this needs re-verifying.",
            league_id, team_id)
        contract_idx = 12

    result: dict[str, int] = {}
    for row in rows:
        if "empty" in row.get("class", []):
            continue
        link = row.select_one("a.playerLink") or row.select_one("a[aria-label]")
        if link is None:
            continue
        name = (link.get("aria-label") or link.text).strip()
        cells = row.find_all("td")
        if contract_idx >= len(cells):
            logger.warning("fetch_contract_years: row for %r has only %d "
                           "cells, expected contract at index %d -- skipping",
                           name, len(cells), contract_idx)
            continue
        raw = cells[contract_idx].get_text(strip=True)
        try:
            result[name] = int(raw)
        except ValueError:
            logger.warning("fetch_contract_years: non-integer contract "
                           "value %r for %r -- skipping", raw, name)
            continue

    logger.info("fetch_contract_years: parsed %d contract values for team "
               "%s in %s", len(result), team_id, league_id)
    return result
