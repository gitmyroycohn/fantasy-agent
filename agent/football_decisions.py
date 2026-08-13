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
    (ECR) and recommends the top 3 -- see _fp_nfl_rankings_by_name()'s
    docstring for why that adapter is defensive/best-effort (the exact API
    response shape has never been checked against a live call). Contract
    expiration is NOT applied here yet -- this module doesn't pass
    contract_data/current_season to keeper_guidance() because there's no
    real acquisition-date source wired up (see sports/football/keepers.py's
    docstring); once transaction-history tracking exists, this is where
    contract_data would be sourced and passed through.

What this does NOT do yet, on purpose:
  - No performance-based start/sit or waiver-add scoring (no lineup
    optimizer, no "best available" ranking beyond ownership%) -- there is
    no real football stat feed wired up yet (all 3 leagues are preseason,
    no roster has ever been loaded/drafted as of 2026-08-01). See
    sports/football/waivers.py's docstring for the reasoning.
  - No trade evaluation, no drop-candidate scoring, no matchup/score
    tracking -- none of these exist for football yet.

DRY_RUN applies here the same as baseball: this module only ever produces
a report, never submits a roster move.
"""

import logging

from sports.football.roster_rules import validate_roster
from sports.football.waivers import find_waiver_candidates_for_open_slots
from sports.football.keepers import keeper_guidance
from cbs.waivers import fetch_waiver_wire
from config.settings import FANTASYPROS_API_KEY
from fantasypros.client import FantasyProsClient

logger = logging.getLogger(__name__)

_fp_client = FantasyProsClient(FANTASYPROS_API_KEY) if FANTASYPROS_API_KEY else None


def _fp_nfl_rankings_by_name(client) -> dict[str, float]:
    """Best-effort adapter from FantasyProsClient.nfl_consensus_rankings()
    to a {normalized_name: rank} dict.

    CAVEAT: fantasypros/client.py's NFL methods were already implemented
    (nfl_projections/nfl_consensus_rankings/nfl_news) but appear unused
    anywhere in the codebase, and this build has no network access to call
    the live FantasyPros API even once -- so the exact response field
    names for /nfl/{season}/consensus-rankings are unverified (the MLB
    equivalent, players(), uses a "name" field per its docstring, so that's
    assumed here too, but the rank field name is a guess across a few
    plausible options). This function degrades to an empty dict (meaning
    "no ranking available", never a fabricated one) rather than raising or
    guessing wrong -- if a mismatch is happening silently, keeper_guidance()
    downstream will just report every player as unranked, which is a safe
    failure mode. Once this has been checked against one real API response,
    tighten this function to the confirmed field names and remove the
    multi-key fallback.
    """
    if client is None:
        return {}
    try:
        entries = client.nfl_consensus_rankings(rank_type="ROS")
    except Exception as e:
        logger.warning("FantasyPros NFL consensus rankings fetch failed: %s", e)
        return {}

    if entries:
        logger.info("FP nfl_consensus_rankings sample keys: %s",
                    list(entries[0].keys())[:20])

    rankings = {}
    for entry in entries:
        name = entry.get("name") or entry.get("player_name")
        rank = (entry.get("rank_ecr") or entry.get("rank")
                 or entry.get("ecr") or entry.get("avg_rank"))
        if name and rank is not None:
            try:
                rankings[name.strip().lower()] = float(rank)
            except (TypeError, ValueError):
                continue
    return rankings


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
            candidates_by_slot = find_waiver_candidates_for_open_slots(
                team.roster, waivers, internal_id)
            if any(candidates_by_slot.values()):
                actions.append({
                    "type": "waiver_targets",
                    # cap at 5 per slot for output -- these are sorted by
                    # ownership% ascending (see sports/football/waivers.py),
                    # not by any performance signal, so treat this as "who's
                    # available", not "who's best".
                    "by_slot": {
                        slot: [
                            {"player": wp.player.name, "team": wp.player.team,
                             "positions": wp.player.positions,
                             "ownership_pct": wp.ownership_pct}
                            for wp in wps[:5]
                        ]
                        for slot, wps in candidates_by_slot.items() if wps
                    },
                })

    rankings = _fp_nfl_rankings_by_name(_fp_client) if _fp_client else {}
    keepers = keeper_guidance(
        team.roster, internal_id,
        rankings=rankings or None,
        ranking_source="fantasypros_ecr" if rankings else None,
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
