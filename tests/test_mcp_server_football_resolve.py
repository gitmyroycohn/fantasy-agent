"""
Tests for mcp_server.py::_resolve_leagues()'s sport-filtering behavior
(now parameterized by a `sports` set so most tools stay baseball-only by
default while get_roster/get_team_roster/list_league_teams/daily_decisions
opt into football).

mcp_server.py cannot be imported directly in this environment: the `mcp`
package actually resolved by `pip install -r requirements-mcp.txt` here is
"Model Context Protocol SDK" v2.0.0, which does not expose
`mcp.server.fastmcp.FastMCP` the way this repo's code expects (import
fails with ModuleNotFoundError). This looks like a package-name collision
on PyPI rather than anything football-specific -- flagging it, but not
fixing it here since it's unrelated to the football build and Christopher's
actual working environment may already have a different/pinned `mcp`
install that works fine.

Given that, this test mirrors _resolve_leagues()'s logic exactly (same
approach tests/test_leagues_yaml_iteration.py already uses for
agent/main.py) rather than importing the real function.
"""

import yaml


_BASEBALL_ONLY_SPORTS  = {"baseball"}   # mirrors mcp_server.py
_FOOTBALL_AWARE_SPORTS = {"baseball", "football"}   # mirrors mcp_server.py


def _resolve_leagues(config, league_id, sports=_BASEBALL_ONLY_SPORTS):
    """Mirrors mcp_server.py::_resolve_leagues() exactly."""
    results = []
    for sport, leagues in config.items():
        if not isinstance(leagues, list):
            continue
        if sport not in sports:
            continue
        for league in (leagues or []):
            if not isinstance(league, dict) or "cbs_league_id" not in league:
                continue
            lid = league.get("id", league.get("cbs_league_id", ""))
            if league_id in ("all", lid):
                results.append((league, sport))
    return results


def _load_config():
    with open("config/leagues.yaml") as f:
        return yaml.safe_load(f)


def test_default_sports_arg_is_still_baseball_only():
    """The tools that DON'T pass a `sports` kwarg (waiver_recommendations,
    roster_value_signals, evaluate_trade_tool, hitting_matchups,
    probe_schedule) must keep resolving baseball leagues only -- this is
    what makes leaving them untouched safe."""
    config = _load_config()
    results = _resolve_leagues(config, "all")  # no sports kwarg -> default
    sports_seen = {sport for _, sport in results}
    assert sports_seen == {"baseball"}


def test_football_aware_sports_resolves_all_5_leagues():
    config = _load_config()
    results = _resolve_leagues(config, "all", sports=_FOOTBALL_AWARE_SPORTS)
    ids = {league.get("id") for league, _ in results}
    assert ids == {
        "pins_and_pills", "casey_stengel",
        "f_league", "hard_chargers", "east_coast",
    }


def test_football_aware_sports_resolves_single_football_league_by_id():
    config = _load_config()
    results = _resolve_leagues(config, "f_league", sports=_FOOTBALL_AWARE_SPORTS)
    assert len(results) == 1
    league, sport = results[0]
    assert sport == "football"
    assert league["cbs_league_id"] == "sfflf"
    assert league["cbs_team_id"] == "11"


def test_baseball_only_sports_never_resolves_a_football_league_by_id():
    """Even asking for a football league_id explicitly must return nothing
    when the caller didn't opt into _FOOTBALL_AWARE_SPORTS -- this is what
    keeps waiver_recommendations etc. safe without any per-tool code change."""
    config = _load_config()
    results = _resolve_leagues(config, "f_league")  # default sports kwarg
    assert results == []
