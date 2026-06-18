"""
MLB injury integration — IL transactions and active IL roster.

Uses the free MLB Stats API (no auth required), same as mlb/schedule.py.

Public API
----------
fetch_il_transactions(lookback_days=7)
    → list of dicts: {player, team, type, date, description}
    Recent IL placements, activations, and transfers.

fetch_active_il()
    → dict: {norm_name: {"name", "team", "il_type", "date"}}
    All players currently on any IL across MLB.

annotate_roster_injuries(roster_slots, active_il)
    → list of dicts: {player, slot, il_type, date}
    Flags your CBS roster players found in the active IL.
"""

import re
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

_ET      = ZoneInfo("America/New_York")
MLB_API  = "https://statsapi.mlb.com/api/v1"
TIMEOUT  = 20

# Transaction type codes we care about
_IL_PLACE_TYPES = {
    "IL placement", "10-Day IL", "15-Day IL", "60-Day IL",
    "7-Day IL", "Placed on Injured List",
}
_IL_ACTIVATE_TYPES = {
    "IL activation", "Activated from Injured List",
    "Reinstated from Injured List",
}
_TRANSFER_TYPES = {"IL transfer"}

# Roster types that represent IL in the MLB API
_IL_ROSTER_TYPES = ["injuries"]


def _today_et() -> date:
    return datetime.now(_ET).date()


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _fmt(d: str) -> str:
    """Convert YYYY-MM-DD to MM/DD."""
    try:
        return datetime.strptime(d[:10], "%Y-%m-%d").strftime("%-m/%-d")
    except Exception:
        return d[:10]


# ---------------------------------------------------------------------------
# Recent IL transactions
# ---------------------------------------------------------------------------

def fetch_il_transactions(lookback_days: int = 7) -> list[dict]:
    """
    Return recent IL placements, activations, and transfers.

    Each entry: {player, team, type, date, description}
    type is one of: "placed", "activated", "transfer"
    """
    today = _today_et()
    start = today - timedelta(days=lookback_days)
    url = f"{MLB_API}/transactions"
    params = {
        "sportId": 1,
        "startDate": start.strftime("%Y-%m-%d"),
        "endDate": today.strftime("%Y-%m-%d"),
    }
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        logger.warning("fetch_il_transactions error: %s", exc)
        return []

    results = []
    for txn in data.get("transactions", []):
        type_desc = txn.get("typeDesc", "") or txn.get("type", {}).get("description", "")
        player_data = txn.get("person") or txn.get("player") or {}
        player_name = (
            player_data.get("fullName")
            or player_data.get("nameFirstLast")
            or ""
        )
        team_data = txn.get("toTeam") or txn.get("team") or {}
        team_name = team_data.get("abbreviation") or team_data.get("name") or ""
        eff_date = (
            txn.get("effectiveDate")
            or txn.get("date")
            or ""
        )[:10]
        description = txn.get("description", "")

        # Classify
        if any(t.lower() in type_desc.lower() for t in ["placement", "placed", "il"]) and \
           "activ" not in type_desc.lower() and "transfer" not in type_desc.lower():
            txn_type = "placed"
        elif any(t.lower() in type_desc.lower() for t in ["activ", "reinstat"]):
            txn_type = "activated"
        elif "transfer" in type_desc.lower():
            txn_type = "transfer"
        else:
            continue  # skip non-IL transactions

        if not player_name:
            continue

        results.append({
            "player":      player_name,
            "norm":        _norm(player_name),
            "team":        team_name,
            "type":        txn_type,
            "type_desc":   type_desc,
            "date":        eff_date,
            "description": description,
        })

    logger.info("fetch_il_transactions: %d IL moves in last %d days",
                len(results), lookback_days)
    return results


# ---------------------------------------------------------------------------
# Active IL roster (all of MLB)
# ---------------------------------------------------------------------------

def fetch_active_il() -> dict[str, dict]:
    """
    Return all players currently on any MLB IL.

    Returns {norm_name: {"name", "team", "il_type", "date"}}

    Uses /api/v1/teams + /api/v1/teams/{id}/roster?rosterType=injuries
    per team. Caches results for the session.
    """
    # Get all MLB team IDs
    try:
        r = requests.get(f"{MLB_API}/teams", params={"sportId": 1}, timeout=TIMEOUT)
        r.raise_for_status()
        teams = r.json().get("teams", [])
    except Exception as exc:
        logger.warning("fetch_active_il: failed to get teams: %s", exc)
        return {}

    active_il: dict[str, dict] = {}

    for team in teams:
        team_id   = team.get("id")
        team_abbr = team.get("abbreviation", "")
        if not team_id:
            continue
        try:
            r = requests.get(
                f"{MLB_API}/teams/{team_id}/roster",
                params={"rosterType": "injuries", "season": _today_et().year},
                timeout=TIMEOUT,
            )
            if r.status_code == 404:
                continue
            r.raise_for_status()
            roster = r.json().get("roster", [])
        except Exception as exc:
            logger.debug("fetch_active_il team %s: %s", team_id, exc)
            continue

        for entry in roster:
            person = entry.get("person", {})
            name   = person.get("fullName", "")
            if not name:
                continue
            status = entry.get("status", {})
            il_type = status.get("description", "IL")
            # injuryDate or statusDate
            il_date = entry.get("statusDate", "")[:10] if entry.get("statusDate") else ""

            active_il[_norm(name)] = {
                "name":    name,
                "team":    team_abbr,
                "il_type": il_type,
                "date":    il_date,
            }

    logger.info("fetch_active_il: %d players currently on IL", len(active_il))
    return active_il


# ---------------------------------------------------------------------------
# Roster cross-reference
# ---------------------------------------------------------------------------

def annotate_roster_injuries(roster_slots, active_il: dict) -> list[dict]:
    """
    Cross-reference your CBS roster against active IL.

    Returns list of {player_name, slot, il_type, date} for roster players
    found in the MLB active IL that CBS may not yet have flagged.
    """
    flagged = []
    for slot in roster_slots:
        p = slot.player
        norm = _norm(p.name)
        if norm in active_il:
            entry = active_il[norm]
            flagged.append({
                "player_name": p.name,
                "slot":        slot.slot,
                "cbs_status":  p.status,
                "il_type":     entry["il_type"],
                "date":        entry["date"],
                "team":        entry["team"],
            })
    return flagged


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------

def format_transactions(txns: list[dict], roster_norms: set[str] = None) -> str:
    """
    Format IL transactions for display.

    If roster_norms provided, adds ★ next to your roster players.
    """
    if not txns:
        return "  No IL transactions in the last 7 days."

    placed    = [t for t in txns if t["type"] == "placed"]
    activated = [t for t in txns if t["type"] == "activated"]
    transfers = [t for t in txns if t["type"] == "transfer"]

    lines = []
    roster_norms = roster_norms or set()

    def _star(t):
        return " ★ ON YOUR ROSTER" if t["norm"] in roster_norms else ""

    if placed:
        lines.append("  🚑 Placed on IL:")
        for t in sorted(placed, key=lambda x: x["date"], reverse=True):
            lines.append(f"    {_fmt(t['date'])}  {t['player']} ({t['team']}) — {t['type_desc']}{_star(t)}")

    if activated:
        lines.append("  ✅ Activated from IL:")
        for t in sorted(activated, key=lambda x: x["date"], reverse=True):
            lines.append(f"    {_fmt(t['date'])}  {t['player']} ({t['team']}) — {t['type_desc']}{_star(t)}")

    if transfers:
        lines.append("  🔄 IL Transfers:")
        for t in sorted(transfers, key=lambda x: x["date"], reverse=True):
            lines.append(f"    {_fmt(t['date'])}  {t['player']} ({t['team']}) — {t['type_desc']}{_star(t)}")

    return "\n".join(lines)
