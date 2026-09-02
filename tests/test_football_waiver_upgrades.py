"""
Tests for the upgrade-candidate tier added to sports/football/waivers.py
2026-09-02, after Christopher reported that a fully-legal, fully-started
roster (the normal state once the season's under way) got zero
recommendations from find_waiver_candidates_for_open_slots()'s gap-only
filtering -- correctly flagged as "that's not how it should work." See
find_upgrade_candidates()'s docstring for the full context.
"""

from data.models import Player, RosterSlot, WaiverPlayer
from sports.football.roster_rules import slot_occupants
from sports.football.waivers import (
    find_upgrade_candidates, rank_waiver_recommendations, UPGRADE_MIN_POINT_EDGE,
)


def _starter(name, position):
    return RosterSlot(player=Player(id=name, name=name, position=position),
                       slot="STARTER", is_starting=True)


def _bench(name, position):
    return RosterSlot(player=Player(id=name, name=name, position=position),
                       slot="BENCH", is_starting=False)


def _fa(pid, name, position, ownership_pct=10.0):
    return WaiverPlayer(player=Player(id=pid, name=name, position=position),
                        ownership_pct=ownership_pct)


def _full_f_league_roster():
    starters = [
        _starter("QB1", "QB"), _starter("RB1", "RB"), _starter("WR1", "WR"),
        _starter("TE1", "TE"), _starter("K1", "K"), _starter("DST1", "DST"),
        _starter("FlexTE", "TE"), _starter("FlexRB1", "RB"), _starter("FlexWR1", "WR"),
    ]
    bench = [_bench(f"Bench{i}", "WR") for i in range(8)]
    return starters + bench


def test_full_roster_still_gets_upgrade_recommendations():
    # This is the exact bug report: fully-legal, fully-started roster used
    # to get nothing back at all.
    roster = _full_f_league_roster()
    waivers = [_fa("100", "Breakout WR", "WR", ownership_pct=15.0)]
    projections = {"wr1": 8.0, "breakout wr": 20.0}
    recs = rank_waiver_recommendations(roster, waivers, "f_league", projections=projections)
    assert len(recs) == 1
    assert recs[0]["player"].player.name == "Breakout WR"
    assert recs[0]["upgrade_over"] == ("WR1", 12.0)


def test_upgrade_requires_minimum_point_edge():
    roster = _full_f_league_roster()
    # Only 0.5 pts better than the starter -- below UPGRADE_MIN_POINT_EDGE (1.0)
    waivers = [_fa("100", "Marginal WR", "WR", ownership_pct=15.0)]
    projections = {"wr1": 8.0, "marginal wr": 8.5}
    recs = rank_waiver_recommendations(roster, waivers, "f_league", projections=projections)
    assert recs == []


def test_no_upgrade_when_free_agent_projects_worse():
    roster = _full_f_league_roster()
    waivers = [_fa("100", "Worse WR", "WR", ownership_pct=15.0)]
    projections = {"wr1": 15.0, "worse wr": 5.0}
    recs = rank_waiver_recommendations(roster, waivers, "f_league", projections=projections)
    assert recs == []


def test_no_upgrades_fabricated_without_projections():
    # No projections supplied at all -- must not guess an upgrade off
    # ownership_pct; full roster + no signal = no recommendations, honestly.
    roster = _full_f_league_roster()
    waivers = [_fa("100", "High Owned WR", "WR", ownership_pct=90.0)]
    recs = rank_waiver_recommendations(roster, waivers, "f_league")
    assert recs == []


def test_occupant_with_no_projection_match_is_skipped_not_guessed():
    roster = _full_f_league_roster()
    waivers = [_fa("100", "Some WR", "WR", ownership_pct=15.0)]
    # "WR1" (the starter) has no entry in projections -- can't honestly compare.
    projections = {"some wr": 20.0}
    recs = rank_waiver_recommendations(roster, waivers, "f_league", projections=projections)
    assert recs == []


def test_slot_occupants_reports_who_is_actually_starting():
    roster = _full_f_league_roster()
    occupants = slot_occupants(roster, "f_league")
    assert occupants["Wide Receiver"][0].player.name == "WR1"
    assert occupants["Quarterback"][0].player.name == "QB1"


def test_find_upgrade_candidates_skips_unfilled_slots():
    # A roster with an empty starting slot shouldn't get "upgrade" noise for
    # that slot -- that's find_waiver_candidates_for_open_slots()'s job.
    roster = [rs for rs in _full_f_league_roster() if rs.player.name != "QB1"]
    waivers = [_fa("100", "Backup QB", "QB", ownership_pct=15.0)]
    projections = {"backup qb": 10.0}
    upgrades = find_upgrade_candidates(roster, waivers, "f_league", projections=projections)
    assert "Quarterback" not in upgrades
