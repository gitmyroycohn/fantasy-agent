"""
MLB park factors — all 30 ballparks.

Source: FanGraphs multi-year park factors (runs, 5-year regressed where available).
100 = neutral. >100 = hitter-friendly. <100 = pitcher-friendly.

Keyed by CBS team abbreviation. Update annually (factors shift as parks age and
team front offices change the configuration — fences, humidor, etc.).

Last updated: 2025 season data.
"""

from mlb.teams import mlb_to_cbs

# Park factor by CBS home-team abbreviation.
# Factor represents RUNS relative to league average (100 = average).
_PARK_FACTORS: dict[str, int] = {
    "COL": 117,   # Coors Field         — extreme altitude, far fences
    "CIN": 107,   # Great American Ball Park — short porch in RF
    "BOS": 104,   # Fenway Park          — Green Monster, short LF
    "PHI": 103,   # Citizens Bank Park   — hitter-friendly dimensions
    "TEX": 102,   # Globe Life Field     — warm climate, lively ball
    "MIL": 102,   # American Family Field— roof helps, dimensions favor hitters
    "CHC": 101,   # Wrigley Field        — wind-dependent; slight hitter edge YTD
    "ATL": 101,   # Truist Park          — modest hitter advantage
    "BAL": 100,   # Camden Yards         — neutral; post-2022 shift
    "NYY": 100,   # Yankee Stadium       — RF porch inflates HR but overall neutral
    "DET": 100,   # Comerica Park        — deep OF, but warm summers offset it
    "STL": 99,    # Busch Stadium        — neutral, slight pitcher edge
    "HOU": 99,    # Minute Maid Park     — Crawford boxes help L-handers; overall neutral
    "ARI": 99,    # Chase Field          — retractable roof, humidor since 2018
    "LAA": 98,    # Angel Stadium        — spacious OF, marine layer
    "CLE": 98,    # Progressive Field    — open-air, cold starts
    "KCR": 98,    # Kauffman Stadium     — large OF, winds variable
    "MIN": 98,    # Target Field         — cold weather suppresses early offense
    "TOR": 98,    # Rogers Centre        — turf speeds game but dimensions neutral
    "CWS": 97,    # Guaranteed Rate Field— large OF dimensions
    "NYM": 97,    # Citi Field           — pitcher-friendly dimensions, cold spring
    "WSH": 97,    # Nationals Park       — pitcher-friendly; slightly suppresses runs
    "LAD": 97,    # Dodger Stadium       — classic pitcher's park; marine layer
    "SFG": 96,    # Oracle Park          — marine layer, deep CF
    "PIT": 96,    # PNC Park             — large park, cool weather
    "SEA": 96,    # T-Mobile Park        — deepest park in MLB, marine air
    "TBR": 95,    # Tropicana Field      — dome reduces weather variance, suppresses HR
    "MIA": 95,    # loanDepot park       — deep alleys, humid air
    "SDP": 95,    # Petco Park           — marine layer, deep OF historically
    "ATH": 97,    # Sacramento (2025+)   — new park, neutral until multi-year data
}

# HR-specific park factors (some parks inflate HR more than overall runs)
_HR_PARK_FACTORS: dict[str, int] = {
    "COL": 122,   # Coors — extreme HR inflation
    "CIN": 112,
    "PHI": 108,
    "NYY": 107,   # RF porch is short for LHB
    "MIL": 105,
    "BOS": 103,
    "TEX": 104,
    "CHC": 101,
    "ATL": 101,
    "BAL": 100,
    "DET": 99,
    "STL": 99,
    "ARI": 100,
    "HOU": 98,
    "LAA": 98,
    "TOR": 99,
    "CLE": 97,
    "KCR": 97,
    "MIN": 97,
    "CWS": 96,
    "NYM": 96,
    "WSH": 97,
    "LAD": 96,
    "SFG": 95,
    "PIT": 95,
    "SEA": 93,
    "TBR": 94,
    "MIA": 93,
    "SDP": 93,
    "ATH": 97,
}


def park_factor(cbs_home_team: str, stat: str = "runs") -> int:
    """
    Return the park factor for the home team's ballpark.

    Args:
        cbs_home_team: CBS team abbreviation for the HOME team.
        stat: "runs" (default) or "hr"

    Returns:
        Park factor integer (100 = neutral, >100 = hitter-friendly).
    """
    key = cbs_home_team.upper()
    if stat == "hr":
        return _HR_PARK_FACTORS.get(key, 100)
    return _PARK_FACTORS.get(key, 100)


def park_label(factor: int) -> str:
    """Human-readable description of a park factor."""
    if factor >= 112:
        return "extreme hitter's park"
    if factor >= 106:
        return "strong hitter's park"
    if factor >= 102:
        return "hitter-friendly"
    if factor >= 98:
        return "neutral"
    if factor >= 94:
        return "pitcher-friendly"
    return "strong pitcher's park"


def park_score_bonus(cbs_home_team: str) -> float:
    """
    Return a score bonus/penalty to add to a batter's matchup score
    based on the park they're playing in today.

    Neutral (100) → 0.0
    Coors (117) → +5.1
    Petco (95) → -1.5
    """
    pf = park_factor(cbs_home_team)
    return (pf - 100) * 0.30


def all_park_factors() -> dict[str, dict]:
    """Return all park factors as a dict for display."""
    result = {}
    for team, pf in sorted(_PARK_FACTORS.items(), key=lambda x: x[1], reverse=True):
        result[team] = {
            "runs":  pf,
            "hr":    _HR_PARK_FACTORS.get(team, 100),
            "label": park_label(pf),
        }
    return result
