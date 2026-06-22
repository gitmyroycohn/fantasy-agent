"""
Roster fetching via the CBS Fantasy JSON API (validated by cbs_probe.py),
with HTML scraping of the league subdomain as a fallback.
"""

import logging
from bs4 import BeautifulSoup
from data.models import Player, RosterSlot
from cbs.auth import CBSAuth, CBSAPIError

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

    slots = []
    for p in team.get("players", []) or []:
        # roster_status: A=active lineup, RS=reserve/bench, I=injured list,
        # ML=minor leagues (validated against live league data)
        roster_status = str(p.get("roster_status", "")).upper()
        player = Player(
            id=str(p.get("id", "")),
            name=p.get("fullname") or p.get("name", "Unknown"),
            position=p.get("position", ""),
            team=p.get("pro_team", ""),
            status=roster_status or "A",
        )
        slots.append(RosterSlot(
            player=player,
            slot=p.get("roster_pos") or player.position,
            is_starting=roster_status == "A",
        ))
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

def _slots_from_team_payload(team: dict) -> list[RosterSlot]:
    """Shared parser: CBS team dict -> list[RosterSlot]. Same logic as
    _roster_from_api's inner loop, factored out so get_all_team_rosters
    can reuse it for every team in a single API response."""
    slots = []
    for p in team.get("players", []) or []:
        roster_status = str(p.get("roster_status", "")).upper()
        player = Player(
            id=str(p.get("id", "")),
            name=p.get("fullname") or p.get("name", "Unknown"),
            position=p.get("position", ""),
            team=p.get("pro_team", ""),
            status=roster_status or "A",
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

    result = {}
    for t in teams:
        team_id   = str(t.get("id", ""))
        team_name = t.get("name") or t.get("nickname") or f"Team {team_id}"
        result[team_id] = {
            "name":   team_name,
            "roster": _slots_from_team_payload(t),
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
