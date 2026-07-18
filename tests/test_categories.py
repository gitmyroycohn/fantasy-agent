"""
BUG 6 + ENH 6 tests: leagues.yaml category correctness, drift-validation
warning, and full-category-set emission.
"""
import logging
import yaml

from sports.baseball.categories import (
    analyze_matchup, validate_scoring_config, priority_categories,
)


def test_leagues_yaml_pins_and_pills_has_exactly_12_correct_categories():
    with open("config/leagues.yaml") as f:
        cfg = yaml.safe_load(f)
    pins = next(l for l in cfg["baseball"] if l["id"] == "pins_and_pills")
    scoring = pins["scoring"]
    hitting = scoring["hitting"]
    pitching = scoring["pitching"]

    assert set(hitting) == {"H", "HR", "OPS", "R", "RBI", "SB"}
    assert set(pitching) == {"ERA", "INNdGS", "K", "S", "W", "WHIP"}
    assert len(hitting) + len(pitching) == 12

    # The old fabricated categories must be gone.
    all_cats = set(hitting) | set(pitching)
    for bogus in ("AVG", "TB", "XBH", "QS", "HLD", "K_BB"):
        assert bogus not in all_cats


def test_leagues_yaml_casey_stengel_has_10_categories_unchanged():
    with open("config/leagues.yaml") as f:
        cfg = yaml.safe_load(f)
    casey = next(l for l in cfg["baseball"] if l["id"] == "casey_stengel")
    scoring = casey["scoring"]
    assert len(scoring["hitting"]) + len(scoring["pitching"]) == 10


def _raw_stats_for(cats: dict) -> dict:
    return {
        "system": "h2h", "period": "16", "opponent": "Foo", "opponent_id": "1",
        "my_score": "6-6-0",
        "categories": {c: {"mine": 1, "opp": 2} for c in cats},
    }


def test_validate_scoring_config_warns_on_deliberate_mismatch(caplog):
    """Done-criteria: a deliberate mismatch in leagues.yaml triggers the warning."""
    cfg_scoring = {"hitting": ["H", "HR", "AVG"], "pitching": ["ERA"]}  # AVG doesn't exist per CBS below; H missing from CBS
    raw_stats = _raw_stats_for(["H", "HR", "ERA", "SB"])  # CBS actually scores SB too, and doesn't score AVG

    with caplog.at_level(logging.WARNING):
        problems = validate_scoring_config(cfg_scoring, raw_stats, "Test League")

    assert problems  # non-empty: a mismatch was found
    assert any("AVG" in p for p in problems)
    assert any("SB" in p for p in problems)
    assert any("Category mismatch" in r.message for r in caplog.records)


def test_validate_scoring_config_silent_when_matching(caplog):
    cfg_scoring = {"hitting": ["H", "HR"], "pitching": ["ERA"]}
    raw_stats = _raw_stats_for(["H", "HR", "ERA"])

    with caplog.at_level(logging.WARNING):
        problems = validate_scoring_config(cfg_scoring, raw_stats, "Test League")

    assert problems == []
    assert not any("Category mismatch" in r.message for r in caplog.records)


def test_analyze_matchup_full_category_standings_includes_all_cats():
    """ENH 6: matchup.category_standings must include every scored category,
    not just the losing ones -- hemp has 12, casey_stengel has 10."""
    raw_stats = _raw_stats_for(["H", "HR", "OPS", "R", "RBI", "SB",
                                "ERA", "INNdGS", "K", "S", "W", "WHIP"])
    matchup = analyze_matchup(raw_stats, week=16)
    assert len(matchup.category_standings) == 12
    cat_names = {c.category for c in matchup.category_standings}
    assert cat_names == {"H", "HR", "OPS", "R", "RBI", "SB",
                         "ERA", "INNdGS", "K", "S", "W", "WHIP"}
