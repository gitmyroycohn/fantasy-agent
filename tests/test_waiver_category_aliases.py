"""
Regression test: agent.decisions._waiver_adds_for_cats must recognize CBS's
REAL category keys ("S" for saves, "BA" for average) confirmed live on
2026-07-18, not just the previously-assumed names ("SV", "AVG") -- otherwise
losing_cats sourced from real CBS category names (as fixed for BUG 6) would
never match this function's internal CAT_POSITIONS dict / score-boost
checks, silently breaking saves/average waiver targeting.
"""
from agent.decisions import _waiver_adds_for_cats
from data.models import Player, WaiverPlayer


def _closer(name="Some Closer", sv=20, g=40, own=15.0):
    p = Player(id="1", name=name, position="RP", team="NYY",
               stats={"SV": sv, "G": g, "ERA": 3.0})
    return WaiverPlayer(player=p, ownership_pct=own)


def _high_avg_batter(name="Contact Hitter", avg=0.320, g=80, own=15.0):
    p = Player(id="2", name=name, position="2B", team="NYY",
               stats={"AVG": avg, "G": g})
    return WaiverPlayer(player=p, ownership_pct=own)


def test_saves_category_s_matches_reliever():
    recs = _waiver_adds_for_cats([_closer()], losing_cats=["S"])
    assert len(recs) == 1
    assert "S" in recs[0]["helps_cats"]


def test_saves_category_sv_still_matches_reliever_backward_compat():
    recs = _waiver_adds_for_cats([_closer()], losing_cats=["SV"])
    assert len(recs) == 1


def test_average_category_ba_matches_batter():
    recs = _waiver_adds_for_cats([_high_avg_batter()], losing_cats=["BA"])
    assert len(recs) == 1
    assert "BA" in recs[0]["helps_cats"]


def test_average_category_avg_still_matches_batter_backward_compat():
    recs = _waiver_adds_for_cats([_high_avg_batter()], losing_cats=["AVG"])
    assert len(recs) == 1
