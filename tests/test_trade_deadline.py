"""
Trade deadline enhancement tests.

Covers:
  - config/leagues.yaml parses a `trade_deadline` for both leagues.
  - agent.decisions.trade_window_status resolves open/urgent/closed correctly
    off a real calendar date (mirrors the Phase C real-calendar approach --
    `today` is injectable for tests instead of relying on mlb.clock.today_et()
    at import time).
  - daily_decisions-shaped output (via agent.main._print_decisions) omits all
    trade sections once a league's deadline has passed, and this is per-league
    independent -- one league closing doesn't affect the other.
  - evaluate_trade_tool-equivalent behavior: agent.decisions.trade_window_status
    reports "closed" for both leagues on 2026-08-13 (today's real date), which
    is what mcp_server.evaluate_trade_tool checks before returning a verdict.
"""
import io
import sys
from datetime import date

import yaml

from agent.decisions import trade_window_status
from agent.main import _print_decisions


def _load_league(league_id):
    with open("config/leagues.yaml") as f:
        config = yaml.safe_load(f)
    for league in config["baseball"]:
        if league["cbs_league_id"] == league_id:
            return league
    raise AssertionError(f"{league_id} not found in config/leagues.yaml")


# -- config parsing ---------------------------------------------------------

def test_hemp_trade_deadline_parses():
    league = _load_league("hemp")
    assert league["trade_deadline"] == "2026-08-03"


def test_casey_stengel_trade_deadline_parses():
    league = _load_league("baberuthdivingclubformen")
    assert league["trade_deadline"] == "2026-07-31"


def test_both_leagues_have_distinct_deadlines():
    """Confirms the two leagues are independently configured, not aliased."""
    hemp = _load_league("hemp")
    casey = _load_league("baberuthdivingclubformen")
    assert hemp["trade_deadline"] != casey["trade_deadline"]


# -- trade_window_status: status thresholds ---------------------------------

def test_open_more_than_a_week_out():
    cfg = {"trade_deadline": "2026-08-03"}
    status = trade_window_status(cfg, today=date(2026, 7, 20))
    assert status["status"] == "open"
    assert status["days_left"] == 14


def test_urgent_within_seven_days():
    cfg = {"trade_deadline": "2026-08-03"}
    status = trade_window_status(cfg, today=date(2026, 7, 27))
    assert status["status"] == "urgent"
    assert status["days_left"] == 7


def test_urgent_at_boundary_three_days_left():
    cfg = {"trade_deadline": "2026-08-03"}
    status = trade_window_status(cfg, today=date(2026, 7, 31))
    assert status["status"] == "urgent"
    assert status["days_left"] == 3


def test_closed_on_deadline_day():
    cfg = {"trade_deadline": "2026-08-03"}
    status = trade_window_status(cfg, today=date(2026, 8, 3))
    assert status["status"] == "closed"


def test_closed_after_deadline():
    cfg = {"trade_deadline": "2026-08-03"}
    status = trade_window_status(cfg, today=date(2026, 8, 20))
    assert status["status"] == "closed"
    assert status["days_left"] < 0


def test_unset_when_no_deadline_configured():
    status = trade_window_status({}, today=date(2026, 7, 1))
    assert status["status"] == "unset"
    assert status["deadline"] is None


def test_unset_on_malformed_deadline_does_not_raise():
    status = trade_window_status({"trade_deadline": "not-a-date"}, today=date(2026, 7, 1))
    assert status["status"] == "unset"


# -- real 2026-08-13 date: both leagues closed, independently ---------------

def test_both_leagues_closed_on_real_current_date():
    """As of 2026-08-13, both hemp (deadline 8/3) and casey_stengel
    (deadline 7/31) are past their trade deadlines."""
    today = date(2026, 8, 13)
    hemp_status  = trade_window_status(_load_league("hemp"), today=today)
    casey_status = trade_window_status(_load_league("baberuthdivingclubformen"), today=today)
    assert hemp_status["status"] == "closed"
    assert casey_status["status"] == "closed"


def test_per_league_independence_one_open_one_closed():
    """One league's deadline passing must not affect the other league's status."""
    today = date(2026, 8, 1)  # after casey_stengel's 7/31 deadline, before hemp's 8/3
    hemp_status  = trade_window_status(_load_league("hemp"), today=today)
    casey_status = trade_window_status(_load_league("baberuthdivingclubformen"), today=today)
    assert hemp_status["status"] in ("open", "urgent")
    assert casey_status["status"] == "closed"


# -- rendered daily_decisions output -----------------------------------------

def _render(actions):
    result = {"format": "H2H Categories", "actions": actions}
    buf = io.StringIO()
    original = sys.stdout
    sys.stdout = buf
    try:
        _print_decisions(result, dry_run=True)
    finally:
        sys.stdout = original
    return buf.getvalue()


def test_closed_output_has_no_trade_signals_or_board():
    actions = [
        {"type": "trade_window_closed", "deadline": "2026-08-03"},
    ]
    out = _render(actions)
    assert "Trade deadline (2026-08-03) has passed" in out
    assert "Trade Value Signals" not in out
    assert "Trade Board" not in out


def test_urgent_output_shows_days_left_banner():
    actions = [
        {"type": "trade_urgency", "days_left": 3, "deadline": "2026-08-03"},
        {"type": "trade_signals", "signals": [
            {"signal": "sell_high", "name": "Test Player", "team": "NYY",
             "positions": ["OF"], "confidence": "strong", "reason": "test"},
        ]},
    ]
    out = _render(actions)
    assert "Trade deadline in 3 days" in out
    assert "Trade Value Signals" in out


def test_open_output_has_no_urgency_framing():
    actions = [
        {"type": "trade_signals", "signals": [
            {"signal": "sell_high", "name": "Test Player", "team": "NYY",
             "positions": ["OF"], "confidence": "strong", "reason": "test"},
        ]},
    ]
    out = _render(actions)
    assert "⏰" not in out
    assert "Trade Value Signals" in out
