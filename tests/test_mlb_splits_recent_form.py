"""mlb/splits.py recent-form fetch: MLB's Stats API rejects stats=lastXDays
("Invalid Request with value: lastXDays", confirmed 2026-09-20), so the
window is requested as stats=byDateRange with explicit dates."""
from datetime import date

import pytest
import requests

from mlb import splits


class _Resp:
    def __init__(self, payload=None, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} Client Error")

    def json(self):
        return self._payload


_PAYLOAD = {"stats": [{"splits": [{
    "player": {"fullName": "Gabriel Arias"},
    "stat": {"avg": "1.000", "ops": "2.000", "homeRuns": 0, "stolenBases": 0,
             "runs": 0, "rbi": 0, "gamesPlayed": 2},
}]}]}


@pytest.fixture(autouse=True)
def _clear_cache():
    splits._fetch_last_x_days.cache_clear()
    yield
    splits._fetch_last_x_days.cache_clear()


def test_recent_form_uses_by_date_range_ending_yesterday(monkeypatch):
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen.update(params)
        return _Resp(_PAYLOAD)

    monkeypatch.setattr(splits.requests, "get", fake_get)
    monkeypatch.setattr(splits, "today_et", lambda: date(2026, 9, 20))

    out = splits.fetch_recent_form(14, 2026)

    assert seen["stats"] == "byDateRange"
    assert "numDays" not in seen
    assert seen["endDate"] == "2026-09-19"      # yesterday
    assert seen["startDate"] == "2026-09-06"    # 14 days inclusive
    assert len(out) == 1
    (row,) = out.values()
    assert row["games"] == 2 and row["ops"] == 2.0


def test_cache_is_keyed_per_day(monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(params["endDate"])
        return _Resp(_PAYLOAD)

    monkeypatch.setattr(splits.requests, "get", fake_get)
    monkeypatch.setattr(splits, "today_et", lambda: date(2026, 9, 20))
    splits.fetch_recent_form(14, 2026)
    splits.fetch_recent_form(14, 2026)
    assert calls == ["2026-09-19"]              # second call served from cache

    monkeypatch.setattr(splits, "today_et", lambda: date(2026, 9, 21))
    splits.fetch_recent_form(14, 2026)
    assert calls == ["2026-09-19", "2026-09-20"]  # new day, new window


def test_api_error_returns_empty_and_logs_mlb_message(monkeypatch, caplog):
    body = '{"message":"Invalid Request with value: x"}'
    monkeypatch.setattr(splits.requests, "get",
                        lambda *a, **k: _Resp(status=400, text=body))
    monkeypatch.setattr(splits, "today_et", lambda: date(2026, 9, 20))
    with caplog.at_level("ERROR"):
        assert splits.fetch_recent_form(14, 2026) == {}
    assert "Invalid Request with value" in caplog.text   # MLB's reason is logged
