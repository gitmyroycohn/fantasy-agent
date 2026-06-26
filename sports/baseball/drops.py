"""
Drop candidate identification for CBS fantasy baseball.

A player is a drop candidate when their production is below the position
replacement threshold. Scarce positions (C, SS) require a confirmed wire
replacement before flagging as CUT.

Savant xStats are used to soften recommendations for unlucky players:
  - Batter with low AVG but xBA above floor -> MONITOR not CUT
  - Pitcher with high ERA but xERA below floor -> MONITOR not CUT
"""
import logging
from data.models import RosterSlot, WaiverPlayer

logger = logging.getLogger(__name__)

_BAT_FLOOR = {
    "AVG":  0.225,
    "HR":   5,
    "R":    25,
    "RBI":  20,
    "SB":   4,
    "OPS":  0.650,
}

_PITCH_FLOOR = {
    "ERA":  5.50,
    "WHIP": 1.55,
    "K":    25,
    "IP":   20.0,
}

_SP_MIN_IP           = 15.0
_BAT_FAIL_THRESHOLD  = 3
_PITCH_FAIL_THRESHOLD = 2
_PITCHER_POS         = {"SP", "RP", "P"}
_IL_STATUS           = {"DL", "IL", "DTD", "SUSP", "NA"}
_SCARCE_POS          = {"C", "SS"}


def find_drop_candidates(roster, waiver_wire, nl_only=False, stash_names=None):
    """
    Evaluate each roster player and return drop candidates.

    Returns list of:
      {player, team, positions, slot, is_starting, severity, reason, replace_with}

    severity: "cut" or "monitor"

    stash_names: optional set/list of player names to never flag (prospect stash).
    """
    from mlb.teams import norm_name as _norm
    stash_set = {_norm(n) for n in (stash_names or [])}

    drops = []

    for rs in roster:
        p = rs.player
        if p.status in _IL_STATUS:
            continue
        if not rs.is_starting:
            continue
        if not p.stats:
            continue
        # Skip players the manager is intentionally holding as prospect stash
        if stash_set and _norm(p.name) in stash_set:
            continue

        is_pitcher = bool(set(p.positions) & _PITCHER_POS)
        result = _evaluate_pitcher(p) if is_pitcher else _evaluate_batter(p)
        if not result:
            continue

        severity, reason = result
        replacement = _find_replacement(p, waiver_wire, is_pitcher)

        if severity == "cut" and set(p.positions) & _SCARCE_POS and not replacement:
            severity = "monitor"
            reason   = f"[scarce pos] {reason}"

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


def _evaluate_batter(player):
    s   = player.stats
    h   = s.get("H", 0) or 0
    if h < 10:
        return None

    avg = s.get("AVG") or 0.0
    hr  = s.get("HR")  or 0
    r   = s.get("R")   or 0
    rbi = s.get("RBI") or 0
    sb  = s.get("SB")  or 0
    ops = s.get("OPS") or 0.0

    sv_xba = s.get("sv_xba")

    fails    = []
    xba_pass = False

    if avg < _BAT_FLOOR["AVG"]:
        if sv_xba is not None and sv_xba >= _BAT_FLOOR["AVG"]:
            # Hitting below average but contact quality is fine -- unlucky
            fails.append(f"AVG {avg:.3f} [xBA {sv_xba:.3f} OK -- unlucky]")
            xba_pass = True
        else:
            fails.append(f"AVG {avg:.3f}")
    if hr  < _BAT_FLOOR["HR"]:   fails.append(f"HR {hr}")
    if r   < _BAT_FLOOR["R"]:    fails.append(f"R {r}")
    if rbi < _BAT_FLOOR["RBI"]:  fails.append(f"RBI {rbi}")
    if sb  < _BAT_FLOOR["SB"]:   fails.append(f"SB {sb}")
    if ops < _BAT_FLOOR["OPS"]:  fails.append(f"OPS {ops:.3f}")

    n = len(fails)
    if xba_pass and n >= _BAT_FAIL_THRESHOLD + 1:
        return ("monitor", f"Unlucky (xBA OK): {', '.join(fails[:3])}")
    if n >= _BAT_FAIL_THRESHOLD + 1:
        return ("cut", f"Below replacement: {', '.join(fails[:4])}")
    if n >= _BAT_FAIL_THRESHOLD:
        return ("monitor", f"Borderline: {', '.join(fails[:3])}")
    return None


def _evaluate_pitcher(player):
    s    = player.stats
    ip   = s.get("IP")   or 0.0
    era  = s.get("ERA")  or 0.0
    whip = s.get("WHIP") or 0.0
    k    = s.get("K")    or 0

    if ip < 5:
        return None

    sv_xera     = s.get("sv_xera")
    sv_era_diff = s.get("sv_era_diff")  # positive = ERA worse than xERA = unlucky

    fails     = []
    xera_pass = False

    if era > _PITCH_FLOOR["ERA"]:
        if (sv_xera is not None and sv_xera < _PITCH_FLOOR["ERA"]
                and sv_era_diff is not None and sv_era_diff > 0.75):
            fails.append(f"ERA {era:.2f} [xERA {sv_xera:.2f} OK -- unlucky]")
            xera_pass = True
        else:
            fails.append(f"ERA {era:.2f}")
    if whip > _PITCH_FLOOR["WHIP"]:  fails.append(f"WHIP {whip:.2f}")
    if ip   < _PITCH_FLOOR["IP"]:    fails.append(f"only {ip:.1f} IP")
    if k    < _PITCH_FLOOR["K"] and ip >= _SP_MIN_IP:
        fails.append(f"K {k}")

    n = len(fails)
    if xera_pass and n >= _PITCH_FAIL_THRESHOLD + 1:
        return ("monitor", f"Unlucky (xERA OK): {', '.join(fails[:2])}")
    if n >= _PITCH_FAIL_THRESHOLD + 1:
        return ("cut", f"Below replacement: {', '.join(fails[:3])}")
    if n >= _PITCH_FAIL_THRESHOLD:
        return ("monitor", f"Borderline: {', '.join(fails[:2])}")
    return None


def _find_replacement(player, waiver_wire, is_pitcher):
    pos_set    = set(player.positions)
    candidates = []

    for wp in waiver_wire:
        if not set(wp.player.positions) & pos_set:
            continue
        if not wp.player.stats:
            continue
        s = wp.player.stats
        if is_pitcher:
            ip  = s.get("IP")  or 0.0
            era = s.get("ERA") or 99.0
            k   = s.get("K")   or 0
            if ip >= _SP_MIN_IP and era < 4.50 and k >= 20:
                score = k - era * 5
                candidates.append((score, wp.player.name))
        else:
            ops = s.get("OPS") or 0.0
            h   = s.get("H")   or 0
            if h >= 15 and ops > 0.700:
                score = ops * 100 + (s.get("HR") or 0) * 2
                candidates.append((score, wp.player.name))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]
