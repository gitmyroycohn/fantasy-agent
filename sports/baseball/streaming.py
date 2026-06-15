"""
Streaming SP logic for Pins & Pills (H2H categories).

Identifies the best available SPs to add before the weekly Monday lock,
targeting pitchers who contribute W, K, QS, and keep ERA/WHIP manageable.
"""
import logging
from data.models import WaiverPlayer, Player  # noqa: F401 (Player used by type hints)
from config.settings import MIN_SP_OWNERSHIP_DROP, MAX_ERA_STREAMER, MIN_K9_STREAMER

logger = logging.getLogger(__name__)


def rank_streaming_sps(
    waiver_players: list[WaiverPlayer],
    current_cat_standings: dict,
    max_results: int = 5,
) -> list[dict]:
    """
    Score and rank available SPs for streaming.
    Returns a list of recommendation dicts, best first.
    """
    sp_candidates = [
        wp for wp in waiver_players
        if "SP" in wp.player.positions
        and wp.ownership_pct < MIN_SP_OWNERSHIP_DROP
    ]

    scored = []
    for wp in sp_candidates:
        score = _score_sp(wp.player, current_cat_standings)
        if score > 0:
            scored.append({
                "player": wp.player.name,
                "team": wp.player.team,
                "ownership": wp.ownership_pct,
                "score": round(score, 2),
                "reason": _reason(wp.player, current_cat_standings),
            })

    scored.sort(key=lambda x: x["score"], reverse=True)
    logger.info("Ranked %d SP streaming candidates", len(scored))
    return scored[:max_results]


def _score_sp(player: Player, standings: dict) -> float:
    """
    Heuristic score based on:
    - Projected K/9 contribution
    - ERA relative to threshold
    - Whether we're losing W, K, QS categories
    """
    stats = player.stats
    era   = stats.get("ERA", 99.0)
    k9    = stats.get("K9", 0.0)
    whip  = stats.get("WHIP", 9.0)
    ip    = stats.get("IP", 0.0)

    # Require at least 10 IP to avoid fluky ERA 0.0 from tiny samples
    if ip < 10 or era > MAX_ERA_STREAMER or k9 < MIN_K9_STREAMER:
        return 0.0

    score = 0.0

    # Reward pitchers who help losing categories
    if standings.get("K", {}).get("winning") is False:
        score += k9 * 0.4
    if standings.get("W", {}).get("winning") is False:
        score += 2.0
    if standings.get("QS", {}).get("winning") is False:
        score += 1.5
    if standings.get("ERA", {}).get("winning") is False and era < 3.50:
        score += 1.0
    if standings.get("WHIP", {}).get("winning") is False and whip < 1.15:
        score += 1.0

    # Small baseline for solid arms even in winning cats
    score += max(0, (4.50 - era) * 0.3)
    score += max(0, (k9 - MIN_K9_STREAMER) * 0.2)

    return score


def _reason(player: Player, standings: dict) -> str:
    parts = []
    stats = player.stats
    era   = stats.get("ERA", "?")
    k9    = stats.get("K9", "?")
    whip  = stats.get("WHIP", "?")

    parts.append(f"ERA {era}, K/9 {k9}, WHIP {whip}")

    _PITCHER_CATS = {"K", "W", "QS", "SV", "HLD", "ERA", "WHIP", "K9", "S"}
    losing_pitcher = [cat for cat, s in standings.items()
                      if s.get("winning") is False and cat in _PITCHER_CATS]
    if losing_pitcher:
        parts.append(f"helps: {', '.join(losing_pitcher)}")

    return " | ".join(parts)
