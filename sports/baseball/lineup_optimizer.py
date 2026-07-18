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

ENH 2 fix: find_legal_swaps() below proposes bench -> active swaps using
each player's FULL CBS position eligibility (Player.eligible_positions, via
cbs/players.py -- e.g. a 2B/SS-eligible bench player is considered for an SS
slot, not just the position they're currently rostered at), and only ever
proposes a swap into a slot the incoming player is actually eligible for.

ENH 3 fix: optimize_daily_lineup() now accepts opp_hand_by_team and
down-ranks batters with a real platoon disadvantage against today's
opposing starter (see the docstring on optimize_daily_lineup).

DRY_RUN=True: output only, no CBS submissions.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

from mlb.teams import norm_name as _norm, canonical_team as _canon_team
from config.settings import PLATOON_OPS_GAP, PLATOON_FLOOR_OPS

logger = logging.getLogger(__name__)

_PITCHER_POS = {"SP", "RP", "P"}
_BATTER_SLOTS_ANY = {"UT", "U"}   # utility slots -- any batter is legal here


def _ordinal(n: int) -> str:
    """1 -> "1st", 2 -> "2nd", 3 -> "3rd", 11 -> "11th", etc."""
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"

@dataclass
class LineupAdvice:
    """Lineup recommendation for a single player."""
    player_name: str
    team: str
    positions: list[str]
    slot: str           # current CBS slot (C, SP, BN, etc.)
    is_starting: bool   # currently active in lineup
    advice: str         # "start", "bench", "ok", "bench_pitcher", "on_il", "out_of_lineup"
    reason: str
    # pitcher-specific
    is_probable_starter: Optional[bool] = None  # None = unknown (RP/batter)
    # ENH 4/7: posted-lineup awareness (batters only)
    lineup_status: str = "unknown"          # "confirmed" | "not_in_lineup" | "unknown"
    batting_order: Optional[int] = None

    @property
    def is_pitcher(self) -> bool:
        return bool(self.positions and self.positions[0] in _PITCHER_POS
                    or "SP" in self.positions or "RP" in self.positions)

    @property
    def lineup_label(self) -> str:
        """confirmed / expected / not-in-lineup -- for output display
        (ENH 4/7 done-criteria: label expected vs confirmed, never present a
        projected lineup as final)."""
        if self.lineup_status == "confirmed":
            return "confirmed"
        if self.lineup_status == "not_in_lineup":
            return "not-in-lineup"
        if not self.is_pitcher and self.advice in ("start", "ok"):
            return "expected"
        return "unknown"

def optimize_daily_lineup(
    lineup_slots: list[dict],
    teams_playing: set[str],
    probable_starters: set[str],  # norm names from mlb.schedule
    il_players: set[str] | None = None,  # norm names currently on MLB IL
    opp_hand_by_team: dict[str, str] | None = None,  # ENH 3: {cbs_team_abbr: "L"|"R"}
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
    opp_hand_by_team: {cbs_team_abbr: "L"|"R"} -- the throwing hand of the
        opposing probable starter each team's batters face today, from
        mlb.schedule.todays_matchups(). Used for ENH 3 platoon weighting:
        a batter with a real platoon split (>= PLATOON_OPS_GAP) who is weak
        (< PLATOON_FLOOR_OPS) against today's hand is down-ranked to "bench"
        with the reason surfaced, unless agent/decisions.py's must-start
        floor (season OPS >= .850) overrides it afterward.

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

    # BUG (found in 2026-07-18 live run): canonicalize both sides of every
    # team comparison. CBS's own player.team field returns the short,
    # MLB-native abbreviation for SF/TB/KC/SD, while teams_playing/
    # opp_hand_by_team here are built from the MLB schedule feed's
    # mlb_to_cbs()-mapped (longer) form -- "SF" vs "SFG" never compared
    # equal, so e.g. Landen Roupp (SF) was wrongly flagged "no game today"
    # on a day the Giants played, while probable-starter teammate Logan Webb
    # (also SF) looked fine only because that check doesn't compare teams at
    # all. canonical_team() maps every known alias to one code.
    teams_playing = {_canon_team(t) for t in (teams_playing or set())}
    opp_hand_by_team = {_canon_team(k): v for k, v in (opp_hand_by_team or {}).items()}

    logger.info("optimize_daily_lineup: %d teams playing (reliable=%s), %d probable starters, %d IL",
                len(teams_playing), schedule_reliable, len(probable_starters), len(il_players))

    advice_list: list[LineupAdvice] = []

    for slot_info in lineup_slots:
        name      = slot_info.get("player_name") or slot_info.get("name", "")
        team      = slot_info.get("team", "")
        team_canon = _canon_team(team)
        positions = slot_info.get("positions") or []
        slot      = slot_info.get("slot", "")
        active    = slot_info.get("is_starting", True)

        if not name or name.lower() in ("empty", ""):
            continue

        is_pitcher        = ("SP" in positions or "RP" in positions)
        confirmed_playing = team_canon in teams_playing
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
            lineup_status = slot_info.get("lineup_status", "unknown")
            batting_order = slot_info.get("batting_order")

            if lineup_status == "not_in_lineup":
                # ENH 4/7 fix: MLB's OFFICIAL posted lineup is out and this
                # player isn't in it -- more authoritative than "team has a
                # game" from the schedule alone (platooned out, resting,
                # etc.). This is its own advice value (not "bench") so the
                # must-start floor -- a heuristic override for schedule/
                # platoon *uncertainty* -- does not sweep away a confirmed
                # real-world absence just because a bat is normally elite.
                advice_list.append(LineupAdvice(
                    player_name=name, team=team, positions=positions,
                    slot=slot, is_starting=active, advice="out_of_lineup",
                    reason=f"Not in {team}'s official posted lineup today (CBS shows active)",
                    lineup_status=lineup_status, batting_order=batting_order,
                ))
                continue

            if confirmed_playing:
                advice = "start" if not active else "ok"
                if lineup_status == "confirmed":
                    order_str = f", batting {_ordinal(batting_order)}" if batting_order else ""
                    reason = f"Confirmed in {team}'s official posted lineup today{order_str}"
                else:
                    reason = f"{team} has a game today (lineup not posted yet -- expected)"

                # ENH 3: platoon weighting. Down-rank a batter with a real
                # platoon split who's facing their disadvantage hand today --
                # the must-start floor (agent/decisions.py, OPS >= .850)
                # overrides this back to "ok" for elite bats afterward, the
                # same way it already overrides an off-day "bench".
                hand = opp_hand_by_team.get(team_canon)
                stats = slot_info.get("stats") or {}
                vs_l = stats.get("split_vs_l_ops")
                vs_r = stats.get("split_vs_r_ops")
                if hand in ("L", "R") and vs_l is not None and vs_r is not None:
                    dis_ops = vs_l if hand == "L" else vs_r
                    adv_ops = vs_r if hand == "L" else vs_l
                    if (adv_ops - dis_ops) >= PLATOON_OPS_GAP and dis_ops < PLATOON_FLOOR_OPS:
                        hand_label = "LHP" if hand == "L" else "RHP"
                        other_label = "RHP" if hand == "L" else "LHP"
                        advice = "bench"
                        reason = (
                            f"Platoon disadvantage vs {hand_label} today: "
                            f"{dis_ops:.3f} OPS vs {hand_label} (vs {other_label}: "
                            f"{adv_ops:.3f}) -- {team} has a game today but the "
                            f"matchup favors sitting"
                        )
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
                lineup_status=lineup_status, batting_order=batting_order,
            ))

    return advice_list

_MUST_START_OPS_DEFAULT = 0.850   # ENH 3: batters at/above this season OPS are always active


def apply_must_start_floor(
    advice_list: list["LineupAdvice"],
    ops_by_norm: dict[str, float],
    floor: float = _MUST_START_OPS_DEFAULT,
) -> None:
    """Mutate advice_list in place: elite batters (season OPS >= floor)
    always get advice="ok" instead of "bench" -- overriding both plain
    schedule-uncertainty benches AND ENH 3's platoon-driven down-ranks
    (done-criteria: "the must-start floor must continue to override platoon
    down-ranking for elite bats").

    Deliberately does NOT touch "out_of_lineup" (ENH 4/7): that status means
    MLB's official posted lineup confirms the player isn't starting today --
    a real-world fact, not a heuristic guess -- so a season-long OPS floor
    must never override it.
    """
    for a in advice_list:
        if (a.advice == "bench"
                and not a.is_pitcher
                and ops_by_norm.get(_norm(a.player_name), 0) >= floor):
            a.advice = "ok"
            a.reason = (
                f"Must-start floor (OPS >= {floor:.3f} -- elite bat, always active): {a.reason}"
            )


def find_legal_swaps(
    lineup_slots: list[dict],
    advice_list: list[LineupAdvice],
) -> list[dict]:
    """ENH 2: propose legal bench -> active swaps for slots that should sit.

    For every active player whose advice says they should sit (bench,
    bench_pitcher, or on_il), look for the best available bench player who is
    BOTH (a) eligible for the vacated slot per their FULL CBS position
    eligibility (lineup_slots[i]["eligible_positions"] -- e.g. a 2B/SS player
    can fill an SS slot even though he's rostered at 2B today) and (b)
    confirmed playing today per their own advice entry. Never proposes an
    illegal swap: eligibility is checked against the vacated slot (with
    LF/CF/RF normalized to OF, and any per-league DH-for-all rule already
    folded into eligible_positions upstream), and utility slots (UT/U) accept
    any batter.

    Each bench candidate is used for at most one swap. Returns a list of:
        {"out": name, "out_slot": slot, "in": name, "in_positions": [...],
         "reason": str}
    """
    by_name = {a.player_name: a for a in advice_list}
    slot_by_name = {s.get("player_name") or s.get("name", ""): s for s in lineup_slots}

    # Slots that need to be filled: currently-active players advised to sit.
    vacated = [
        a for a in advice_list
        if a.is_starting and a.advice in ("bench", "bench_pitcher", "on_il", "out_of_lineup")
    ]

    # Bench candidates: currently-inactive players confirmed playing today
    # (advice "start" -- i.e. should be activated) and not on IL.
    candidates = [
        a for a in advice_list
        if not a.is_starting and a.advice == "start"
    ]

    used_candidates: set[str] = set()
    swaps: list[dict] = []

    def _slot_legal(candidate_advice: LineupAdvice, slot: str) -> bool:
        slot_norm = "OF" if slot in ("LF", "CF", "RF") else slot
        if slot_norm in _BATTER_SLOTS_ANY and not candidate_advice.is_pitcher:
            return True
        info = slot_by_name.get(candidate_advice.player_name, {})
        eligible = info.get("eligible_positions") or candidate_advice.positions
        eligible_norm = {("OF" if p in ("LF", "CF", "RF") else p) for p in eligible}
        return slot_norm in eligible_norm

    for out_advice in vacated:
        best = None
        for cand in candidates:
            if cand.player_name in used_candidates:
                continue
            # A pitcher slot can only be filled by a pitcher, and vice versa.
            if out_advice.is_pitcher != cand.is_pitcher:
                continue
            if not _slot_legal(cand, out_advice.slot):
                continue
            best = cand
            break
        if best is None:
            continue
        used_candidates.add(best.player_name)
        swaps.append({
            "out":          out_advice.player_name,
            "out_slot":     out_advice.slot,
            "out_reason":   out_advice.reason,
            "in":           best.player_name,
            "in_positions": slot_by_name.get(best.player_name, {}).get(
                                "eligible_positions", best.positions),
            "reason": (
                f"{out_advice.player_name} ({out_advice.slot}) should sit -- "
                f"{best.player_name} is eligible for {out_advice.slot} and "
                f"confirmed playing today"
            ),
        })

    return swaps


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
