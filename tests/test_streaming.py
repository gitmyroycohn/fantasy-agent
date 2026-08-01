"""
Regression test for the SP-streaming 'helps: S' mislabel fix (2026-08-01).

rank_streaming_sps only ever surfaces starting pitchers. Starting pitchers
essentially never record saves or holds, so the "helps: <cat>" reason text
must never claim a streamed SP helps S/SV/HLD, even when saves is a losing
category in the standings.
"""
from data.models import Player, WaiverPlayer
from sports.baseball.streaming import rank_streaming_sps


def _sp_candidate(name="Fake Starter", era=3.20, k9=9.0, whip=1.05, ip=40):
    player = Player(
        id="fake1", name=name, position="SP", team="NYM",
        stats={"ERA": era, "K9": k9, "WHIP": whip, "IP": ip},
    )
    return WaiverPlayer(player=player, ownership_pct=10.0)


def test_streaming_sp_never_claims_helps_saves():
    standings = {
        "S":   {"winning": False},   # losing saves
        "K":   {"winning": False},   # losing strikeouts
        "ERA": {"winning": True},
    }
    results = rank_streaming_sps([_sp_candidate()], standings)
    assert results, "expected at least one streaming candidate"
    for r in results:
        assert "helps: S" not in r["reason"]
        assert "SV" not in r["reason"]
        assert "HLD" not in r["reason"]


def test_streaming_sp_still_reports_real_pitching_cats():
    standings = {"K": {"winning": False}, "W": {"winning": False}}
    results = rank_streaming_sps([_sp_candidate()], standings)
    assert results
    reason = results[0]["reason"]
    assert "helps:" in reason
    assert "K" in reason and "W" in reason
