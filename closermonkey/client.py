"""
Closer Monkey client -- fetches closer depth chart and recent news
from closermonkey.com (WordPress, no API).

Sources:
  depth chart : https://closermonkey.com/2015/05/04/updated-closer-depth-chart/
  RSS feed    : https://closermonkey.com/feed/
"""

import re
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import logging

logger = logging.getLogger(__name__)

_DEPTH_URL = "https://closermonkey.com/2015/05/04/updated-closer-depth-chart/"
_FEED_URL  = "https://closermonkey.com/feed/"
_UA        = "Mozilla/5.0 (compatible; FantasyAgent/1.0)"
_TAG_RE    = re.compile(r"<[^>]+>")

_ENTITIES = {
    "&amp;": "&", "&nbsp;": " ", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&#8217;": "'", "&#8216;": "'", "&#8220;": '"', "&#8221;": '"',
    "&#243;": "o", "&#233;": "e", "&#237;": "i",
    "&#250;": "u", "&#225;": "a", "&#241;": "n",
}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _strip_html(text: str) -> str:
    s = _TAG_RE.sub("", text)
    for entity, char in _ENTITIES.items():
        s = s.replace(entity, char)
    return s.strip()


def _fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_depth_chart(html: str) -> dict:
    """Parse the depth chart HTML table. Returns {TEAM: {...}}."""
    _skip = {
        "team", "closer", "1st in line", "2nd in line", "tendency",
        "american league", "national league", "",
    }
    row_re = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
    td_re  = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
    result = {}

    for row_m in row_re.finditer(html):
        cells = [_strip_html(td.group(1)) for td in td_re.finditer(row_m.group(1))]
        if len(cells) < 5:
            continue
        team = cells[0].upper().strip("*")
        if team.lower() in _skip or not cells[1]:
            continue
        committee = cells[1].startswith("*")
        result[team] = {
            "closer":          cells[1].lstrip("*").strip(),
            "first_in_line":   cells[2].lstrip("*").strip(),
            "second_in_line":  cells[3].lstrip("*").strip(),
            "tendency":        cells[4],
            "committee":       committee,
        }
    return result


def _parse_rss(data: bytes) -> list[dict]:
    """Parse WordPress RSS XML into a list of post dicts."""
    root = ET.fromstring(data)
    channel = root.find("channel")
    if channel is None:
        return []
    items = []
    for item in channel.findall("item"):
        title = item.findtext("title", "").strip()
        link  = item.findtext("link",  "").strip()
        date  = item.findtext("pubDate", "").strip()
        cats  = [c.text.strip() for c in item.findall("category") if c.text]
        desc  = _strip_html(item.findtext("description", ""))[:400]
        items.append({
            "title": title, "link": link, "date": date,
            "categories": cats, "summary": desc,
        })
    return items


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class CloserMonkeyClient:
    """Wraps closermonkey.com depth chart + RSS with lazy fetch and caching."""

    def __init__(self):
        self._chart: dict | None = None
        self._news:  list | None = None

    # -- depth chart --------------------------------------------------------

    def depth_chart(self) -> dict:
        """Returns {TEAM: {closer, first_in_line, second_in_line, tendency, committee}}."""
        if self._chart is None:
            try:
                raw  = _fetch(_DEPTH_URL)
                html = raw.decode("utf-8", errors="replace")
                parsed = _parse_depth_chart(html)
                self._chart = parsed
                print(f"  CM depth chart: {len(parsed)} teams loaded")
            except Exception as e:
                logger.warning("CM depth chart fetch failed: %s", e)
                print(f"  CM depth chart: unavailable ({e})")
                self._chart = {}
        return self._chart

    def find_player(self, player_name: str) -> dict | None:
        """Look up a player by name in the depth chart.

        Returns:
            {team, role, closer, first_in_line, tendency, committee} or None.
        """
        chart = self.depth_chart()
        target = _norm(player_name)
        for team, info in chart.items():
            for role in ("closer", "first_in_line", "second_in_line"):
                if target == _norm(info.get(role, "")):
                    return {
                        "team":          team,
                        "role":          role,
                        "closer":        info["closer"],
                        "first_in_line": info["first_in_line"],
                        "tendency":      info["tendency"],
                        "committee":     info["committee"],
                    }
        return None

    # -- news ---------------------------------------------------------------

    def recent_news(self, limit: int = 20) -> list[dict]:
        """Return recent posts from the RSS feed."""
        if self._news is None:
            try:
                raw = _fetch(_FEED_URL)
                self._news = _parse_rss(raw)
            except Exception as e:
                logger.warning("CM RSS fetch failed: %s", e)
                self._news = []
        return self._news[:limit]

    def rapid_reactions(self, limit: int = 5) -> list[dict]:
        """Return Rapid Reaction posts (closer situation changes)."""
        return [
            n for n in self.recent_news(50)
            if "rapid" in n["title"].lower()
        ][:limit]

    def leverage_ledger(self, limit: int = 2) -> list[dict]:
        """Return Leverage Ledger posts (daily save recap)."""
        return [
            n for n in self.recent_news(50)
            if "leverage" in n["title"].lower()
        ][:limit]
