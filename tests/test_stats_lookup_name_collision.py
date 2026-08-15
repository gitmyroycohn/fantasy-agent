"""
P1 bug fix (2026-08-15): mlb.stats._lookup()'s name-only fallback could
silently attribute one player's stat line to a same-named player on a
different team (same risk class as the Franklin Arias incident, fixed
2026-08-13 in mlb.lineups.lineup_status_for()).

Flagged as an unconfirmed, same-risk-class item in the bug tracker's "Also
worth knowing" section since 2026-08-13. Fix: _fetch_stats()'s name-only
fallback index now only contains names that resolve to exactly one team
this season; a genuine collision is left out entirely, so a lookup that
misses the precise "{name}_{team}" key falls through to {} (unknown)
instead of risking attribution to the wrong player.
"""
import mlb.stats as stats_mod
from data.models import Player


def _fake_response(splits):
    return {"stats": [{"splits": splits}]}


def _split(name, team_abbr, **stat_overrides):
    stat = {
        "era": 3.00, "whip": 1.10, "strikeoutsPer9Inn": 9.0,
        "wins": 10, "saves": 0, "holds": 0, "strikeOuts": 100,
        "baseOnBalls": 30, "qualityStarts": 15, "inningsPitched": "150.0",
        "gamesStarted": 25, "gamesPlayed": 25,
    }
    stat.update(stat_overrides)
    return {
        "player": {"fullName": name},
        "team":   {"abbreviation": team_abbr},
        "stat":   stat,
    }


def test_name_collision_not_added_to_name_only_fallback(monkeypatch):
    # Two different real pitchers both named "Chris Young", on different
    # teams, in the same season's pitching stats.
    splits = [
        _split("Chris Young", "SD", wins=8),
        _split("Chris Young", "KC", wins=3),
    ]

    def _fake_get(url, params=None, timeout=None):
        class _Resp:
            def raise_for_status(self): pass
            def json(self): return _fake_response(splits)
        return _Resp()

    monkeypatch.setattr(stats_mod.requests, "get", _fake_get)
    stats_mod._fetch_stats.cache_clear()

    db = stats_mod._fetch_stats("pitching", 2026)

    # Precise keys must both resolve correctly (SD -> SDP, KC -> KCR via
    # mlb.teams.mlb_to_cbs()).
    assert db["chrisyoung_sdp"]["W"] == 8
    assert db["chrisyoung_kcr"]["W"] == 3
    # The name-only fallback must NOT exist for this ambiguous name -- the
    # old bug would have silently picked whichever entry was processed
    # first ("chrisyoung" -> one of the two, non-deterministically correct).
    assert "chrisyoung" not in db


def test_unique_name_still_gets_name_only_fallback(monkeypatch):
    splits = [_split("Unique Pitcher", "TEX", wins=15)]

    def _fake_get(url, params=None, timeout=None):
        class _Resp:
            def raise_for_status(self): pass
            def json(self): return _fake_response(splits)
        return _Resp()

    monkeypatch.setattr(stats_mod.requests, "get", _fake_get)
    stats_mod._fetch_stats.cache_clear()

    db = stats_mod._fetch_stats("pitching", 2026)
    assert db["uniquepitcher_tex"]["W"] == 15
    # A name with only one team this season IS safe to use as a fallback.
    assert db["uniquepitcher"]["W"] == 15


def test_lookup_falls_through_to_empty_on_ambiguous_name_with_wrong_team(monkeypatch):
    # If the caller's player.team doesn't match either real team (e.g. a
    # stale/incorrect team on the roster side), _lookup() must return {}
    # rather than guess between the two collided players.
    splits = [
        _split("Chris Young", "SD", wins=8),
        _split("Chris Young", "KC", wins=3),
    ]

    def _fake_get(url, params=None, timeout=None):
        class _Resp:
            def raise_for_status(self): pass
            def json(self): return _fake_response(splits)
        return _Resp()

    monkeypatch.setattr(stats_mod.requests, "get", _fake_get)
    stats_mod._fetch_stats.cache_clear()

    p_db = stats_mod._fetch_stats("pitching", 2026)
    h_db = stats_mod._fetch_stats("hitting", 2026)

    player = Player(id="x", name="Chris Young", position="SP", team="BOS")  # neither SD nor KC
    result = stats_mod._lookup(player, p_db, h_db)
    assert result == {}
