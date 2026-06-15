"""
CBS Sports Fantasy authentication.

CBS's login form is a React/XHR flow that cannot be replicated with a plain
requests POST. Instead, auth works like this (validated by cbs_probe.py):

1. The user logs into CBS in their browser and captures the Cookie header
   into .env as CBS_COOKIE (see README / probe instructions).
2. Each league lives on a subdomain: https://{league}.{sport}.cbssports.com
3. Each league's pages embed an API access token in their JavaScript.
   We extract it fresh every run and validate it against league/details.
4. The token is then used with https://api.cbssports.com/fantasy/* JSON
   endpoints. Some leagues also require an explicit league_id param.

Cookie expiry is detected (league pages redirect to /login) and raised as
CBSCookieExpiredError with re-capture instructions.
"""

import os
import re
import requests

API_BASE = "https://api.cbssports.com/fantasy"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

TIMEOUT = 15

TOKEN_PATTERNS = [
    ("var token",     r'var\s+token\s*=\s*["\']([^"\']{10,})["\']'),
    ("access_token=", r'access_token=([A-Za-z0-9%_\-\.+/=]{10,})'),
    ("access_token:", r'["\']?access_token["\']?\s*[:=]\s*["\']([^"\']{10,})["\']'),
    ("apiToken",      r'["\']?api[_]?[tT]oken["\']?\s*[:=]\s*["\']([^"\']{10,})["\']'),
    ("token:",        r'["\']token["\']\s*:\s*["\']([^"\']{20,})["\']'),
]

COOKIE_HELP = (
    "CBS session cookie is missing or expired. To fix:\n"
    "  1. Log into your league in Chrome (e.g. https://hemp.baseball.cbssports.com)\n"
    "  2. F12 -> Network tab -> reload -> click the first request\n"
    "  3. Request Headers -> copy the full 'cookie:' value\n"
    "  4. Put it in .env as one line:  CBS_COOKIE=\"<paste>\"\n"
    "  5. Re-run."
)


class CBSAuthError(Exception):
    pass


class CBSCookieExpiredError(CBSAuthError):
    pass


class CBSAPIError(CBSAuthError):
    pass


def league_base(league_id: str, sport: str = "baseball") -> str:
    """CBS fantasy leagues live on per-league subdomains."""
    return f"https://{league_id}.{sport}.cbssports.com"


class CBSAuth:
    """Cookie-based CBS session with per-league API token management."""

    def __init__(self, cookie: str | None = None):
        self.cookie = cookie or os.getenv("CBS_COOKIE", "")
        if not self.cookie:
            raise CBSAuthError("No CBS_COOKIE set.\n" + COOKIE_HELP)
        self._session: requests.Session | None = None
        # league_id -> (token, needs_league_id_param: bool)
        self._tokens: dict[str, tuple[str, bool]] = {}

    # -- session ------------------------------------------------------------

    def get_session(self) -> requests.Session:
        if self._session is None:
            s = requests.Session()
            s.headers.update(HEADERS)
            s.headers["Cookie"] = self.cookie
            self._session = s
        return self._session

    def fetch_league_page(self, league_id: str, sport: str = "baseball",
                          path: str = "/") -> requests.Response:
        """GET a page on the league subdomain; raises if cookie is dead."""
        url = f"{league_base(league_id, sport)}{path}"
        r = self.get_session().get(url, timeout=TIMEOUT, allow_redirects=True)
        if "login" in r.url.lower() or "signin" in r.url.lower():
            raise CBSCookieExpiredError(
                f"{url} redirected to login ({r.url}).\n" + COOKIE_HELP)
        r.raise_for_status()
        return r

    # -- token --------------------------------------------------------------

    def get_token(self, league_id: str, sport: str = "baseball") -> tuple[str, bool]:
        """Return (token, needs_league_id_param) for a league.
        Extracted fresh from league page JS and validated against the API."""
        if league_id in self._tokens:
            return self._tokens[league_id]

        candidates: list[str] = []
        seen: set[str] = set()
        for path in ["/", "/setup/league-details", "/teams"]:
            try:
                r = self.fetch_league_page(league_id, sport, path)
            except CBSCookieExpiredError:
                raise
            except Exception:
                continue
            for _label, pat in TOKEN_PATTERNS:
                for m in re.finditer(pat, r.text):
                    tok = m.group(1)
                    if tok not in seen:
                        seen.add(tok)
                        candidates.append(tok)

        for tok in candidates:
            for needs_league in (False, True):
                if self._validate_token(tok, league_id, needs_league):
                    self._tokens[league_id] = (tok, needs_league)
                    return self._tokens[league_id]

        raise CBSAPIError(
            f"Could not find a working API token on {league_base(league_id, sport)} "
            f"({len(candidates)} candidates tried). Run cbs_probe.py to diagnose.")

    def _validate_token(self, token: str, league_id: str,
                        needs_league: bool) -> bool:
        params = {"version": "3.0", "access_token": token,
                  "response_format": "JSON"}
        if needs_league:
            params["league_id"] = league_id
        try:
            r = self.get_session().get(f"{API_BASE}/league/details",
                                       params=params, timeout=TIMEOUT)
            data = r.json()
            return r.status_code < 400 and int(data.get("statusCode", 200)) < 400
        except Exception:
            return False

    # -- API ----------------------------------------------------------------

    def api_get(self, endpoint: str, league_id: str,
                sport: str = "baseball", **params) -> dict:
        """GET an api.cbssports.com/fantasy endpoint as JSON.
        endpoint e.g. 'league/rosters', 'league/details'."""
        token, needs_league = self.get_token(league_id, sport)
        q = {"version": "3.0", "access_token": token,
             "response_format": "JSON", **params}
        if needs_league:
            q["league_id"] = league_id
        r = self.get_session().get(f"{API_BASE}/{endpoint.strip('/')}",
                                   params=q, timeout=TIMEOUT)
        try:
            data = r.json()
        except Exception:
            raise CBSAPIError(
                f"{endpoint}: HTTP {r.status_code}, non-JSON response: {r.text[:200]}")
        status = int(data.get("statusCode", r.status_code))
        if status >= 400:
            raise CBSAPIError(
                f"{endpoint}: API status {status}: {data.get('statusMessage', '')}")
        return data
