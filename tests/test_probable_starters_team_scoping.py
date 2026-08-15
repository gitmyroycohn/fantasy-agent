"""
P1 bug fix (2026-08-15): mlb.schedule.probable_starters_today() name-only
matching risk (same class as the Franklin Arias incident, 2026-08-13, in
mlb.lineups.lineup_status_for()).

Flagged as an unconfirmed, same-risk-class item in the bug tracker's "Also
worth knowing" section since 2026-08-13: probable_starters_today() returned
a flat set of normalized pitcher names with no team association, so a
roster SP could be wrongly flagged "confirmed probable starter today" if a
DIFFERENT pitcher who merely shares a normalized name was the real
confirmed starter on some other team. optimize_daily_lineup() now requires
both the name AND canonical_team() to match.
"""
from sports.baseball.lineup_optimizer import optimize_daily_lineup


def test_name_collision_across_teams_is_not_a_confirmed_starter():
    # "J. Smith" on the user's roster (team WSH) shares a normalized name
    # with the REAL confirmed probable starter "J. Smith" on team NYY.
    # Only the NYY pitcher should be treated as confirmed; the WSH roster
    # player must not inherit that confirmation just by sharing a name.
    lineup_slots = [
        {"player_name": "J. Smith", "team": "WSH", "positions": ["SP"],
         "slot": "SP1", "is_starting": True, "stats": {}},
    ]
    teams_playing = {"WSH", "NYY"} | {f"T{i}" for i in range(9)}  # >=10 -> reliable
    probable_starters = {("jsmith", "NYY")}  # the OTHER J. Smith, not ours

    advice = optimize_daily_lineup(lineup_slots, teams_playing, probable_starters)
    assert len(advice) == 1
    a = advice[0]
    # WSH plays today but our J. Smith isn't NYY's confirmed starter --
    # must resolve to "team has a game, not yet confirmed", not "confirmed".
    assert a.advice == "ok"
    assert a.is_probable_starter is False
    # The old bug: this would have been True, purely from the name match.


def test_name_collision_same_team_still_confirms():
    # Sanity check: when name AND team both match, it must still confirm --
    # the fix must not become overly strict and break the normal case.
    lineup_slots = [
        {"player_name": "J. Smith", "team": "NYY", "positions": ["SP"],
         "slot": "SP1", "is_starting": True, "stats": {}},
    ]
    teams_playing = {"NYY"} | {f"T{i}" for i in range(9)}
    probable_starters = {("jsmith", "NYY")}

    advice = optimize_daily_lineup(lineup_slots, teams_playing, probable_starters)
    assert advice[0].is_probable_starter is True
    assert advice[0].advice == "ok"  # already active -> "ok" not "start"


def test_probable_starters_today_returns_name_team_tuples(monkeypatch):
    import mlb.schedule as schedule
    from datetime import date

    fake_games = [
        {
            "teams": {
                "home": {
                    "team": {"abbreviation": "SF"},
                    "probablePitcher": {"fullName": "Logan Webb"},
                },
                "away": {
                    "team": {"abbreviation": "MIA"},
                    "probablePitcher": {"fullName": "Sandy Alcantara"},
                },
            }
        }
    ]
    monkeypatch.setattr(schedule, "_fetch_today_games", lambda date_str: fake_games)

    result = schedule.probable_starters_today(date(2026, 8, 15))
    assert result == {("loganwebb", "SFG"), ("sandyalcantara", "MIA")}
    # Every element must be a (name, team) tuple, not a bare name.
    for entry in result:
        assert isinstance(entry, tuple) and len(entry) == 2
