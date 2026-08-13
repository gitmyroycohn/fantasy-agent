"""
Tests for sports/football/keepers.py.

Real facts encoded in KEEPER_POLICIES (verified 2026-08-01, see module
docstring): east_coast has a live-confirmed 3-keeper cap, individual-manager
selection, plus a Christopher-authored 3-year-contract house rule (confirmed
2026-08-13) -- players carry a 3-fantasy-season contract from acquisition
that survives fantasy/real-life trades and expires after season 3, at which
point they can't be kept and re-enter the draft pool. f_league has a
2-keeper cap, any position, no cost mechanic, confirmed directly by
Christopher (not visible via CBS's UI since he isn't f_league's
commissioner); hard_chargers isn't a keeper league at all. These tests
check the guidance logic respects that -- especially that it never
fabricates a ranking or a contract expiration when the underlying data
isn't supplied.
"""

import pytest

from data.models import Player, RosterSlot
from sports.football.keepers import (
    keeper_guidance, KEEPER_POLICIES, contract_status, CONTRACT_YEARS,
)


def _slot(name, position):
    return RosterSlot(player=Player(id=name, name=name, position=position),
                       slot="ROSTER", is_starting=True)


def test_hard_chargers_is_not_a_keeper_league():
    roster = [_slot("Player A", "WR")]
    result = keeper_guidance(roster, "hard_chargers")
    assert result.is_keeper_league is False
    assert result.max_keepers == 0
    assert result.recommended_keeps == []
    assert "not a keeper league" in result.note.lower()


def test_f_league_known_cap_of_2_any_position():
    assert KEEPER_POLICIES["f_league"]["max_keepers"] == 2
    assert KEEPER_POLICIES["f_league"]["cost_mechanic"] == "none"


def test_f_league_with_rankings_recommends_top_2():
    roster = [_slot(n, "WR") for n in ["Player A", "Player B", "Player C"]]
    rankings = {"player a": 10, "player b": 50, "player c": 30}
    result = keeper_guidance(roster, "f_league", rankings=rankings,
                              ranking_source="fantasypros_ecr")
    assert result.max_keepers == 2
    # Best 2 by rank (lower = better): A(10), C(30)
    assert result.recommended_keeps == ["Player A", "Player C"]
    assert result.other_eligible == ["Player B"]


def test_f_league_without_rankings_lists_full_roster_no_pick():
    roster = [_slot("Player A", "WR")]
    result = keeper_guidance(roster, "f_league")
    assert result.is_keeper_league is True
    assert result.max_keepers == 2
    assert result.recommended_keeps == []  # no ranking signal -> no guessed picks
    assert result.other_eligible == ["Player A"]
    assert "confirmed by christopher" in result.note.lower()


def test_east_coast_known_cap_of_3():
    assert KEEPER_POLICIES["east_coast"]["max_keepers"] == 3
    assert KEEPER_POLICIES["east_coast"]["cost_mechanic"] == "contract"
    assert KEEPER_POLICIES["east_coast"]["contract_years"] == 3


def test_east_coast_without_rankings_lists_full_roster_no_pick():
    roster = [_slot(n, "WR") for n in ["A", "B", "C", "D"]]
    result = keeper_guidance(roster, "east_coast")
    assert result.max_keepers == 3
    assert result.recommended_keeps == []  # no ranking signal -> no guessed picks
    assert set(result.other_eligible) == {"A", "B", "C", "D"}


def test_east_coast_with_rankings_recommends_top_3():
    roster = [_slot(n, "WR") for n in
              ["Jalen Hurts", "Tony Pollard", "Stefon Diggs", "Jake Ferguson"]]
    rankings = {
        "jalen hurts": 5,
        "tony pollard": 40,
        "stefon diggs": 25,
        "jake ferguson": 90,
    }
    result = keeper_guidance(roster, "east_coast", rankings=rankings,
                              ranking_source="fantasypros_ecr")
    # Best 3 by rank (lower = better): Hurts(5), Diggs(25), Pollard(40)
    assert result.recommended_keeps == ["Jalen Hurts", "Stefon Diggs", "Tony Pollard"]
    assert result.other_eligible == ["Jake Ferguson"]
    assert result.ranking_source == "fantasypros_ecr"


def test_east_coast_unranked_players_never_fill_a_recommended_slot():
    roster = [_slot(n, "WR") for n in ["Ranked Guy", "Mystery Guy"]]
    rankings = {"ranked guy": 10}
    result = keeper_guidance(roster, "east_coast", rankings=rankings)
    # max_keepers=3 but only 1 player has a real ranking -- the cap having
    # room left over must NOT pull an unranked player into "recommended".
    assert result.recommended_keeps == ["Ranked Guy"]
    assert result.other_eligible == ["Mystery Guy"]


def test_unknown_league_id_raises():
    with pytest.raises(ValueError):
        keeper_guidance([], "not_a_real_league")


# --- 3-year contract rule (east_coast only) -------------------------------

def test_contract_status_math_mid_contract():
    # Acquired 2024, evaluating 2026 -> season 3 of the contract, not expired.
    status = contract_status("Player X", acquired_season=2024, current_season=2026)
    assert status.years_elapsed == 3
    assert status.expires_after_season == 2026
    assert status.is_expired is False


def test_contract_status_math_expired():
    # Acquired 2023, evaluating 2026 -> would-be season 4, expired.
    status = contract_status("Player X", acquired_season=2023, current_season=2026)
    assert status.years_elapsed == 4
    assert status.expires_after_season == 2025
    assert status.is_expired is True


def test_contract_status_first_season_not_expired():
    status = contract_status("Player X", acquired_season=2026, current_season=2026)
    assert status.years_elapsed == 1
    assert status.is_expired is False


def test_east_coast_without_contract_data_flags_missing_and_excludes_nobody():
    roster = [_slot(n, "WR") for n in ["A", "B"]]
    result = keeper_guidance(roster, "east_coast")
    assert result.contract_expired == []
    assert set(result.other_eligible) == {"A", "B"}
    assert "no contract data supplied" in result.note.lower()


def test_east_coast_contract_data_excludes_expired_player_no_rankings():
    roster = [_slot(n, "WR") for n in ["Old Contract", "Fresh Contract"]]
    contract_data = {"Old Contract": 2023, "Fresh Contract": 2025}
    result = keeper_guidance(roster, "east_coast",
                              contract_data=contract_data, current_season=2026)
    assert result.contract_expired == ["Old Contract"]
    assert result.other_eligible == ["Fresh Contract"]
    assert "1 player(s) have an expired 3-year contract" in result.note


def test_east_coast_contract_data_excludes_expired_player_with_rankings():
    roster = [_slot(n, "WR") for n in
              ["Old Contract", "Fresh Contract A", "Fresh Contract B", "Fresh Contract C"]]
    contract_data = {"Old Contract": 2022, "Fresh Contract A": 2026,
                      "Fresh Contract B": 2026, "Fresh Contract C": 2026}
    rankings = {"old contract": 1.0, "fresh contract a": 10.0,
                "fresh contract b": 20.0, "fresh contract c": 30.0}
    result = keeper_guidance(roster, "east_coast", rankings=rankings,
                              ranking_source="fantasypros_ecr",
                              contract_data=contract_data, current_season=2026)
    # Old Contract has the best rank but is expired -- must never appear in
    # recommended_keeps or other_eligible, only contract_expired.
    assert result.contract_expired == ["Old Contract"]
    assert "Old Contract" not in result.recommended_keeps
    assert "Old Contract" not in result.other_eligible
    assert result.recommended_keeps == ["Fresh Contract A", "Fresh Contract B", "Fresh Contract C"]


def test_east_coast_traded_player_contract_follows_original_acquisition():
    # House rule: a fantasy-league or real-life trade does NOT reset the
    # contract clock -- acquired_season stays whatever it originally was.
    roster = [_slot("Traded Guy", "WR")]
    contract_data = {"Traded Guy": 2023}  # acquired in 2023, traded since, still 2023
    result = keeper_guidance(roster, "east_coast",
                              contract_data=contract_data, current_season=2026)
    assert result.contract_expired == ["Traded Guy"]


def test_east_coast_partial_contract_data_leaves_unknown_players_eligible():
    # A player with no entry in contract_data isn't guessed at -- stays
    # eligible rather than being assumed expired or assumed fresh.
    roster = [_slot(n, "WR") for n in ["Known", "Unknown"]]
    contract_data = {"Known": 2020}  # long expired
    result = keeper_guidance(roster, "east_coast",
                              contract_data=contract_data, current_season=2026)
    assert result.contract_expired == ["Known"]
    assert result.other_eligible == ["Unknown"]


def test_f_league_ignores_contract_data_no_contract_rule_there():
    # Contract rule is east_coast-only -- passing contract_data for f_league
    # must be a no-op, not an error and not a silent filter.
    roster = [_slot(n, "WR") for n in ["Player A", "Player B"]]
    contract_data = {"Player A": 2020}  # would be long-expired under ecfc's rule
    result = keeper_guidance(roster, "f_league",
                              contract_data=contract_data, current_season=2026)
    assert result.contract_expired == []
    assert set(result.other_eligible) == {"Player A", "Player B"}
    assert "contract" not in result.note.lower()
