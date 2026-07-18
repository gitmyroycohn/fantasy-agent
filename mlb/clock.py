"""
Canonical ET-aware "now" for the whole agent.

GitHub Actions runners (and Render) run in UTC. A naive `datetime.now()` or
`date.today()` rolls over at UTC midnight -- which is ~8pm ET -- so anything
computing "today" from an unlocalized clock is silently a day ahead for a
third of every 24 hours. This was the root of BUG 5 item 7 (mlb/schedule.py,
mlb/weather.py); before this module existed, the fix was reimplemented
independently in four different places (mlb/schedule.py, mlb/injuries.py,
agent/matchup_proj.py, agent/summary.py) with identical logic. Import from
here instead of writing another local ZoneInfo("America/New_York") copy.

Public API
----------
now_et()   -> datetime  current date AND time in US Eastern time
today_et() -> date      current date in US Eastern time (now_et().date())
"""

from datetime import datetime, date
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def now_et() -> datetime:
    """Current date and time in US Eastern time."""
    return datetime.now(ET)


def today_et() -> date:
    """Current date in US Eastern time."""
    return now_et().date()
