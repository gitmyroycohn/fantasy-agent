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

BUG 5 fix: players currently on the MLB injured list are now cross-checked
via il_players (norm names from mlb.injuries.fetch_active_il()) BEFORE any
other advice logic runs. Previously a player who was placed on IL could still
be flagged "BENCH -> move to active!" if MLB's probable-starter feed had a
data lag or CBS hadn't moved them to an IL roster slot yet (e.g. Kyle
Harrison, placed on the 15-day IL 7/9, was recommended for activation on
7/11 because he still showed up in the probable-starters set). IL status now
short-circuits all other advice for that player.

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
    advice: str         # "start", "bench", "ok", "bench_pitcher", "on_il"
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
    il_players: set[str] | None = None,  # norm names currently on MLB IL
) -> list[LineupAdvice]:
    """
    Cross-reference current lineup against today's schedule.

    lineup_slots: list of {slot, player_id, player_name, team, positions, is_starting}
    teams_playing: set of CBS team abbreviations with games today
    probable_starters: set of norm-names for pitchers starting today
    il_players: set of norm-names currently on the MLB injured list, from
        mlb.injuries.fetch_active_il(). This is the authoritative "hurt right
        now" source -- independent of CBS roster slot and independent of the
        probable-starters feed, both of which can lag a real-world IL move.

    Returns a list of LineupAdvice objects.

    BUG 1: RP appearing in probable_starters (spot starters) are now flagged
    is_probable_starter=True exactly like SP probable starters.

    BUG 5: any player found in il_players is short-circuited to advice="on_il"
    before the pitcher/batter branches run, so a probable-starter or
    teams-playing match can never override a real injured-list placement.

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
    il_players = il_players or set()

    logger.info("optimize_daily_lineup: %d teams playing (reliable=%s), %d probable starters, %d IL",
                len(teams_playing), schedule_reliable, len(probable_starters), len(il_players))

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

        # BUG 5 fix: a confirmed current IL placement overrides everything
        # else. Never recommend starting/activating a player who is hurt,
        # even if they still appear in the probable-starters feed (data lag)
        # or CBS hasn't moved them to an IL slot yet.
        if norm_name in il_players:
            advice_list.append(LineupAdvice(
                player_name=name, team=team, positions=positions,
                slot=slot, is_starting=active, advice="on_il",
                reason="Currently on the MLB injured list -- ignore any start/activate recommendation",
                is_probable_starter=False,
            ))
            continue

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
    # BUG 5 fix: players on il_players have is_probable_starter forced to False, so an
    # IL placement can never surface here as a "move to active" recommendation.
    starters_today = [a for a in advice_list if a.is_probable_starter is True]
    not_starting   = [a for a in advice_list
                      if a.is_pitcher and a.advice == "bench_pitcher"]
    off_batters    = [a for a in advice_list
                      if not a.is_pitcher and a.advice == "bench"]
    active_batters = [a for a in advice_list
                      if not a.is_pitcher and a.advice in ("start", "ok")]
    on_il          = [a for a in advice_list if a.advice == "on_il"]

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

    if on_il:
        lines.append(f"  On injured list - do not activate ({len(on_il)}):")
        for a in on_il:
            lines.append(f"    🚑 {a.player_name} ({a.team}) {a.reason}")

    # Active batters summary (brief)
    playing_count = len([a for a in active_batters if a.advice in ("start", "ok")])
    if playing_count:
        lines.append(f"  Batters with games today: {playing_count}")

    return lines
