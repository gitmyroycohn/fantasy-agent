"""
Decision memory -- tracks recommendations across daily runs so we can show
how long something has been flagged and suppress genuinely stale advice.

Storage: logs/history.json (committed to repo after each Actions run)

Schema:
  {
    "<league_id>": {
      "waiver_adds":     {"<player>": {"first": "YYYY-MM-DD", "last": "YYYY-MM-DD", "n": 3}},
      "streaming_sp":    {"<player>": {"first": ..., "last": ..., "n": 1}},
      "drop_candidates": {"<player>": {"first": ..., "last": ..., "n": 5, "sev": "cut"}}
    }
  }
"""
import json
import logging
import os
from datetime import date, timedelta

logger = logging.getLogger(__name__)

HISTORY_PATH = "logs/history.json"
PRUNE_AFTER_DAYS = 21   # drop entries not seen in 3 weeks


# ---- Load / save -------------------------------------------------------

def load_history(path: str = HISTORY_PATH) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Could not load history: %s", e)
        return {}


def save_history(history: dict, path: str = HISTORY_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        logger.warning("Could not save history: %s", e)


# ---- Core update -------------------------------------------------------

def _ensure_league(history: dict, league_id: str) -> dict:
    if league_id not in history:
        history[league_id] = {
            "waiver_adds":     {},
            "streaming_sp":    {},
            "drop_candidates": {},
        }
    return history[league_id]


def _record(bucket: dict, player: str, today: str, **extra) -> dict:
    """Upsert a player entry; return the entry."""
    if player not in bucket:
        bucket[player] = {"first": today, "last": today, "n": 1}
    else:
        entry = bucket[player]
        if entry.get("last") != today:
            entry["last"] = today
            entry["n"] = entry.get("n", 0) + 1
    bucket[player].update(extra)
    return bucket[player]


def _days_label(entry: dict, today: str) -> str:
    """Return 'NEW', 'day 2', 'day 5', etc."""
    n = entry.get("n", 1)
    if n == 1:
        return "NEW"
    return f"day {n}"


# ---- Annotate + record in one pass ------------------------------------

def update_and_annotate(result: dict, history: dict,
                        league_id: str, today: str = None) -> None:
    """
    Mutate result['actions'] in-place:
      - adds '_days' tag to each waiver/streaming/drop item
      - records today's recommendations in history

    Call this BEFORE _print_decisions so the tags are available.
    """
    if today is None:
        today = date.today().isoformat()

    league_hist = _ensure_league(history, league_id)

    for action in result.get("actions", []):
        atype = action.get("type", "")

        if atype == "waiver_adds":
            bucket = league_hist["waiver_adds"]
            for rec in action.get("recommendations", []):
                name  = rec.get("player", "")
                entry = _record(bucket, name, today)
                rec["_days"] = _days_label(entry, today)

        elif atype in ("streaming_sp", "streaming_sp_next_week"):
            bucket = league_hist["streaming_sp"]
            for rec in action.get("recommendations", []):
                name  = rec.get("player", "")
                entry = _record(bucket, name, today)
                rec["_days"] = _days_label(entry, today)

        elif atype == "drop_candidates":
            bucket = league_hist["drop_candidates"]
            for drop in action.get("drops", []):
                name  = drop.get("player", "")
                sev   = drop.get("severity", "")
                entry = _record(bucket, name, today, sev=sev)
                drop["_days"] = _days_label(entry, today)


# ---- Pruning -----------------------------------------------------------

def prune_history(history: dict, today: str = None) -> None:
    """Remove entries not seen in PRUNE_AFTER_DAYS days."""
    if today is None:
        today = date.today().isoformat()
    cutoff = (date.fromisoformat(today) - timedelta(days=PRUNE_AFTER_DAYS)).isoformat()

    for league_id, buckets in history.items():
        for bucket_name, bucket in buckets.items():
            stale = [k for k, v in bucket.items()
                     if v.get("last", "0000-00-00") < cutoff]
            for k in stale:
                del bucket[k]
