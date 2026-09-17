"""
Injury/practice-status report for rostered football players -- the
enhancement order's (filed 2026-09-16) acceptance criterion 3: "the agent
answers 'is anyone on my roster injured' from a trusted in-tool source,
not a manual web search," flagging clearly when a status is ambiguous and
degrading honestly (not silently) when a feed is down.

Two independent sources, cross-checked rather than trusted blindly:

  - FantasyPros' /nfl/injuries (fantasypros/client.py::nfl_injuries()) --
    the richer source: named injury type, a 3-day practice-participation
    history, and an explicit probability-of-playing estimate. Confirmed
    live 2026-09-17 (fp_injuries_probe.py-equivalent checks folded into
    the fp_nfl_stats_probe.py session) -- see the football-stats-feed-
    scoping project memory for exact fields.
  - Sleeper's per-player `injury_status` field (already fetched as part of
    the /players/nfl index sleeper/client.py::get_players() pulls for
    actuals -- no extra call needed here). Thinner (just a status word,
    no practice history) but a genuinely separate source, so it catches a
    FantasyPros lag/outage and vice versa.

Cross-checking matters here specifically because the enhancement order
called out "flagged clearly when ambiguous" -- when the two sources
disagree (e.g. FantasyPros says "Questionable" but Sleeper still shows
the player as active), that disagreement IS the ambiguity worth flagging,
not something to silently resolve by picking one source.

KNOWN DATA GAPS:
  - A player with NO status from either source is treated as healthy and
    is NOT included in the report -- this module never fabricates a
    "clean" status; it only reports what a source actually said.
  - If FantasyPros is down/unconfigured, the report still runs on Sleeper
    alone (and vice versa) -- "available" only goes False when BOTH
    sources are unavailable. Each partial-degrade is still surfaced via
    `note` so a caller/user knows the report may be incomplete, per this
    codebase's safe-degrade convention (cbs/players_cache.py and
    agent/football_decisions.py's *_by_name() helpers use the same
    pattern).
"""

from __future__ import annotations

import logging

from fantasypros.client import FantasyProsClient
from sleeper.client import get_players, SleeperUnavailable

logger = logging.getLogger(__name__)


def _norm(name: str) -> str:
    return name.strip().lower()


def _fetch_fantasypros_injuries(fp_client: FantasyProsClient | None,
                                season: int, week: int) -> tuple[dict, str | None]:
    """Returns ({normalized_name: injury_dict}, note). note is None on a
    clean fetch, else an explanation of why the dict may be empty/partial."""
    if fp_client is None:
        return {}, "FantasyPros injury feed unavailable: FANTASYPROS_API_KEY not configured."
    try:
        injuries = fp_client.nfl_injuries(week=week, season=season)
    except Exception as e:
        logger.warning("get_injury_report: FantasyPros injuries fetch failed: %s", e)
        return {}, f"FantasyPros injury feed unavailable this run: {e}"

    by_name: dict[str, dict] = {}
    for entry in injuries:
        name = entry.get("name") or entry.get("player_name")
        if name:
            by_name[_norm(name)] = entry
    return by_name, None


def _fetch_sleeper_injuries() -> tuple[dict, str | None]:
    """Returns ({normalized_name: status_str}, note)."""
    try:
        players = get_players()
    except SleeperUnavailable as e:
        logger.warning("get_injury_report: Sleeper players fetch failed: %s", e)
        return {}, f"Sleeper injury status unavailable this run: {e}"

    by_name: dict[str, str] = {}
    for p in players.values():
        status = p.get("injury_status")
        if not status:
            continue
        full = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        if full:
            by_name[_norm(full)] = status
    return by_name, None


def get_injury_report(roster: list, fp_client: FantasyProsClient | None,
                      season: int, week: int) -> dict:
    """Injury/practice status for every rostered player who has a
    non-empty status from at least one source.

    Returns:
        {"available": bool, "note": str | None,
         "players": [
             {"name": str, "position": str,
              "fp_status": str | None, "fp_injury_type": str | None,
              "fp_practice": [str, str, str] | None,
              "fp_probability_of_playing": str | None,
              "sleeper_status": str | None,
              "disagreement": bool},
             ...
         ]}

    "available": False only when BOTH sources failed outright -- a
    single-source outage still returns available=True with a `note`
    explaining the gap, since a partial report is still more useful than
    none (same convention as agent/football_decisions.py's safe-degrade
    helpers).
    """
    fp_by_name, fp_note = _fetch_fantasypros_injuries(fp_client, season, week)
    sleeper_by_name, sleeper_note = _fetch_sleeper_injuries()

    # "available": False means both sources actually FAILED (fp_note/
    # sleeper_note set) -- not just that neither found a flagged player,
    # which is a legitimate, common result (most of a roster is healthy
    # most weeks) and must not be reported as an outage.
    if fp_note is not None and sleeper_note is not None:
        return {"available": False, "note": f"{fp_note} | {sleeper_note}", "players": []}

    players_out = []
    for rs in roster:
        name = rs.player.name
        key = _norm(name)
        fp = fp_by_name.get(key)
        sleeper_status = sleeper_by_name.get(key)

        if fp is None and sleeper_status is None:
            continue  # no status from either source -- not reported (never fabricated "healthy")

        fp_status = (fp or {}).get("status") or (fp or {}).get("status_short")
        entry = {
            "name": name,
            "position": rs.player.positions[0] if rs.player.positions else rs.player.position,
            "fp_status": fp_status,
            "fp_injury_type": (fp or {}).get("injury_type"),
            "fp_practice": [
                (fp or {}).get("practice_1"),
                (fp or {}).get("practice_2"),
                (fp or {}).get("practice_3"),
            ] if fp else None,
            "fp_probability_of_playing": (fp or {}).get("probability_of_playing"),
            "sleeper_status": sleeper_status,
            "disagreement": bool(
                fp_status and sleeper_status and _norm(fp_status) != _norm(sleeper_status)
            ),
        }
        players_out.append(entry)

    notes = [n for n in (fp_note, sleeper_note) if n]
    return {"available": True, "note": " | ".join(notes) if notes else None, "players": players_out}
