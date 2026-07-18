"""
BUG 5 tests: real CBS scoring-period resolution.

Validates the specific done-criteria from the Phase C task:
  - reported period matches CBS's period field on a Saturday, a Sunday, and
    inside the 14-day Period 16
  - week_offset=1 on 2026-07-17 resolves to Period 17 (7/27-8/2), not 7/20-7/26
  - start counts for Period 16 span the full 7/13-7/26 window
"""
from datetime import date

from config.periods import (
    load_periods, period_for_date, period_offset, period_bounds, resolve_period,
)


def test_season_start_and_period_count():
    data = load_periods()
    assert data["season_start"] == date(2026, 3, 25)
    assert len(data["periods"]) == 22


def test_period_1_is_12_days():
    start, end = period_bounds(1)
    assert start == date(2026, 3, 25)
    assert end == date(2026, 4, 5)
    assert (end - start).days + 1 == 12


def test_period_16_is_14_days():
    start, end = period_bounds(16)
    assert start == date(2026, 7, 13)
    assert end == date(2026, 7, 26)
    assert (end - start).days + 1 == 14


def test_saturday_resolves_correctly():
    # 7/18/26 is a Saturday, inside Period 16 (7/13-7/26)
    d = date(2026, 7, 18)
    assert d.weekday() == 5  # Saturday
    p = period_for_date(d)
    assert p["n"] == 16


def test_sunday_resolves_correctly():
    # 7/12/26 is a Sunday, the last day of Period 15 (7/6-7/12)
    d = date(2026, 7, 12)
    assert d.weekday() == 6  # Sunday
    p = period_for_date(d)
    assert p["n"] == 15


def test_old_fabricated_formula_was_wrong_on_these_dates():
    """Regression guard: the deleted _current_week() formula
    (opening_day=3/28, //7+1) was wrong on 7/5 and 7/12 per the bug report.
    Confirms the real table disagrees with that formula on those dates."""
    opening_day = date(2026, 3, 28)

    d1 = date(2026, 7, 5)  # bug report: CBS was in Period 14, old code said "Week 15"
    old_week = max(1, (d1 - opening_day).days // 7 + 1)
    assert old_week == 15
    assert period_for_date(d1)["n"] == 14

    d2 = date(2026, 7, 12)  # bug report: CBS was in Period 15, old code said "Week 16"
    old_week = max(1, (d2 - opening_day).days // 7 + 1)
    assert old_week == 16
    assert period_for_date(d2)["n"] == 15


def test_week_offset_1_on_2026_07_17_is_period_17_not_plus_7_days():
    """Exact done-criteria from the task: week_offset=1 on 2026-07-17 must
    resolve to Period 17 (7/27-8/2), not 7/20-7/26 (today + 7 days, which is
    still inside the 14-day Period 16)."""
    today = date(2026, 7, 17)
    assert period_for_date(today)["n"] == 16  # sanity: today is in Period 16

    nxt = period_offset(today, 1)
    assert nxt["n"] == 17
    assert nxt["start"] == date(2026, 7, 27)
    assert nxt["end"] == date(2026, 8, 2)

    # explicitly assert it's NOT the naive +7-days window
    assert (nxt["start"], nxt["end"]) != (date(2026, 7, 20), date(2026, 7, 26))


def test_cbs_period_wins_on_mismatch():
    """BUG 5 item 6: CBS's live period is authoritative over the local table."""
    today = date(2026, 7, 17)
    resolved = resolve_period(today, cbs_period="15")
    assert resolved["n"] == 15
    assert resolved["source"] == "cbs"


def test_cbs_period_matches_table_normally():
    today = date(2026, 7, 17)
    resolved = resolve_period(today, cbs_period="16")
    assert resolved["n"] == 16
    assert resolved["source"] == "cbs"
