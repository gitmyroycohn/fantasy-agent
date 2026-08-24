"""
Points-scoring calculation for Christopher's 3 CBS football leagues.

Unlike baseball (sports/baseball/categories.py), all 3 football leagues are
Head-to-Head, **Points** format -- there is no category/roto scoring here.
But the leagues do NOT share one scoring formula:

  - hard_chargers (hcfl05) and east_coast (ecfc) use a standard PPR-style
    flat-rate formula: N points per reception/yard/TD, same math regardless
    of which position racks up the stat.
  - f_league (sfflf) is structurally different: receptions score 0 (not
    PPR), yardage only scores via bonus tiers (not a per-yard rate), and TD
    point values depend on BOTH the TD type and which position scored it
    (e.g. a passing TD is worth 6 to a QB but 12 to a RB/WR/TE on a trick
    play). This can't be expressed as one flat rate table, hence the
    separate `score_sfflf()` path below.

Profiles here mirror what's recorded in config/leagues.yaml under each
league's `scoring_profile` key. Full source rules are in the "football
scoring rules" memory captured 2026-07-31 from each league's CBS /rules
page -- see that memory file for the original text this was transcribed
from.

KNOWN DATA GAPS (do not silently assume these are complete):
  - sfflf defense "Points Against" tier table: only the 0-0=10pt (shutout)
    tier was visible on the live rules page. Tiers above that were not
    captured -- extrapolating linearly would be a guess, not a fact.
  - ecfc has THREE places where the informal commissioner notes on the
    rules page disagree with the structured settings table:
      1. Missed XP: notes say -1pt, structured table says -2pts.
      2. Receptions: notes say "5 points for every 10 receptions" (0.5/rec),
         structured table says 1pt/reception (10x more).
      3. TE yardage: notes say "3pts per 50yds, then 3pts per additional
         25yds", structured table implies the same flat 0.1pt/yd rate as
         every other position.
    This module scores using the STRUCTURED SETTINGS TABLE (the actual
    enforced CBS scoring engine) for all three, since informal notes are
    often stale reminders rather than the live config. Flag to Christopher
    before relying on this for real matchup decisions -- if the notes are
    actually right, real weekly scores won't match this module's output
    and that mismatch is the signal to fix it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Tier helpers
# ---------------------------------------------------------------------------

def _tier_points(value: float, tiers: list[tuple[float, float, float]]) -> float:
    """Return the points for the tier whose [low, high] range contains value.

    tiers is a list of (low, high, points) tuples, checked in order. Returns
    0.0 if value doesn't fall in any tier (e.g. below the lowest threshold --
    tiers here are bonus tiers on top of a base value of 0, not a full
    linear scale).
    """
    for low, high, points in tiers:
        if low <= value <= high:
            return points
    return 0.0


def _long_td_bonus(yards: float, short_bonus: float, long_bonus: float) -> float:
    """10-39 yd TDs get `short_bonus`, 40+ yd TDs get `long_bonus`, else 0."""
    if yards >= 40:
        return long_bonus
    if yards >= 10:
        return short_bonus
    return 0.0


# ---------------------------------------------------------------------------
# hard_chargers (hcfl05) -- standard PPR
# ---------------------------------------------------------------------------

HCFL05_PROFILE = {
    "reception": 1.0,
    "rush_yard": 0.1,
    "rec_yard": 0.1,
    "pass_yard": 0.05,
    "pass_td": 6.0,
    "rush_td": 6.0,
    "rec_td": 6.0,
    "pass_int": -1.0,
    "fumble_lost": -1.0,
    "two_point": 2.0,  # Pa2P / Re2P / Ru2P all score 2
    "xp": 1.0,
    "missed_xp": -0.5,
    "fg_base": 3.0,
    "fg_bonus_tiers": [(50, 59, 1.0), (60, 99, 2.0)],
    "defense": {
        "sack": 1.0,
        "int": 1.0,
        "fumble_rec": 1.0,
        "def_st_td": 6.0,  # DTD -- total defensive/ST TD
        "st_two_point": 2.0,
        "safety": 2.0,
        # DPA = Defensive Points Against, tiered
        "points_against_tiers": [
            (0, 0, 7.0), (1, 7, 5.0), (8, 13, 3.0), (14, 17, 1.0),
            (18, 40, 0.0), (41, 45, -1.0), (46, 55, -3.0), (56, 99, -5.0),
        ],
    },
}

# ---------------------------------------------------------------------------
# east_coast (ecfc) -- standard PPR, stricter penalties
# ---------------------------------------------------------------------------

ECFC_PROFILE = {
    "reception": 1.0,  # NOTE: informal notes conflict here -- see module docstring
    "rush_yard": 0.1,
    "rec_yard": 0.1,  # NOTE: informal notes claim different TE yardage math -- see module docstring
    "pass_yard": 0.05,
    "pass_td": 6.0,
    "rush_td": 6.0,
    "rec_td": 6.0,
    "pass_int": -2.0,
    "fumble_lost": -2.0,
    "two_point_pass": 1.0,
    "two_point_rush": 2.0,
    "two_point_rec": 2.0,
    "xp": 1.0,
    "missed_xp": -2.0,  # NOTE: informal notes say -1 -- see module docstring
    "missed_fg": -1.0,
    "fg_base": 3.0,
    "fg_bonus_tiers": [(50, 999, 3.0)],
    "offensive_fumble_recovery_td": 6.0,
    "defense": {
        "sack": 1.0,
        "int": 2.0,
        "fumble_rec": 2.0,
        "def_st_td": 6.0,
        "st_two_point": 2.0,
        "safety": 2.0,
        # DSTPA = Points Against Defense/ST, tiered. INCOMPLETE beyond 20 --
        # not captured on the live rules page.
        "points_against_tiers": [
            (0, 0, 6.0), (1, 6, 4.0), (7, 13, 2.0), (14, 20, 1.0),
        ],
    },
}


def score_standard_ppr(stats: dict, profile: dict) -> float:
    """Score a flat-rate PPR-style stat line (hcfl05 or ecfc profile).

    stats keys use CBS's own abbreviations, e.g.:
      Recpt, RuYd, ReYd, PaYd, PaTD, RuTD, ReTD, PaInt, FL,
      Pa2P/Re2P/Ru2P (or two_point_pass/rush/rec for ecfc), XP, MXP,
      FG (list of made FG distances), MFG (count of missed FGs, ecfc only)
    Unrecognized/missing keys default to 0 -- caller should only pass the
    stats that actually occurred.
    """
    pts = 0.0
    pts += stats.get("Recpt", 0) * profile["reception"]
    pts += stats.get("RuYd", 0) * profile["rush_yard"]
    pts += stats.get("ReYd", 0) * profile["rec_yard"]
    pts += stats.get("PaYd", 0) * profile["pass_yard"]
    pts += stats.get("PaTD", 0) * profile["pass_td"]
    pts += stats.get("RuTD", 0) * profile["rush_td"]
    pts += stats.get("ReTD", 0) * profile["rec_td"]
    pts += stats.get("PaInt", 0) * profile["pass_int"]
    pts += stats.get("FL", 0) * profile["fumble_lost"]
    pts += stats.get("XP", 0) * profile["xp"]
    pts += stats.get("MXP", 0) * profile["missed_xp"]
    pts += stats.get("OFRTD", 0) * profile.get("offensive_fumble_recovery_td", 0)

    if "two_point" in profile:
        pts += stats.get("Pa2P", 0) * profile["two_point"]
        pts += stats.get("Re2P", 0) * profile["two_point"]
        pts += stats.get("Ru2P", 0) * profile["two_point"]
    else:
        pts += stats.get("Pa2P", 0) * profile.get("two_point_pass", 0)
        pts += stats.get("Re2P", 0) * profile.get("two_point_rec", 0)
        pts += stats.get("Ru2P", 0) * profile.get("two_point_rush", 0)

    if "missed_fg" in profile:
        pts += stats.get("MFG", 0) * profile["missed_fg"]

    # Field goals made: a list of distances, e.g. [42, 51] for two makes.
    for distance in stats.get("FG", []):
        pts += profile["fg_base"]
        pts += _tier_points(distance, profile["fg_bonus_tiers"])

    return pts


def score_standard_ppr_defense(stats: dict, profile: dict) -> float:
    """Score a DST stat line for hcfl05/ecfc.

    stats keys: Sack, Int, DFR (fumble recovery), DTD (any def/ST TD),
    ST2PT, STY, and PA (points allowed by the real-life defense this period).
    """
    d = profile["defense"]
    pts = 0.0
    pts += stats.get("Sack", 0) * d["sack"]
    pts += stats.get("Int", 0) * d["int"]
    pts += stats.get("DFR", 0) * d["fumble_rec"]
    pts += stats.get("DTD", 0) * d["def_st_td"]
    pts += stats.get("ST2PT", 0) * d["st_two_point"]
    pts += stats.get("STY", 0) * d["safety"]
    if "PA" in stats:
        pts += _tier_points(stats["PA"], d["points_against_tiers"])
    return pts


# ---------------------------------------------------------------------------
# f_league (sfflf) -- tiered, non-PPR, position-dependent
# ---------------------------------------------------------------------------

SFFLF_PROFILE = {
    "reception": 0.0,  # NOT PPR -- confirmed 0 pts at every position
    # QB-only: combined passing + rushing yards, tiered (not per-yard)
    "pass_rush_yard_tiers": [
        (210, 259, 3.0), (260, 309, 6.0), (310, 359, 9.0), (360, 409, 12.0),
        (410, 459, 15.0), (460, 509, 18.0), (510, 559, 21.0), (560, 609, 24.0),
    ],
    # Combined rushing + receiving yards, tiered, thresholds differ by position
    "rush_rec_yard_tiers": {
        "RB": [(80, 119, 3.0), (120, 159, 6.0), (160, 199, 9.0), (200, 239, 12.0),
               (240, 279, 15.0), (280, 319, 18.0), (320, 359, 21.0), (360, 399, 24.0)],
        "WR": [(80, 119, 3.0), (120, 159, 6.0), (160, 199, 9.0), (200, 239, 12.0),
               (240, 279, 15.0), (280, 319, 18.0), (320, 359, 21.0), (360, 399, 24.0)],
        "TE": [(50, 89, 3.0), (90, 129, 6.0), (130, 169, 9.0), (170, 209, 12.0),
               (210, 249, 15.0), (250, 289, 18.0)],
    },
    # Base TD points by (position of the SCORER, TD type). A "trick play" TD
    # (e.g. a RB throwing a TD pass) is worth more than the position's usual
    # scoring TD -- these are CBS's actual configured values, not a guess.
    "td_points": {
        "QB": {"PaTD": 6.0, "RuTD": 9.0, "ReTD": 12.0, "OFRTD": 6.0},
        "RB": {"PaTD": 12.0, "RuTD": 6.0, "ReTD": 9.0, "OFRTD": 6.0},
        "WR": {"PaTD": 12.0, "RuTD": 12.0, "ReTD": 6.0, "OFRTD": 6.0},
        "TE": {"PaTD": 12.0, "RuTD": 12.0, "ReTD": 6.0, "OFRTD": 6.0},
    },
    # Long-TD bonus: (10-39yd bonus, 40+yd bonus), by (position, TD type)
    "td_long_bonus": {
        "QB": {"PaTD": (3.0, 6.0), "RuTD": (3.0, 9.0), "ReTD": (6.0, 12.0)},
        "RB": {"PaTD": (6.0, 12.0), "RuTD": (3.0, 6.0), "ReTD": (3.0, 9.0)},
        "WR": {"PaTD": (6.0, 12.0), "RuTD": (6.0, 12.0), "ReTD": (3.0, 6.0)},
        "TE": {"PaTD": (6.0, 12.0), "RuTD": (6.0, 12.0), "ReTD": (3.0, 6.0)},
    },
    "two_point": 2.0,  # Pa2P/Re2P/Ru2P/Fum2PT all score 2 at every skill position
    "kicker": {
        "fg_base": 3.0,
        "fg_bonus_tiers": [(40, 54, 2.0), (55, 999, 7.0)],
        "xp": 1.0,
        "fum2pk": 2.0,  # Fumble Recovery Two-point Conversion, kicking formation
    },
    "defense": {
        "sack": 1.0,
        "int": 1.0,
        "int_td": 6.0, "int_td_long_bonus": (3.0, 6.0),
        "fumble_rec": 1.0,
        "fumble_rec_td": 6.0, "fumble_rec_td_long_bonus": (3.0, 6.0),
        "blocked_fg_td": 6.0, "blocked_fg_td_long_bonus": (3.0, 6.0),
        "blocked_punt_td": 6.0, "blocked_punt_td_long_bonus": (3.0, 6.0),
        "kick_return_td": 6.0, "kick_return_td_long_bonus": (3.0, 6.0),
        "punt_return_td": 6.0, "punt_return_td_long_bonus": (3.0, 6.0),
        "special_teams_fumble_td": 6.0, "special_teams_fumble_td_long_bonus": (3.0, 6.0),
        "safety": 2.0,
        "st_two_point": 2.0,
        "st_one_point_safety": 1.0,
        # INCOMPLETE -- only the shutout tier was visible on the live page.
        # Do not use this for real scoring until the full table is re-pulled.
        "points_allowed_tiers": [(0, 0, 10.0)],
    },
}


def score_sfflf(stats: dict, position: str) -> float:
    """Score a stat line under F-League's tiered, position-dependent system.

    position: one of "QB", "RB", "WR", "TE", "K" (offensive positions) or
    "DST" for defense/special teams. stats keys use CBS abbreviations, plus
    two derived combined-yardage keys this function expects the caller to
    supply directly (CBS's own PaRuYd / RuReYd combined fields):
      PaRuYd (QB passing+rushing yards), RuReYd (RB/WR/TE rushing+receiving)
    TD stats (PaTD, RuTD, ReTD, OFRTD) should each be a list of TD yardages,
    e.g. RuTD=[10, 45] for two rushing TDs of 10 and 45 yards, so long-TD
    bonuses can be computed per-TD rather than assuming uniform length.
    """
    profile = SFFLF_PROFILE
    pts = 0.0

    if position == "K":
        k = profile["kicker"]
        for distance in stats.get("FG", []):
            pts += k["fg_base"] + _tier_points(distance, k["fg_bonus_tiers"])
        pts += stats.get("XP", 0) * k["xp"]
        pts += stats.get("Fum2PK", 0) * k["fum2pk"]
        return pts

    if position == "DST":
        d = profile["defense"]
        pts += stats.get("Sack", 0) * d["sack"]
        pts += stats.get("Int", 0) * d["int"]
        pts += stats.get("DFR", 0) * d["fumble_rec"]
        pts += stats.get("STY", 0) * d["safety"]
        pts += stats.get("ST2PT", 0) * d["st_two_point"]
        pts += stats.get("STY1PT", 0) * d["st_one_point_safety"]
        for td_key, base_key, bonus_key in (
            ("IntTD", "int_td", "int_td_long_bonus"),
            ("DFRTD", "fumble_rec_td", "fumble_rec_td_long_bonus"),
            ("BFTD", "blocked_fg_td", "blocked_fg_td_long_bonus"),
            ("BPTD", "blocked_punt_td", "blocked_punt_td_long_bonus"),
            ("KRTD", "kick_return_td", "kick_return_td_long_bonus"),
            ("PRTD", "punt_return_td", "punt_return_td_long_bonus"),
            ("SFRTD", "special_teams_fumble_td", "special_teams_fumble_td_long_bonus"),
        ):
            for yards in stats.get(td_key, []):
                pts += d[base_key]
                short_b, long_b = d[bonus_key]
                pts += _long_td_bonus(yards, short_b, long_b)
        if "PA" in stats:
            pts += _tier_points(stats["PA"], d["points_allowed_tiers"])
        return pts

    # Offensive skill positions: QB, RB, WR, TE
    pts += stats.get("Recpt", 0) * profile["reception"]  # always 0, kept for clarity

    if position == "QB":
        pts += _tier_points(stats.get("PaRuYd", 0), profile["pass_rush_yard_tiers"])
    else:
        tiers = profile["rush_rec_yard_tiers"].get(position, [])
        pts += _tier_points(stats.get("RuReYd", 0), tiers)

    two_pt_keys = ("Pa2P", "Re2P", "Ru2P", "Fum2PT")
    for key in two_pt_keys:
        pts += stats.get(key, 0) * profile["two_point"]

    td_points = profile["td_points"].get(position, {})
    long_bonus = profile["td_long_bonus"].get(position, {})
    for td_key in ("PaTD", "RuTD", "ReTD"):
        base = td_points.get(td_key, 0.0)
        short_b, long_b = long_bonus.get(td_key, (0.0, 0.0))
        for yards in stats.get(td_key, []):
            pts += base + _long_td_bonus(yards, short_b, long_b)

    pts += stats.get("OFRTD", 0) * td_points.get("OFRTD", 0.0)

    return pts


def estimate_sfflf_points(stats: dict, position: str) -> float | None:
    """Estimate an sfflf (F-League) fantasy-point total from a FantasyPros
    season-long NFL projection stat line, for RANKING waiver-wire value --
    not a promise of exact real scoring.

    sfflf has no PPR-style flat-rate FantasyPros scoring format to project
    against directly (score_sfflf() above is fundamentally tiered and
    position-dependent, not a per-yard/per-reception rate), so this
    reimplements sfflf's own SFFLF_PROFILE tiers/TD-value tables against
    FantasyPros' season-projection stat fields instead of live weekly
    box-score stats:
      pass_yds, rush_yds, rec_yds -- combined per position (QB: pass+rush;
        RB/WR/TE: rush+rec), run through the same yardage bonus tiers
        score_sfflf() uses.
      pass_tds, rush_tds, rec_tds -- each multiplied by that TD type's
        base position-dependent value (profile["td_points"]).
      2pt_tds -- flat 2 pts each, matches SFFLF_PROFILE's uniform
        two_point rate across Pa2P/Re2P/Ru2P/Fum2PT.
    rec_rec (reception count) is intentionally UNUSED -- sfflf scores 0
    pts/reception, confirmed not PPR (see SFFLF_PROFILE["reception"]).

    KNOWN, DELIBERATE GAP vs score_sfflf(): the real formula adds a long-TD
    bonus (3-12 extra pts, depending on position/TD type/length) on top of
    each TD's base value, keyed off that individual TD's own yardage.
    FantasyPros' season projection only gives aggregate TD *counts*, not
    per-TD yardage, so that bonus can't be reconstructed here and is
    omitted entirely. This makes the estimate a systematic UNDERESTIMATE
    of true sfflf points, biased somewhat against big-play/long-TD-prone
    players -- fine for RANKING waiver-wire fill-in candidates against
    each other (every candidate is underestimated the same way), but this
    is not a real point projection and should never be presented as one.

    position: "QB", "RB", "WR", or "TE" only -- returns None for anything
    else (K, DST). FantasyPros' generic season projection doesn't carry
    the granular stat categories sfflf's real K/DST formula needs
    (made-FG-by-distance list, sacks, INTs, points allowed, etc.), and
    guessing at those would be fabricating data, not estimating from it --
    callers should fall back to ownership_pct for K/DST waiver ranking in
    this league, same as before this function existed.
    """
    if position not in ("QB", "RB", "WR", "TE"):
        return None

    profile = SFFLF_PROFILE
    pts = 0.0

    if position == "QB":
        combined_yards = stats.get("pass_yds", 0) + stats.get("rush_yds", 0)
        pts += _tier_points(combined_yards, profile["pass_rush_yard_tiers"])
    else:
        combined_yards = stats.get("rush_yds", 0) + stats.get("rec_yds", 0)
        tiers = profile["rush_rec_yard_tiers"].get(position, [])
        pts += _tier_points(combined_yards, tiers)

    td_points = profile["td_points"].get(position, {})
    pts += stats.get("pass_tds", 0) * td_points.get("PaTD", 0.0)
    pts += stats.get("rush_tds", 0) * td_points.get("RuTD", 0.0)
    pts += stats.get("rec_tds", 0) * td_points.get("ReTD", 0.0)

    pts += stats.get("2pt_tds", 0) * profile["two_point"]

    return pts


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

SCORING_PROFILES = {
    "standard_ppr": HCFL05_PROFILE,
    "standard_ppr_strict": ECFC_PROFILE,
    # "sfflf_tiered" intentionally not listed here -- it uses score_sfflf(),
    # not score_standard_ppr(), because its shape is fundamentally different.
}


def score_player(stats: dict, position: str, scoring_profile: str) -> float:
    """Entry point: score one player's stat line under the named profile.

    scoring_profile must match a `scoring_profile` value from
    config/leagues.yaml's football section (sfflf_tiered, standard_ppr,
    standard_ppr_strict).
    """
    if scoring_profile == "sfflf_tiered":
        return score_sfflf(stats, position)
    if scoring_profile in SCORING_PROFILES:
        if position == "DST":
            return score_standard_ppr_defense(stats, SCORING_PROFILES[scoring_profile])
        return score_standard_ppr(stats, SCORING_PROFILES[scoring_profile])
    raise ValueError(f"Unknown football scoring_profile: {scoring_profile!r}")
