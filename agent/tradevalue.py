"""
Buy-low / sell-high signal generator.

Compares a player's current season pace against their FP ROS projection
to identify:

  SELL HIGH  -- current pace significantly exceeds ROS projection
               (player likely to regress, trade now while value is high)

  BUY LOW    -- ROS projection significantly exceeds current pace
               (player underperforming but projection says bounce-back)

Works on roster players that have both season stats (from MLB Stats API)
and FP ROS projection keys (fp_* prefix, from FantasyPros enrichment).

Optionally layered with Savant xStats to strengthen or weaken the signal:
  - BUY LOW + xBA OK  -> strong buy (unlucky, real talent is there)
  - SELL HIGH + low barrel% -> strong sell (pace driven by luck, not skill)

Output per player:
  {name, team, positions, signal, reason, confidence}
  signal:     "sell_high" | "buy_low" | None
  confidence: "strong" | "moderate"
"""

import logging

logger = logging.getLogger(__name__)

# Minimum games played / PA to have a meaningful current pace
_MIN_PA  = 60
_MIN_IP  = 15.0

# Thresholds: how much better/worse must pace be vs projection to flag
_SELL_THRESHOLD = 0.20   # current pace >= 20% above projection -> sell high
_BUY_THRESHOLD  = 0.20   # projection   >= 20% above current pace -> buy low
_STRONG_MULT    = 0.35   # >= 35% gap -> strong signal

# Savant thresholds for layering
_GOOD_BARREL = 8.0      # above league avg barrel% -> real power
_LOW_BARREL  = 4.0      # below this -> luck-driven power

# Stat pairs: (season_key, fp_key, lower_is_better)
# fp_ keys are set by enrich_with_fp_projections; season keys by MLB Stats API
_BATTER_STATS = [
    ("HR",  "fp_hr",  False),
    ("RBI", "fp_rbi", False),
    ("R",   "fp_r",   False),
    ("SB",  "fp_sb",  False),
    ("AVG", "fp_avg", False),
]

_PITCHER_STATS = [
    ("K",    "fp_k",    False),
    ("ERA",  "fp_era",  True),
    ("WHIP", "fp_whip", True),
    ("SV",   "fp_sv",   False),
]

_PITCHER_POS = {"SP", "RP", "P"}


def _pace_ratio(current, projected, lower_is_better):
    """
    Return ratio of current vs projected.

    For normal stats: ratio > 1 means current is ABOVE projection (potential sell).
    For lower-is-better (ERA): ratio > 1 means current ERA is WORSE than projection.
    We flip so ratio > 1 always means "worse than projected" i.e. buy-low candidate.
    """
    if not projected or projected == 0:
        return None
    if lower_is_better:
        return float(projected) / float(current) if current else None
    return float(current) / float(projected)


def analyze_roster_value(roster_slots) -> list[dict]:
    """
    Evaluate roster players for buy-low / sell-high signals.

    Args:
        roster_slots: list of RosterSlot objects with .player.stats and .player.positions

    Returns:
        list of signal dicts, sorted sell-high first then buy-low,
        strongest signals first within each group.
    """
    signals = []

    for rs in roster_slots:
        p = rs.player
        if not p.stats:
            continue

        is_pitcher = bool(set(p.positions) & _PITCHER_POS)
        s          = p.stats

        pa = s.get("PA") or s.get("AB") or 0
        ip = s.get("IP") or 0.0

        # Skip players with too little data
        if is_pitcher and ip < _MIN_IP:
            continue
        if not is_pitcher and pa < _MIN_PA:
            continue

        stat_pairs = _PITCHER_STATS if is_pitcher else _BATTER_STATS
        above = []   # (gap, label) where current significantly above projection
        below = []   # (gap, label) where projection significantly above current

        for season_key, fp_key, lower_is_better in stat_pairs:
            current   = s.get(season_key)
            projected = s.get(fp_key)
            if current is None or projected is None:
                continue
            try:
                current   = float(current)
                projected = float(projected)
            except (TypeError, ValueError):
                continue
            if projected == 0:
                continue

            if lower_is_better:
                # ERA/WHIP: current > projected means worse than expected
                if current > 0:
                    gap = (current - projected) / projected
                    if gap >= _BUY_THRESHOLD:
                        # ERA worse than projection -> actually a buy-low for ERA improvement
                        below.append((gap, f"{season_key} {current:.2f} vs proj {projected:.2f}"))
                    elif gap <= -_SELL_THRESHOLD:
                        # ERA better than projection -> sell high on ERA
                        above.append((-gap, f"{season_key} {current:.2f} vs proj {projected:.2f}"))
            else:
                if projected > 0:
                    gap = (current - projected) / projected
                    if gap >= _SELL_THRESHOLD:
                        above.append((gap, f"{season_key} {current:.0f} vs proj {projected:.0f}"))
                    elif gap <= -_BUY_THRESHOLD:
                        below.append((-gap, f"{season_key} {current:.0f} vs proj {projected:.0f}"))

        if not above and not below:
            continue

        # Determine primary signal
        sell_score = sum(g for g, _ in above)
        buy_score  = sum(g for g, _ in below)

        if sell_score > buy_score and above:
            signal     = "sell_high"
            top_gaps   = sorted(above, reverse=True)[:3]
            raw_conf   = top_gaps[0][0]
            reasons    = [label for _, label in top_gaps]
        elif buy_score > sell_score and below:
            signal     = "buy_low"
            top_gaps   = sorted(below, reverse=True)[:3]
            raw_conf   = top_gaps[0][0]
            reasons    = [label for _, label in top_gaps]
        else:
            continue

        confidence = "strong" if raw_conf >= _STRONG_MULT else "moderate"

        # Layer Savant xStats
        savant_note = ""
        if not is_pitcher:
            barrel = s.get("sv_barrel_pct")
            xba    = s.get("sv_xba")
            ba     = s.get("AVG") or s.get("avg")
            if signal == "sell_high" and barrel is not None and barrel < _LOW_BARREL:
                savant_note = f" [low Brl%={barrel:.1f} -- luck-driven, strong sell]"
                confidence  = "strong"
            elif signal == "buy_low" and xba is not None and ba is not None:
                if xba > (ba or 0) + 0.020:
                    savant_note = f" [xBA {xba:.3f} > BA -- unlucky, strong buy]"
                    confidence  = "strong"
        else:
            xera     = s.get("sv_xera")
            era_diff = s.get("sv_era_diff")
            if signal == "buy_low" and xera is not None and era_diff is not None:
                if era_diff > 0.50:
                    savant_note = f" [xERA {xera:.2f} confirms ERA regression coming]"
                    confidence  = "strong"

        signals.append({
            "name":       p.name,
            "team":       p.team,
            "positions":  p.positions,
            "signal":     signal,
            "reason":     "; ".join(reasons) + savant_note,
            "confidence": confidence,
            "_sort":      (0 if signal == "sell_high" else 1, -raw_conf),
        })

    signals.sort(key=lambda x: x["_sort"])
    for s in signals:
        s.pop("_sort", None)
    return signals
