"""
League-wide surplus/deficit trade map.

For each team in the league, classify each scoring category as:
  SURPLUS  -- top third (strong, can sell)
  DEFICIT  -- bottom third (weak, need to buy)
  NEUTRAL  -- middle

Then cross-reference with my team to surface trade leads:
  - Teams that have SURPLUS in my DEFICIT cats AND DEFICIT in my SURPLUS cats
    --> strong trade alignment (I give them what they need; they give me what I need)

Usage:
    from agent.surplusmap import build_surplus_map, trade_leads_from_map

    teams_stats = fetch_all_teams_stats(auth, league_id, sport, system)
    my_team_id  = cfg["cbs_team_id"]
    scoring_cats = cfg["scoring"]["hitting"] + cfg["scoring"]["pitching"]

    surplus_map = build_surplus_map(teams_stats, scoring_cats)
    leads       = trade_leads_from_map(surplus_map, my_team_id, top_n=3)
"""
import logging

logger = logging.getLogger(__name__)

# Categories where lower value = better (rank 1 = best = lowest value)
_LOWER_IS_BETTER = {"ERA", "WHIP", "L", "BB"}


def build_surplus_map(teams_stats: list[dict],
                      scoring_cats: list[str]) -> dict:
    """Build a surplus/deficit map for every team.

    Returns:
      {
        team_id: {
          "team_name": str,
          "surplus":   [cat, ...],   # strong categories
          "deficit":   [cat, ...],   # weak categories
          "neutral":   [cat, ...],
          "ranks":     {cat: rank},  # 1 = best, n = worst
        },
        ...
      }
    """
    if not teams_stats:
        return {}

    n = len(teams_stats)
    surplus_thresh = max(1, n // 3)          # top third
    deficit_thresh = n - max(1, n // 3)      # bottom third

    # Normalize scoring_cats to a set for quick membership test
    cat_set = set(scoring_cats)

    result = {}
    for team in teams_stats:
        tid  = team["team_id"]
        name = team["team_name"]
        cats = team.get("cats", {})

        surplus, deficit, neutral = [], [], []
        ranks = {}

        for cat, info in cats.items():
            if cat_set and cat not in cat_set:
                continue
            rank = info.get("rank", 0)
            if rank <= 0:
                # No rank available — skip for classification
                neutral.append(cat)
                continue
            ranks[cat] = rank

            lower = cat in _LOWER_IS_BETTER
            # For lower-is-better cats, rank 1 means lowest ERA = best = surplus
            if lower:
                effective_rank = rank          # rank 1 = best
            else:
                effective_rank = rank          # rank 1 = best (most HR etc.)

            if effective_rank <= surplus_thresh:
                surplus.append(cat)
            elif effective_rank > deficit_thresh:
                deficit.append(cat)
            else:
                neutral.append(cat)

        result[tid] = {
            "team_name": name,
            "surplus":   surplus,
            "deficit":   deficit,
            "neutral":   neutral,
            "ranks":     ranks,
        }

    return result


def trade_leads_from_map(surplus_map: dict,
                          my_team_id: str,
                          top_n: int = 3) -> list[dict]:
    """Surface the best trade partners for my team.

    A strong trade lead = a team that:
      - Has SURPLUS in at least one of my DEFICIT categories
      - Has DEFICIT in at least one of my SURPLUS categories

    Returns sorted list (best alignment first):
      [
        {
          "team_id":   str,
          "team_name": str,
          "alignment": int,    # number of matching surplus<->deficit pairs
          "i_want":    [cat],  # their surplus that fills my deficit
          "they_want": [cat],  # my surplus that fills their deficit
        },
        ...
      ]
    """
    if not surplus_map or my_team_id not in surplus_map:
        logger.warning("surplusmap: my_team_id %s not found in map (have: %s)",
                       my_team_id, list(surplus_map.keys())[:5])
        return []

    me = surplus_map[my_team_id]
    my_surplus = set(me["surplus"])
    my_deficit = set(me["deficit"])

    leads = []
    for tid, team in surplus_map.items():
        if tid == my_team_id:
            continue
        their_surplus = set(team["surplus"])
        their_deficit = set(team["deficit"])

        i_want    = sorted(their_surplus & my_deficit)   # they have what I need
        they_want = sorted(my_surplus & their_deficit)   # I have what they need

        alignment = len(i_want) + len(they_want)
        if alignment == 0:
            continue

        leads.append({
            "team_id":   tid,
            "team_name": team["team_name"],
            "alignment": alignment,
            "i_want":    i_want,
            "they_want": they_want,
        })

    leads.sort(key=lambda x: x["alignment"], reverse=True)
    return leads[:top_n]


def my_category_profile(surplus_map: dict, my_team_id: str) -> dict:
    """Return my surplus/deficit profile for display."""
    if not surplus_map or my_team_id not in surplus_map:
        return {}
    me = surplus_map[my_team_id]
    return {
        "surplus": me["surplus"],
        "deficit": me["deficit"],
        "neutral": me["neutral"],
    }
