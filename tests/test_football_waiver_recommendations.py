"""
Tests for sports/football/waivers.py::rank_waiver_recommendations() -- the
flattening/ranking layer added for the football_waiver_recommendations MCP
tool (2026-08-31 enhancement order). find_waiver_candidates_for_open_slots()
itself (roster-fit + per-league projection ranking) is already covered
elsewhere; these tests focus on flattening, dedup, position filter, limit,
and the f_league-vs-PPR-league differentiation the ticket's acceptance
criteria #3 asks for.
"""

from data.models import Player, RosterSlot, WaiverPlayer
from sports.football.waivers import rank_waiver_recommendations


def _starter(name, position):
    return RosterSlot(player=Player(id=name, name=name, position=position),
                       slot="STARTER", is_starting=True)


def _bench(name, position):
    return RosterSlot(player=Player(id=name, name=name, position=position),
                       slot="BENCH", is_starting=False)


def _fa(pid, name, position, ownership_pct=10.0):
    return WaiverPlayer(player=Player(id=pid, name=name, position=position),
                        ownership_pct=ownership_pct)


def _f_league_roster_with_open_wr_and_flex():
    # f_league starting slots: QB, RB, WR, TE, K, DST, Flex(TE/RB/WR combo
    # per config) -- leave WR and one flex-eligible slot empty.
    starters = [
        _starter("QB1", "QB"), _starter("RB1", "RB"),
        _starter("TE1", "TE"), _starter("K1", "K"), _starter("DST1", "DST"),
    ]
    bench = [_bench(f"Bench{i}", "WR") for i in range(3)]
    return starters + bench


def test_flattens_and_dedupes_a_player_eligible_for_two_open_slots():
    roster = _f_league_roster_with_open_wr_and_flex()
    waivers = [_fa("1", "Multi Slot WR", "WR", ownership_pct=40.0)]
    recs = rank_waiver_recommendations(roster, waivers, "f_league")
    assert len(recs) == 1
    assert recs[0]["player"].player.name == "Multi Slot WR"
    assert len(recs[0]["slots"]) >= 1  # eligible for whichever open slots matched
    assert len(set(recs[0]["slots"])) == len(recs[0]["slots"])  # no duplicate slot listed twice for same player... 


def test_position_filter_excludes_non_matching_players():
    roster = _f_league_roster_with_open_wr_and_flex()
    waivers = [
        _fa("1", "Some WR", "WR", ownership_pct=40.0),
        _fa("2", "Some RB", "RB", ownership_pct=90.0),
    ]
    recs = rank_waiver_recommendations(roster, waivers, "f_league", position="WR")
    names = [r["player"].player.name for r in recs]
    assert "Some WR" in names
    assert "Some RB" not in names


def test_limit_caps_result_count():
    roster = _f_league_roster_with_open_wr_and_flex()
    waivers = [_fa(str(i), f"WR{i}", "WR", ownership_pct=float(i)) for i in range(20)]
    recs = rank_waiver_recommendations(roster, waivers, "f_league", limit=3)
    assert len(recs) == 3


def test_no_projections_falls_back_to_ownership_pct_ascending():
    roster = _f_league_roster_with_open_wr_and_flex()
    waivers = [
        _fa("1", "High Owned", "WR", ownership_pct=80.0),
        _fa("2", "Low Owned", "WR", ownership_pct=5.0),
    ]
    recs = rank_waiver_recommendations(roster, waivers, "f_league")
    names = [r["player"].player.name for r in recs]
    assert names.index("Low Owned") < names.index("High Owned")
    assert all(r["projected_points"] is None for r in recs)


def test_scoring_format_differentiates_f_league_from_ppr_leagues():
    # Same underlying free-agent pool, same open-slot shape, but different
    # per-league projection maps (as agent/football_decisions.py supplies:
    # sfflf-estimate for f_league, points_ppr for hard_chargers/east_coast)
    # must be able to rank the same two players in opposite order --
    # acceptance criteria #3 in the 2026-08-31 enhancement order.
    roster = _f_league_roster_with_open_wr_and_flex()
    waivers = [
        _fa("1", "Volume Slot Guy", "WR", ownership_pct=20.0),
        _fa("2", "Big Play Guy", "WR", ownership_pct=20.0),
    ]
    # f_league (non-PPR, tier/TD-driven): Big Play Guy scores higher
    sfflf_estimate = {"volume slot guy": 8.0, "big play guy": 15.0}
    # PPR leagues: Volume Slot Guy (more catches/targets) scores higher
    ppr_points = {"volume slot guy": 15.0, "big play guy": 8.0}

    f_league_recs = rank_waiver_recommendations(
        roster, waivers, "f_league", projections=sfflf_estimate)
    hard_chargers_recs = rank_waiver_recommendations(
        roster, waivers, "hard_chargers", projections=ppr_points)

    assert f_league_recs[0]["player"].player.name == "Big Play Guy"
    assert hard_chargers_recs[0]["player"].player.name == "Volume Slot Guy"
