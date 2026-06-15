"""
Daily lineup optimizer for CBS fantasy baseball.

Given today's MLB schedule, recommends:
  - Which roster SPs should be ACTIVE (probable starter today)
  - Which roster SPs should be BENCHED (not starting today)
  - Which batters are on teams with off days (consider benching)
  - Which bench players are playing today and could fill in

DRY_RUN=True: output only, no CBS submissions.
"""
import re
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

_PITCHER_POS = {"SP", "RP", "P"}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


@dataclass
class LineupAdvice:
    """Lineup recommendation for a single player."""
    player_name: str
    team: str
    positions: list[str]
    slot: str                        # current CBS slot (C, SP, BN, etc.)
    is_starting: bool                # currently active in lineup
    advice: str                      # "start", "bench", "ok", "bench_pitcher"
    reason: str
    # pitcher-specific
    is_probable_starter: Optional[bool] = None  # None = unknown (RP/batter)

    @property
    def is_pitcher(self) -> bool:
        return bool(self.positions and self.positions[0] in _PITCHER_POS
                    or "SP" in self.positions or "RP" in self.positions)


def optimize_daily_lineup(
    lineup_slots: list[dict],
    teams_playing: set[str],
    probable_starters: set[str],     # norm names from mlb.schedule
) -> list[LineupAdvice]:
    """
    Cross-reference current lineup against today's schedule.

    lineup_slots: list of {slot, player_id, player_name, team, positions, is_starting}
    teams_playing: set of CBS team abbreviations with games today
    probable_starters: set of norm-names for pitchers starting today

    Returns a list of LineupAdvice objects.
    """
    advice_list: list[LineupAdvice] = []

    for slot_info in lineup_slots:
        name      = slot_info.get("player_name") or slot_info.get("name", "")
        team      = slot_info.get("team", "")
        positions = slot_info.get("positions") or []
        slot      = slot_info.get("slot", "")
        active    = slot_info.get("is_starting", True)

        if not name or name.lower() in ("empty", ""):
            continue

        is_pitcher = ("SP" in positions or "RP" in positions)
        playing    = team in teams_playing
        norm_name  = _norm(name)

        if is_pitcher:
            probable = norm_name in probable_starters
            if "SP" in positions:
                if probable:
                    advice = "start" if not active else "ok"
                    reason = f"Probable starter today ({team} playing)"
                else:
                    advice = "bench_pitcher"
                    reason = (f"{team} playing but not listed as probable starter"
                              if playing
                              else f"{team} has no game today")
                advice_list.append(LineupAdvice(
                    player_name=name, team=team, positions=positions,
                    slot=slot, is_starting=active, advice=advice, reason=reason,
                    is_probable_starter=probable,
                ))
            else:
                # RP — just note if their team is playing
                advice = "ok"
                reason = f"{team} playing today" if playing else f"{team} off today"
                advice_list.append(LineupAdvice(
                    player_name=name, team=team, positions=positions,
                    slot=slot, is_starting=active, advice=advice, reason=reason,
                    is_probable_starter=None,
                ))
        else:
            # Batter
            if playing:
                advice = "start" if not active else "ok"
                reason = f"{team} playing today"
            else:
                advice = "bench"
                reason = f"{team} has no game today - bench if possible"
            advice_list.append(LineupAdvice(
                player_name=name, team=team, positions=positions,
                slot=slot, is_starting=active, advice=advice, reason=reason,
            ))

    return advice_list


def format_lineup_advice(advice_list: list[LineupAdvice], today_str: str = "") -> list[str]:
    """Return a list of print-ready lines summarizing lineup advice."""
    lines = []
    label = f"Daily lineup{' - ' + today_str if today_str else ''}"
    lines.append(f"  {label}:")

    # SP starters today
    starters_today = [a for a in advice_list
                      if "SP" in a.positions and a.is_probable_starter]
    not_starting   = [a for a in advice_list
                      if "SP" in a.positions and a.is_probable_starter is False]
    off_batters    = [a for a in advice_list
                      if not a.is_pitcher and a.advice == "bench"]
    active_batters = [a for a in advice_list
                      if not a.is_pitcher and a.advice in ("start", "ok")]

    if starters_today:
        lines.append(f"  SPs starting today ({len(starters_today)}):")
        for a in starters_today:
            mark = "[ACTIVE]" if a.is_starting else "[BENCH - move to active]"
            lines.append(f"    {mark} {a.player_name} ({a.team})")
    else:
        lines.append("  SPs starting today: none confirmed yet (check back ~90 min before games)")

    if not_starting:
        lines.append(f"  SPs NOT starting today - consider benching ({len(not_starting)}):")
        for a in not_starting:
            mark = "[ACTIVE - bench!]" if a.is_starting else "[already benched]"
            lines.append(f"    {mark} {a.player_name} ({a.team})  {a.reason}")

    if off_batters:
        lines.append(f"  Batters on off days - bench these ({len(off_batters)}):")
        for a in off_batters:
            mark = "[ACTIVE - bench!]" if a.is_starting else "[already benched]"
            lines.append(f"    {mark} {a.player_name} ({a.team})")

    # Active batters summary (brief)
    playing_count = len([a for a in active_batters if a.advice in ("start", "ok")])
    if playing_count:
        lines.append(f"  Batters with games today: {playing_count}")

    return lines
