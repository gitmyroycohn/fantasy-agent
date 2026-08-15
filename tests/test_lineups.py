"""
ENH 4/7 tests: mlb/lineups.py posted-lineup parsing and status resolution.
"""
from mlb.lineups import lineup_status_for, batting_order_for


def test_lineup_status_confirmed():
    posted = {
        "players": {"johnsmith": {"team": "NYY", "batting_order": 2}},
        "posted_teams": {"NYY"},
    }
    assert lineup_status_for("John Smith", "NYY", posted) == "confirmed"


def test_lineup_status_name_match_wrong_team_is_not_confirmed():
    """Regression test for the Franklin Arias bug (found 2026-08-13): a
    Double-A prospect never called up to MLB surfaced as a "confirmed
    playing" swap-in because lineup_status_for() matched on name alone,
    ignoring which team the posted-lineup entry actually belonged to.

    Here "Franklin Arias" (a minor leaguer, team "CHC") shares a normalized
    name with a real MLB player confirmed in a lineup for a different team
    ("SEA") -- the CHC prospect must NOT be reported as confirmed.
    """
    posted = {
        "players": {"franklinarias": {"team": "SEA", "batting_order": 6}},
        "posted_teams": {"SEA"},
    }
    assert lineup_status_for("Franklin Arias", "CHC", posted) != "confirmed"


def test_lineup_status_name_match_wrong_team_falls_back_to_unknown():
    """When the prospect's own team hasn't posted a lineup (or has no MLB
    game), the wrong-team name match must resolve to "unknown", not
    "not_in_lineup" or "confirmed" -- his own team's status is simply not
    known from this data."""
    posted = {
        "players": {"franklinarias": {"team": "SEA", "batting_order": 6}},
        "posted_teams": {"SEA"},
    }
    assert lineup_status_for("Franklin Arias", "CHC", posted) == "unknown"


def test_batting_order_for_wrong_team_returns_none():
    posted = {
        "players": {"franklinarias": {"team": "SEA", "batting_order": 6}},
        "posted_teams": {"SEA"},
    }
    assert batting_order_for("Franklin Arias", "CHC", posted) is None


def test_batting_order_for_matching_team_returns_slot():
    posted = {
        "players": {"johnsmith": {"team": "NYY", "batting_order": 2}},
        "posted_teams": {"NYY"},
    }
    assert batting_order_for("John Smith", "NYY", posted) == 2


def test_lineup_status_not_in_lineup_when_team_posted_but_player_absent():
    posted = {
        "players": {"someoneelse": {"team": "NYY", "batting_order": 1}},
        "posted_teams": {"NYY"},
    }
    assert lineup_status_for("John Smith", "NYY", posted) == "not_in_lineup"


def test_lineup_status_unknown_when_team_not_posted_yet():
    posted = {"players": {}, "posted_teams": set()}
    assert lineup_status_for("John Smith", "NYY", posted) == "unknown"


def test_fetch_posted_lineups_parses_homeplayers_awayplayers(monkeypatch):
    import mlb.lineups as lineups_mod

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "dates": [{
                    "games": [{
                        "teams": {
                            "home": {"team": {"abbreviation": "NYY"}},
                            "away": {"team": {"abbreviation": "BOS"}},
                        },
                        "lineups": {
                            "homePlayers": [
                                {"fullName": "Home Batter One"},
                                {"fullName": "Home Batter Two"},
                            ],
                            "awayPlayers": [
                                {"fullName": "Away Batter One"},
                            ],
                        },
                    }],
                }],
            }

    def _fake_get(url, params=None, timeout=None):
        return _FakeResp()

    monkeypatch.setattr(lineups_mod.requests, "get", _fake_get)

    from datetime import date
    result = lineups_mod.fetch_posted_lineups(d=date(2026, 7, 18))
    assert "NYY" in result["posted_teams"]
    assert "BOS" in result["posted_teams"]
    from mlb.teams import norm_name
    assert norm_name("Home Batter One") in result["players"]
    assert result["players"][norm_name("Home Batter One")]["batting_order"] == 1
    assert result["players"][norm_name("Home Batter Two")]["batting_order"] == 2
