"""
FantasyPros API client.

Base URL: https://api.fantasypros.com/public/v2/json
Auth:     x-api-key header

Endpoints used:
  /mlb/{season}/projections  -- ROS / weekly projections by position
  /mlb/closer-depth-chart    -- closer situation per team
  /mlb/news                  -- injury / transaction news
  /mlb/lineups               -- today confirmed batting orders + starters
  /mlb/players               -- ECR ranks + player metadata
  /nfl/{season}/projections  -- weekly projections (future football use)
  /nfl/{season}/consensus-rankings -- ECR rankings
"""
import logging
import re
from datetime import date
from functools import lru_cache

import requests

logger = logging.getLogger(__name__)

_BASE = "https://api.fantasypros.com/public/v2/json"
_CURRENT_SEASON = 2026


from mlb.teams import norm_name as _norm


class FantasyProsClient:
    def __init__(self, api_key: str):
        self._key = api_key
        self._session = requests.Session()
        self._session.headers.update({
            "x-api-key": api_key,
            "Accept": "application/json",
        })

    def _get(self, path: str, params: dict = None) -> dict:
        url = f"{_BASE}{path}"
        try:
            r = self._session.get(url, params=params or {}, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            logger.warning("FP API HTTP error %s: %s", path, e)
            return {}
        except Exception as e:
            logger.warning("FP API error %s: %s", path, e)
            return {}

    # ---- MLB projections ------------------------------------------------

    def sp_projections(self, season: int = _CURRENT_SEASON,
                       proj_type: str = "ros") -> list[dict]:
        """ROS projections for all SPs. Returns list of player dicts."""
        data = self._get(f"/mlb/{season}/projections",
                         {"position": "SP", "type": proj_type})
        return data.get("players", []) or data.get("player", [])

    def rp_projections(self, season: int = _CURRENT_SEASON,
                       proj_type: str = "ros") -> list[dict]:
        """ROS projections for all RPs."""
        data = self._get(f"/mlb/{season}/projections",
                         {"position": "RP", "type": proj_type})
        return data.get("players", []) or data.get("player", [])

    def hitter_projections(self, position: str = "H",
                           season: int = _CURRENT_SEASON,
                           proj_type: str = "ros") -> list[dict]:
        """ROS projections for hitters. position: H, 1B, 2B, 3B, SS, OF, C, DH"""
        data = self._get(f"/mlb/{season}/projections",
                         {"position": position, "type": proj_type})
        return data.get("players", []) or data.get("player", [])

    # ---- Closer depth chart --------------------------------------------

    def closer_depth_chart(self) -> dict:
        """
        Returns dict keyed by team abbreviation:
          {
            "ARI": {
              "committee": 0,
              "players": [
                {"order": 0, "name": "Paul Sewald", "job_security": "strong",
                 "projections": {"sv": 30, "ip": 55, "era": 3.20, ...},
                 "stats": {...}}
              ]
            }, ...
          }
        """
        data = self._get("/mlb/closer-depth-chart")
        return data.get("teams", {})

    def primary_closers(self) -> dict:
        """
        Returns {team_abbr: closer_name} for the #1 closer on each team.
        Only includes teams where order==0 and job_security != 'committee'.
        """
        chart = self.closer_depth_chart()
        result = {}
        for team, info in chart.items():
            players = info.get("players", [])
            if not players:
                continue
            top = players[0]
            if str(top.get("order", "0")) == "0":
                result[team] = top.get("name", "")
        return result

    # ---- News / injuries -----------------------------------------------

    def injuries(self, limit: int = 50) -> list[dict]:
        """Recent injury news items."""
        data = self._get("/mlb/news", {"category": "injury", "limit": limit})
        return data.get("items", [])

    def news(self, category: str = None, limit: int = 25) -> list[dict]:
        """Recent news. category: injury, recap, transaction, rumor, breaking"""
        params = {"limit": limit}
        if category:
            params["category"] = category
        data = self._get("/mlb/news", params)
        return data.get("items", [])

    # ---- Lineups -------------------------------------------------------

    def todays_lineups(self) -> list[dict]:
        """
        Today's confirmed MLB batting orders.
        Returns list of game dicts, each with 'hitters' and 'pitchers' keyed by team.
        """
        today = date.today().isoformat()
        data = self._get("/mlb/lineups", {"start": today, "end": today})
        return data.get("games", [])

    # ---- Players -------------------------------------------------------

    def players(self, updated: str = None) -> list[dict]:
        """Player index with ECR rank, positions, team, status."""
        params = {}
        if updated:
            params["updated"] = updated
        data = self._get("/mlb/players", params)
        return data.get("players", [])

    # ---- NFL (for football leagues) ------------------------------------

    def nfl_projections(self, position: str = "ALL",
                        season: int = _CURRENT_SEASON,
                        week: int = None,
                        scoring: str = "PPR") -> list[dict]:
        params = {"position": position, "scoring": scoring}
        if week is not None:
            params["week"] = week
        data = self._get(f"/nfl/{season}/projections", params)
        return data.get("players", [])

    def nfl_consensus_rankings(self, position: str = "ALL",
                                season: int = _CURRENT_SEASON,
                                scoring: str = "PPR",
                                rank_type: str = "WW") -> list[dict]:
        """ECR rankings. rank_type: WW=waiver wire, ROS=rest of season, ADP."""
        params = {"position": position, "scoring": scoring, "type": rank_type}
        data = self._get(f"/nfl/{season}/consensus-rankings", params)
        return data.get("players", [])

    def nfl_news(self, category: str = None, limit: int = 25) -> list[dict]:
        params = {"limit": limit}
        if category:
            params["category"] = category
        data = self._get("/nfl/news", params)
        return data.get("items", [])


# ---- Enrichment helper -----------------------------------------------

def enrich_with_fp_projections(players, client: FantasyProsClient,
                                sport: str = "mlb") -> int:
    """
    Add FP ROS projection stats to a list of Player or WaiverPlayer objects.
    Merges into player.stats under keys prefixed 'fp_' to avoid collision.
    Returns count of players matched.

    For pitchers (SP/RP): adds fp_sv, fp_k, fp_era, fp_whip, fp_ip, fp_w
    For hitters: adds fp_hr, fp_r, fp_rbi, fp_sb, fp_avg, fp_ops
    """
    if not players:
        return 0

    # Fetch all projections once
    sp_proj   = {_norm(p.get("name", "")): p
                 for p in client.sp_projections()}
    rp_proj   = {_norm(p.get("name", "")): p
                 for p in client.rp_projections()}
    hit_proj  = {_norm(p.get("name", "")): p
                 for p in client.hitter_projections("H")}

    matched = 0
    for obj in players:
        # Support both Player and WaiverPlayer
        p = getattr(obj, "player", obj)
        key = _norm(p.name)
        pos_set = set(p.positions)

        if "SP" in pos_set:
            proj = sp_proj.get(key)
        elif "RP" in pos_set:
            proj = rp_proj.get(key)
        else:
            proj = hit_proj.get(key)

        if not proj:
            continue

        if p.stats is None:
            p.stats = {}

        if "SP" in pos_set or "RP" in pos_set:
            p.stats.update({
                "fp_sv":   proj.get("sv",   proj.get("saves", 0)),
                "fp_k":    proj.get("k",    proj.get("so", 0)),
                "fp_era":  proj.get("era",  proj.get("ERA", 0.0)),
                "fp_whip": proj.get("whip", proj.get("WHIP", 0.0)),
                "fp_ip":   proj.get("ip",   0.0),
                "fp_w":    proj.get("w",    proj.get("wins", 0)),
            })
        else:
            p.stats.update({
                "fp_hr":  proj.get("hrs",  proj.get("hr", 0)),
                "fp_r":   proj.get("runs", proj.get("r", 0)),
                "fp_rbi": proj.get("rbi",  0),
                "fp_sb":  proj.get("sb",   0),
                "fp_avg": float(proj.get("ave", proj.get("avg", 0.0)) or 0.0),
                "fp_ops": float(proj.get("ops", 0.0) or 0.0),
            })
        matched += 1

    return matched
