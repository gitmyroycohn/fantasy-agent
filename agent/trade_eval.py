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

---------------------------------------------------------------------------
Bug fixes (2026-08-01, Cowork):

1. Category mismap: the per-player scoring loop used to iterate over the
   league's FULL scoring_cats list (hitting + pitching combined) regardless
   of the player's own position type. A missing stat defaulted to 0.0 via
   `stats.get(fp_key, 0.0)`, which for ERA/WHIP (lower-is-better) turned a
   hitter's *absence* of pitching stats into a large fabricated POSITIVE
   score, and turned a pitcher's *absence* of hitting stats into a large
   fabricated NEGATIVE score. Fixed by only scoring categories that belong
   to the player's own type (see _HITTER_CATS / _PITCHER_CATS below).

2. Unmatched players (including non-player assets like draft picks) got the
   same "default to 0.0 per missing stat" treatment as above, producing
   fabricated values (~ -5 to -6.2 net) instead of being excluded. Fixed:
   any player not found in FP data is now explicitly excluded from scoring
   (total=0.0, cat_scores={}, excluded=True) and surfaced separately in the
   result/summary rather than silently contributing fake value.

3. Category-name mismatch against config/leagues.yaml: the league configs
   use "S" for saves and "BA" for Casey Stengel's batting-average category
   (confirmed in config/leagues.yaml's own comments, cross-checked against
   CBS's live league/scoring/live categories). This module's _FP_KEY /
   _BASELINE dicts only had "SV" and "AVG", so saves and Casey Stengel's
   batting average were SILENTLY skipped in every trade evaluation. Also,
   Pins and Pills' "H" (raw hits) category had no FP-stat mapping at all.
   Fixed: added "S" and "BA" as aliases, and added fp_h / "H" support.
   NOTE: INNdGS, QS, HLD, K_BB, TB, XBH still have no reliable ROS-projection
   analogue and are intentionally left unscored (skipped) rather than
   guessed at -- category fit for those should still be checked manually.

4. FP name-matching misses (e.g. a real active player resolving to
   "unknown"): _fp_candidates() now tries a few common cross-source name
   variants (exact norm, suffix-stripped norm for "Jr./Sr./II/III/IV", and
   swapped "Last, First" order) on both the FP-side index and the query
   name. This is a best-effort hardening pass -- the live FantasyPros API
   was not reachable from the environment this fix was written in, so the
   exact cause of any specific historical miss (e.g. Wheeler) could not be
   directly confirmed. Recommend running a live evaluate_trade_tool call
   with a previously-missed player to confirm before fully trusting this.
---------------------------------------------------------------------------
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

# Which league scoring-category names belong to which player type. Used to
# make sure a hitter is never scored on pitching categories (and vice
# versa) even if the league's combined hitting+pitching cat list is passed
# in wholesale. Keep in sync with config/leagues.yaml's actual category
# names -- both leagues use "S" (not "SV") for saves, and Casey Stengel
# uses "BA" (not "AVG") for batting average.
_HITTER_CATS = {"H", "HR", "R", "RBI", "SB", "AVG", "BA", "OPS", "TB", "XBH"}
_PITCHER_CATS = {"W", "S", "SV", "K", "ERA", "WHIP", "QS", "HLD", "INNdGS", "K_BB"}

# Per-category baseline replacement values (used for normalisation)
_BASELINE = {
    "H": 70, "HR": 15, "R": 60, "RBI": 55, "SB": 10,
    "AVG": 0.250, "BA": 0.250, "OPS": 0.700,
    "TB": 150, "XBH": 35,
    "W": 8, "SV": 15, "S": 15, "K": 100, "ERA": 4.20, "WHIP": 1.30,
    "QS": 12, "HLD": 12, "INNdGS": 90, "K_BB": 2.5,
}

# FP projection key for each scoring category. "S" and "BA" are aliases
# for the same underlying FP stats as "SV" and "AVG" -- CBS's category
# labels differ from FantasyPros' but the stat is identical.
_FP_KEY = {
    "H": "fp_h", "HR": "fp_hr", "R": "fp_r", "RBI": "fp_rbi", "SB": "fp_sb",
    "AVG": "fp_avg", "BA": "fp_avg", "OPS": "fp_ops",
    "W": "fp_w", "SV": "fp_sv", "S": "fp_sv", "K": "fp_k",
    "ERA": "fp_era", "WHIP": "fp_whip",
    # INNdGS, QS, HLD, K_BB, TB, XBH intentionally omitted -- no FP ROS
    # projection field maps cleanly onto these; leaving them unscored is
    # safer than guessing. See module docstring bug-fix note 3.
}

# Savant quality keys for display
_SAV_DISPLAY = {
    "batter": [("sv_xwoba", "xwOBA"), ("sv_barrel_pct", "Brl%"),
               ("sv_hard_hit_pct", "HH%")],
    "pitcher": [("sv_xera", "xERA"), ("sv_era_diff", "ERA-xERA")],
}

_PITCHER_POS = {"SP", "RP", "P"}

_SUFFIX_RE = re.compile(r"\s+(Jr\.?|Sr\.?|II|III|IV)$", re.IGNORECASE)


from mlb.teams import norm_name as _norm


def _fp_candidates(name: str) -> list[str]:
    """Generate candidate normalized keys for cross-source name matching.

    Handles a couple of common formatting mismatches between data sources:
    a trailing generational suffix present on one side but not the other,
    and "Last, First" ordering. Returns candidates in preference order with
    duplicates removed.
    """
    name = (name or "").strip()
    candidates = [_norm(name)]

    stripped = _SUFFIX_RE.sub("", name)
    if stripped != name:
        candidates.append(_norm(stripped))

    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            candidates.append(_norm(f"{parts[1]} {parts[0]}"))

    seen = set()
    out = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _index_fp_player(combined: dict, name: str, pos_type: str, proj: dict) -> None:
    """Index one FP player dict under every candidate normalized key so
    lookups from either name-formatting convention succeed."""
    for key in _fp_candidates(name):
        combined[key] = (pos_type, proj)


def _fetch_fp_all(fp_client: FantasyProsClient) -> dict:
    """Fetch all FP projections once, keyed by every candidate norm name
    (see _fp_candidates) so downstream lookups tolerate suffix / ordering
    differences between data sources."""
    combined = {}
    # H first so pitcher entries override on key collision (multi-eligible).
    for p in fp_client.hitter_projections("H"):
        _index_fp_player(combined, p.get("name", ""), "H", p)
    for p in fp_client.rp_projections():
        _index_fp_player(combined, p.get("name", ""), "RP", p)
    for p in fp_client.sp_projections():
        _index_fp_player(combined, p.get("name", ""), "SP", p)
    return combined  # {norm_name_variant: (pos_type, proj_dict)}


def _lookup_fp(name: str, fp_all: dict):
    """Look up a player in fp_all trying every candidate key for `name`.
    Returns (pos_type, proj) or (None, None) if no candidate matches."""
    for key in _fp_candidates(name):
        if key in fp_all:
            return fp_all[key]
    return None, None


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
            "fp_h":   float(proj.get("h",    proj.get("hits", 0))       or 0),
        }


def _player_value(name: str, fp_all: dict, sav_client: SavantClient,
                  scoring_cats: list[str]) -> dict:
    """Compute per-player value dict.

    Returns:
      {
        "name":      str,
        "found_fp":  bool,
        "excluded":  bool,        # True if no FP match -- not scored
        "pos_type":  str,         # SP / RP / H / unknown
        "stats":     {fp_*: val, sv_*: val},
        "cat_scores":{cat: normalised_score},  # positive = above baseline
        "total":     float,       # weighted sum across scoring cats
        "savant":    {label: val},
      }
    """
    result = {
        "name": name, "found_fp": False, "excluded": True, "pos_type": "unknown",
        "stats": {}, "cat_scores": {}, "total": 0.0, "savant": {},
    }

    pos_type, proj = _lookup_fp(name, fp_all)
    if pos_type is None:
        # No FP match -- likely a non-player asset (draft pick) or a genuine
        # name-matching miss. Either way, do NOT fabricate a value for it:
        # leave total/cat_scores empty and flag it as excluded so callers
        # can surface it separately instead of silently scoring the trade
        # as if this asset were worth exactly 0.
        return result

    stats = _fp_stats(pos_type, proj)
    result.update({
        "found_fp": True, "excluded": False, "pos_type": pos_type, "stats": stats,
    })

    # -- Savant xStats
    is_pitcher = pos_type in ("SP", "RP")
    sav_data = (sav_client.pitcher_data(name) if is_pitcher
                else sav_client.batter_data(name))
    if sav_data:
        result["stats"].update(sav_data)
        sav_keys = _SAV_DISPLAY["pitcher"] if is_pitcher else _SAV_DISPLAY["batter"]
        for key2, label in sav_keys:
            v = sav_data.get(key2)
            if v is not None:
                result["savant"][label] = round(v, 3)

    # -- Per-category normalised scores, restricted to categories that
    # actually apply to this player's type (bug fix 1: no more scoring
    # hitters on pitching cats or pitchers on hitting cats).
    relevant_cats = _PITCHER_CATS if is_pitcher else _HITTER_CATS
    stats = result["stats"]
    total = 0.0
    for cat in scoring_cats:
        if cat not in relevant_cats:
            continue
        fp_key = _FP_KEY.get(cat)
        if not fp_key or fp_key not in stats:
            continue
        val      = stats[fp_key]
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
        "verdict":  "ACCEPT" | "DECLINE" | "CLOSE" | "INSUFFICIENT DATA",
        "summary":  str,
        "give":     [player_value, ...],
        "receive":  [player_value, ...],
        "cat_delta":{cat: delta},   # positive = receive better
        "net_score": float,
        "league":   str,
        "unscored": [name, ...],    # give+receive assets with no FP match
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

    unscored = [pv["name"] for pv in give_vals + receive_vals if pv["excluded"]]
    for pv in give_vals + receive_vals:
        if pv["excluded"]:
            logger.info("trade_eval: %r excluded from scoring (no FP match -- "
                        "verify whether this is a non-player asset like a "
                        "draft pick, or a genuine name-matching miss)",
                        pv["name"])

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

    scored_give    = [pv for pv in give_vals if not pv["excluded"]]
    scored_receive = [pv for pv in receive_vals if not pv["excluded"]]

    # Verdict
    if not scored_give and not scored_receive:
        verdict = "INSUFFICIENT DATA"
        summary = "Could not find FP projections for any of the players involved."
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
        if unscored:
            summary += (f" NOTE: {len(unscored)} asset(s) had no FP match and "
                        f"were excluded from scoring -- {', '.join(unscored)}. "
                        f"Evaluate those manually (likely a draft pick, "
                        f"prospect, or a name-matching miss).")

    return {
        "verdict":   verdict,
        "summary":   summary,
        "give":      give_vals,
        "receive":   receive_vals,
        "cat_delta": cat_delta,
        "net_score": net_score,
        "league":    league_name,
        "unscored":  unscored,
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
            if pv["excluded"]:
                lines.append(f"  {pv['name']} [excluded -- no FP match, "
                              f"not scored; evaluate manually]")
                continue
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
