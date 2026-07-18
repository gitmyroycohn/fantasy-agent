"""
ENH 2 tests: multi-position eligibility from the full CBS player list,
per-league eligibility rules, and slot-legal-only lineup swaps.
"""
from data.models import Player, RosterSlot
from cbs.players import apply_league_eligibility_rules, fetch_position_eligibility_index
import cbs.players as players_mod


class _FakeAuth:
    def __init__(self, players):
        self._players = players

    def api_get(self, endpoint, league_id, sport, **params):
        assert endpoint == "players/list"
        return {"body": {"players": self._players}}


def test_player_eligible_positions_uses_override_when_present():
    p = Player(id="1", name="Multi Pos", position="2B",
               eligible_positions_override=["2B", "SS"])
    assert p.eligible_positions == ["2B", "SS"]


def test_player_eligible_positions_falls_back_to_position_when_no_override():
    p = Player(id="2", name="Slot Only", position="1B")
    assert p.eligible_positions == ["1B"]


def test_player_eligible_positions_normalizes_of_tags_with_override():
    p = Player(id="3", name="Outfielder", position="LF",
               eligible_positions_override=["LF", "CF"])
    assert p.eligible_positions == ["OF"]  # deduped after OF-normalization


def test_fetch_position_eligibility_index_builds_full_eligibility(monkeypatch):
    players_mod.clear_cache()
    fake_players = [
        {"id": "100", "position": "2B/SS"},
        {"id": "101", "position": "LF/CF/RF"},
        {"id": "102", "position": "1B"},
    ]
    auth = _FakeAuth(fake_players)
    index = fetch_position_eligibility_index(auth, "hemp", "baseball")
    assert index["100"] == ["2B", "SS"]
    assert index["101"] == ["OF"]  # LF/CF/RF normalized+deduped
    assert index["102"] == ["1B"]


def test_all_players_dh_eligible_rule_applied_for_pins_and_pills():
    league_cfg = {"eligibility": {"all_players_dh_eligible": True}}
    result = apply_league_eligibility_rules(["2B", "SS"], league_cfg)
    assert "DH" in result
    assert result[:2] == ["2B", "SS"]


def test_dh_rule_not_applied_when_league_does_not_set_it():
    league_cfg = {"eligibility": {"all_players_dh_eligible": False}}
    result = apply_league_eligibility_rules(["2B", "SS"], league_cfg)
    assert "DH" not in result


def test_dh_rule_no_duplicate_if_already_present():
    league_cfg = {"eligibility": {"all_players_dh_eligible": True}}
    result = apply_league_eligibility_rules(["DH", "1B"], league_cfg)
    assert result.count("DH") == 1
