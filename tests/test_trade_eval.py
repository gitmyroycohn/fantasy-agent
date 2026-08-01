"""
Regression tests for the 2026-08-01 evaluate_trade_tool bug fixes:

1. Category mismap -- hitters must not be scored on pitching categories
   (and vice versa), even when the league's combined hitting+pitching cat
   list is passed in.
2. Unmatched players (e.g. draft picks) must be excluded from scoring
   rather than fabricating a value from missing-stat defaults.
3. Category-name aliasing -- "S" (saves) and "BA" (batting average), the
   actual names used in config/leagues.yaml, must resolve to the same FP
   stats as "SV" and "AVG".
4. Name-matching hardening -- a suffix difference ("Jr.") between the
   query name and the FP-side name should still resolve to a match.
"""
from agent.trade_eval import evaluate_trade


class _FakeFPClient:
    """Stub FantasyProsClient -- no network calls."""

    def __init__(self, hitters=None, sps=None, rps=None):
        self._hitters = hitters or []
        self._sps = sps or []
        self._rps = rps or []

    def hitter_projections(self, position="H", **kw):
        return self._hitters

    def sp_projections(self, **kw):
        return self._sps

    def rp_projections(self, **kw):
        return self._rps


class _FakeSavantClient:
    def batter_data(self, name):
        return {}

    def pitcher_data(self, name):
        return {}


_HITTER = {
    "name": "Rafael Devers", "hrs": 30, "runs": 90, "rbi": 95, "sb": 2,
    "ave": 0.280, "ops": 0.880, "h": 150,
}
_PITCHER = {
    "name": "Hunter Brown", "sv": 0, "k": 180, "era": 3.10, "whip": 1.05,
    "ip": 170, "w": 12,
}
_SAVES_PITCHER = {
    "name": "Josh Hader", "sv": 35, "k": 90, "era": 2.40, "whip": 0.95,
    "ip": 65, "w": 4,
}


def _league_cfg(hitting, pitching, name="Test League"):
    return {"name": name, "scoring": {"hitting": hitting, "pitching": pitching}}


def _fp_client():
    return _FakeFPClient(hitters=[_HITTER], sps=[_PITCHER, _SAVES_PITCHER])


def test_hitter_not_scored_on_pitching_categories():
    league_cfg = _league_cfg(hitting=["H", "HR", "OPS", "R", "RBI", "SB"],
                              pitching=["ERA", "K", "S", "W", "WHIP"])
    result = evaluate_trade(
        give=[], receive=["Rafael Devers"],
        league_cfg=league_cfg, fp_client=_fp_client(), sav_client=_FakeSavantClient(),
    )
    devers = result["receive"][0]
    assert devers["pos_type"] == "H"
    # Bug 1 regression: a hitter must never get ERA/WHIP/K/S/W in cat_scores.
    for pitching_cat in ("ERA", "K", "S", "W", "WHIP"):
        assert pitching_cat not in devers["cat_scores"]
    # But real hitting cats should be scored.
    for hitting_cat in ("HR", "R", "RBI", "SB", "H"):
        assert hitting_cat in devers["cat_scores"]


def test_pitcher_not_scored_on_hitting_categories():
    league_cfg = _league_cfg(hitting=["H", "HR", "OPS", "R", "RBI", "SB"],
                              pitching=["ERA", "K", "S", "W", "WHIP"])
    result = evaluate_trade(
        give=[], receive=["Hunter Brown"],
        league_cfg=league_cfg, fp_client=_fp_client(), sav_client=_FakeSavantClient(),
    )
    brown = result["receive"][0]
    assert brown["pos_type"] == "SP"
    for hitting_cat in ("H", "HR", "OPS", "R", "RBI", "SB"):
        assert hitting_cat not in brown["cat_scores"]
    for pitching_cat in ("ERA", "K", "W", "WHIP"):
        assert pitching_cat in brown["cat_scores"]


def test_unmatched_asset_excluded_not_fabricated():
    """A draft pick (or any name with no FP match) must not receive a
    fabricated value from the old 'default missing stat to 0.0' behaviour."""
    league_cfg = _league_cfg(hitting=["H", "HR", "OPS", "R", "RBI", "SB"],
                              pitching=["ERA", "K", "S", "W", "WHIP"])
    result = evaluate_trade(
        give=["2027 3rd Round Pick"], receive=["Rafael Devers"],
        league_cfg=league_cfg, fp_client=_fp_client(), sav_client=_FakeSavantClient(),
    )
    pick = result["give"][0]
    assert pick["excluded"] is True
    assert pick["total"] == 0.0
    assert pick["cat_scores"] == {}
    assert "2027 3rd Round Pick" in result["unscored"]
    # The pick contributing exactly 0 (not a fabricated negative) means the
    # net score reflects only Devers' real value.
    assert result["net_score"] == result["receive"][0]["total"]


def test_saves_category_alias_S_matches_SV_stat():
    """config/leagues.yaml uses 'S' for saves, not 'SV' -- must still score."""
    league_cfg = _league_cfg(hitting=["R", "HR", "RBI", "SB", "BA"],
                              pitching=["W", "S", "K", "ERA", "WHIP"])
    fp_client = _FakeFPClient(sps=[_SAVES_PITCHER])
    result = evaluate_trade(
        give=[], receive=["Josh Hader"],
        league_cfg=league_cfg, fp_client=fp_client, sav_client=_FakeSavantClient(),
    )
    hader = result["receive"][0]
    assert "S" in hader["cat_scores"]
    assert hader["cat_scores"]["S"] > 0  # 35 saves vs 15 baseline -> positive


def test_batting_average_alias_BA_matches_AVG_stat():
    league_cfg = _league_cfg(hitting=["R", "HR", "RBI", "SB", "BA"],
                              pitching=["W", "S", "K", "ERA", "WHIP"])
    result = evaluate_trade(
        give=[], receive=["Rafael Devers"],
        league_cfg=league_cfg, fp_client=_fp_client(), sav_client=_FakeSavantClient(),
    )
    devers = result["receive"][0]
    assert "BA" in devers["cat_scores"]


def test_name_matching_tolerates_suffix_difference():
    fp_client = _FakeFPClient(hitters=[{
        "name": "Vladimir Guerrero", "hrs": 25, "runs": 85, "rbi": 80,
        "sb": 3, "ave": 0.300, "ops": 0.900, "h": 160,
    }])
    league_cfg = _league_cfg(hitting=["HR", "R", "RBI"], pitching=[])
    result = evaluate_trade(
        give=[], receive=["Vladimir Guerrero Jr."],
        league_cfg=league_cfg, fp_client=fp_client, sav_client=_FakeSavantClient(),
    )
    vlad = result["receive"][0]
    assert vlad["excluded"] is False
    assert vlad["found_fp"] is True
