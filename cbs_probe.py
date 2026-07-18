"""
cbs_probe.py — CBS Sports Fantasy auth & roster diagnostic
Run this before the main agent to confirm CBS connectivity.

Usage:
    python cbs_probe.py --league-id XXXX --team-id Y
    python cbs_probe.py          # reads from .env / leagues.yaml

Writes PROBE_RESULT.md on completion. Paste that file back into Claude.
"""

import sys
import os
import argparse
import json
from datetime import datetime

# Force UTF-8 console output (Windows cp1252 can't print ✅/❌)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Optional imports
# ---------------------------------------------------------------------------
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    print("[error] requests not installed.")
    print("  Run:  pip install requests")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False
    print("[warn] beautifulsoup4 not installed — HTML parsing checks will be skipped")
    print("  Run:  pip install beautifulsoup4 lxml")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CBS_BASE        = "https://www.cbssports.com"
CBS_LOGIN_URL   = f"{CBS_BASE}/login"

def league_base(league_id: str, sport: str = "baseball") -> str:
    """CBS fantasy leagues live on per-league subdomains: {league}.{sport}.cbssports.com"""
    return f"https://{league_id}.{sport}.cbssports.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": CBS_BASE,
}

TIMEOUT = 15


# ---------------------------------------------------------------------------
# Probe result accumulator
# ---------------------------------------------------------------------------
class ProbeResult:
    def __init__(self):
        self.steps: list[dict] = []
        self.passed = 0
        self.failed = 0
        self.start = datetime.now()

    def record(self, name: str, ok: bool, detail: str = "", data: str = ""):
        status = "PASS" if ok else "FAIL"
        icon   = "✅" if ok else "❌"
        self.steps.append({"name": name, "ok": ok, "detail": detail, "data": data})
        if ok:
            self.passed += 1
        else:
            self.failed += 1
        print(f"  {icon} [{status}] {name}")
        if detail:
            print(f"         {detail}")
        if data:
            preview = data[:300].replace("\n", " ")
            print(f"         data: {preview}...")

    def summary(self) -> str:
        elapsed = (datetime.now() - self.start).total_seconds()
        lines = [
            f"# CBS Probe Result — {self.start.strftime('%Y-%m-%d %H:%M')}",
            f"Elapsed: {elapsed:.1f}s | Passed: {self.passed} | Failed: {self.failed}",
            "",
        ]
        for s in self.steps:
            icon = "✅" if s["ok"] else "❌"
            lines.append(f"## {icon} {s['name']}")
            if s["detail"]:
                lines.append(s["detail"])
            if s["data"]:
                lines.append(f"```\n{s['data'][:1000]}\n```")
            lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Credential loading
# ---------------------------------------------------------------------------
def load_credentials(args) -> tuple[str, str, str, str, str]:
    username = getattr(args, "user", None) or ""
    password = getattr(args, "password", None) or ""
    league_id = getattr(args, "league_id", None) or ""
    team_id = str(getattr(args, "team_id", None) or "")
    cookie = getattr(args, "cookie", None) or ""

    # Try .env
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if not username and k == "CBS_USERNAME":
                    username = v
                if not password and k == "CBS_PASSWORD":
                    password = v
                if not cookie and k == "CBS_COOKIE":
                    cookie = v

    # Try leagues.yaml for first league
    yaml_path = os.path.join(os.path.dirname(__file__), "config", "leagues.yaml")
    if (not league_id or not team_id) and os.path.exists(yaml_path):
        try:
            with open(yaml_path) as f:
                content = f.read()
            # naive YAML parse — just grab first league_id and team_id values
            for line in content.splitlines():
                if not league_id and "league_id:" in line:
                    league_id = line.split(":", 1)[1].strip().strip('"').strip("'")
                if not team_id and "team_id:" in line:
                    team_id = line.split(":", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass

    return username, password, league_id, team_id, cookie


# ---------------------------------------------------------------------------
# Probe steps
# ---------------------------------------------------------------------------
def step_network(probe: ProbeResult, session: requests.Session):
    print("\n[1] Network reachability...")
    try:
        r = session.get(CBS_BASE, headers=HEADERS, timeout=TIMEOUT)
        probe.record("Network: GET cbssports.com", r.status_code < 400,
                     f"HTTP {r.status_code}")
    except Exception as e:
        probe.record("Network: GET cbssports.com", False, str(e))


def step_csrf(probe: ProbeResult, session: requests.Session) -> str | None:
    print("\n[2] CSRF token extraction...")
    csrf_token = None
    try:
        r = session.get(CBS_LOGIN_URL, headers=HEADERS, timeout=TIMEOUT)
        if not BS4_AVAILABLE:
            probe.record("CSRF token", False, "beautifulsoup4 not installed — skipped")
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        # Try multiple common field names
        for name in ["_csrf_token", "csrf_token", "csrfToken", "authenticity_token",
                     "xsrf_token", "_token"]:
            tag = soup.find("input", {"name": name})
            if tag:
                csrf_token = tag.get("value", "")
                probe.record("CSRF token", True, f"field={name}, value={csrf_token[:20]}...")
                return csrf_token
        # Fallback: scan all hidden inputs
        hidden = soup.find_all("input", {"type": "hidden"})
        hidden_names = [h.get("name") for h in hidden]
        probe.record("CSRF token", False,
                     f"Not found. Hidden fields on page: {hidden_names}")
    except Exception as e:
        probe.record("CSRF token", False, str(e))
    return csrf_token


def step_login(probe: ProbeResult, session: requests.Session,
               username: str, password: str, csrf_token: str | None) -> bool:
    print("\n[3] Login POST...")
    if not username or not password:
        probe.record("Login POST", False,
                     "Credentials missing — check .env for CBS_USERNAME / CBS_PASSWORD")
        return False

    # Try multiple field-name patterns CBS has used
    payloads = [
        {"email": username, "password": password},
        {"username": username, "password": password},
        {"userid": username, "password": password},
    ]
    if csrf_token:
        for p in payloads:
            p["_csrf_token"] = csrf_token

    last_err = ""
    for payload in payloads:
        try:
            r = session.post(
                CBS_LOGIN_URL,
                data=payload,
                headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded",
                         "Referer": CBS_LOGIN_URL},
                timeout=TIMEOUT,
                allow_redirects=True,
            )
            # Real check: did we get auth-looking cookies back?
            cookie_names = [c.name.lower() for c in session.cookies]
            auth_cookies = [n for n in cookie_names
                            if any(x in n for x in ["pid", "sess", "auth", "token", "login"])]
            if r.status_code < 400 and auth_cookies:
                probe.record("Login POST", True,
                             f"HTTP {r.status_code}, fields={list(payload.keys())}, "
                             f"auth cookies: {auth_cookies}")
                return True
            last_err = (f"HTTP {r.status_code}, no auth cookies after POST "
                        f"(got: {cookie_names}). CBS login is a JS/XHR flow — "
                        f"a plain form POST won't authenticate.")
        except Exception as e:
            last_err = str(e)

    probe.record("Login POST", False, last_err)
    return False


def step_cookies(probe: ProbeResult, session: requests.Session):
    print("\n[4] Session cookies...")
    cookies = dict(session.cookies)
    relevant = {k: v[:30] + "..." for k, v in cookies.items()
                if any(x in k.lower() for x in ["sess", "auth", "token", "login", "cbs"])}
    if relevant:
        probe.record("Session cookies", True,
                     f"Found {len(cookies)} cookies, relevant: {list(relevant.keys())}",
                     json.dumps(relevant, indent=2))
    else:
        probe.record("Session cookies", len(cookies) > 0,
                     f"Found {len(cookies)} cookies (none look auth-related): {list(cookies.keys())}")


def step_fantasy_home(probe: ProbeResult, session: requests.Session,
                      league_id: str, sport: str = "baseball"):
    print("\n[5] Fantasy league home page...")
    if not league_id:
        probe.record("Fantasy home", False, "No league_id provided")
        return
    url = f"{league_base(league_id, sport)}/"
    try:
        r = session.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        ok = r.status_code < 400
        redirected_to_login = "login" in r.url.lower() or "signin" in r.url.lower()
        detail = f"HTTP {r.status_code} — {url}"
        if redirected_to_login:
            detail += f" (redirected to login: {r.url} — page exists but needs auth)"
        snippet = r.text[:500] if not ok else ""
        probe.record("Fantasy home", ok, detail, snippet)
    except Exception as e:
        probe.record("Fantasy home", False, str(e))


def step_roster_page(probe: ProbeResult, session: requests.Session,
                     league_id: str, team_id: str, sport: str = "baseball"):
    print("\n[6] Roster page fetch & parse...")
    if not league_id or not team_id:
        probe.record("Roster fetch", False, "league_id or team_id missing")
        return

    url = f"{league_base(league_id, sport)}/teams/{team_id}"
    try:
        r = session.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code >= 400:
            probe.record("Roster fetch", False, f"HTTP {r.status_code} — {url}")
            return
        detail = f"HTTP {r.status_code} — {url}"
        if "login" in r.url.lower() or "signin" in r.url.lower():
            detail += f" (redirected to login: {r.url} — needs auth)"
        probe.record("Roster fetch", True, detail)
    except Exception as e:
        probe.record("Roster fetch", False, str(e))
        return

    if not BS4_AVAILABLE:
        probe.record("Roster parse", False, "beautifulsoup4 not installed")
        return

    print("\n[6b] HTML parsing strategies...")
    soup = BeautifulSoup(r.text, "html.parser")
    strategies = [
        ("table.playerRow",         lambda s: s.select("table.playerRow")),
        ("tr.playerRow",            lambda s: s.select("tr.playerRow")),
        ("[class*=player]",         lambda s: s.select("[class*=player]")),
        ("table rows (generic)",    lambda s: s.select("table tr")),
        (".roster",                 lambda s: s.select(".roster")),
        ("div[id*=roster]",         lambda s: s.select("div[id*=roster]")),
    ]
    any_found = False
    for label, fn in strategies:
        results = fn(soup)
        if results:
            probe.record(f"Parse: {label}", True, f"{len(results)} elements found",
                         str(results[0])[:300])
            any_found = True
            break
        else:
            probe.record(f"Parse: {label}", False, "0 elements")

    if not any_found:
        # Dump page title and first 600 chars so we can diagnose
        title = soup.title.string if soup.title else "no <title>"
        probe.record("Raw page dump", False,
                     f"Title: {title}",
                     r.text[:600])


def step_token_hunt(probe: ProbeResult, session: requests.Session,
                    league_id: str, sport: str = "baseball") -> list[tuple[str, str]]:
    """Scan logged-in league pages for embedded CBS API access tokens.
    Returns list of (label, token) candidates — validated later against the API."""
    print("\n[7] API token hunt (scan league page JS)...")
    if not league_id:
        probe.record("Token hunt", False, "No league_id provided")
        return []

    import re
    patterns = [
        ("var token",     r'var\s+token\s*=\s*["\']([^"\']{10,})["\']'),
        ("access_token=", r'access_token=([A-Za-z0-9%_\-\.+/=]{10,})'),
        ("access_token:", r'["\']?access_token["\']?\s*[:=]\s*["\']([^"\']{10,})["\']'),
        ("apiToken",      r'["\']?api[_]?[tT]oken["\']?\s*[:=]\s*["\']([^"\']{10,})["\']'),
        ("token:",        r'["\']token["\']\s*:\s*["\']([^"\']{20,})["\']'),
    ]

    pages = [
        f"{league_base(league_id, sport)}/",
        f"{league_base(league_id, sport)}/setup/league-details",
        f"{league_base(league_id, sport)}/teams",
    ]
    candidates: list[tuple[str, str]] = []   # (label@url, token)
    seen = set()
    for page_url in pages:
        try:
            r = session.get(page_url, headers=HEADERS, timeout=TIMEOUT)
        except Exception as e:
            probe.record("Token hunt", False, f"{page_url}: {e}")
            continue
        if "login" in r.url.lower():
            probe.record("Token hunt", False,
                         f"{page_url} redirected to login — cookie expired?")
            return []
        for label, pat in patterns:
            for m in re.finditer(pat, r.text):
                token = m.group(1)
                if token not in seen:
                    seen.add(token)
                    candidates.append((f"{label} @ {page_url}", token))

    if candidates:
        probe.record("Token hunt", True,
                     f"{len(candidates)} candidate token(s): "
                     + "; ".join(f"'{lbl}' {tok[:20]}..." for lbl, tok in candidates[:4]))
    else:
        probe.record("Token hunt", False,
                     "No token pattern matched on league pages")
    return candidates


def step_stats_probe(probe: ProbeResult, session: requests.Session,
                     league_id: str, team_id: str, sport: str,
                     valid_token: str, valid_extra: str):
    """Step 9: probe all plausible CBS stats/scoring API endpoints.
    Dumps field names and sample values so we know what's available."""
    print("\n[9] CBS Stats & Scoring API probe...")

    def api_get(endpoint: str, **params):
        base_params = (f"version=3.0&access_token={valid_token}"
                       f"&response_format=JSON{valid_extra}")
        extra = "".join(f"&{k}={v}" for k, v in params.items())
        url = f"https://api.cbssports.com/fantasy/{endpoint}?{base_params}{extra}"
        try:
            r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
            try:
                data = r.json()
            except Exception:
                return r.status_code, None, r.text[:200]
            return r.status_code, data, None
        except Exception as e:
            return 0, None, str(e)

    def summarise(data: dict) -> str:
        """Return a compact summary: top-level keys, body keys, first-record fields."""
        if data is None:
            return "(no data)"
        top = list(data.keys())
        body = data.get("body") or {}
        body_keys = list(body.keys()) if isinstance(body, dict) else f"list[{len(body)}]"
        # Try to find a list with player/stat records and show first record's keys
        sample_keys = None
        if isinstance(body, dict):
            for v in body.values():
                if isinstance(v, list) and v:
                    first = v[0]
                    if isinstance(first, dict):
                        sample_keys = list(first.keys())[:20]
                    break
        parts = [f"top={top}", f"body_keys={body_keys}"]
        if sample_keys:
            parts.append(f"first_record_keys={sample_keys}")
        return " | ".join(str(p) for p in parts)

    endpoints = [
        # Scoring / matchup
        ("league/scoring/live",        {}),
        ("league/scoring",             {}),
        ("league/standings",           {}),
        ("league/matchups",            {}),
        # Player stats — season
        ("players/stats",              {"SPORT": sport}),
        ("players/stats",              {"SPORT": sport, "stats_type": "season"}),
        ("players/stats",              {"SPORT": sport, "stats_type": "ytd"}),
        ("players/stats/season",       {"SPORT": sport}),
        # Player stats — projections
        ("players/projections",        {"SPORT": sport}),
        ("players/projections/season", {"SPORT": sport}),
        # Player stats on a specific team's roster
        ("players/stats",              {"SPORT": sport, "team_id": team_id}),
        # What does players/list actually return?  (validated, dump first record)
        ("players/list",               {"SPORT": sport}),
    ]

    found_useful = []
    for endpoint, params in endpoints:
        status, data, err = api_get(endpoint, **params)
        if err or data is None:
            probe.record(f"Stats: {endpoint} {params}",
                         False, err or f"HTTP {status} non-JSON")
            continue

        api_status = int(data.get("statusCode", status))
        body = data.get("body") or {}

        # Check if there's any meaningful content
        has_content = False
        first_record = None
        if isinstance(body, dict):
            for v in body.values():
                if isinstance(v, list) and v:
                    has_content = True
                    first_record = v[0] if isinstance(v[0], dict) else None
                    break
                elif isinstance(v, dict) and v:
                    has_content = True
                    break
        elif isinstance(body, list) and body:
            has_content = True
            first_record = body[0] if isinstance(body[0], dict) else None

        ok = api_status < 400 and has_content
        summary = summarise(data)

        # For players/list, show what stat fields are on the first record
        sample_str = ""
        if first_record and endpoint == "players/list":
            # Show first record's numeric / stat-looking fields
            stat_fields = {k: v for k, v in first_record.items()
                           if isinstance(v, (int, float, str)) and k not in
                           ("id", "fullname", "name", "position", "pro_team",
                            "owned_by_team_id", "on_waivers")}
            sample_str = f"\nFirst record stat fields: {json.dumps(stat_fields, indent=2)[:600]}"
        elif first_record:
            sample_str = f"\nFirst record keys: {list(first_record.keys())[:25]}"
            # Show any numeric fields that look like stats
            stat_fields = {k: v for k, v in first_record.items()
                           if isinstance(v, (int, float)) and k not in ("id",)}
            if stat_fields:
                sample_str += f"\nNumeric fields: {json.dumps(stat_fields)[:400]}"

        label = f"Stats: {endpoint}" + (f" {params}" if params else "")
        probe.record(label, ok, summary, sample_str)

        if ok:
            found_useful.append(endpoint)

    if found_useful:
        probe.record("Stats probe summary", True,
                     f"Useful endpoints found: {found_useful}")
    else:
        probe.record("Stats probe summary", False,
                     "No CBS stats endpoints returned usable data — "
                     "will need external source (FantasyPros/Fangraphs)")

    # Deep-dive into league/scoring/live — dump my team's entry in full
    print("\n[9b] league/scoring/live — my team deep dump...")
    status, data, err = api_get("league/scoring/live")
    if err or data is None:
        probe.record("scoring/live deep dump", False, err or f"HTTP {status}")
        return

    live = (data.get("body") or {}).get("live_scoring") or {}
    system = live.get("system", "?")
    my_team_id = str(live.get("my_team_id", ""))
    teams = live.get("teams", [])

    # Find my team entry
    my_team = next((t for t in teams if str(t.get("id", t.get("team_id", ""))) == my_team_id), None)
    if my_team is None and teams:
        my_team = teams[0]  # fallback: first team

    probe.record(f"scoring/live deep dump ({system})", my_team is not None,
                 f"system={system}, my_team_id={my_team_id}, teams={len(teams)}",
                 json.dumps(my_team, indent=2)[:5000] if my_team else "(team not found)")


def step_api_endpoint(probe: ProbeResult, session: requests.Session,
                      league_id: str, team_id: str,
                      candidates: list[tuple[str, str]] | None = None):
    """Validate each candidate token against league/details, then fetch rosters
    with the first one that works. Tries with and without explicit league_id."""
    print("\n[8] CBS JSON API endpoints (validating candidate tokens)...")
    if not candidates:
        probe.record("JSON API", False,
                     "No access token candidates — skipping API test")
        return None, ""

    def api_get(endpoint: str, token: str, extra: str = ""):
        url = (f"https://api.cbssports.com/fantasy/{endpoint}?version=3.0"
               f"&access_token={token}&response_format=JSON{extra}")
        r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
        try:
            return r, r.json()
        except Exception:
            return r, None

    valid_token = None
    valid_extra = ""
    for label, token in candidates:
        for extra in ["", f"&league_id={league_id}"]:
            try:
                r, data = api_get("league/details", token, extra)
                if r.status_code < 400 and data and data.get("statusCode", r.status_code) < 400:
                    valid_token, valid_extra = token, extra
                    probe.record("JSON API: league/details", True,
                                 f"HTTP {r.status_code} via '{label}'"
                                 + (f" with {extra}" if extra else ""),
                                 json.dumps(data, indent=2)[:800])
                    break
            except Exception as e:
                probe.record(f"JSON API try ({label})", False, str(e))
        if valid_token:
            break

    if not valid_token:
        probe.record("JSON API: league/details", False,
                     f"None of {len(candidates)} candidate token(s) accepted by API")
        return None, ""

    try:
        r, data = api_get("league/rosters", valid_token,
                          f"{valid_extra}&team_id={team_id}")
        ok = r.status_code < 400 and data is not None
        probe.record("JSON API: league/rosters", ok,
                     f"HTTP {r.status_code}",
                     json.dumps(data, indent=2)[:800] if data else r.text[:300])
    except Exception as e:
        probe.record("JSON API: league/rosters", False, str(e))

    return valid_token, valid_extra


# ---------------------------------------------------------------------------
# Multi-league support
# ---------------------------------------------------------------------------
def load_all_leagues() -> list[dict]:
    """Load every league from config/leagues.yaml.
    Returns [{name, league_id, team_id, sport}, ...]"""
    yaml_path = os.path.join(os.path.dirname(__file__), "config", "leagues.yaml")
    if not os.path.exists(yaml_path):
        return []
    try:
        import yaml as _yaml
        with open(yaml_path) as f:
            data = _yaml.safe_load(f)
        leagues = []
        for sport, entries in (data or {}).items():
            # leagues.yaml also carries top-level season_start/periods keys
            # (BUG 5 fix) that aren't sport -> [league, ...] entries.
            if not isinstance(entries, list):
                continue
            for e in entries or []:
                if not isinstance(e, dict):
                    continue
                leagues.append({
                    "name": e.get("name", e.get("id", "?")),
                    "league_id": str(e.get("cbs_league_id", "")),
                    "team_id": str(e.get("cbs_team_id", "")),
                    "sport": sport,
                })
        return leagues
    except ImportError:
        print("[warn] PyYAML not installed — run: pip install pyyaml")
        return []
    except Exception as e:
        print(f"[warn] Could not parse leagues.yaml: {e}")
        return []


def run_league_steps(probe, session, league_id, team_id, sport):
    """Steps 5-9 for a single league."""
    print(f"\n{'─' * 60}")
    print(f"LEAGUE: {league_id} ({sport})  TEAM: {team_id}")
    print(f"{'─' * 60}")
    step_fantasy_home(probe, session, league_id, sport)
    step_roster_page(probe, session, league_id, team_id, sport)
    candidates = step_token_hunt(probe, session, league_id, sport)
    token, extra = step_api_endpoint(probe, session, league_id, team_id, candidates)
    if token:
        step_stats_probe(probe, session, league_id, team_id, sport, token, extra)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="CBS Sports fantasy probe")
    parser.add_argument("--user",      help="CBS username/email")
    parser.add_argument("--password",  help="CBS password")
    parser.add_argument("--league-id", dest="league_id", help="CBS league ID")
    parser.add_argument("--team-id",   dest="team_id",   help="Your team ID")
    parser.add_argument("--skip-auth", action="store_true",
                        help="Skip login, test roster page with anonymous session")
    parser.add_argument("--cookie",
                        help="Browser-captured Cookie header value (or set CBS_COOKIE in .env)")
    parser.add_argument("--sport", default="baseball",
                        help="Sport for the league subdomain (baseball, football)")
    parser.add_argument("--all", action="store_true",
                        help="Probe every league in config/leagues.yaml")
    args = parser.parse_args()

    username, password, league_id, team_id, cookie = load_credentials(args)

    print("=" * 60)
    print("CBS Sports Fantasy Probe")
    print(f"League: {league_id or '(not set)'}  Team: {team_id or '(not set)'}")
    print(f"User:   {username or '(not set)'}")
    print("=" * 60)

    probe   = ProbeResult()
    session = requests.Session()
    session.headers.update(HEADERS)

    step_network(probe, session)

    if cookie:
        session.headers["Cookie"] = cookie
        logged_in = True
        print("\n[auth] Using browser-captured cookie (skipping login POST)")
        probe.record("Cookie auth", True,
                     f"Cookie header set ({len(cookie)} chars)")
    elif not args.skip_auth:
        csrf = step_csrf(probe, session)
        logged_in = step_login(probe, session, username, password, csrf)
        step_cookies(probe, session)
    else:
        logged_in = False
        print("\n[skip] Auth skipped via --skip-auth flag")

    if args.all:
        leagues = load_all_leagues()
        if not leagues:
            print("[error] --all given but no leagues loaded from config/leagues.yaml")
            sys.exit(1)
        print(f"\nProbing {len(leagues)} league(s) from leagues.yaml...")
        for lg in leagues:
            run_league_steps(probe, session, lg["league_id"], lg["team_id"], lg["sport"])
    else:
        run_league_steps(probe, session, league_id, team_id, args.sport)

    # Write report
    report = probe.summary()
    out_path = os.path.join(os.path.dirname(__file__), "PROBE_RESULT.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)

    print("\n" + "=" * 60)
    print(f"PASSED: {probe.passed}  FAILED: {probe.failed}")
    print(f"Report written to: {out_path}")
    print("Paste PROBE_RESULT.md into Claude for diagnosis.")
    print("=" * 60)

    sys.exit(0 if probe.failed == 0 else 1)


if __name__ == "__main__":
    main()
