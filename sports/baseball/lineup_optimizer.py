"""
Daily lineup optimizer for CBS fantasy baseball.

Given today's MLB schedule, recommends:
- Which roster SPs should be ACTIVE (probable starter today)
- Which roster SPs should be BENCHED (not starting today)
- Which batters are on teams with off days (consider benching)
- Which bench players are playing today and could fill in

BUG 1 fix: RP-designated players who appear in the MLB probable-pitcher list
(i.e. spot starters) are now detected and labelled is_probable_starter=True.
Previously, is_probable_starter was always None for RP regardless of whether
they appeared as a confirmed starter. format_lineup_advice now checks
is_probable_starter is True for ALL pitchers, not just those tagged "SP".

DRY_RUN=True: output only, no CBS submissions.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

from mlb.teams import norm_name as _norm

logger = logging.getLogger(__name__)

_PITCHER_POS = {"SP", "RP", "P"}

@dataclass
class LineupAdvice:
    """Lineup recommendation for a single player."""
    player_name: str
    team: str
    positions: list[str]
    slot: str           # current CBS slot (C, SP, BN, etc.)
    is_starting: bool   # currently active in lineup
    advice: str         # "start", "bench", "ok", "bench_pitcher"
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
    probable_starters: set[str],  # norm names from mlb.schedule
) -> list[LineupAdvice]:
    """
    Cross-reference current lineup against today's schedule.

    lineup_slots: list of {slot, player_id, player_name, team, positions, is_starting}
    teams_playing: set of CBS team abbreviations with games today
    probable_starters: set of norm-names for pitchers starting today

    Returns a list of LineupAdvice objects.

    BUG 1: RP appearing in probable_starters (spot starters) are now flagged
    is_probable_starter=True exactly like SP probable starters.

    IMPORTANT: schedule data may be incomplete (API lag, team abbrev mismatches,
    UTC/ET boundary issues). The rule is: only assert positively confirmed facts.
    Never tell the user to bench a batter based purely on absence from the
    schedule set -- that absence may reflect a data gap, not a true off day.
    Only flag confirmed off days when the schedule API returned a healthy number
    of games (>=10 teams playing), reducing false negatives on partial-data days.
    """
    # Sanity check: if fewer than 10 team-sides are playing, the schedule data
    # is likely incomplete (API error, off-season, or wrong date). Suppress all
    # negative schedule inferences in that case.
    schedule_reliable = len(teams_playing) >= 10

    logger.info("optimize_daily_lineup: %d teams playing (reliable=%s), %d probable starters",
                len(teams_playing), schedule_reliable, len(probable_starters))

    advice_list: list[LineupAdvice] = []

    for slot_info in lineup_slots:
        name      = slot_info.get("player_name") or slot_info.get("name", "")
        team      = slot_info.get("team", "")
        positions = slot_info.get("positions") or []
        slot      = slot_info.get("slot", "")
        active    = slot_info.get("is_starting", True)

        if not name or name.lower() in ("empty", ""):
            continue

        is_pitcher        = ("SP" in positions or "RP" in positions)
        confirmed_playing = team.upper() in teams_playing
        norm_name         = _norm(name)

        if is_pitcher:
            # BUG 1 fix: check ALL pitchers (SP and RP) against probable_starters.
            # An RP in the probable list is a confirmed spot starter.
            probable = norm_name in probable_starters

            if "SP" in positions:
                if probable:
                    advice = "start" if not active else "ok"
                    reason = f"Confirmed probable starter today ({team})"
                elif confirmed_playing:
                    # Team has a game but not yet listed as probable -- inconclusive
                    advice = "ok"
                    reason = f"{team} has a game -- probable starters not yet posted"
                elif schedule_reliable:
                    # Only flag as no-game when schedule data looks complete
                    advice = "bench_pitcher"
                    reason = f"{team} has no game today per MLB schedule"
                else:
                    advice = "ok"
                    reason = f"{team} schedule unclear -- verify before benching"
                advice_list.append(LineupAdvice(
                    player_name=name, team=team, positions=positions,
                    slot=slot, is_starting=active, advice=advice, reason=reason,
                    is_probable_starter=probable if (probable or confirmed_playing) else None,
                ))
            else:
                # RP: flag spot starters from probable list; otherwise informational only.
                # BUG 1: was always is_probable_starter=None for RP -- now True if confirmed.
                if probable:
                    reason = f"Confirmed spot starter today ({team}) -- RP in MLB probable list"
                elif confirmed_playing:
                    reason = f"{team} playing today"
                else:
                    reason = f"{team} schedule unconfirmed"
                advice_list.append(LineupAdvice(
                    player_name=name, team=team, positions=positions,
                    slot=slot, is_starting=active, advice="ok", reason=reason,
                    is_probable_starter=True if probable else None,
                ))
        else:
            # Batter
            if confirmed_playing:
                advice = "start" if not active else "ok"
                reason = f"{team} has a game today"
            elif schedule_reliable:
                # Only assert off-day when schedule data is healthy
                advice = "bench"
                reason = f"{team} has no game today per MLB schedule -- verify before benching"
            else:
                # Inconclusive -- don't recommend action
                advice = "ok"
                reason = f"{team} schedule unconfirmed"
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

    # BUG 1 fix: starters_today now includes RP spot starters (is_probable_starter is True
    # for any pitcher -- SP or RP -- confirmed in the MLB probable-pitcher list).
    # Previously this check required "SP" in positions, silently dropping RP spot starters.
    starters_today = [a for a in advice_list if a.is_probable_starter is True]
    not_starting   = [a for a in advice_list
                      if a.is_pitcher and a.advice == "bench_pitcher"]
    off_batters    = [a for a in advice_list
                      if not a.is_pitcher and a.advice == "bench"]
    active_batters = [a for a in advice_list
                      if not a.is_pitcher and a.advice in ("start", "ok")]

    if starters_today:
        lines.append(f"  SPs starting today ({len(starters_today)}):")
        for a in starters_today:
            pos_tag = "/".join(a.positions)
            mark = "[ACTIVE]" if a.is_starting else "[BENCH - move to active]"
            lines.append(f"    {mark} {a.player_name} ({a.team}) [{pos_tag}]")
    else:
        lines.append("  SPs starting today: none confirmed yet (check back ~90 min before games)")

    if not_starting:
        lines.append(f"  SPs NOT starting today - consider benching ({len(not_starting)}):")
        for a in not_starting:
            mark = "[ACTIVE - bench!]" if a.is_starting else "[already benched]"
            lines.append(f"    {mark} {a.player_name} ({a.team}) {a.reason}")

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
