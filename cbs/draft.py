"""
Fetch draft order / draft results from CBS for a fantasy league.

Unlike rosters/standings/scoring (cbs/standings.py, sports/*), CBS does not
expose the draft board through the api.cbssports.com JSON API used
elsewhere in this repo -- there is no documented league/draft endpoint, and
none was found by grepping this repo's earlier API probing (cbs_probe.py
has no draft coverage as of 2026-08-24). The draft board *is* available as
a plain server-rendered HTML table at:

    https://{cbs_league_id}.{sport}.cbssports.com/draft/results

This works both BEFORE a draft (shows the pick order, empty player cells)
and AFTER/DURING one (player cells fill in pick by pick), so this one
function and endpoint cover both "draft order" and "draft results".

Confirmed live via Chrome inspection 2026-08-24 across all 3 football
leagues (sfflf, hcfl05, ecfc), all still pre-draft at that point:
    <table class="data borderTop">
      <tr class="subtitle"><td colspan="3">Round 1</td></tr>
      <tr class="label"><th>Pick</th><th>Team</th><th>Player</th></tr>
      <tr class="row1"><td>1</td><td>H.E. Pennypacker</td><td></td></tr>
      <tr class="row2"><td>2</td><td>The Maestro</td><td></td></tr>
      ...
      <tr class="subtitle"><td colspan="3">Round 2</td></tr>
      ...

Team names in the "Team" cell are plain text, not links -- there's no team
ID in this markup, so callers that need a team ID must match the name
against get_all_team_rosters()'s team map (name matching, not by id).

CAVEAT (unverified): no league had completed even a single real pick as of
2026-08-24, so the exact text format CBS puts in a filled "Player" cell
(name / position / NFL team layout, any "*" auto-pick or "**" queued-pick
markers seen on other CBS pages) has not been observed against live markup.
_parse_rounds() extracts that cell's stripped text as-is into "player_raw"
without further parsing -- treat player_raw as an opaque string until a
real pick has been checked and this docstring is updated.
"""
import logging
import re

from cbs.auth import CBSAuth, CBSAPIError

logger = logging.getLogger(__name__)

_ROUND_RE = re.compile(
    r'<tr class="subtitle"><td colspan="3">Round\s+(\d+)</td></tr>', re.I)
_ROW_RE = re.compile(
    r'<tr class="row[12]"[^>]*>\s*'
    r'<td[^>]*>\s*(\d+)\s*</td>\s*'
    r'<td[^>]*>(.*?)</td>\s*'
    r'<td[^>]*>(.*?)</td>\s*'
    r'</tr>',
    re.I | re.S,
)
_TAG_RE = re.compile(r'<[^>]+>')


def fetch_draft_board(auth: CBSAuth, league_id: str, sport: str = "football") -> dict:
    """Scrape /draft/results for one league. Works pre- or post-draft.

    Returns:
      {
        "status": "not_started" | "in_progress" | "completed" | "unknown",
        "rounds": [
          {"round": 1, "picks": [
              {"round": 1, "pick": 1, "overall": 1,
               "team": "H.E. Pennypacker", "player_raw": None}, ...
          ]}, ...
        ],
        "team_order_round1": ["H.E. Pennypacker", "The Maestro", ...],
      }

    Raises CBSAPIError (via CBSAuth.fetch_league_page) on auth failure.
    """
    r = auth.fetch_league_page(league_id, sport, "/draft/results")
    rounds = _parse_rounds(r.text)

    if not rounds:
        logger.warning(
            "draft board: no rounds parsed for %s/%s -- CBS page layout may "
            "have changed (table.data.borderTop / tr.subtitle structure "
            "assumed, see cbs/draft.py docstring)", sport, league_id)
        return {"status": "unknown", "rounds": [], "team_order_round1": []}

    all_picks = [p for rnd in rounds for p in rnd["picks"]]
    made = [p for p in all_picks if p["player_raw"]]
    if not made:
        status = "not_started"
    elif len(made) == len(all_picks):
        status = "completed"
    else:
        status = "in_progress"

    return {
        "status": status,
        "rounds": rounds,
        "team_order_round1": [p["team"] for p in rounds[0]["picks"]],
    }


def my_picks(board: dict, my_team_name: str) -> list[dict]:
    """Every pick belonging to my_team_name, in draft order.

    Matches on the exact team-name text CBS renders in the Team cell (see
    module docstring -- there's no team ID in this markup). Returns [] if
    the name doesn't match anything found on the board (e.g. stale
    my_team_name in config/leagues.yaml after a CBS team rename -- check
    board["team_order_round1"] against config if this comes back empty
    unexpectedly).
    """
    return [
        {"round": p["round"], "pick_in_round": p["pick"],
         "overall": p["overall"], "player_raw": p["player_raw"]}
        for rnd in board["rounds"] for p in rnd["picks"]
        if p["team"] == my_team_name
    ]


def _parse_rounds(html: str) -> list[dict]:
    markers = list(_ROUND_RE.finditer(html))
    rounds = []
    picks_per_round = None
    for i, m in enumerate(markers):
        round_num = int(m.group(1))
        start = m.end()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(html)
        chunk = html[start:end]

        picks = []
        for row_m in _ROW_RE.finditer(chunk):
            pick_num = int(row_m.group(1))
            team = _strip_tags(row_m.group(2)).strip()
            player_raw = _strip_tags(row_m.group(3)).strip() or None
            picks.append({"round": round_num, "pick": pick_num,
                          "team": team, "player_raw": player_raw})

        if picks_per_round is None:
            picks_per_round = len(picks)
        for p in picks:
            p["overall"] = (round_num - 1) * picks_per_round + p["pick"]

        rounds.append({"round": round_num, "picks": picks})
    return rounds


def _strip_tags(s: str) -> str:
    return _TAG_RE.sub("", s)
