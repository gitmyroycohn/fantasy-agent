"""
Tests for sports/football/scoring.py and the football section of
config/leagues.yaml.

Stat lines here are synthetic (hand-computed expected values), since no live
CBS football data source is wired up yet (season hasn't started, no MCP
tools exist for football -- see project memory). These tests only verify
the scoring *math* against each league's documented rules is implemented
correctly; they cannot verify CBS's actual live stat-field shapes until a
real matchup/roster fetch exists for football.
"""

import yaml

from sports.football.scoring import (
    score_player,
    score_standard_ppr,
    score_standard_ppr_defense,
    score_sfflf,
    HCFL05_PROFILE,
    ECFC_PROFILE,
)


# ---------------------------------------------------------------------------
# config/leagues.yaml structural checks
# ---------------------------------------------------------------------------

def _football_leagues():
    with open("config/leagues.yaml") as f:
        cfg = yaml.safe_load(f)
    return {l["id"]: l for l in cfg["football"]}


def test_leagues_yaml_has_all_3_football_leagues():
    leagues = _football_leagues()
    assert set(leagues) == {"f_league", "hard_chargers", "east_coast"}


def test_leagues_yaml_football_ids_and_team_ids_confirmed_2026_07_31():
    leagues = _football_leagues()
    assert leagues["f_league"]["cbs_league_id"] == "sfflf"
    assert leagues["f_league"]["cbs_team_id"] == "11"
    assert leagues["hard_chargers"]["cbs_league_id"] == "hcfl05"
    assert leagues["hard_chargers"]["cbs_team_id"] == "17"
    assert leagues["east_coast"]["cbs_league_id"] == "ecfc"
    assert leagues["east_coast"]["cbs_team_id"] == "5"


def test_leagues_yaml_all_3_football_leagues_are_h2h_points():
    leagues = _football_leagues()
    for league in leagues.values():
        assert league["format"] == "h2h_points"


def test_leagues_yaml_scoring_profiles_match_scoring_py():
    leagues = _football_leagues()
    assert leagues["f_league"]["scoring_profile"] == "sfflf_tiered"
    assert leagues["hard_chargers"]["scoring_profile"] == "standard_ppr"
    assert leagues["east_coast"]["scoring_profile"] == "standard_ppr_strict"


# ---------------------------------------------------------------------------
# hard_chargers (hcfl05) -- standard PPR
# ---------------------------------------------------------------------------

def test_hcfl05_qb_stat_line():
    # 300 pass yds, 2 pass TD, 1 INT, 5 rush yds
    stats = {"PaYd": 300, "PaTD": 2, "PaInt": 1, "RuYd": 5}
    # 300*.05 + 2*6 + 1*(-1) + 5*.1 = 15 + 12 - 1 + 0.5
    assert score_standard_ppr(stats, HCFL05_PROFILE) == 26.5


def test_hcfl05_kicker_fg_distance_bonus():
    # One 45-yarder (no bonus tier), one 51-yarder (+1 for 50-59 tier)
    stats = {"FG": [45, 51], "XP": 3}
    # (3 + 0) + (3 + 1) + 3*1 = 3 + 4 + 3
    assert score_standard_ppr(stats, HCFL05_PROFILE) == 10.0


def test_hcfl05_defense_points_against_tier():
    stats = {"Sack": 3, "Int": 1, "DFR": 1, "PA": 10}
    # 3*1 + 1*1 + 1*1 + tier(8-13 -> 3)
    assert score_standard_ppr_defense(stats, HCFL05_PROFILE) == 8.0


def test_hcfl05_missed_xp_penalty():
    stats = {"MXP": 1}
    assert score_standard_ppr(stats, HCFL05_PROFILE) == -0.5


# ---------------------------------------------------------------------------
# east_coast (ecfc) -- standard PPR, stricter penalties
# ---------------------------------------------------------------------------

def test_ecfc_wr_stat_line():
    stats = {"Recpt": 7, "ReYd": 90, "ReTD": 1}
    # 7*1 + 90*.1 + 1*6 = 7 + 9 + 6
    assert score_standard_ppr(stats, ECFC_PROFILE) == 22.0


def test_ecfc_missed_xp_is_minus_2_per_structured_settings():
    # NOTE: this uses the structured settings table value (-2), which
    # conflicts with the league's informal commish notes (-1) -- see
    # scoring.py module docstring. This test documents current behavior,
    # not a confirmed-correct value.
    stats = {"MXP": 1}
    assert score_standard_ppr(stats, ECFC_PROFILE) == -2.0


def test_ecfc_fg_50_plus_bonus():
    stats = {"FG": [52]}
    assert score_standard_ppr(stats, ECFC_PROFILE) == 6.0


def test_ecfc_interception_thrown_is_minus_2():
    stats = {"PaInt": 1}
    assert score_standard_ppr(stats, ECFC_PROFILE) == -2.0


# ---------------------------------------------------------------------------
# f_league (sfflf) -- tiered, non-PPR, position-dependent
# ---------------------------------------------------------------------------

def test_sfflf_qb_yardage_tier_and_short_td_bonus():
    stats = {"PaRuYd": 280, "PaTD": [15]}
    # yardage tier (260-309 -> 6) + TD base 6 + short-TD bonus 3 (10-39yd)
    assert score_sfflf(stats, "QB") == 15.0


def test_sfflf_rb_yardage_tier_and_long_td_bonus():
    stats = {"RuReYd": 250, "RuTD": [45]}
    # yardage tier (240-279 -> 15) + TD base 6 + long-TD bonus 6 (40+yd)
    assert score_sfflf(stats, "RB") == 27.0


def test_sfflf_reception_scores_zero():
    stats = {"Recpt": 10, "RuReYd": 0}
    assert score_sfflf(stats, "WR") == 0.0


def test_sfflf_trick_play_td_scores_position_dependent_value():
    # A WR throwing a passing TD (trick play) is worth 12, not the 6 a QB gets
    stats = {"PaTD": [5]}
    assert score_sfflf(stats, "WR") == 12.0


def test_sfflf_kicker_long_fg_bonus():
    stats = {"FG": [57], "XP": 2}
    # (3 + 7 for 55+) + 2*1
    assert score_sfflf(stats, "K") == 12.0


def test_sfflf_defense_int_return_td():
    stats = {"Sack": 2, "Int": 1, "IntTD": [45]}
    # 2*1 + 1*1 + (base 6 + long bonus 6 for 40+yd)
    assert score_sfflf(stats, "DST") == 15.0


# ---------------------------------------------------------------------------
# score_player() dispatch
# ---------------------------------------------------------------------------

def test_score_player_dispatches_to_correct_profile():
    stats = {"Recpt": 5, "ReYd": 50}
    hcfl05_result = score_player(stats, "WR", "standard_ppr")
    ecfc_result = score_player(stats, "WR", "standard_ppr_strict")
    sfflf_result = score_player({"RuReYd": 50}, "WR", "sfflf_tiered")

    assert hcfl05_result == 10.0  # 5*1 + 50*.1
    assert ecfc_result == 10.0    # same rates for this stat subset
    assert sfflf_result == 0.0    # 50yd doesn't clear the 80yd first tier for WR


def test_score_player_rejects_unknown_profile():
    import pytest
    with pytest.raises(ValueError):
        score_player({}, "WR", "not_a_real_profile")
