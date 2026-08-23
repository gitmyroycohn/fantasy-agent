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
  - Waiver-target ranking for hard_chargers/east_coast (both real per-play
    PPR leagues): when FANTASYPROS_API_KEY is configured, ranks open-slot
    waiver candidates by FantasyPros' points_ppr season-long projection
    (see _fp_nfl_projections_by_name() and
    sports/football/waivers.py::_PPR_PROJECTION_LEAGUES) instead of
    ownership_pct alone. Field mapping (name / stats.points_ppr) was also
    verified via fp_probe.py on 2026-08-23. sfflf is intentionally excluded
    -- its non-PPR tiered/position-diff scoring isn't what points_ppr
    represents, so it always stays on ownership_pct-only sorting.

What this does NOT do yet, on purpose:
  - No performance-based start/sit or lineup-optimizer logic, and no
    performance-based waiver ranking at all for sfflf (its scoring format
    has no FantasyPros projection equivalent -- see
    sports/football/waivers.py's docstring).
  - No trade evaluation, no drop-candidate scoring, no matchup/score
    tracking -- none of these exist for football yet.

DRY_RUN applies here the same as baseball: this module only ever produces
a report, never submits a roster move.
"""

import logging

from sports.football.roster_rules import validate_roster
from sports.football.waivers import (
    find_waiver_candidates_for_open_slots,
    _PPR_PROJECTION_LEAGUES,
)
from sports.football.keepers import keeper_guidance, contract_years_to_acquired_seasons
from cbs.waivers import fetch_waiver_wire
from cbs.roster import fetch_contract_years
from config.settings import FANTASYPROS_API_KEY
from fantasypros.client import FantasyProsClient

logger = logging.getLogger(__name__)

_fp_client = FantasyProsClient(FANTASYPROS_API_KEY) if FANTASYPROS_API_KEY else None

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
    fallback keys. Note: type="WW" (waiver wire) rankings come back empty
    for Christopher's API key -- that ranking type is gated behind
    FantasyPros' premium tier (confirmed via the response's own
    "tier":"premium" flag) -- but type="ROS", used here, is not affected.
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
            projections = _fp_nfl_projections_by_name(_fp_client) if _fp_client else {}
            use_projections = bool(projections) and internal_id in _PPR_PROJECTION_LEAGUES
            candidates_by_slot = find_waiver_candidates_for_open_slots(
                team.roster, waivers, internal_id, projections=projections or None)
            if any(candidates_by_slot.values()):
                actions.append({
                    "type": "waiver_targets",
                    # Ranked by FantasyPros points_ppr projection for
                    # hard_chargers/east_coast when available (see
                    # sports/football/waivers.py); sfflf (non-PPR scoring)
                    # and any league with no projections data still falls
                    # back to ownership_pct ascending. Capped at 5 per slot.
                    "ranking_source": "fantasypros_projected_ppr" if use_projections else "ownership_pct",
                    "by_slot": {
                        slot: [
                            {"player": wp.player.name, "team": wp.player.team,
                             "positions": wp.player.positions,
                             "ownership_pct": wp.ownership_pct,
                             "projected_ppr": projections.get(wp.player.name.strip().lower())}
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
