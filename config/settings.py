import os
from dotenv import load_dotenv

load_dotenv()

CBS_USERNAME = os.getenv("CBS_USERNAME")
CBS_PASSWORD = os.getenv("CBS_PASSWORD")
# Primary auth: browser-captured session cookie (see cbs/auth.py for capture steps)
CBS_COOKIE   = os.getenv("CBS_COOKIE", "")
CBS_API_URL  = "https://api.cbssports.com/fantasy"

FANTASYPROS_API_KEY = os.getenv("FANTASYPROS_API_KEY", "")

WAIVER_PRIORITY_THRESHOLD = 50

# Streaming SP thresholds (used by sports/baseball/streaming.py)
STREAMING_SP_MIN_ERA  = 4.00
STREAMING_SP_MIN_K9   = 7.5
MAX_ERA_STREAMER      = STREAMING_SP_MIN_ERA   # alias
MIN_K9_STREAMER       = STREAMING_SP_MIN_K9    # alias
# Only consider SPs owned in <X% of leagues (avoids already-rostered guys)
MIN_SP_OWNERSHIP_DROP = 50.0

# Platoon weighting thresholds (ENH 3; sports/baseball/lineup_optimizer.py).
# A batter is down-ranked for today's start/sit advice when their OPS split
# against the hand they're facing today is both meaningfully worse than
# their split against the other hand (PLATOON_OPS_GAP) AND weak in absolute
# terms (below PLATOON_FLOOR_OPS). The must-start floor (_MUST_START_OPS in
# agent/decisions.py) always overrides this for elite bats.
PLATOON_OPS_GAP           = 0.100
PLATOON_FLOOR_OPS         = 0.700

AUTO_SET_LINEUP           = True
DRY_RUN                   = True   # Leave True until fully validated
