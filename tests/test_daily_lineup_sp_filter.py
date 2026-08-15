"""
P0 bug fix (2026-08-15): "SPs starting today" was over-inclusive.

Live incident: on 2026-08-15, Pins and Pills' "SPs starting today (9)" and
Casey Stengel's "SPs pitching today (6)" sections both listed several SPs
who were NOT actually that day's confirmed probable starter -- e.g. Sandy
Alcantara, Jose Soriano, Jake Bennett, Zack Wheeler, Nick Martinez, and Kyle
Harrison -- cross-checked against RotoWire's confirmed slate. Only Logan
Webb (and, on the Stengel side, Logan Henderson for a different date) were
real confirmed starters.

Root cause: agent/main.py's CLI renderer bucketed "starting today" as
`a["advice"] in ("start", "ok")`, but optimize_daily_lineup() also returns
advice="ok" for a SP whose team merely has a game today but who is not yet
(or not at all) the confirmed probable starter -- a strictly weaker claim
that got conflated with a real confirmed start. This test reproduces the
exact scenario (some SPs confirmed starting, some just on a team with a
game, one on the IL) and asserts classify_sp_advice() -- the fixed,
single-source-of-truth helper now used by both of agent/main.py's
daily_lineup renderers -- keeps them apart.
"""
from sports.baseball.lineup_optimizer import optimize_daily_lineup, classify_sp_advice


def _advice_dicts(lineup_slots, teams_playing, probable_starters, il_players=None):
    """Mirror agent/decisions.py._add_lineup_advice's dict serialization of
    LineupAdvice objects (the exact shape classify_sp_advice consumes)."""
    advice = optimize_daily_lineup(
        lineup_slots, teams_playing, probable_starters, il_players=il_players,
    )
    return [
        {
            "player": a.player_name, "team": a.team, "positions": a.positions,
            "slot": a.slot, "is_starting": a.is_starting, "advice": a.advice,
            "reason": a.reason, "is_probable_starter": a.is_probable_starter,
        }
        for a in advice
    ]


def test_sp_on_team_with_game_but_not_confirmed_starter_is_not_starting_today():
    # Reproduces the 2026-08-15 Pins and Pills incident: Logan Webb (SF) is
    # the real confirmed probable starter; Sandy Alcantara (MIA), Jose
    # Soriano (TOR), and Jake Bennett (BOS) are just SPs whose MLB teams
    # also have a game today, with probable starters not yet posted for
    # them specifically.
    lineup_slots = [
        {"player_name": "Logan Webb", "team": "SF", "positions": ["SP"],
         "slot": "SP1", "is_starting": True, "stats": {}},
        {"player_name": "Sandy Alcantara", "team": "MIA", "positions": ["SP"],
         "slot": "SP2", "is_starting": True, "stats": {}},
        {"player_name": "Jose Soriano", "team": "TOR", "positions": ["SP"],
         "slot": "BN", "is_starting": False, "stats": {}},
        {"player_name": "Jake Bennett", "team": "BOS", "positions": ["SP"],
         "slot": "BN", "is_starting": False, "stats": {}},
    ]
    teams_playing = {"SF", "MIA", "TOR", "BOS"} | {f"T{i}" for i in range(8)}  # >=10 -> reliable
    # (norm_name, canonical_team) tuples -- see mlb.schedule.probable_starters_today()'s
    # 2026-08-15 team-scoping fix. Only Webb is confirmed today.
    probable_starters = {("loganwebb", "SFG")}

    advice = _advice_dicts(lineup_slots, teams_playing, probable_starters)
    buckets = classify_sp_advice(advice)

    confirmed_names = {a["player"] for a in buckets["confirmed"]}
    pending_names   = {a["player"] for a in buckets["pending"]}

    assert confirmed_names == {"Logan Webb"}
    assert pending_names == {"Sandy Alcantara", "Jose Soriano", "Jake Bennett"}
    # The old bug: all four would have landed in "confirmed" together
    # because advice for all of them is "start" or "ok".
    assert "Sandy Alcantara" not in confirmed_names
    assert "Jose Soriano" not in confirmed_names
    assert "Jake Bennett" not in confirmed_names


def test_sp_with_no_game_today_is_benched_not_pending_or_confirmed():
    lineup_slots = [
        {"player_name": "Off Day SP", "team": "ZZZ", "positions": ["SP"],
         "slot": "SP1", "is_starting": True, "stats": {}},
    ]
    teams_playing = {f"T{i}" for i in range(10)}  # ZZZ not playing; >=10 -> reliable
    advice = _advice_dicts(lineup_slots, teams_playing, set())
    buckets = classify_sp_advice(advice)

    assert buckets["confirmed"] == []
    assert buckets["pending"] == []
    assert len(buckets["benched"]) == 1
    assert buckets["benched"][0]["player"] == "Off Day SP"


def test_decisions_dict_export_includes_is_probable_starter():
    """agent/decisions.py's daily_lineup action dict must carry
    is_probable_starter -- without it, classify_sp_advice() (and any other
    consumer) can't distinguish confirmed vs. merely-on-turf SPs at all."""
    import inspect
    import agent.decisions as decisions_mod
    src = inspect.getsource(decisions_mod._add_lineup_advice)
    assert '"is_probable_starter"' in src
