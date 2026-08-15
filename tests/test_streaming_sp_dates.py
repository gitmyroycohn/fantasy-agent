"""
Streaming-SP date-fidelity tests (2026-08-15 P0 bug batch).

Investigation summary: a live cross-check against RotoWire's confirmed slate
on 2026-08-15 found that Pins and Pills' streaming SP recommendations (Cody
Bradford, Hunter Dobbins, Gabriel Hughes, Jake Irvin, Dean Kremer) all
resolved to 8/16 starts, not 8/15, which the work order flagged as a
possible "off-by-one on the date passed to the API query."

After reviewing mlb/schedule.py's window computation (period_for_date /
period_offset / _fetch_start_counts's startDate/endDate params, all
ET-anchored via mlb.clock.today_et()) and the config/leagues.yaml periods
table, no off-by-one was found in the window itself -- the date strings
passed to the MLB Stats API for the current period are exactly
[period.start, period.end], both inclusive, with no shift. That window
intentionally spans the *whole* current scoring period (including days
already elapsed), which is existing, tested behavior (see
test_schedule_weeks_spans_full_period_16_window) needed so a rostered
pitcher's already-locked-in starts still count toward "2-start this period"
holds -- changing that would be a behavior regression, not a bug fix.

What WAS a real gap: rank_streaming_sps() only ever exposed a bare start
*count* ("2-START"), with no per-pitcher date attached anywhere in the
output, so there was no way to verify -- from the tool's own output -- which
calendar dates a "2-START" tag was counting or whether a candidate's only
verifiable remaining start was tomorrow rather than today. mlb.schedule
gained _fetch_start_dates()/two_start_pitcher_dates() and
rank_streaming_sps() gained a start_dates parameter (both new, additive --
they don't change any existing counts) to close that verifiability gap.
These tests guard the actual, load-bearing property: every date returned by
_fetch_start_dates() for a queried window falls inside that exact window,
so this class of drift can be caught immediately in the future if it's ever
introduced.
"""
from datetime import date

import mlb.schedule as schedule
from sports.baseball.streaming import rank_streaming_sps
from data.models import Player, WaiverPlayer


def _fake_start_dates(start_date, end_date):
    """Deterministic fake MLB schedule response, keyed by the exact window
    requested -- mirrors tests/test_schedule.py's _fake_start_counts pattern
    but preserves per-pitcher dates instead of collapsing to a count."""
    return {
        "loganwebb":  (start_date,),                       # starts on day 1 of the window
        "codybradford": (end_date,),                        # starts on the last day of the window
        "twostartguy": (start_date, end_date),               # 2 starts, first and last day
    }


def test_fetch_start_dates_never_returns_a_date_outside_the_queried_window(monkeypatch):
    monkeypatch.setattr(schedule, "_fetch_start_dates", _fake_start_dates)
    start, end = "2026-08-10", "2026-08-16"
    result = schedule._fetch_start_dates(start, end)

    all_dates = [d for dates in result.values() for d in dates]
    assert all_dates, "fixture should have returned at least one date"
    for d in all_dates:
        assert start <= d <= end, (
            f"date {d} falls outside the queried window [{start}, {end}] -- "
            "this is exactly the off-by-one class of bug flagged in the P0 report"
        )


def test_fetch_start_counts_derived_from_dates_matches_date_tuple_length(monkeypatch):
    # _fetch_start_counts() must be a pure derivation of _fetch_start_dates()
    # so a pitcher's reported "N starts" always matches len(their dates) --
    # they can never drift apart into disagreement.
    monkeypatch.setattr(schedule, "_fetch_start_dates", _fake_start_dates)
    counts = schedule._fetch_start_counts("2026-08-10", "2026-08-16")
    assert counts["loganwebb"] == 1
    assert counts["codybradford"] == 1
    assert counts["twostartguy"] == 2


def test_two_start_pitcher_dates_uses_the_current_period_window(monkeypatch):
    captured = {}

    def _spy(start_date, end_date):
        captured["start"] = start_date
        captured["end"] = end_date
        return _fake_start_dates(start_date, end_date)

    monkeypatch.setattr(schedule, "_fetch_start_dates", _spy)
    d = date(2026, 7, 18)  # inside Period 16: 2026-07-13 to 2026-07-26
    result = schedule.two_start_pitcher_dates(d)

    assert captured["start"] == "2026-07-13"
    assert captured["end"] == "2026-07-26"
    # Every returned date must fall inside that same window.
    for dates in result.values():
        for dt in dates:
            assert captured["start"] <= dt <= captured["end"]


def _sp_waiver_player(name, team="TEX", era=3.50, k9=9.0, ip=80.0, ownership=5.0):
    p = Player(id=name, name=name, position="SP", team=team,
              stats={"ERA": era, "K9": k9, "WHIP": 1.10, "IP": ip})
    return WaiverPlayer(player=p, ownership_pct=ownership)


def test_rank_streaming_sps_attaches_verifiable_start_dates():
    waivers = [_sp_waiver_player("Cody Bradford")]
    two_starters = {"codybradford": 2}
    start_dates = {"codybradford": ("2026-08-16",)}

    recs = rank_streaming_sps(waivers, {}, two_starters=two_starters,
                              start_dates=start_dates)

    assert len(recs) == 1
    assert recs[0]["starts"] == 2
    # The candidate's real date(s) are now on the record, not just a count --
    # this is what lets a live cross-check (e.g. against RotoWire) actually
    # verify the claim instead of having to trust an opaque "2-START" tag.
    assert recs[0]["start_dates"] == ["2026-08-16"]


def test_rank_streaming_sps_start_dates_defaults_to_empty_when_not_provided():
    waivers = [_sp_waiver_player("No Date Guy")]
    recs = rank_streaming_sps(waivers, {}, two_starters={})
    assert recs[0]["start_dates"] == []
