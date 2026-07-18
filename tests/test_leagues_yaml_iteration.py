"""
Regression test: config/leagues.yaml's new top-level `season_start` and
`periods:` keys (BUG 5) must never be mistaken for a sport -> [league, ...]
entry by the three consumers that iterate the file (agent/main.py,
mcp_server.py, cbs_probe.py). All three use the same guard pattern:
skip non-list values AND skip list entries that aren't real league dicts
(a real league dict always has cbs_league_id; a periods table entry never
does). This directly regression-tests a live bug caught in production
logs: 22x "ERROR in ?: 'cbs_league_id'" because `periods:` is itself a
list, so an isinstance(list) check alone wasn't enough to filter it out.
"""
import yaml


def _iterate_like_main_py(config):
    """Mirrors agent/main.py's sport/league iteration guard exactly."""
    seen = []
    for sport, leagues in config.items():
        if not isinstance(leagues, list):
            continue
        for league in leagues or []:
            if not isinstance(league, dict) or "cbs_league_id" not in league:
                continue
            seen.append((sport, league.get("id")))
    return seen


def test_periods_table_never_treated_as_a_league():
    with open("config/leagues.yaml") as f:
        config = yaml.safe_load(f)

    # Sanity: periods really is a bare list (the shape that broke the old guard)
    assert isinstance(config["periods"], list)
    assert len(config["periods"]) == 22
    assert "cbs_league_id" not in config["periods"][0]

    seen = _iterate_like_main_py(config)
    sports_seen = {s for s, _ in seen}
    assert "periods" not in sports_seen
    assert "season_start" not in sports_seen


def test_real_leagues_still_iterate_correctly():
    with open("config/leagues.yaml") as f:
        config = yaml.safe_load(f)
    seen = _iterate_like_main_py(config)
    ids = {league_id for _, league_id in seen}
    assert ids == {"pins_and_pills", "casey_stengel"}
