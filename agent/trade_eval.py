"""
Trade evaluator.

Given lists of player names going out (give) and coming in (receive),
fetches FP ROS projections + Savant xStats for each player, computes
a per-category delta, and returns a structured verdict.

Verdict:
  ACCEPT  -- receive package value >= give package value by >15%
  DECLINE -- give package value > receive by >15%
  CLOSE   -- within 15% either way; include detailed breakdown

Usage (via MCP tool or CLI):
    from agent.trade_eval import evaluate_trade
    result = evaluate_trade(
        give=["Jarren Duran", "Hunter Brown"],
        receive=["Rafael Devers"],
        league_cfg=cfg,      # league dict from leagues.yaml
        fp_client=...,
        sav_client=...,
    )
"""
import logging
import re

from fantasypros.client import FantasyProsClient
from savant.client import SavantClient

logger = logging.getLogger(__name__)

# Verdict thresholds
_ACCEPT_THRESHOLD = 0.15   # receive > give by 15% -> ACCEPT
_DECLINE_THRESHOLD = 0.15  # give > receive by 15% -> DECLINE

# Categories where lower is better (pitcher stats)
_LOWER_IS_BETTER = {"ERA", "WHIP"}

# Per-category baseline replacement values (used for normalisation)
_BASELINE = {
    "HR": 15, "R": 60, "RBI": 55, "SB": 10, "AVG": 0.250, "OPS": 0.700,
    "TB": 150, "XBH": 35,
    "W": 8, "SV": 15, "K": 100, "ERA": 4.20, "WHIP": 1.30,
    "QS": 12, "HLD": 12, "INNdGS": 90, "K_BB": 2.5,
}

# FP projection key for each scoring category
_FP_KEY = {
    "HR": "fp_hr", "R": "fp_r", "RBI": "fp_rbi", "SB": "fp_sb",
    "AVG": "fp_avg", "OPS": "fp_ops",
    "W": "fp_w", "SV": "fp_sv", "K": "fp_k",
    "ERA": "fp_era", "WHIP": "fp_whip",
}

# Savant quality keys for display
_SAV_DISPLAY = {
    "batter": [("sv_xwoba", "xwOBA"), ("sv_barrel_pct", "Brl%"),
               ("sv_hard_hit_pct", "HH%")],
    "pitcher": [("sv_xera", "xERA"), ("sv_era_diff", "ERA-xERA")],
}

_PITCHER_POS = {"SP", "RP", "P"}


from mlb.teams import norm_name as _norm


def _fetch_fp_all(fp_client: FantasyProsClient) -> dict:
    """Fetch all FP projections once, keyed by norm name."""
    combined = {}
    sp = {_norm(p.get("name", "")): ("SP", p) for p in fp_client.sp_projections()}
    rp = {_norm(p.get("name", "")): ("RP", p) for p in fp_client.rp_projections()}
    h  = {_norm(p.get("name", "")): ("H",  p) for p in fp_client.hitter_projections("H")}
    # H first so pitcher overrides if multi-eligible
    combined.update(h)
    combined.update(rp)
    combined.update(sp)
    return combined  # {norm_name: (pos_type, proj_dict)}


def _fp_stats(pos_type: str, proj: dict) -> dict:
    """Extract fp_* keyed stats from a FP projection dict."""
    if pos_type in ("SP", "RP"):
        return {
            "fp_sv":   float(proj.get("sv",   proj.get("saves", 0)) or 0),
            "fp_k":    float(proj.get("k",    proj.get("so", 0))    or 0),
            "fp_era":  float(proj.get("era",  proj.get("ERA", 0.0)) or 0),
            "fp_whip": float(proj.get("whip", proj.get("WHIP", 0.0)) or 0),
            "fp_ip":   float(proj.get("ip",   0)                    or 0),
            "fp_w":    float(proj.get("w",    proj.get("wins", 0))  or 0),
        }
    else:
        return {
            "fp_hr":  float(proj.get("hrs",  proj.get("hr", 0))         or 0),
            "fp_r":   float(proj.get("runs", proj.get("r", 0))          or 0),
            "fp_rbi": float(proj.get("rbi",  0)                         or 0),
            "fp_sb":  float(proj.get("sb",   0)                         or 0),
            "fp_avg": float(proj.get("ave",  proj.get("avg", 0.0))      or 0),
            "fp_ops": float(proj.get("ops",  0.0)                       or 0),
        }


def _player_value(name: str, fp_all: dict, sav_client: SavantClient,
                  scoring_cats: list[str]) -> dict:
    """Compute per-player value dict.

    Returns:
      {
        "name":      str,
        "found_fp":  bool,
        "pos_type":  str,         # SP / RP / H / unknown
        "stats":     {fp_*: val, sv_*: val},
        "cat_scores":{cat: normalised_score},  # positive = above baseline
        "total":     float,       # weighted sum across scoring cats
        "savant":    {label: val},
      }
    """
    key = _norm(name)
    result = {
        "name": name, "found_fp": False, "pos_type": "unknown",
        "stats": {}, "cat_scores": {}, "total": 0.0, "savant": {},
    }

    # -- FP projections
    if key in fp_all:
        pos_type, proj = fp_all[key]
        stats = _fp_stats(pos_type, proj)
        result.update({"found_fp": True, "pos_type": pos_type, "stats": stats})

    # -- Savant xStats
    is_pitcher = result["pos_type"] in ("SP", "RP")
    sav_data = (sav_client.pitcher_data(name) if is_pitcher
                else sav_client.batter_data(name))
    if sav_data:
        result["stats"].update(sav_data)
        sav_keys = _SAV_DISPLAY["pitcher"] if is_pitcher else _SAV_DISPLAY["batter"]
        for key2, label in sav_keys:
            v = sav_data.get(key2)
            if v is not None:
                result["savant"][label] = round(v, 3)

    # -- Per-category normalised scores
    stats = result["stats"]
    total = 0.0
    for cat in scoring_cats:
        fp_key = _FP_KEY.get(cat)
        if not fp_key:
            continue
        val      = stats.get(fp_key, 0.0)
        baseline = _BASELINE.get(cat, 1.0)
        if baseline == 0:
            continue
        if cat in _LOWER_IS_BETTER:
            # Lower ERA = better; score = how much better than baseline
            score = (baseline - val) / baseline
        else:
            score = (val - baseline) / baseline
        result["cat_scores"][cat] = round(score, 3)
        total += score
    result["total"] = round(total, 3)
    return result


def evaluate_trade(give: list[str], receive: list[str],
                   league_cfg: dict,
                   fp_client: FantasyProsClient,
                   sav_client: SavantClient) -> dict:
    """
    Evaluate a proposed trade.

    Returns:
      {
        "verdict":  "ACCEPT" | "DECLINE" | "CLOSE",
        "summary":  str,
        "give":     [player_value, ...],
        "receive":  [player_value, ...],
        "cat_delta":{cat: delta},   # positive = receive better
        "net_score": float,
        "league":   str,
      }
    """
    scoring = league_cfg.get("scoring", {})
    scoring_cats = (list(scoring.get("hitting", [])) +
                    list(scoring.get("pitching", [])))
    league_name = league_cfg.get("name", league_cfg.get("cbs_league_id", "?"))

    # Fetch FP projections once
    try:
        fp_all = _fetch_fp_all(fp_client)
    except Exception as e:
        logger.warning("trade_eval: FP fetch failed: %s", e)
        fp_all = {}

    # Evaluate each player
    give_vals    = [_player_value(n, fp_all, sav_client, scoring_cats) for n in give]
    receive_vals = [_player_value(n, fp_all, sav_client, scoring_cats) for n in receive]

    # Per-category delta (receive - give); positive = receive wins that cat
    all_cats = set()
    for pv in give_vals + receive_vals:
        all_cats.update(pv["cat_scores"].keys())

    cat_delta = {}
    for cat in sorted(all_cats):
        give_sum    = sum(pv["cat_scores"].get(cat, 0.0) for pv in give_vals)
        receive_sum = sum(pv["cat_scores"].get(cat, 0.0) for pv in receive_vals)
        cat_delta[cat] = round(receive_sum - give_sum, 3)

    give_total    = sum(pv["total"] for pv in give_vals)
    receive_total = sum(pv["total"] for pv in receive_vals)
    net_score     = round(receive_total - give_total, 3)

    # Verdict
    if give_total == 0 and receive_total == 0:
        verdict = "INSUFFICIENT DATA"
        summary = "Could not find FP projections for the players involved."
    else:
        denom = max(abs(give_total), abs(receive_total), 0.01)
        ratio = net_score / denom
        if ratio >= _ACCEPT_THRESHOLD:
            verdict = "ACCEPT"
            summary = (f"Receive package is stronger. "
                       f"Net score: +{net_score:+.2f} in your favour.")
        elif ratio <= -_DECLINE_THRESHOLD:
            verdict = "DECLINE"
            summary = (f"Give package is stronger -- you'd be losing value. "
                       f"Net score: {net_score:+.2f}.")
        else:
            verdict = "CLOSE"
            summary = (f"Nearly even trade. Net score: {net_score:+.2f}. "
                       f"Check category fit before deciding.")

    return {
        "verdict":   verdict,
        "summary":   summary,
        "give":      give_vals,
        "receive":   receive_vals,
        "cat_delta": cat_delta,
        "net_score": net_score,
        "league":    league_name,
    }


def format_trade_result(result: dict) -> str:
    """Format evaluate_trade result as readable text for MCP/CLI output."""
    lines = []
    verdict = result["verdict"]
    verdict_icon = {"ACCEPT": "✅", "DECLINE": "❌", "CLOSE": "🔶",
                    "INSUFFICIENT DATA": "⚠️"}.get(verdict, "")

    lines.append(f"=== Trade Evaluation: {result['league']} ===")
    lines.append(f"Verdict: {verdict_icon} {verdict}")
    lines.append(f"Summary: {result['summary']}")
    lines.append("")

    for side, players in (("YOU GIVE", result["give"]),
                           ("YOU RECEIVE", result["receive"])):
        lines.append(f"--- {side} ---")
        for pv in players:
            fp_note = "" if pv["found_fp"] else " [no FP data]"
            sav_str = ""
            if pv["savant"]:
                sav_str = "  " + " | ".join(
                    f"{k}={v}" for k, v in pv["savant"].items()
                )
            lines.append(f"  {pv['name']} [{pv['pos_type']}]{fp_note}"
                          f"  value={pv['total']:+.2f}{sav_str}")
            # Top category scores
            top_cats = sorted(pv["cat_scores"].items(),
                              key=lambda x: abs(x[1]), reverse=True)[:4]
            if top_cats:
                cat_str = "  ".join(f"{c}: {s:+.2f}" for c, s in top_cats)
                lines.append(f"    cats: {cat_str}")
        lines.append("")

    # Category delta table
    deltas = result.get("cat_delta", {})
    if deltas:
        lines.append("--- Category Impact (+ = receive wins, - = give wins) ---")
        for cat, delta in sorted(deltas.items(), key=lambda x: -abs(x[1])):
            bar = "▲" if delta > 0.05 else ("▼" if delta < -0.05 else "~")
            lines.append(f"  {bar} {cat:<8} {delta:+.3f}")

    return "\n".join(lines)
