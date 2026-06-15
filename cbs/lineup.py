"""
Lineup reading and (eventually) setting.

READ: derives the current lineup from the validated league/rosters API data.
WRITE (set_lineup): NOT validated — dry-run only until the real lineup-save
request is captured from the browser and verified.
"""

import logging
from cbs.auth import CBSAuth
from cbs.roster import get_roster

logger = logging.getLogger(__name__)


def get_current_lineup(auth: CBSAuth, league_id: str, team_id: str,
                       sport: str = "baseball") -> list[dict]:
    """Current lineup as [{slot, player_id, player_name, is_starting}]."""
    roster = get_roster(auth, league_id, team_id, sport)
    return [
        {
            "slot": rs.slot,
            "player_id": rs.player.id,
            "player_name": rs.player.name,
            "is_starting": rs.is_starting,
        }
        for rs in roster
    ]


def set_lineup(auth: CBSAuth, league_id: str, team_id: str,
               lineup: list, sport: str = "baseball",
               dry_run: bool = True) -> bool:
    """Submit lineup changes. WRITE PATH NOT YET VALIDATED — refuses to run
    outside dry-run until the real save request is captured and tested."""
    if dry_run:
        print(f"  DRY_RUN: would set {len(lineup)} lineup slots in {league_id}")
        return False
    raise NotImplementedError(
        "Live lineup submission not yet validated. Capture the lineup-save "
        "request from the browser (DevTools Network tab) and implement here.")
