"""
Decision engine for football leagues -- the football equivalent of
agent/decisions.py, kept in its own file rather than added as branches
inside decisions.py because that module is 100% baseball-specific (hard
imports sports.baseball.* throughout) and mixing sports there risked
breaking the working baseball pipeline. This file is deliberately much
smaller: it only does what sports/football/ currently supports.

What this DOES do (real, CBS-data-grounded logic):
  - Fetch the team's live roster (already done generically by the caller
    via cbs/roster.py::get_roster -- no football-specific fetch needed).
  - Validate starting-lineup/roster legality against the league's real
    rules (sports/football/roster_rules.py).
  - If any starting slots are open, fetch the free-agent pool (generic
    cbs/waivers.py::fetch_waiver_wire, same endpoint baseball uses) and
    surface eligible fills per slot (sports/football/waivers.py).
  - Keeper guidance (sports/football/keepers.py): reports each league's
    REAL keeper policy (f_league: keeper league, max 2 any position,
    confirmed directly by Christopher; hard_chargers: not a keeper league;
    east_coast: keeper league, max 3, individual-manager selection, plus a
    3-year-contract house rule -- confirmed live via commissioner settings
    2026-08-01 and 2026-08-13). For east_coast, if FANTASYPROS_API_KEY is
    configured, ranks the roster by FantasyPros' NFL consensus rankings
    (ECR) and recommends the top 3 -- see _fp_nfl_rankings_by_name(). Its
    field mapping (player_name / rank_ecr) was VERIFIED against a real
    /nfl/2026/consensus-rankings response via fp_probe.py on 2026-08-23
    (see project memory) -- this is no longer a guess.
  - Contract expiration (east_coast ONLY, wired up 2026-08-23): before
    ranking, fetches CBS's live CONTRACT column off the roster page
    (cbs.roster.fetch_contract_years, verified live 2026-08-18 -- see
    project memory) and converts it to the acquired_season map
    keeper_guidance() needs (sports/football/keepers.py::
    contract_years_to_acquired_seasons()). Players whose 3-year contract
    has expired are excluded from recommended_keeps/other_eligible
    entirely (they show up in contract_expired instead) -- this is what
    correctly excludes a player like Jonathan Taylor (contract=0) even
    though he might otherwise be the #1 ECR-ranked keeper candidate on the
    roster. See _east_coast_contract_data()'s docstring for the safe-
    degrade behavior on any fetch/match failure.
  - Waiver-target ranking for all 3 leagues, when FANTASYPROS_API_KEY is
    configured, instead of ownership_pct alone (see
    sports/football/waivers.py::_PROJECTION_LEAGUES) -- but not from one
    shared signal, since the leagues don't share a scoring format:
      * hard_chargers/east_coast (both real per-play PPR): ranked by
        FantasyPros' own points_ppr season-long projection directly (see
        _fp_nfl_projections_by_name()). Field mapping (name /
        stats.points_ppr) verified via fp_probe.py on 2026-08-23.
      * f_league (non-PPR, tiered, position-dependent scoring -- points_ppr
        doesn't represent it): ranked by an ESTIMATE of sfflf's own
        formula, reimplemented against FantasyPros' raw per-category stat
        projections (see _fp_sfflf_points_by_name() and
        sports/football/scoring.py::estimate_sfflf_points()). This omits
        the real formula's long-TD-yardage bonus (season projections don't
        carry per-TD yardage) -- a systematic but uniform underestimate,
        fine for ranking candidates against each other, not a real point
        projection. K/DST get no estimate (returns None) and always fall
        back to ownership_pct within f_league.

What this does NOT do yet, on purpose:
  - No performance-based start/sit or lineup-optimizer logic.
  - No trade evaluation, no drop-candidate scoring, no matchup/score
    tracking -- none of these exist for football yet.

DRY_RUN applies here the same as baseball: this module only ever produces
a report, never submits a roster move.
"""

import logging
from pathlib import Path

import yaml

from sports.football.roster_rules import validate_roster
from sports.football.waivers import (
    find_waiver_candidates_for_open_slots,
    _PROJECTION_LEAGUES,
)
from sports.football.scoring import estimate_sfflf_points
from sports.football.keepers import keeper_guidance, contract_years_to_acquired_seasons, KEEPER_POLICIES
from cbs.waivers import fetch_waiver_wire
from cbs.roster import fetch_contract_years, get_all_team_rosters
from config.settings import FANTASYPROS_API_KEY
from fantasypros.client import FantasyProsClient

logger = logging.getLogger(__name__)

_fp_client = FantasyProsClient(FANTASYPROS_API_KEY) if FANTASYPROS_API_KEY else None

# -- manual keeper overrides -------------------------------------------------
# See config/manual_keepers.yaml's own docstring for the full rationale:
# keeper SELECTION for a commissioner-set league (f_league) isn't something
# a roster scrape can predict, and the real picks may not be known until
# draft day. This lets a manually-confirmed answer, once known, take
# priority over the algorithmic guess -- entered either by editing that
# file directly or via the set_manual_keepers/clear_manual_keepers MCP
# tools (mcp_server.py).
MANUAL_KEEPERS_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "manual_keepers.yaml"
)


def _load_manual_keepers(internal_league_id: str) -> dict[str, list[str]]:
    """{team_name: [player names]} of manually-confirmed keepers for this
    league, keyed by team name exactly as CBS reports it. Returns {} if the
    file is missing, malformed, or has no entries for this league -- never
    raises and never fabricates an override; an empty result just means
    "no manual override on file, use the algorithmic prediction."
    """
    try:
        with open(MANUAL_KEEPERS_PATH) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("manual_keepers.yaml failed to parse (%s) -- "
                       "ignoring manual overrides this run", e)
        return {}
    return (data.get("leagues") or {}).get(internal_league_id) or {}


def save_manual_keepers(internal_league_id: str, team_name: str,
                        players: list[str]) -> None:
    """Write/replace one team's manually-confirmed keeper list.

    Best-effort, not necessarily durable: this writes to
    config/manual_keepers.yaml on whatever filesystem this process is
    running on. Editing it via Claude in a coding session and committing
    the change to git is durable (same as every other config file in this
    repo); calling this from the LIVE deployed MCP server (e.g. Render) is
    only durable if that file also gets committed/pushed afterward --
    Render's free tier filesystem does not survive a redeploy. Callers on
    a live server should treat this as "good for the rest of this
    session/until the next deploy" unless they know the change has also
    been committed.

    players=[] is a valid, meaningful entry (confirmed: keeping nobody) --
    distinct from no entry at all.
    """
    try:
        with open(MANUAL_KEEPERS_PATH) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {}
    data.setdefault("leagues", {}).setdefault(internal_league_id, {})[team_name] = list(players)
    with open(MANUAL_KEEPERS_PATH, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    logger.info("save_manual_keepers: %s / %r -> %d player(s)",
               internal_league_id, team_name, len(players))


def clear_manual_keepers(internal_league_id: str, team_name: str | None = None) -> None:
    """Remove one team's manual override (team_name given), or every
    manual override for the league (team_name=None) -- e.g. once real
    keepers are locked in via CBS and the manual entry is no longer
    needed, or to undo a wrong entry. No-op (not an error) if there was
    nothing to remove."""
    try:
        with open(MANUAL_KEEPERS_PATH) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return
    leagues = data.get("leagues") or {}
    if internal_league_id not in leagues:
        return
    if team_name is None:
        del leagues[internal_league_id]
    else:
        leagues[internal_league_id].pop(team_name, None)
    with open(MANUAL_KEEPERS_PATH, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)

# east_coast's fantasy football season year, for the 3-year-contract math
# (sports/football/keepers.py::contract_years_to_acquired_seasons()) --
# bump this once per year. Not auto-derived from today's date because an
# NFL fantasy season spans two calendar years (draft in Aug, playoffs into
# the following Jan/Feb) and "which season is this" isn't a pure function
# of the current date without also knowing where in that span we are.
_FOOTBALL_CURRENT_SEASON = 2026


def _fp_nfl_rankings_by_name(client) -> dict[str, float]:
    """Adapter from FantasyProsClient.nfl_consensus_rankings() to a
    {normalized_name: rank} dict, lower rank = better player.

    Field mapping (player_name, rank_ecr) was VERIFIED live on 2026-08-23
    via fp_probe.py against a real /nfl/2026/consensus-rankings?type=ROS
    response (513 players returned) -- this used to be a multi-key guess
    across a few plausible field names because no network access existed
    to check it; that guess happened to match reality, so no behavior
    changed here, only the docstring and the removal of the now-unneeded
    fallback keys.

    CORRECTED 2026-08-25: type="WW" (waiver wire) rankings come back with
    0 players -- this was previously attributed to FantasyPros' premium
    tier gating the endpoint (the response carries a "tier":"premium"
    field). That diagnosis doesn't hold up: Christopher upgraded to
    FantasyPros' HOF plan and re-checked (fp_hof_diag.py) -- WW still
    returns 0 players, while ROS/ADP both return real data (530/681
    players) with the SAME "tier":"premium" field present on every
    response type, working or not. That field is evidently a static
    label, not a live access gate. The far more likely explanation:
    it's preseason (week 1 hasn't happened), and "waiver wire" rankings
    are a during-season product with nothing to rank yet -- WW's own
    response even carries a stale "last_updated":"1/01" vs. ROS/ADP's
    current "8/25"/"8/26". Worth re-checking once the season is
    underway; type="ROS", used here, was never affected either way.
    Still degrades to an empty dict (never a fabricated ranking) on any
    fetch error, same safe-failure pattern as the rest of this module.
    """
    if client is None:
        return {}
    try:
        entries = client.nfl_consensus_rankings(rank_type="ROS")
    except Exception as e:
        logger.warning("FantasyPros NFL consensus rankings fetch failed: %s", e)
        return {}

    rankings = {}
    for entry in entries:
        name = entry.get("player_name")
        rank = entry.get("rank_ecr")
        if name and rank is not None:
            try:
                rankings[name.strip().lower()] = float(rank)
            except (TypeError, ValueError):
                continue
    return rankings


def _fp_nfl_projections_by_name(client) -> dict[str, float]:
    """Adapter from FantasyProsClient.nfl_projections() to a
    {normalized_name: points_ppr} dict -- FantasyPros' own season-long PPR
    fantasy-point projection per player, used to rank waiver-wire
    candidates for hard_chargers/east_coast (see
    sports/football/waivers.py::_PPR_PROJECTION_LEAGUES). Both are real
    per-play PPR leagues, so points_ppr is a legitimate value signal for
    them specifically -- NOT for sfflf, whose non-PPR tiered/position-diff
    scoring points_ppr doesn't represent (waivers.py enforces that
    exclusion, not this function).

    Field mapping (name, stats.points_ppr) was VERIFIED live on 2026-08-23
    via fp_probe.py against a real /nfl/2026/projections response (602
    players returned). Degrades to an empty dict on any fetch error or
    missing field, same safe-failure pattern used throughout this module --
    no fabricated projections.
    """
    if client is None:
        return {}
    try:
        entries = client.nfl_projections(position="ALL", scoring="PPR")
    except Exception as e:
        logger.warning("FantasyPros NFL projections fetch failed: %s", e)
        return {}

    projections = {}
    for entry in entries:
        name = entry.get("name")
        pts = (entry.get("stats") or {}).get("points_ppr")
        if name and pts is not None:
            try:
                projections[name.strip().lower()] = float(pts)
            except (TypeError, ValueError):
                continue
    return projections


def _fp_sfflf_points_by_name(client) -> dict[str, float]:
    """Adapter from FantasyProsClient.nfl_projections() to a
    {normalized_name: estimated_sfflf_points} dict -- ranks f_league
    (sfflf) waiver-wire candidates by an ESTIMATE of f_league's OWN
    tiered/position-dependent scoring, instead of leaving it on
    ownership_pct-only sorting (the previous behavior, still the fallback
    if this returns {} or FANTASYPROS_API_KEY isn't configured).

    Same underlying data source as _fp_nfl_projections_by_name() --
    FantasyPros' /nfl/{season}/projections -- just scored through sfflf's
    own formula (sports/football/scoring.py::estimate_sfflf_points())
    instead of read directly as points_ppr, since sfflf isn't a PPR
    format and points_ppr doesn't represent it. See that function's
    docstring for the one deliberate gap (no long-TD-yardage bonus --
    season projections don't carry per-TD yardage) and why it's an
    acceptable estimate for ranking, not a real point projection.

    K/DST entries are skipped (estimate_sfflf_points() returns None for
    them -- see its docstring on why). Degrades to an empty dict on any
    fetch error, same safe-failure pattern as the rest of this module --
    never a fabricated estimate.
    """
    if client is None:
        return {}
    try:
        entries = client.nfl_projections(position="ALL", scoring="PPR")
    except Exception as e:
        logger.warning("FantasyPros NFL projections fetch failed (sfflf estimate): %s", e)
        return {}

    projections = {}
    for entry in entries:
        name = entry.get("name")
        position = entry.get("position_id")
        stats = entry.get("stats") or {}
        pts = estimate_sfflf_points(stats, position)
        if name and pts is not None:
            projections[name.strip().lower()] = pts
    return projections


def _east_coast_contract_data(auth, league_id, team_id, roster) -> dict[str, int] | None:
    """east_coast ONLY: fetch CBS's live CONTRACT column
    (cbs.roster.fetch_contract_years) and convert it into the
    {roster_player.name: acquired_season} shape keeper_guidance() expects.

    Matched by NORMALIZED name against the roster actually passed in
    (`roster`, a list of data.models.RosterSlot), not by trusting the
    scrape's own name strings directly -- fetch_contract_years() pulls
    from the HTML team page while `roster` here typically comes from
    cbs.roster.get_roster()'s JSON-API path, a different CBS data source
    whose exact name formatting isn't guaranteed to match character-for-
    character. keeper_guidance() itself does an EXACT (non-normalized)
    lookup on contract_data, so this function does the normalization once
    here and returns a dict keyed by each roster player's real, exact
    `.name` value.

    Returns None (never a partial/wrong guess treated as complete) if the
    fetch fails for any reason, or if nothing in the scrape matches
    anything on the roster -- keeper_guidance()'s existing "no contract
    data supplied" fallback applies in that case, same safe-degrade
    pattern as the FantasyPros adapters above.
    """
    try:
        raw = fetch_contract_years(auth, league_id, team_id, sport="football")
    except Exception as e:
        logger.warning("east_coast contract-years fetch failed: %s", e)
        return None
    if not raw:
        return None

    acquired_by_norm = {
        name.strip().lower(): season
        for name, season in contract_years_to_acquired_seasons(
            raw, _FOOTBALL_CURRENT_SEASON).items()
    }
    matched = {
        rs.player.name: acquired_by_norm[rs.player.name.strip().lower()]
        for rs in roster
        if rs.player.name.strip().lower() in acquired_by_norm
    }
    if len(matched) < len(raw):
        logger.warning(
            "east_coast contract data: matched %d/%d scraped players "
            "against the live roster by name -- some contract values "
            "went unused this run (name mismatch between the CONTRACT "
            "scrape and the roster source)", len(matched), len(raw))
    return matched or None


def league_keeper_report(auth, league_id, league_config, sport="football") -> dict:
    """Keeper guidance for EVERY team in a football keeper league, not just
    Christopher's own roster -- i.e. "what will each manager keep."

    run_football_decisions()/keeper_guidance() already answer this
    correctly for Christopher's own team (including the east_coast 3-year
    contract check that excludes contract=0 players). This function reuses
    that exact same per-team logic -- keeper_guidance(), the same contract
    fetch/normalization as _east_coast_contract_data(), the same FantasyPros
    ranking adapter -- just looped across every team cbs.roster.
    get_all_team_rosters() returns for the league, instead of only the one
    team_id in config/leagues.yaml.

    league_id: the CBS league id (e.g. "sfflf", "ecfc") -- same argument
               shape run_football_decisions() takes, NOT the internal id.
    league_config: the full league dict from config/leagues.yaml (its "id"
               key, e.g. "f_league", is what KEEPER_POLICIES/keeper_guidance
               key off of).

    Returns:
        {"league": league_name, "internal_id": ..., "is_keeper_league": bool,
         "policy_note": str, "teams": {team_id: {"team_name": ...,
         "recommended_keeps": [...], "other_eligible": [...],
         "contract_expired": [...], "ranking_source": ..., "note": ...}}}
    is_keeper_league False (hard_chargers) returns an empty "teams" dict --
    calling this on a non-keeper league is a valid no-op, not an error, so
    callers can loop every football league without special-casing.

    Every team's `note` carries forward keeper_guidance()'s own honest
    degrade text (e.g. "no contract data supplied" for a team whose CONTRACT
    column scrape failed or matched nothing) -- so a manager showing an
    empty/wrong-looking keeper list is legible as "we don't have data for
    them" rather than presented as a confident answer.
    """
    internal_id = league_config["id"]
    league_name = league_config.get("name", internal_id)
    policy = KEEPER_POLICIES.get(internal_id)
    if policy is None:
        raise ValueError(f"Unknown football league_id: {internal_id!r}")

    if not policy["is_keeper_league"]:
        return {
            "league": league_name, "internal_id": internal_id,
            "is_keeper_league": False, "policy_note": policy["note"],
            "teams": {},
        }

    all_rosters = get_all_team_rosters(auth, league_id, sport)
    rankings = _fp_nfl_rankings_by_name(_fp_client) if _fp_client else {}
    ranking_source = "fantasypros_ecr" if rankings else None
    has_contract_rule = internal_id in {"east_coast"}  # see keepers.py's _CONTRACT_LEAGUES
    manual = _load_manual_keepers(internal_id)

    teams = {}
    for team_id, info in all_rosters.items():
        # Manual override takes priority whenever present, no matter what
        # the algorithmic prediction would say -- see config/
        # manual_keepers.yaml's docstring. This matters most for f_league,
        # where keepers are picked by the COMMISSIONER and may simply not
        # be predictable from roster/ranking data at all.
        if info["name"] in manual:
            teams[team_id] = {
                "team_name":         info["name"],
                "recommended_keeps": list(manual[info["name"]]),
                "other_eligible":    [],
                "contract_expired":  [],
                "ranking_source":    "confirmed manually",
                "note":              "Manually entered -- not a prediction.",
            }
            continue

        contract_data = None
        if has_contract_rule:
            # Same fetch + exact-name-normalization-against-this-roster
            # logic as run_football_decisions() uses for Christopher's own
            # team -- reused verbatim so a teammate's contract data is
            # matched the same safe way (never a partial/wrong guess
            # treated as complete; see _east_coast_contract_data()'s
            # docstring).
            contract_data = _east_coast_contract_data(
                auth, league_id, team_id, info["roster"])

        kg = keeper_guidance(
            info["roster"], internal_id,
            rankings=rankings or None,
            ranking_source=ranking_source,
            contract_data=contract_data,
            current_season=_FOOTBALL_CURRENT_SEASON if contract_data else None,
        )
        teams[team_id] = {
            "team_name":          info["name"],
            "recommended_keeps":  kg.recommended_keeps,
            "other_eligible":     kg.other_eligible,
            "contract_expired":   kg.contract_expired,
            "ranking_source":     kg.ranking_source,
            "note":               kg.note,
        }

    return {
        "league": league_name, "internal_id": internal_id,
        "is_keeper_league": True,
        "max_keepers": policy["max_keepers"],
        "decided_by": policy["decided_by"],
        "policy_note": policy["note"],
        "teams": teams,
    }


def predicted_keepers(auth, league_id, league_config, sport="football") -> set[str]:
    """The flat, league-wide set of every manager's predicted keeper names.

    Convenience wrapper around league_keeper_report() for callers that just
    need "which players are off the board for this league's live draft" --
    draft-board building (fantasypros/draft_board.py) and the draft guides
    built from it, chiefly. A kept player doesn't re-enter that league's
    draft pool, so any tool ranking/listing draft-available players should
    exclude this set.

    Deliberately uses ONLY each team's recommended_keeps (the top
    max_keepers players by rank), not other_eligible -- other_eligible
    players are NOT predicted to be kept (they lost the cutoff) and so are
    legitimately still available in the draft. contract_expired players
    (east_coast) are excluded from recommended_keeps already by
    keeper_guidance() and so are correctly absent from this set too --
    they're back in the draft pool, not kept.

    Returns an empty set for a non-keeper league (hard_chargers), or for a
    keeper league where no ranking signal was available to predict anyone's
    keeps (see keeper_guidance()'s own degrade behavior) -- never a partial
    or guessed set presented as complete.
    """
    report = league_keeper_report(auth, league_id, league_config, sport)
    if not report["is_keeper_league"]:
        return set()
    names: set[str] = set()
    for info in report["teams"].values():
        names.update(info["recommended_keeps"])
    return names


def run_football_decisions(auth, league_id, league_config, team, sport="football") -> dict:
    """league_id is the CBS league id (e.g. "sfflf"); league_config is the
    full dict from config/leagues.yaml's football section, whose "id" key
    (e.g. "f_league") is what sports/football/roster_rules.py keys off of --
    NOT the same as the CBS league_id."""
    internal_id = league_config["id"]
    league_name = league_config.get("name", internal_id)

    actions = []

    validation = validate_roster(team.roster, internal_id)
    actions.append({
        "type":              "roster_legality",
        "legal":             validation.legal,
        "starters_required": validation.starters_required,
        "starters_present":  validation.starters_present,
        "issues":            validation.issues,
        "unfilled_slots":    validation.unfilled_slots,
    })

    if validation.unfilled_slots:
        try:
            waivers = fetch_waiver_wire(auth, league_id, sport, position="all", limit=300)
        except Exception as e:
            logger.warning("Football waiver-wire fetch failed for %s: %s", league_id, e)
            waivers = []

        if waivers:
            if internal_id == "f_league":
                projections = _fp_sfflf_points_by_name(_fp_client) if _fp_client else {}
                ranking_source_label = "fantasypros_estimated_sfflf_points"
            else:
                projections = _fp_nfl_projections_by_name(_fp_client) if _fp_client else {}
                ranking_source_label = "fantasypros_projected_ppr"
            use_projections = bool(projections) and internal_id in _PROJECTION_LEAGUES
            candidates_by_slot = find_waiver_candidates_for_open_slots(
                team.roster, waivers, internal_id, projections=projections or None)
            if any(candidates_by_slot.values()):
                actions.append({
                    "type": "waiver_targets",
                    # Ranked by FantasyPros points_ppr for hard_chargers/
                    # east_coast, or by an sfflf-scoring estimate for
                    # f_league (see sports/football/waivers.py and
                    # _fp_sfflf_points_by_name()) when available; any
                    # league with no projections data falls back to
                    # ownership_pct ascending. Capped at 5 per slot.
                    "ranking_source": ranking_source_label if use_projections else "ownership_pct",
                    "by_slot": {
                        slot: [
                            {"player": wp.player.name, "team": wp.player.team,
                             "positions": wp.player.positions,
                             "ownership_pct": wp.ownership_pct,
                             "projected_points": projections.get(wp.player.name.strip().lower())}
                            for wp in wps[:5]
                        ]
                        for slot, wps in candidates_by_slot.items() if wps
                    },
                })

    rankings = _fp_nfl_rankings_by_name(_fp_client) if _fp_client else {}

    contract_data = None
    if internal_id == "east_coast":
        contract_data = _east_coast_contract_data(
            auth, league_id, league_config["cbs_team_id"], team.roster)

    keepers = keeper_guidance(
        team.roster, internal_id,
        rankings=rankings or None,
        ranking_source="fantasypros_ecr" if rankings else None,
        contract_data=contract_data,
        current_season=_FOOTBALL_CURRENT_SEASON if contract_data else None,
    )
    actions.append({
        "type":               "keeper_guidance",
        "is_keeper_league":   keepers.is_keeper_league,
        "max_keepers":        keepers.max_keepers,
        "decided_by":         keepers.decided_by,
        "selection_deadline": keepers.selection_deadline,
        "note":               keepers.note,
        "recommended_keeps":  keepers.recommended_keeps,
        "other_eligible":     keepers.other_eligible,
        "ranking_source":     keepers.ranking_source,
        "contract_expired":   keepers.contract_expired,
    })

    return {
        "league": league_name,
        "format": "H2H Points",
        "actions": actions,
    }
