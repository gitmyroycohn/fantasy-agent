"""
Category analysis utilities for both H2H and Rotisserie leagues.

analyze_matchup() accepts the MatchupData dict from cbs.stats.fetch_matchup_stats
and returns a Matchup dataclass with CategoryStanding entries for every category.

H2H: winning = my value beats opponent value (ERA/WHIP: lower is better).
Roto: winning = in the top half of the league by roto rank.
"""

import logging

from data.models import CategoryStanding, Matchup, Player  # noqa: F401
from mlb.teams import canonical_team

logger = logging.getLogger(__name__)

# Categories where lower value is better
_LOWER_IS_BETTER = {"ERA", "WHIP", "L", "BB", "BBI"}


# ---------------------------------------------------------------------------
# H2H + Roto unified entry point
# ---------------------------------------------------------------------------

def analyze_matchup(raw_data: dict, week: int,
                    opponent: str = "?", opponent_id: str = "?") -> Matchup:
    """Convert fetch_matchup_stats output into a Matchup dataclass.

    Works for both H2H and Roto.  raw_data is the dict returned by
    cbs.stats.fetch_matchup_stats — see that module's docstring for shape.
    """
    system     = raw_data.get("system", "h2h")
    cats_dict  = raw_data.get("categories", {})
    num_teams  = int(raw_data.get("num_teams", 8))

    # Prefer values embedded in raw_data over the "?" defaults from decisions.py
    if opponent == "?":
        opponent    = raw_data.get("opponent", "Unknown")
    if opponent_id == "?":
        opponent_id = raw_data.get("opponent_id", "")

    cat_standings: list[CategoryStanding] = []

    for cat, data in cats_dict.items():
        mine = float(data.get("mine", 0.0))

        if system == "roto":
            rank    = int(data.get("rank", 0))
            rotopts = int(data.get("rotopts", 0))
            dif     = int(data.get("dif", 0))
            # "winning" = top half of league
            winning = bool(rank > 0 and rank <= num_teams // 2)
            # gap = roto points below the maximum possible
            max_pts = num_teams - 1
            gap     = float(max(0, max_pts - rotopts))
            cat_standings.append(CategoryStanding(
                category=cat,
                my_value=mine,
                opp_value=0.0,
                winning=winning,
                gap=gap,
                rank=rank,
                rotopts=rotopts,
                dif=dif,
            ))
        else:
            theirs  = float(data.get("opp", 0.0))
            winning = _h2h_winning(cat, mine, theirs)
            gap     = abs(mine - theirs)
            cat_standings.append(CategoryStanding(
                category=cat,
                my_value=mine,
                opp_value=theirs,
                winning=winning,
                gap=gap,
            ))

    cats_winning = sum(1 for c in cat_standings if c.winning)
    cats_losing  = sum(1 for c in cat_standings if not c.winning and c.gap > 0)
    cats_tied    = sum(1 for c in cat_standings if c.gap == 0)

    return Matchup(
        week=week,
        opponent_name=opponent,
        opponent_id=opponent_id,
        category_standings=cat_standings,
        cats_winning=cats_winning,
        cats_losing=cats_losing,
        cats_tied=cats_tied,
    )


def validate_scoring_config(cfg_scoring: dict, raw_stats: dict,
                           league_name: str = "") -> list[str]:
    """BUG 6 fix: cross-check the configured hitting+pitching category list in
    config/leagues.yaml against the categories CBS actually returns from
    league/scoring/live (already fetched by cbs.stats.fetch_matchup_stats --
    no extra API call needed). Logs a WARNING on any mismatch.

    This is exactly the class of bug BUG 6 was: leagues.yaml declared 17
    categories (AVG, TB, XBH, QS, HLD, K_BB do not exist in this league; H
    was missing) while CBS actually scores 12. Category analysis and waiver
    targeting were silently optimizing for stats that aren't scored. This
    check makes that drift loud instead of silent.

    Returns a list of human-readable mismatch descriptions (empty if none).
    """
    configured = set(cfg_scoring.get("hitting", []) or []) | \
                 set(cfg_scoring.get("pitching", []) or [])
    actual = set((raw_stats.get("categories") or {}).keys())

    if not actual:
        # Nothing to compare against (e.g. live_scoring returned empty) --
        # don't false-positive a mismatch warning.
        return []

    extra_in_config = configured - actual     # configured but CBS doesn't score
    extra_in_cbs    = actual - configured      # CBS scores but leagues.yaml is missing it

    problems = []
    if extra_in_config:
        problems.append(
            f"configured in leagues.yaml but NOT scored by CBS: {sorted(extra_in_config)}")
    if extra_in_cbs:
        problems.append(
            f"scored by CBS but MISSING from leagues.yaml: {sorted(extra_in_cbs)}")

    if problems:
        logger.warning(
            "Category mismatch for %s: %s (configured=%d cats, CBS=%d cats). "
            "Fix config/leagues.yaml's scoring block.",
            league_name or "league", "; ".join(problems), len(configured), len(actual),
        )

    return problems


def priority_categories(matchup: Matchup) -> list[str]:
    """Return losing/weak categories sorted by closeness — easiest to flip first.

    For H2H: losing cats sorted by gap (smallest gap = easiest flip).
    For Roto: bottom-half cats sorted by roto points (fewest pts = most room to gain).
    """
    losing = [c for c in matchup.category_standings
              if not c.winning and c.gap > 0]   # exclude ties (gap == 0)
    # For roto, rotopts > 0 and gap tells us room to gain; sort ascending gap
    losing.sort(key=lambda c: c.gap)
    return [c.category for c in losing]


def summary_line(matchup: Matchup, system: str = "h2h") -> str:
    """One-line human-readable matchup summary."""
    if system == "roto":
        total_pts = sum(c.rotopts for c in matchup.category_standings)
        return (f"Period {matchup.week}: {total_pts} roto pts - "
                f"winning {matchup.cats_winning} cats, "
                f"losing {matchup.cats_losing}")
    return (f"Week {matchup.week} vs {matchup.opponent_name}: "
            f"{matchup.cats_winning}-{matchup.cats_losing}-{matchup.cats_tied}")


# ---------------------------------------------------------------------------
# NL-only (Casey Stengel)
# ---------------------------------------------------------------------------

_AL_TEAMS = {
    "NYY", "BOS", "BAL", "TBR", "TOR",
    "CWS", "CLE", "DET", "KCR", "MIN",
    "HOU", "LAA", "OAK", "ATH", "SEA", "TEX",  # ATH = A's (relocated from OAK)
}

# Positive allowlist — only these teams are NL-eligible.
# Using an allowlist means unknown/empty team strings are automatically excluded.
_NL_TEAMS = {
    "ARI", "ATL", "CHC", "CIN", "COL",
    "LAD", "MIA", "MIL", "NYM", "PHI",
    "PIT", "SDP", "SFG", "STL", "WSH",
}


def check_nl_eligibility(players: list[Player]) -> list[dict]:
    """Flag any player on an AL team — ineligible for Casey Stengel."""
    warnings = []
    for p in players:
        # BUG (found in 2026-07-18 live run): canonicalize before the
        # membership check. CBS's own player.team field returns the short
        # MLB-native form ("SD", "SF") for some teams, not the longer form
        # these sets use ("SDP", "SFG") -- an uncanonicalized check would
        # silently miss/misclassify Padres and Giants players.
        if canonical_team(p.team) in _AL_TEAMS:
            warnings.append({
                "player":  p.name,
                "team":    p.team,
                "warning": (f"{p.name} is on an AL team ({p.team}) "
                            "and is INELIGIBLE for Casey Stengel."),
            })
    return warnings


def filter_nl_waiver_pool(players: list, league_config: dict) -> list:
    """Keep only NL-team players for NL-only leagues (Casey Stengel).

    Uses a positive allowlist (_NL_TEAMS) so players with missing/unknown
    team abbreviations are excluded rather than slipping through.
    """
    if not league_config.get("nl_only") and league_config.get("roster_type") != "nl_only":
        return players
    return [wp for wp in players if canonical_team(wp.player.team or "") in _NL_TEAMS]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _h2h_winning(cat: str, mine: float, theirs: float) -> bool:
    if cat in _LOWER_IS_BETTER:
        return mine < theirs
    return mine > theirs
