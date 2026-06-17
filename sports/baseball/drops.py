"""
Drop candidate identification for CBS fantasy baseball.

A player is a drop candidate when:
  1. Their production is below the position replacement threshold, AND
  2. They are not at a scarce position (C, SS) unless a clear replacement exists

Output: list of {player, reason, severity, replace_with} dicts.
"""
import logging
from data.models import RosterSlot, WaiverPlayer

logger = logging.getLogger(__name__)

# ---- Replacement-level thresholds (season totals, ~2026 pace) ----

_BAT_FLOOR = {
    "AVG":  0.225,
    "HR":   5,
    "R":    25,
    "RBI":  20,
    "SB":   4,
    "OPS":  0.650,
    "H":    35,
}

_PITCH_FLOOR = {
    "ERA":   5.50,   # upper bound
    "WHIP":  1.55,   # upper bound
    "K":     25,     # lower bound
    "IP":    20.0,
    "W":     1,
}

_SP_MIN_IP = 15.0

_BAT_FAIL_THRESHOLD  = 3
_PITCH_FAIL_THRESHOLD = 2

_PITCHER_POS = {"SP", "RP", "P"}
_IL_STATUS   = {"DL", "IL", "DTD", "SUSP", "NA"}

# Positions where replacements are hard to find -- require confirmed replacement
# before flagging as CUT (will still flag as MONITOR if struggling)
_SCARCE_POS = {"C", "SS"}


def find_drop_candidates(
    roster: list[RosterSlot],
    waiver_wire: list[WaiverPlayer],
    nl_only: bool = False,
) -> list[dict]:
    """
    Evaluate each roster player and flag weak ones as drop candidates.

    Returns list of dicts:
      {player, team, positions, slot, reason, severity, replace_with}

    severity: "cut" (clear drop) or "monitor" (borderline)
    """
    drops = []

    for rs in roster:
        p = rs.player

        # Skip IL / stashed players
        if p.status in _IL_STATUS:
            continue
        # Skip pure bench players
        if rs.slot == "BN" and not rs.is_starting:
            continue

        is_pitcher = bool(set(p.positions) & _PITCHER_POS)

        if not p.stats:
            continue

        if is_pitcher:
            result = _evaluate_pitcher(p)
        else:
            result = _evaluate_batter(p)

        if result:
            severity, reason = result
            replacement = _find_replacement(p, waiver_wire, is_pitcher)
            pos_set = set(p.positions)

            # Downgrade scarce positions from CUT to MONITOR
            # unless we have a confirmed wire replacement
            if severity == "cut" and pos_set & _SCARCE_POS and not replacement:
                severity = "monitor"
                reason = f"[scarce pos] {reason}"

            drops.append({
                "player":       p.name,
                "team":         p.team,
                "positions":    p.positions,
                "slot":         rs.slot,
                "is_starting":  rs.is_starting,
                "severity":     severity,
                "reason":       reason,
                "replace_with": replacement,
            })

    drops.sort(key=lambda d: (0 if d["severity"] == "cut" else 1,
                               0 if d["is_starting"] else 1))
    return drops


def _evaluate_batter(player) -> tuple | None:
    s = player.stats
    h = s.get("H", 0)
    if h < 10:
        return None

    avg  = s.get("AVG", 0.0)
    hr   = s.get("HR", 0)
    r    = s.get("R", 0)
    rbi  = s.get("RBI", 0)
    sb   = s.get("SB", 0)
    ops  = s.get("OPS", 0.0)

    fails = []
    if avg < _BAT_FLOOR["AVG"]:   fails.append(f"AVG {avg:.3f}")
    if hr  < _BAT_FLOOR["HR"]:    fails.append(f"HR {hr}")
    if r   < _BAT_FLOOR["R"]:     fails.append(f"R {r}")
    if rbi < _BAT_FLOOR["RBI"]:   fails.append(f"RBI {rbi}")
    if sb  < _BAT_FLOOR["SB"]:    fails.append(f"SB {sb}")
    if ops < _BAT_FLOOR["OPS"]:   fails.append(f"OPS {ops:.3f}")

    n = len(fails)
    if n >= _BAT_FAIL_THRESHOLD + 1:
        return ("cut", f"Below replacement: {', '.join(fails[:4])}")
    if n >= _BAT_FAIL_THRESHOLD:
        return ("monitor", f"Borderline: {', '.join(fails[:3])}")
    return None


def _evaluate_pitcher(player) -> tuple | None:
    s = player.stats
    ip   = s.get("IP", 0.0)
    era  = s.get("ERA", 0.0)
    whip = s.get("WHIP", 0.0)
    k    = s.get("K", 0)

    if ip < 5:
        return None

    fails = []
    if era  > _PITCH_FLOOR["ERA"]:    fails.append(f"ERA {era}")
    if whip > _PITCH_FLOOR["WHIP"]:   fails.append(f"WHIP {whip}")
    if ip   < _PITCH_FLOOR["IP"]:     fails.append(f"only {ip} IP")
    if k    < _PITCH_FLOOR["K"] and ip >= _SP_MIN_IP:
        fails.append(f"K {k}")

    n = len(fails)
    if n >= _PITCH_FAIL_THRESHOLD + 1:
        return ("cut", f"Below replacement: {', '.join(fails[:3])}")
    if n >= _PITCH_FAIL_THRESHOLD:
        return ("monitor", f"Borderline: {', '.join(fails[:2])}")
    return None


def _find_replacement(player, waiver_wire: list[WaiverPlayer],
                      is_pitcher: bool) -> str | None:
    pos_set = set(player.positions)
    candidates = []

    for wp in waiver_wire:
        if not set(wp.player.positions) & pos_set:
            continue
        if not wp.player.stats:
            continue
        s = wp.player.stats
        if is_pitcher:
            ip  = s.get("IP", 0.0)
            era = s.get("ERA", 99.0)
            k   = s.get("K", 0)
            if ip >= _SP_MIN_IP and era < 4.50 and k >= 20:
                score = k - era * 5
                candidates.append((score, wp.player.name))
        else:
            ops = s.get("OPS", 0.0)
            h   = s.get("H", 0)
            if h >= 15 and ops > 0.700:
                score = ops * 100 + s.get("HR", 0) * 2
                candidates.append((score, wp.player.name))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]
