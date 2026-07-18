"""
ENH 2 (legal-slot swaps) + ENH 3 (platoon weighting) + ENH 4/7
(posted-lineup awareness) tests for sports/baseball/lineup_optimizer.py.
"""
from sports.baseball.lineup_optimizer import (
    optimize_daily_lineup, find_legal_swaps, apply_must_start_floor,
)


# ---------------------------------------------------------------------------
# ENH 2: legal-slot-only swaps
# ---------------------------------------------------------------------------

def test_legal_swap_proposed_for_2b_ss_eligible_bench_player():
    lineup_slots = [
        {
            "player_name": "Active SS", "team": "OFF", "positions": ["SS"],
            "eligible_positions": ["SS"], "slot": "SS", "is_starting": True,
            "stats": {},
        },
        {
            "player_name": "Bench Util", "team": "PLAY", "positions": ["2B"],
            "eligible_positions": ["2B", "SS"], "slot": "BN", "is_starting": False,
            "stats": {},
        },
    ]
    teams_playing = {"PLAY"} | {f"T{i}" for i in range(10)}  # >=10 for schedule_reliable
    advice = optimize_daily_lineup(lineup_slots, teams_playing, set())
    swaps = find_legal_swaps(lineup_slots, advice)

    assert len(swaps) == 1
    assert swaps[0]["out"] == "Active SS"
    assert swaps[0]["in"] == "Bench Util"
    assert swaps[0]["out_slot"] == "SS"


def test_no_illegal_swap_when_bench_player_not_eligible():
    lineup_slots = [
        {
            "player_name": "Active SS", "team": "OFF", "positions": ["SS"],
            "eligible_positions": ["SS"], "slot": "SS", "is_starting": True,
            "stats": {},
        },
        {
            "player_name": "OF Only Bench", "team": "PLAY", "positions": ["OF"],
            "eligible_positions": ["OF"], "slot": "BN", "is_starting": False,
            "stats": {},
        },
    ]
    teams_playing = {"PLAY"} | {f"T{i}" for i in range(10)}
    advice = optimize_daily_lineup(lineup_slots, teams_playing, set())
    swaps = find_legal_swaps(lineup_slots, advice)
    assert swaps == []  # OF-only player can never legally fill SS


def test_2b_3b_ss_player_can_fill_any_of_the_three():
    for slot in ("2B", "3B", "SS"):
        lineup_slots = [
            {
                "player_name": f"Active {slot}", "team": "OFF", "positions": [slot],
                "eligible_positions": [slot], "slot": slot, "is_starting": True,
                "stats": {},
            },
            {
                "player_name": "Super Util", "team": "PLAY", "positions": ["2B"],
                "eligible_positions": ["2B", "3B", "SS"], "slot": "BN", "is_starting": False,
                "stats": {},
            },
        ]
        teams_playing = {"PLAY"} | {f"T{i}" for i in range(10)}
        advice = optimize_daily_lineup(lineup_slots, teams_playing, set())
        swaps = find_legal_swaps(lineup_slots, advice)
        assert len(swaps) == 1, f"expected a legal swap into {slot}"
        assert swaps[0]["in"] == "Super Util"


def test_dh_eligible_via_league_rule_can_fill_dh_slot():
    from cbs.players import apply_league_eligibility_rules
    league_cfg = {"eligibility": {"all_players_dh_eligible": True}}
    bench_positions = apply_league_eligibility_rules(["OF"], league_cfg)
    assert "DH" in bench_positions

    lineup_slots = [
        {
            "player_name": "Active DH", "team": "OFF", "positions": ["DH"],
            "eligible_positions": ["DH"], "slot": "DH", "is_starting": True,
            "stats": {},
        },
        {
            "player_name": "OF Bench (hemp -- all DH-eligible)", "team": "PLAY",
            "positions": ["OF"], "eligible_positions": bench_positions,
            "slot": "BN", "is_starting": False, "stats": {},
        },
    ]
    teams_playing = {"PLAY"} | {f"T{i}" for i in range(10)}
    advice = optimize_daily_lineup(lineup_slots, teams_playing, set())
    swaps = find_legal_swaps(lineup_slots, advice)
    assert len(swaps) == 1
    assert swaps[0]["out_slot"] == "DH"


# ---------------------------------------------------------------------------
# ENH 3: platoon weighting
# ---------------------------------------------------------------------------

def test_platoon_down_rank_fires_for_weak_split_vs_disadvantage_hand():
    lineup_slots = [{
        "player_name": "Lefty Masher", "team": "PLAY", "positions": ["OF"],
        "eligible_positions": ["OF"], "slot": "OF", "is_starting": True,
        "stats": {"split_vs_l_ops": 0.550, "split_vs_r_ops": 0.900, "OPS": 0.780},
    }]
    teams_playing = {"PLAY"} | {f"T{i}" for i in range(10)}
    advice = optimize_daily_lineup(lineup_slots, teams_playing, set(),
                                   opp_hand_by_team={"PLAY": "L"})
    a = advice[0]
    assert a.advice == "bench"
    assert "platoon" in a.reason.lower()


def test_must_start_floor_overrides_platoon_down_rank_for_elite_bat():
    lineup_slots = [{
        "player_name": "Elite Bat", "team": "PLAY", "positions": ["OF"],
        "eligible_positions": ["OF"], "slot": "OF", "is_starting": True,
        "stats": {"split_vs_l_ops": 0.550, "split_vs_r_ops": 0.950, "OPS": 0.900},
    }]
    teams_playing = {"PLAY"} | {f"T{i}" for i in range(10)}
    advice = optimize_daily_lineup(lineup_slots, teams_playing, set(),
                                   opp_hand_by_team={"PLAY": "L"})
    assert advice[0].advice == "bench"  # platoon fires first

    ops_by_norm = {"elitebat": 0.900}
    apply_must_start_floor(advice, ops_by_norm, floor=0.850)
    assert advice[0].advice == "ok"
    assert "must-start floor" in advice[0].reason.lower()


def test_no_platoon_downrank_without_meaningful_split_gap():
    lineup_slots = [{
        "player_name": "Even Hitter", "team": "PLAY", "positions": ["OF"],
        "eligible_positions": ["OF"], "slot": "OF", "is_starting": True,
        "stats": {"split_vs_l_ops": 0.760, "split_vs_r_ops": 0.780, "OPS": 0.770},
    }]
    teams_playing = {"PLAY"} | {f"T{i}" for i in range(10)}
    advice = optimize_daily_lineup(lineup_slots, teams_playing, set(),
                                   opp_hand_by_team={"PLAY": "L"})
    assert advice[0].advice in ("start", "ok")


# ---------------------------------------------------------------------------
# ENH 4/7: posted-lineup awareness
# ---------------------------------------------------------------------------

def test_confirmed_in_posted_lineup():
    lineup_slots = [{
        "player_name": "Starter Guy", "team": "PLAY", "positions": ["OF"],
        "eligible_positions": ["OF"], "slot": "OF", "is_starting": True,
        "stats": {}, "lineup_status": "confirmed", "batting_order": 3,
    }]
    teams_playing = {"PLAY"} | {f"T{i}" for i in range(10)}
    advice = optimize_daily_lineup(lineup_slots, teams_playing, set())
    a = advice[0]
    assert a.lineup_label == "confirmed"
    assert a.advice in ("start", "ok")
    assert "3rd" in a.reason


def test_not_in_lineup_is_down_ranked_and_not_overridden_by_must_start_floor():
    lineup_slots = [{
        "player_name": "Benched Elsewhere", "team": "PLAY", "positions": ["OF"],
        "eligible_positions": ["OF"], "slot": "OF", "is_starting": True,
        "stats": {"OPS": 0.900}, "lineup_status": "not_in_lineup", "batting_order": None,
    }]
    teams_playing = {"PLAY"} | {f"T{i}" for i in range(10)}
    advice = optimize_daily_lineup(lineup_slots, teams_playing, set())
    a = advice[0]
    assert a.advice == "out_of_lineup"
    assert a.lineup_label == "not-in-lineup"

    # must-start floor must NOT override a confirmed real-world absence
    ops_by_norm = {"benchedelsewhere": 0.900}
    apply_must_start_floor(advice, ops_by_norm, floor=0.850)
    assert advice[0].advice == "out_of_lineup"


def test_expected_label_when_lineup_not_posted_yet():
    lineup_slots = [{
        "player_name": "Probably Playing", "team": "PLAY", "positions": ["OF"],
        "eligible_positions": ["OF"], "slot": "OF", "is_starting": True,
        "stats": {}, "lineup_status": "unknown", "batting_order": None,
    }]
    teams_playing = {"PLAY"} | {f"T{i}" for i in range(10)}
    advice = optimize_daily_lineup(lineup_slots, teams_playing, set())
    a = advice[0]
    assert a.lineup_label == "expected"
    assert a.advice in ("start", "ok")
