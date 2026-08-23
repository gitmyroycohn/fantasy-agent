"""
Tests for agent/football_decisions.py (the football decision-pipeline
wiring) and the "roster_legality"/"waiver_targets"/"keeper_guidance"
renderers added to agent/main.py::_print_decisions().

cbs.waivers.fetch_waiver_wire is mocked -- there is no live CBS football
data to fetch against yet (see module docstrings). fd._fp_nfl_rankings_by_name
is also patched in most tests to avoid a real network call to FantasyPros
during the test run (it degrades safely to {} on failure in production, but
tests shouldn't depend on real network access or API quota). These tests
verify the wiring and output shape, not real CBS/FantasyPros API behavior.
"""

from unittest.mock import patch

from data.models import Player, RosterSlot, Team, WaiverPlayer

import agent.football_decisions as fd
from agent.main import _print_decisions


def _starter(name, position):
    return RosterSlot(player=Player(id=name, name=name, position=position),
                       slot="STARTER", is_starting=True)


def _bench(name, position):
    return RosterSlot(player=Player(id=name, name=name, position=position),
                       slot="BENCH", is_starting=False)


def _legal_f_league_roster():
    starters = [
        _starter("QB1", "QB"), _starter("RB1", "RB"), _starter("WR1", "WR"),
        _starter("TE1", "TE"), _starter("K1", "K"), _starter("DST1", "DST"),
        _starter("FlexTE", "TE"), _starter("FlexRB1", "RB"), _starter("FlexWR1", "WR"),
    ]
    bench = [_bench(f"Bench{i}", "WR") for i in range(8)]
    return starters + bench


_F_LEAGUE_CONFIG = {
    "id": "f_league", "name": "F-League",
    "cbs_league_id": "sfflf", "cbs_team_id": "11",
    "format": "h2h_points", "scoring_profile": "sfflf_tiered",
}

_EAST_COAST_CONFIG = {
    "id": "east_coast", "name": "East Coast Fantasy Football League",
    "cbs_league_id": "ecfc", "cbs_team_id": "5",
    "format": "h2h_points", "scoring_profile": "standard_ppr_strict",
}


def test_legal_roster_produces_no_waiver_fetch():
    roster = _legal_f_league_roster()
    team = Team(id="11", name="COWBOYS", roster=roster)

    with patch.object(fd, "fetch_waiver_wire") as mock_fetch, \
         patch.object(fd, "_fp_nfl_rankings_by_name", return_value={}):
        result = fd.run_football_decisions(auth=None, league_id="sfflf",
                                            league_config=_F_LEAGUE_CONFIG, team=team)
        mock_fetch.assert_not_called()

    assert result["league"] == "F-League"
    assert result["format"] == "H2H Points"
    actions = result["actions"]
    types = [a["type"] for a in actions]
    assert types == ["roster_legality", "keeper_guidance"]
    assert actions[0]["legal"] is True
    assert actions[0]["starters_present"] == 9

    # f_league: keeper league, max 2 (confirmed directly by Christopher,
    # not visible via CBS's UI since he isn't f_league's commissioner). No
    # rankings mocked in here, so no picks should be guessed either.
    kg = actions[1]
    assert kg["is_keeper_league"] is True
    assert kg["max_keepers"] == 2
    assert kg["recommended_keeps"] == []
    assert "confirmed by christopher" in kg["note"].lower()


def test_illegal_roster_fetches_waivers_and_targets_open_slot():
    roster = _legal_f_league_roster()
    roster[0] = _starter("NotAQB", "WR")  # opens the Quarterback slot
    team = Team(id="11", name="COWBOYS", roster=roster)

    fake_wire = [
        WaiverPlayer(player=Player(id="sleeper", name="Sleeper QB", position="QB"),
                     ownership_pct=5.0),
        WaiverPlayer(player=Player(id="popular", name="Popular QB", position="QB"),
                     ownership_pct=80.0),
        WaiverPlayer(player=Player(id="irrelevant", name="Irrelevant WR", position="WR"),
                     ownership_pct=1.0),
    ]

    with patch.object(fd, "fetch_waiver_wire", return_value=fake_wire) as mock_fetch, \
         patch.object(fd, "_fp_nfl_rankings_by_name", return_value={}):
        result = fd.run_football_decisions(auth=None, league_id="sfflf",
                                            league_config=_F_LEAGUE_CONFIG, team=team)
        mock_fetch.assert_called_once()

    actions = result["actions"]
    types = [a["type"] for a in actions]
    assert types == ["roster_legality", "waiver_targets", "keeper_guidance"]
    assert actions[0]["legal"] is False

    by_slot = actions[1]["by_slot"]
    assert "Quarterback" in by_slot
    names = [p["player"] for p in by_slot["Quarterback"]]
    assert names == ["Sleeper QB", "Popular QB"]  # ownership-ascending
    assert all(p["player"] != "Irrelevant WR" for p in by_slot.get("Quarterback", []))


def test_waiver_fetch_failure_is_caught_and_omits_waiver_targets():
    roster = _legal_f_league_roster()
    roster[0] = _starter("NotAQB", "WR")
    team = Team(id="11", name="COWBOYS", roster=roster)

    with patch.object(fd, "fetch_waiver_wire", side_effect=RuntimeError("CBS API down")), \
         patch.object(fd, "_fp_nfl_rankings_by_name", return_value={}):
        result = fd.run_football_decisions(auth=None, league_id="sfflf",
                                            league_config=_F_LEAGUE_CONFIG, team=team)

    types = [a["type"] for a in result["actions"]]
    assert types == ["roster_legality", "keeper_guidance"]  # no waiver_targets, but no crash


def test_east_coast_keeper_guidance_uses_fp_rankings_when_available():
    roster = [
        _starter("Jalen Hurts", "QB"), _starter("Tony Pollard", "RB"),
        _starter("Stefon Diggs", "WR"), _starter("Jake Ferguson", "TE"),
        _starter("K1", "K"), _starter("DST1", "DST"),
        _starter("Flex1", "RB"),
    ]
    bench = [_bench(f"Bench{i}", "WR") for i in range(9)]
    team = Team(id="5", name="Hotlanta Hussies", roster=roster + bench)

    fake_rankings = {
        "jalen hurts": 5.0, "tony pollard": 40.0,
        "stefon diggs": 25.0, "jake ferguson": 90.0,
    }

    with patch.object(fd, "fetch_waiver_wire", return_value=[]), \
         patch.object(fd, "_fp_nfl_rankings_by_name", return_value=fake_rankings):
        result = fd.run_football_decisions(auth=None, league_id="ecfc",
                                            league_config=_EAST_COAST_CONFIG, team=team)

    kg = next(a for a in result["actions"] if a["type"] == "keeper_guidance")
    assert kg["is_keeper_league"] is True
    assert kg["max_keepers"] == 3
    assert kg["ranking_source"] == "fantasypros_ecr"
    assert kg["recommended_keeps"] == ["Jalen Hurts", "Stefon Diggs", "Tony Pollard"]
    # fetch_contract_years isn't mocked in this test, and auth=None here,
    # so the real fetch inside _east_coast_contract_data() fails (no
    # .fetch_league_page on None) and is caught -- contract_data stays
    # None, same safe fallback as "no contract data supplied". See
    # test_east_coast_contract_expiration_excludes_player_via_live_wiring
    # below for the real wiring exercised end-to-end with a mock.
    assert kg["contract_expired"] == []


def test_east_coast_contract_expiration_excludes_player_via_live_wiring():
    # End-to-end exercise of the 2026-08-23 fix: fetch_contract_years()
    # (the CBS CONTRACT-column scrape) is mocked; everything downstream --
    # conversion to acquired_season, normalized-name matching against the
    # roster, and exclusion from keeper eligibility -- runs for real
    # through run_football_decisions(). This is the actual bug: Jonathan
    # Taylor is the #1 ECR-ranked keeper candidate here but has an expired
    # contract and must never be recommended.
    roster = [
        _starter("Jonathan Taylor", "RB"), _starter("Saquon Barkley", "RB"),
        _starter("Drake Maye", "QB"), _starter("Stefon Diggs", "WR"),
        _starter("Jake Ferguson", "TE"), _starter("K1", "K"), _starter("DST1", "DST"),
    ]
    bench = [_bench(f"Bench{i}", "WR") for i in range(9)]
    team = Team(id="5", name="Hotlanta Hussies", roster=roster + bench)

    fake_rankings = {
        "jonathan taylor": 1.0, "saquon barkley": 10.0, "drake maye": 20.0,
        "stefon diggs": 30.0, "jake ferguson": 40.0,
    }
    # Deliberately mixed-case / padded -- this comes from a different CBS
    # page (HTML scrape) than the roster names above, and the matching in
    # _east_coast_contract_data() must normalize, not require an exact
    # string match between the two sources.
    fake_contract_years = {"jonathan taylor ": 0, " Saquon Barkley": 1, "DRAKE MAYE": 2}

    with patch.object(fd, "fetch_waiver_wire", return_value=[]), \
         patch.object(fd, "_fp_nfl_rankings_by_name", return_value=fake_rankings), \
         patch.object(fd, "fetch_contract_years", return_value=fake_contract_years):
        result = fd.run_football_decisions(auth=object(), league_id="ecfc",
                                            league_config=_EAST_COAST_CONFIG, team=team)

    kg = next(a for a in result["actions"] if a["type"] == "keeper_guidance")
    assert kg["contract_expired"] == ["Jonathan Taylor"]
    assert "Jonathan Taylor" not in kg["recommended_keeps"]
    assert "Jonathan Taylor" not in kg["other_eligible"]
    assert kg["recommended_keeps"][0] == "Saquon Barkley"


def test_east_coast_contract_fetch_failure_does_not_crash():
    # fetch_contract_years() raising (auth problem, CBS page format
    # changed, etc.) must degrade to "no contract data" like every other
    # FantasyPros-adjacent adapter in this module, never propagate.
    roster = [_starter("Jalen Hurts", "QB")]
    bench = [_bench(f"Bench{i}", "WR") for i in range(9)]
    team = Team(id="5", name="Hotlanta Hussies", roster=roster + bench)

    with patch.object(fd, "fetch_waiver_wire", return_value=[]), \
         patch.object(fd, "_fp_nfl_rankings_by_name", return_value={}), \
         patch.object(fd, "fetch_contract_years", side_effect=RuntimeError("CBS page changed")):
        result = fd.run_football_decisions(auth=object(), league_id="ecfc",
                                            league_config=_EAST_COAST_CONFIG, team=team)

    kg = next(a for a in result["actions"] if a["type"] == "keeper_guidance")
    assert kg["contract_expired"] == []


def test_fp_rankings_adapter_returns_empty_dict_when_no_client():
    # _fp_client is None in this test environment (no FANTASYPROS_API_KEY
    # set), so the adapter must short-circuit to {} rather than error.
    assert fd._fp_nfl_rankings_by_name(None) == {}


def test_print_decisions_renders_roster_legality_waiver_targets_and_keepers(capsys):
    result = {
        "league": "F-League",
        "format": "H2H Points",
        "actions": [
            {
                "type": "roster_legality",
                "legal": False,
                "starters_required": 9,
                "starters_present": 8,
                "issues": ["You have an unfilled Quarterback slot (1 short)."],
                "unfilled_slots": ["Quarterback"],
            },
            {
                "type": "waiver_targets",
                "by_slot": {
                    "Quarterback": [
                        {"player": "Sleeper QB", "team": "KC", "positions": ["QB"],
                         "ownership_pct": 5.0},
                    ],
                },
            },
            {
                "type": "keeper_guidance",
                "is_keeper_league": True,
                "max_keepers": 3,
                "decided_by": "individual_manager",
                "selection_deadline": "2025-08-31",
                "note": "Deadline is stale, ask the commissioner to update it.",
                "recommended_keeps": ["Jalen Hurts", "Stefon Diggs", "Tony Pollard"],
                "other_eligible": ["Jake Ferguson"],
                "ranking_source": "fantasypros_ecr",
            },
        ],
    }
    _print_decisions(result, dry_run=True)
    out = capsys.readouterr().out
    assert "ILLEGAL" in out
    assert "unfilled Quarterback slot" in out
    assert "Open-Slot Waiver Targets" in out
    assert "Sleeper QB" in out
    assert "Recommended keeps (via fantasypros_ecr): Jalen Hurts, Stefon Diggs, Tony Pollard" in out
    assert "Other eligible: Jake Ferguson" in out
    assert "DRY_RUN=True" in out


def test_print_decisions_renders_contract_expired_players(capsys):
    result = {
        "league": "East Coast Fantasy Football League",
        "format": "H2H Points",
        "actions": [
            {"type": "roster_legality", "legal": True, "starters_required": 9,
             "starters_present": 9, "issues": [], "unfilled_slots": []},
            {
                "type": "keeper_guidance",
                "is_keeper_league": True,
                "max_keepers": 3,
                "decided_by": "individual_manager",
                "selection_deadline": "2025-08-31",
                "note": "Contract check applied for season 2026: 1 player(s) have an expired 3-year contract.",
                "recommended_keeps": ["Jalen Hurts"],
                "other_eligible": ["Jake Ferguson"],
                "ranking_source": "fantasypros_ecr",
                "contract_expired": ["Old Timer"],
            },
        ],
    }
    _print_decisions(result, dry_run=True)
    out = capsys.readouterr().out
    assert "Contract expired (not keeper-eligible, to draft pool): Old Timer" in out


def test_print_decisions_renders_non_keeper_league_note(capsys):
    result = {
        "league": "Hard Chargers Fantasy League",
        "format": "H2H Points",
        "actions": [
            {"type": "roster_legality", "legal": True, "starters_required": 9,
             "starters_present": 9, "issues": [], "unfilled_slots": []},
            {"type": "keeper_guidance", "is_keeper_league": False, "max_keepers": 0,
             "decided_by": None, "selection_deadline": None,
             "note": "Not a keeper league -- straight redraft every year.",
             "recommended_keeps": [], "other_eligible": [], "ranking_source": None},
        ],
    }
    _print_decisions(result, dry_run=True)
    out = capsys.readouterr().out
    assert "Not a keeper league" in out
