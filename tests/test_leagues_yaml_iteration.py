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


# agent/main.py and mcp_server.py each have their own _SUPPORTED_SPORTS
# guard, and as of 2026-08-01 they've DIVERGED on purpose:
#   - agent/main.py now has a real (if limited) football decision pipeline
#     (agent/football_decisions.py: roster legality + open-slot waiver
#     targets, no performance scoring yet) so it includes "football".
#   - mcp_server.py's individual tools (daily_decisions,
#     waiver_recommendations, evaluate_trade_tool, etc.) still hard-call
#     baseball-only logic and haven't been updated one by one yet, so it
#     stays baseball-only until that happens.
# Each mirror below matches its real file exactly -- keep them in sync by
# hand when either file's _SUPPORTED_SPORTS changes.
_MAIN_PY_SUPPORTED_SPORTS = {"baseball", "football"}
_MCP_SERVER_SUPPORTED_SPORTS = {"baseball"}


def _iterate(config, supported_sports):
    seen = []
    for sport, leagues in config.items():
        if not isinstance(leagues, list):
            continue
        if sport not in supported_sports:
            continue
        for league in leagues or []:
            if not isinstance(league, dict) or "cbs_league_id" not in league:
                continue
            seen.append((sport, league.get("id")))
    return seen


def _iterate_like_main_py(config):
    return _iterate(config, _MAIN_PY_SUPPORTED_SPORTS)


def _iterate_like_mcp_server(config):
    return _iterate(config, _MCP_SERVER_SUPPORTED_SPORTS)


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
    """agent/main.py's run-loop: both baseball leagues AND all 3 football
    leagues (agent/football_decisions.py gives football a real, if limited,
    pipeline as of 2026-08-01 -- roster legality + open-slot waiver
    targets)."""
    with open("config/leagues.yaml") as f:
        config = yaml.safe_load(f)
    seen = _iterate_like_main_py(config)
    ids = {league_id for _, league_id in seen}
    assert ids == {
        "pins_and_pills", "casey_stengel",
        "f_league", "hard_chargers", "east_coast",
    }


def test_football_leagues_present_but_excluded_from_mcp_servers_default_resolve():
    """Football leagues must exist in config (real cbs_league_id/team_id --
    see sports/football/scoring.py). mcp_server.py's _resolve_leagues() was
    updated (2026-08-01) to accept a `sports` kwarg -- 4 tools
    (daily_decisions, get_roster, get_team_roster, list_league_teams) opt
    into football by passing sports=_FOOTBALL_AWARE_SPORTS explicitly (see
    tests/test_mcp_server_football_resolve.py), but every OTHER tool
    (waiver_recommendations, evaluate_trade_tool, roster_value_signals,
    hitting_matchups, probe_schedule) still calls _resolve_leagues() with
    no `sports` arg at all, which is what THIS test's
    _iterate_like_mcp_server() mirrors -- the default stays baseball-only,
    which is exactly what keeps those untouched tools safe."""
    with open("config/leagues.yaml") as f:
        config = yaml.safe_load(f)

    # The leagues are really there...
    assert isinstance(config["football"], list)
    football_ids = {l["id"] for l in config["football"]}
    assert football_ids == {"f_league", "hard_chargers", "east_coast"}

    # ...but mcp_server.py's guarded iteration must not surface them yet.
    seen = _iterate_like_mcp_server(config)
    sports_seen = {s for s, _ in seen}
    assert "football" not in sports_seen

    # while agent/main.py's DOES now surface them (see
    # test_real_leagues_still_iterate_correctly above) -- the two files are
    # meant to have diverged here, not accidentally drifted.
    seen_main = _iterate_like_main_py(config)
    assert "football" in {s for s, _ in seen_main}
