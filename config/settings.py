import os
from dotenv import load_dotenv

load_dotenv()

CBS_USERNAME = os.getenv("CBS_USERNAME")
CBS_PASSWORD = os.getenv("CBS_PASSWORD")
# Primary auth: browser-captured session cookie (see cbs/auth.py for capture steps)
CBS_COOKIE   = os.getenv("CBS_COOKIE", "")
CBS_API_URL  = "https://api.cbssports.com/fantasy"

WAIVER_PRIORITY_THRESHOLD = 50

# Streaming SP thresholds (used by sports/baseball/streaming.py)
STREAMING_SP_MIN_ERA  = 4.00
STREAMING_SP_MIN_K9   = 7.5
MAX_ERA_STREAMER      = STREAMING_SP_MIN_ERA   # alias
MIN_K9_STREAMER       = STREAMING_SP_MIN_K9    # alias
# Only consider SPs owned in <X% of leagues (avoids already-rostered guys)
MIN_SP_OWNERSHIP_DROP = 50.0

AUTO_SET_LINEUP           = True
DRY_RUN                   = True   # Leave True until fully validated
