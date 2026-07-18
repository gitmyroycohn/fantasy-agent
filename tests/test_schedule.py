"""
BUG 5 tests: mlb/schedule.py week_bounds() / schedule_weeks() driven by real
CBS periods instead of Monday+7-day arithmetic.
"""
from datetime import date

import mlb.schedule as schedule


def _fake_start_counts(start_date, end_date):
    """Deterministic fake probable-starter counts keyed by date range, so
    tests don't depend on network access to statsapi.mlb.com."""
    # Give "player x" a different start count depending on the window length,
    # simulating a 14-day period producing 3-4 starts vs a 7-day period's 2.
    from datetime import datetime
    d1 = datetime.strptime(start_date, "%Y-%m-%d").date()
    d2 = datetime.strptime(end_date, "%Y-%m-%d").date()
    days = (d2 - d1).days + 1
    starts = 4 if days >= 14 else 2
    return {"aceypitcher": starts, "onestarter": 1}


def test_week_bounds_uses_real_period_not_monday_math(monkeypatch):
    # Saturday 7/18/26 -- Monday-math would anchor to 7/13 (also happens to
    # match Period 16's start here) but Sunday would be 7/19, NOT the real
    # period end of 7/26. This is the core of BUG 5: a plain 7-day window
    # cannot represent the 14-day Period 16.
    d = date(2026, 7, 18)
    start, end = schedule.week_bounds(d)
    assert start == date(2026, 7, 13)
    assert end == date(2026, 7, 26)
    assert (end - start).days + 1 == 14


def test_week_bounds_next_week_is_next_real_period():
    d = date(2026, 7, 17)
    start, end = schedule.week_bounds(d, next_week=True)
    assert start == date(2026, 7, 27)
    assert end == date(2026, 8, 2)


def test_schedule_weeks_spans_full_period_16_window(monkeypatch):
    monkeypatch.setattr(schedule, "_fetch_start_counts", _fake_start_counts)
    schedule._fetch_start_counts.cache_clear = lambda: None  # not an lru_cache anymore in the fake

    weeks = schedule.schedule_weeks(n=1, d=date(2026, 7, 18))
    assert len(weeks) == 1
    wk = weeks[0]
    assert wk["period"] == 16
    assert wk["monday"] == date(2026, 7, 13)
    assert wk["sunday"] == date(2026, 7, 26)
    assert wk["period_days"] == 14
    # BUG 5 item 5: a 3-4 start SP in a 14-day period must be visible, and
    # distinctly flagged in multi_starters (3+), not just two_starters (2+).
    assert wk["two_starters"]["aceypitcher"] == 4
    assert wk["multi_starters"]["aceypitcher"] == 4
    assert "onestarter" not in wk["two_starters"]


def test_schedule_weeks_offset_1_is_period_17(monkeypatch):
    monkeypatch.setattr(schedule, "_fetch_start_counts", _fake_start_counts)
    weeks = schedule.schedule_weeks(n=2, d=date(2026, 7, 17))
    assert weeks[0]["period"] == 16
    assert weeks[1]["period"] == 17
    assert weeks[1]["monday"] == date(2026, 7, 27)
    assert weeks[1]["sunday"] == date(2026, 8, 2)
    # a plain 7-day period, so only the "2-start" bucket should be non-empty
    assert weeks[1]["two_starters"]["aceypitcher"] == 2
    assert weeks[1]["multi_starters"] == {}
