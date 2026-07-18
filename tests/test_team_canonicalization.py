"""
Regression tests for the team-abbreviation mismatch found in the 2026-07-18
live run: CBS's own player.team field returns the short MLB-native form
("SF", "TB", "KC", "SD") while the MLB schedule feed, after mlb_to_cbs(),
produces the longer form ("SFG", "TBR", "KCR", "SDP") -- these never
compared equal, so e.g. Landen Roupp (SF) was wrongly flagged "SF has no
game today" on a day the Giants played.
"""
from mlb.teams import canonical_team
from sports.baseball.lineup_optimizer import optimize_daily_lineup
from sports.baseball.categories import check_nl_eligibility, filter_nl_waiver_pool
from data.models import Player, WaiverPlayer


def test_canonical_team_unifies_short_and_long_forms():
    assert canonical_team("SF") == canonical_team("SFG")
    assert canonical_team("TB") == canonical_team("TBR")
    assert canonical_team("KC") == canonical_team("KCR")
    assert canonical_team("SD") == canonical_team("SDP")


def test_canonical_team_passthrough_for_unaffected_teams():
    assert canonical_team("NYY") == "NYY"
    assert canonical_team("bos") == "BOS"


def test_sf_pitcher_not_confirmed_starter_is_recognized_as_teams_playing():
    """The exact live-run scenario: a bench SP (not today's probable
    starter) on a team CBS tags "SF" must be recognized as "team has a
    game today" when the schedule-derived teams_playing set holds "SFG"."""
    lineup_slots = [{
        "player_name": "Landen Roupp", "team": "SF", "positions": ["SP"],
        "eligible_positions": ["SP"], "slot": "BN", "is_starting": False,
        "stats": {},
    }]
    teams_playing = {"SFG"} | {f"T{i}" for i in range(10)}  # schedule-derived, mapped form
    advice = optimize_daily_lineup(lineup_slots, teams_playing, probable_starters=set())
    a = advice[0]
    assert a.advice != "bench_pitcher"
    assert "no game today" not in a.reason


def test_platoon_opp_hand_lookup_works_across_sf_alias():
    lineup_slots = [{
        "player_name": "SF Batter", "team": "SF", "positions": ["OF"],
        "eligible_positions": ["OF"], "slot": "OF", "is_starting": True,
        "stats": {"split_vs_l_ops": 0.550, "split_vs_r_ops": 0.900},
    }]
    teams_playing = {"SFG"} | {f"T{i}" for i in range(10)}
    advice = optimize_daily_lineup(lineup_slots, teams_playing, set(),
                                   opp_hand_by_team={"SFG": "L"})
    assert advice[0].advice == "bench"
    assert "platoon" in advice[0].reason.lower()


def test_nl_eligibility_recognizes_short_form_sd_sf():
    players = [
        Player(id="1", name="Giants Guy", position="SP", team="SF"),
        Player(id="2", name="Padres Guy", position="1B", team="SD"),
    ]
    warnings = check_nl_eligibility(players)
    assert warnings == []  # neither is on an AL team -- no false-positive warning


def test_nl_waiver_pool_includes_short_form_sd_sf():
    waivers = [
        WaiverPlayer(player=Player(id="1", name="Giants Guy", position="SP", team="SF")),
        WaiverPlayer(player=Player(id="2", name="Padres Guy", position="1B", team="SD")),
        WaiverPlayer(player=Player(id="3", name="Yankee Guy", position="1B", team="NYY")),
    ]
    result = filter_nl_waiver_pool(waivers, {"roster_type": "nl_only"})
    names = {wp.player.name for wp in result}
    assert names == {"Giants Guy", "Padres Guy"}
