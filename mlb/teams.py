"""
Authoritative team abbreviation mapping: MLB API → CBS Fantasy.

Used by mlb/schedule.py (schedule API) and mlb/stats.py (stats API) so
both sources resolve to the same CBS abbreviation that CBS stores in
pro_team on roster and waiver wire players.

Also provides norm_name() — the canonical player-name normalizer for
cross-source matching. Handles accented characters so e.g. "Rodríguez"
(CBS) matches "Rodriguez" (MLB API) and vice versa.

CBS Fantasy abbreviations for all 30 teams (validated):
  ARI ATL BAL BOS CHC CWS CIN CLE COL DET
  HOU KCR LAA LAD MIA MIL MIN NYM NYY ATH
  PHI PIT SDP SFG SEA STL TBR TEX TOR WSH
"""

import re
import unicodedata

# ---------------------------------------------------------------------------
# Team abbreviation mapping: MLB API → CBS Fantasy
#
# Only teams whose MLB API abbreviation differs from CBS are listed.
# Anything not here passes through unchanged.
# ---------------------------------------------------------------------------
MLB_TO_CBS: dict[str, str] = {
    # Team             MLB API  →  CBS
    "CHW": "CWS",   # Chicago White Sox
    "KC":  "KCR",   # Kansas City Royals
    "SD":  "SDP",   # San Diego Padres
    "SF":  "SFG",   # San Francisco Giants
    "TB":  "TBR",   # Tampa Bay Rays
    "WSN": "WSH",   # Washington Nationals (schedule API sometimes returns WSN)
    # Athletics — moved to Sacramento 2025; MLB API now uses "ATH"
    # CBS also adopted "ATH". Map old "OAK" as a safety catch.
    "OAK": "ATH",
}

# CBS → MLB API (reverse mapping, for completeness)
CBS_TO_MLB: dict[str, str] = {v: k for k, v in MLB_TO_CBS.items()}


def mlb_to_cbs(abbr: str) -> str:
    """Convert an MLB API team abbreviation to the CBS equivalent."""
    return MLB_TO_CBS.get(abbr, abbr)


def cbs_to_mlb(abbr: str) -> str:
    """Convert a CBS team abbreviation to the MLB API equivalent."""
    return CBS_TO_MLB.get(abbr, abbr)


# ---------------------------------------------------------------------------
# Player name normalizer
# ---------------------------------------------------------------------------

def norm_name(name: str) -> str:
    """
    Canonical player-name normalizer for cross-source matching.

    Strips accents (Rodríguez → rodriguez), removes all non-alphanumeric
    characters, and lowercases. Consistent across CBS, MLB Stats API,
    and MLB Schedule API.

    Examples:
      "Willy Adames"     → "willyadames"
      "J.D. Martinez"    → "jdmartinez"
      "Luis García"      → "luisgarcia"
      "Édgar Martínez"   → "edgarmartinez"
      "Ha-Seong Kim"     → "haseongkim"
    """
    # Decompose accented characters (é → e + combining accent) then drop non-ASCII
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = nfkd.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", ascii_name.lower())
