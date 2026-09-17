"""
Tests for sports/football/actuals.py -- the Sleeper-raw-stats-to-CBS-shape
adapter that feeds sports/football/scoring.py's real, exact scoring engine.

Expected point totals here are hand-computed against the real profile
tables in sports/football/scoring.py (HCFL05_PROFILE/SFFLF_PROFILE), same
approach as tests/test_football_scoring.py -- this file tests the Sleeper
raw-field mapping (sleeper_stats_to_cbs_shape / _td_yardage_list), not the
scoring math itself (already covered there).
"""

from unittest.mock import patch

from data.models import Player, RosterSlot

from sports.football.actuals import (
    _td_yardage_list,
    sleeper_stats_to_cbs_shape,
    score_actual_points,
    snap_share,
    actual_points_for_roster,
)


# ---------------------------------------------------------------------------
# _td_yardage_list
# ---------------------------------------------------------------------------

def test_td_yardage_list_zero_count_is_empty():
    assert _td_yardage_list(0, 45) == []
    assert _td_yardage_list(None, 45) == []


def test_td_yardage_list_one_td_is_just_the_longest():
    assert _td_yardage_list(1, 25) == [25.0]


def test_td_yardage_list_multiple_tds_pads_zeros_after_the_longest():
    # Documents the known approximation this leaves (see actuals.py's
    # module docstring): only the longest TD's real yardage is known.
    assert _td_yardage_list(3, 25) == [25.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# sleeper_stats_to_cbs_shape / score_actual_points -- standard_ppr (flat rate)
# ---------------------------------------------------------------------------

def test_score_actual_points_hard_chargers_rb_flat_rate():
    raw = {"rec": 5, "rush_yd": 80, "rec_yd": 30, "rush_td": 1, "rush_td_lng": 20}
    # 5*1 + 80*.1 + 30*.1 + 1*6 (flat rate, no long-TD bonus for standard_ppr)
    assert score_actual_points(raw, "RB", "standard_ppr") == 22.0


def test_score_actual_points_east_coast_uses_stricter_penalties():
    raw = {"rec": 3, "rec_yd": 40, "pass_int": 1, "fum_lost": 1}
    # 3*1 + 40*.1 - 2 (pass_int) - 2 (fumble_lost) = 3 + 4 - 2 - 2
    assert score_actual_points(raw, "WR", "standard_ppr_strict") == 3.0


# ---------------------------------------------------------------------------
# sleeper_stats_to_cbs_shape / score_actual_points -- sfflf_tiered (needs
# TD-yardage lists + combined-yardage keys, unlike the flat-rate leagues)
# ---------------------------------------------------------------------------

def test_score_actual_points_f_league_rb_combines_yardage_and_applies_long_td_bonus():
    raw = {"rec": 3, "rush_yd": 150, "rec_yd": 60, "rush_td": 2, "rush_td_lng": 45}
    shaped = sleeper_stats_to_cbs_shape(raw, "RB", "sfflf_tiered")
    assert shaped["RuReYd"] == 210  # 150 + 60 -> tier(200-239) = 12
    assert shaped["RuTD"] == [45.0, 0.0]
    # yardage tier 12 + TD base(6)+long_bonus(6) for the 45yd TD + TD base(6)+no bonus for the 2nd
    assert score_actual_points(raw, "RB", "sfflf_tiered") == 30.0


def test_score_actual_points_f_league_qb_uses_pass_rush_combined_yards():
    raw = {"pass_yd": 250, "rush_yd": 30, "pass_td": 1, "pass_td_lng": 5}
    shaped = sleeper_stats_to_cbs_shape(raw, "QB", "sfflf_tiered")
    assert shaped["PaRuYd"] == 280  # tier(260-309) = 6
    # yardage tier 6 + PaTD base(6) + no long bonus (5yd < 10)
    assert score_actual_points(raw, "QB", "sfflf_tiered") == 12.0


def test_score_actual_points_returns_none_for_k_and_dst():
    assert score_actual_points({"fg_made": [45]}, "K", "standard_ppr") is None
    assert score_actual_points({"sacks": 3}, "DST", "sfflf_tiered") is None


# ---------------------------------------------------------------------------
# snap_share
# ---------------------------------------------------------------------------

def test_snap_share_computes_ratio():
    assert snap_share({"off_snp": 45, "tm_off_snp": 60}) == 0.75


def test_snap_share_none_when_missing():
    assert snap_share({"off_snp": 45}) is None
    assert snap_share({}) is None


# ---------------------------------------------------------------------------
# actual_points_for_roster -- Sleeper client calls mocked (no live network)
# ---------------------------------------------------------------------------

def _rs(name, position):
    return RosterSlot(player=Player(id=name, name=name, position=position),
                      slot="STARTER", is_starting=True)


def test_actual_points_for_roster_handles_matched_unmatched_and_no_stat_line():
    roster = [
        _rs("Jonathan Taylor", "RB"),   # matched, has a stat line
        _rs("Bye Week Guy", "RB"),      # matched, no stat line this week
        _rs("Some Rando", "WR"),        # not in Sleeper's name index at all
    ]
    fake_players = {"jt1": {"full_name": "Jonathan Taylor"},
                    "byeguy": {"full_name": "Bye Week Guy"}}
    fake_index = {"jonathan taylor": "jt1", "bye week guy": "byeguy"}
    fake_week_stats = {"jt1": {"rec": 5, "rush_yd": 80, "rec_yd": 30, "rush_td": 1,
                               "off_snp": 40, "tm_off_snp": 50}}

    with patch("sports.football.actuals.get_players", return_value=fake_players), \
         patch("sports.football.actuals.build_name_index", return_value=fake_index), \
         patch("sports.football.actuals.get_weekly_stats", return_value=fake_week_stats):
        result = actual_points_for_roster(roster, "hard_chargers", "standard_ppr", 2026, 2)

    assert result["Jonathan Taylor"]["sleeper_matched"] is True
    assert result["Jonathan Taylor"]["points"] == 22.0
    assert result["Jonathan Taylor"]["snap_share"] == 0.8

    assert result["Bye Week Guy"]["sleeper_matched"] is True
    assert result["Bye Week Guy"]["points"] is None
    assert result["Bye Week Guy"]["snap_share"] is None

    assert result["Some Rando"]["sleeper_matched"] is False
    assert result["Some Rando"]["points"] is None
