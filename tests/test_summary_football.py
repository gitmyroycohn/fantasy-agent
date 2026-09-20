"""agent/summary.py TL;DR: football leagues (format "H2H Points") used to hit the
baseball "H2H" branch and print an empty, mislabeled PINS & PILLS header."""
import pytest

from agent import summary


@pytest.fixture(autouse=True)
def _no_image(monkeypatch):
    monkeypatch.setattr(summary, "_random_image_block", lambda: "")


def _football(name, actions):
    return {"league": name, "format": "H2H Points", "actions": actions}


def test_football_league_uses_its_own_name_not_pins_and_pills():
    out = summary.format_tldr([_football("East Coast Fantasy Football League", [])])
    assert "[ EAST COAST FANTASY FOOTBALL LEAGUE  |  H2H Points ]" in out
    assert "PINS & PILLS" not in out


def test_football_actions_produce_lines():
    out = summary.format_tldr([_football("F-League", [
        {"type": "roster_legality", "legal": False,
         "issues": ["You have 10 reserve players.", "You have 20 total players."]},
        {"type": "waiver_targets", "fa_source": "fantasypros_fallback",
         "by_slot": {"WR": [{"player": "Jalen Coker", "team": "CAR",
                             "positions": ["WR"]}]}},
        {"type": "keeper_guidance", "is_keeper_league": True,
         "recommended_keeps": ["Saquon Barkley", "Trey McBride"]},
    ])])
    assert "!! Roster ILLEGAL: You have 10 reserve players. (+1 more)" in out
    assert "Top Add  : Jalen Coker (CAR) [WR]" in out
    assert "FantasyPros" in out
    assert "Keepers  : Saquon Barkley, Trey McBride" in out


def test_legal_roster_and_non_keeper_league():
    out = summary.format_tldr([_football("Hard Chargers", [
        {"type": "roster_legality", "legal": True, "issues": []},
        {"type": "keeper_guidance", "is_keeper_league": False,
         "recommended_keeps": []},
    ])])
    assert "Roster   : legal" in out
    assert "Keepers" not in out


def test_baseball_headers_unchanged():
    out = summary.format_tldr([
        {"league": "Pins and Pills", "format": "H2H Categories", "actions": []},
        {"league": "Casey", "format": "NL-Only Roto", "actions": []},
    ])
    assert "[ PINS & PILLS  |  H2H Categories ]" in out
    assert "[ CASEY STENGEL  |  NL-Only Roto ]" in out
