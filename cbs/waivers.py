"""
Waivers / free agents.

READ (get_available_players): tries the JSON API players endpoint, falls back
to scraping the league subdomain players page. NOTE: neither path has been
validated by cbs_probe.py yet — if both fail, extend the probe to find the
right endpoint/page before trusting this module.

WRITE (claim_player): NOT validated. Stays dry-run-only until the actual
add/drop submission flow is captured from the browser and verified.
"""

import logging
from bs4 import BeautifulSoup
from data.models import Player, WaiverPlayer
from cbs.auth import CBSAuth, CBSAPIError

logger = logging.getLogger(__name__)


def get_available_players(auth: CBSAuth, league_id: str,
                          sport: str = "baseball",
                          position: str = "all") -> list[WaiverPlayer]:
    """Free agents in the league. API first, HTML fallback."""
    try:
        return _available_from_api(auth, league_id, sport, position)
    except CBSAPIError as e:
        logger.warning("JSON API free agents failed (%s) — trying HTML", e)
        return _available_from_html(auth, league_id, sport)


# Alias used by agent/decisions.py
def fetch_waiver_wire(auth: CBSAuth, league_id: str,
                      sport: str = "baseball",
                      position: str = "all",
                      limit: int = 0) -> list[WaiverPlayer]:
    """Alias for get_available_players with optional result cap."""
    players = get_available_players(auth, league_id, sport, position)
    return players[:limit] if limit else players


def _available_from_api(auth: CBSAuth, league_id: str, sport: str,
                        position: str) -> list[WaiverPlayer]:
    # VALIDATED: players/list returns the league's full player universe
    # (~8400 records); owned players carry owned_by_team_id, so free agents
    # are simply the records without it. on_waivers=1 means claimable via
    # waivers rather than immediate add.
    data = auth.api_get("players/list", league_id, sport)
    raw = (data.get("body", {}) or {}).get("players", []) or []
    results = []
    for i, p in enumerate(raw):
        if p.get("owned_by_team_id"):
            continue
        pos = p.get("position", "")
        if position not in ("all", "", "ALL") and pos != position:
            continue
        on_w = bool(p.get("on_waivers"))
        player = Player(
            id=str(p.get("id", "")),
            name=p.get("fullname") or p.get("name", "Unknown"),
            position=pos,
            team=p.get("pro_team", ""),
            status="W" if on_w else "FA",
        )
        results.append(WaiverPlayer(
            player=player,
            add_rank=i,
            ownership_pct=float(p.get("owned_pct") or p.get("ownership_pct") or 0),
            on_waivers=on_w,
        ))
    if not results:
        raise CBSAPIError("players/list returned no unowned players")
    logger.info("API free agents: %d players in %s", len(results), league_id)
    return results


def _available_from_html(auth: CBSAuth, league_id: str,
                         sport: str) -> list[WaiverPlayer]:
    # UNVALIDATED page path — common CBS layout. tr.playerRow selector is
    # validated on roster pages; player-list pages typically share it.
    r = auth.fetch_league_page(league_id, sport, "/players/add-drop")
    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    for i, row in enumerate(soup.select("tr.playerRow")):
        link = row.select_one("a.playerLink") or row.select_one("a[aria-label]")
        if link is None:
            continue
        name = (link.get("aria-label") or link.text).strip()
        href = link.get("href", "")
        pid = href.rstrip("/").split("/")[-1] if href else ""
        pos_el = row.select_one("td.playerPosition")
        pos = pos_el.text.strip() if pos_el else ""
        results.append(WaiverPlayer(
            player=Player(id=pid, name=name, position=pos),
            add_rank=i,
        ))
    logger.info("HTML free agents: %d players in %s", len(results), league_id)
    return results


def claim_player(auth: CBSAuth, league_id: str, team_id: str,
                 add_player_id: str, drop_player_id: str,
                 sport: str = "baseball", dry_run: bool = True) -> bool:
    """Submit a waiver claim / add-drop. WRITE PATH NOT YET VALIDATED —
    refuses to run outside dry-run until the real submission flow is
    captured and tested."""
    if dry_run:
        print(f"  DRY_RUN: would claim {add_player_id}, drop {drop_player_id} "
              f"in {league_id}")
        return False
    raise NotImplementedError(
        "Live waiver submission not yet validated. Capture the add/drop "
        "request from the browser (DevTools Network tab) and implement here.")
