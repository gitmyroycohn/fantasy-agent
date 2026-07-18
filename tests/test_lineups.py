"""
ENH 4/7 tests: mlb/lineups.py posted-lineup parsing and status resolution.
"""
from mlb.lineups import lineup_status_for


def test_lineup_status_confirmed():
    posted = {
        "players": {"johnsmith": {"team": "NYY", "batting_order": 2}},
        "posted_teams": {"NYY"},
    }
    assert lineup_status_for("John Smith", "NYY", posted) == "confirmed"


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
