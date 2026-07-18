"""
mlb/clock.py: canonical ET-aware "now", and its re-export/dedup into every
module that used to compute this independently.
"""
from datetime import date, datetime

from mlb.clock import now_et, today_et


def test_now_et_returns_aware_datetime_in_et():
    n = now_et()
    assert isinstance(n, datetime)
    assert n.tzinfo is not None
    assert str(n.tzinfo) == "America/New_York"


def test_today_et_matches_now_et_date():
    assert today_et() == now_et().date()
    assert isinstance(today_et(), date)


def test_today_et_reexported_consistently_across_modules():
    """mlb/schedule.py and mlb/injuries.py each used to define their own
    independent (duplicated) _today_et(); both must now be the exact same
    function object re-exported from mlb.clock, not just equal-behaving
    copies, so there's only ever one implementation to get right."""
    from mlb.clock import today_et as canonical
    from mlb.schedule import _today_et as schedule_today_et
    from mlb.injuries import _today_et as injuries_today_et

    assert schedule_today_et is canonical
    assert injuries_today_et is canonical
