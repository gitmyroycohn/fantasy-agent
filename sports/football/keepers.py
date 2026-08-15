"""
Keeper guidance for Christopher's football leagues.

Real keeper policy per league (verified 2026-08-01, f_league's cap
confirmed directly by Christopher on 2026-08-01 after checking with that
league -- CBS's own public /rules page doesn't publish it):

  - f_league (sfflf): IS a keeper league (its /rules page lists a "Keeper
    Policy" under PLAYER POLICIES AND KEEPERS). Christopher is NOT the
    commissioner there (his /setup redirects to the plain team dashboard,
    no Commissioner Tools), so this cap isn't visible via CBS's UI to him --
    it's confirmed by Christopher directly instead: max 2 keepers per team,
    any position, no cost mechanic. Selection is commissioner-administered
    per the /rules page ("Commissioner is responsible for setting all
    teams' Keepers"), unlike east_coast where individual managers pick.

  - hard_chargers (hcfl05): NOT a keeper league. Its /rules page has no
    Keeper Policy section at all, and its League dropdown nav has no
    "Keepers" item (f_league and east_coast both do).

  - east_coast (ecfc): IS a keeper league, and Christopher IS the
    commissioner (confirmed via /setup, "Welcome Back For The 2026 Fantasy
    Season, Commissioner Chris!"). Read directly off the live Keeper
    Policies settings page (/setup/league-settings/draft-management/keepers)
    on 2026-08-01:
      - Max keepers per team: 3, any position (no position-eligibility
        restriction on which of your players can be kept)
      - Selection: Individual Team Managers pick their own (not commissioner-set)
      - Selection deadline: currently set to 8/31/2025 -- STALE (a red ALERT
        banner on that page says so) -- needs the commissioner to update
        the year to 2026 before keepers can run correctly this season.
      - No cost mechanic of any kind (no round penalty, no salary/contract
        system) -- it's purely "pick your best 3", unlike keeper leagues
        that make you pay an escalating draft-round cost to keep a player
        multiple years.
    As of 2026-08-01, 9 of ecfc's 10 teams (including Hotlanta Hussies) show
    "Not Yet Selected"; only "Pain Inc." has picked (3 selected).

Ranking WHICH players to keep requires some notion of player value.
fantasypros/client.py already has nfl_consensus_rankings() (ECR rankings)
implemented and unused -- this module can rank a roster by those rankings
when available, but the exact response shape from FantasyPros' NFL
endpoint has never been hit against the live API in this build (no network
access here, and no verified sample response), so the adapter that calls it
(agent/football_decisions.py) is written defensively and degrades to "no
ranking available" rather than guessing at field names. Treat any FP-based
ranking output as unverified until it's been checked against a real API
response once FANTASYPROS_API_KEY + live network access are both available.

east_coast (ecfc) ONLY -- 3-year keeper contract rule (Christopher-authored
house rule, confirmed 2026-08-13, does NOT apply to f_league or
hard_chargers):
  - Every player carries a 3 fantasy-season contract. The contract starts
    the season he's acquired (draft pick, waiver add, or incoming trade).
  - The contract stays in effect as long as the player remains on the
    fantasy roster -- no re-signing needed each year.
  - If the player is traded within the fantasy league, his contract
    follows him to the new team unchanged (acquisition season doesn't
    reset). Same if he's traded in real life (NFL team change) -- the
    fantasy contract still follows the player, not the roster spot.
  - 1 contract year = 1 fantasy league season. After season 3 (i.e.
    entering a would-be season 4 on the same contract), the contract is
    expired. A player with an expired contract cannot be kept -- he enters
    the draft pool for the next season's draft instead.

  CBS itself has a generic "Salary/Contracts" commissioner feature on
  ecfc's live roster page (a "CONTRACT" column + "Edit Salary & Contracts"
  tool, confirmed present via Commissioner Tools > Salaries/Contracts:
  Salary and Contract Info / Autofill Salaries/Contracts / Clear Free
  Agent Salary and Contract Info). That column's per-player value could
  NOT be reliably read as plain data via automated page-read -- it renders
  as an edit-only widget, not extractable text, so this module does not
  scrape CBS for per-player contract years. It also wouldn't necessarily
  match Christopher's specific rule above anyway (CBS's feature is a
  generic configurable number, not an enforced "3 years, trade-follows"
  engine). Instead, this module expects the caller to supply
  `contract_data` -- a {player_name: season_acquired} map -- from whatever
  actually tracks acquisitions (e.g. league transaction history, once
  that's built). When it's not supplied, keeper guidance falls back to
  treating the whole roster as contract-eligible and flags that contract
  data is missing, the same fallback pattern used above for rankings: no
  ranking signal -> no guessed picks; no contract data -> no guessed
  expirations.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# east_coast only -- see module docstring for the full house rule.
CONTRACT_YEARS = 3
_CONTRACT_LEAGUES = {"east_coast"}


KEEPER_POLICIES: dict[str, dict] = {
    "f_league": {
        "is_keeper_league": True,
        "max_keepers": 2,   # any position, confirmed by Christopher 2026-08-01
        "decided_by": "commissioner",
        "cost_mechanic": "none",  # flat count cap, no round/salary cost
        "selection_deadline": None,  # not published/visible to Christopher (not commissioner here)
        "note": ("Max 2 keepers, any position, no cost mechanic -- confirmed "
                 "by Christopher directly (CBS's public rules page doesn't "
                 "publish this; he isn't f_league's commissioner). Selection "
                 "deadline isn't visible to him -- ask the commissioner if "
                 "that date matters for planning."),
    },
    "hard_chargers": {
        "is_keeper_league": False,
        "max_keepers": 0,
        "decided_by": None,
        "cost_mechanic": None,
        "selection_deadline": None,
        "note": "Not a keeper league -- straight redraft every year.",
    },
    "east_coast": {
        "is_keeper_league": True,
        "max_keepers": 3,
        "decided_by": "individual_manager",
        "cost_mechanic": "contract",  # 3-year contract, see module docstring
        "contract_years": CONTRACT_YEARS,
        "selection_deadline": "2025-08-31",  # STALE, needs commissioner update to 2026
        "note": ("Confirmed live via commissioner settings 2026-08-01. "
                 "Deadline is still last season's date -- flag to the "
                 "commissioner (Christopher) to update it before keepers "
                 "can be processed correctly this season. Cost mechanic: "
                 "every player carries a 3-fantasy-season contract starting "
                 "on acquisition; it survives fantasy-league and real-life "
                 "trades alike (follows the player, not the roster spot); "
                 "after season 3 the contract expires and that player "
                 "cannot be kept -- he re-enters next season's draft pool "
                 "(house rule confirmed by Christopher 2026-08-13)."),
    },
}


@dataclass
class ContractStatus:
    player_name: str
    acquired_season: int
    current_season: int
    years_elapsed: int          # current_season - acquired_season + 1
    expires_after_season: int   # acquired_season + CONTRACT_YEARS - 1
    is_expired: bool            # years_elapsed > CONTRACT_YEARS


def contract_status(player_name: str, acquired_season: int,
                     current_season: int) -> ContractStatus:
    """east_coast-only 3-year contract math. acquired_season/current_season
    are fantasy-league season years (e.g. 2026), not calendar dates -- a
    player acquired in season Y is on-contract for seasons Y, Y+1, Y+2 and
    expired starting Y+3. Trades (fantasy or real-life) don't change
    acquired_season -- the caller is responsible for carrying the original
    acquisition season forward when a player changes hands, per the house
    rule in this module's docstring."""
    years_elapsed = current_season - acquired_season + 1
    expires_after_season = acquired_season + CONTRACT_YEARS - 1
    return ContractStatus(
        player_name=player_name, acquired_season=acquired_season,
        current_season=current_season, years_elapsed=years_elapsed,
        expires_after_season=expires_after_season,
        is_expired=years_elapsed > CONTRACT_YEARS,
    )


@dataclass
class KeeperGuidance:
    league_id: str
    is_keeper_league: bool
    max_keepers: int | None
    decided_by: str | None
    selection_deadline: str | None
    note: str
    # Only populated when the league has a known max_keepers AND a
    # value/ranking signal was supplied -- otherwise empty, not guessed.
    recommended_keeps: list = field(default_factory=list)
    other_eligible: list = field(default_factory=list)
    ranking_source: str | None = None  # e.g. "fantasypros_ecr", or None
    # east_coast only -- players whose 3-year contract has expired and who
    # are therefore NOT keeper-eligible at all (re-enter the draft pool).
    # Empty for every other league, and empty here too whenever no
    # contract_data was supplied (see keeper_guidance()'s docstring).
    contract_expired: list = field(default_factory=list)


def keeper_guidance(roster: list, league_id: str,
                    rankings: dict[str, float] | None = None,
                    ranking_source: str | None = None,
                    contract_data: dict[str, int] | None = None,
                    current_season: int | None = None) -> KeeperGuidance:
    """Build keeper guidance for one team.

    roster: list of data.models.RosterSlot (only rs.player.name/.positions
    are used -- keeper eligibility here doesn't depend on starting/bench
    status, since kept players carry over to next season regardless of
    this year's lineup slot).
    rankings: optional {normalized_player_name: rank} map, lower = better.
              Pass None if no external ranking signal is available -- this
              function will NOT fabricate one. See module docstring re:
              FantasyPros NFL rankings not being schema-verified yet.
    contract_data: east_coast ONLY. Optional {player_name: season_acquired}
              map -- see module docstring for the full 3-year-contract house
              rule and why this isn't scraped from CBS automatically. Must
              be passed together with current_season, or it's ignored (both
              needed to compute expiration). Ignored entirely for leagues
              outside _CONTRACT_LEAGUES (only east_coast today).
    current_season: the fantasy season being evaluated (e.g. 2026),
              required alongside contract_data to compute expiration.
    """
    if league_id not in KEEPER_POLICIES:
        raise ValueError(f"Unknown football league_id: {league_id!r}")

    policy = KEEPER_POLICIES[league_id]

    if not policy["is_keeper_league"]:
        return KeeperGuidance(
            league_id=league_id, is_keeper_league=False, max_keepers=0,
            decided_by=None, selection_deadline=None, note=policy["note"],
        )

    max_keepers = policy["max_keepers"]

    if max_keepers is None:
        # f_league case: policy exists but the count/cost isn't known to us.
        return KeeperGuidance(
            league_id=league_id, is_keeper_league=True, max_keepers=None,
            decided_by=policy["decided_by"],
            selection_deadline=policy["selection_deadline"],
            note=policy["note"],
        )

    players = [rs.player for rs in roster]
    note = policy["note"]
    expired_names: list[str] = []

    has_contract_rule = league_id in _CONTRACT_LEAGUES
    apply_contracts = (has_contract_rule and contract_data is not None
                        and current_season is not None)

    if has_contract_rule:
        if apply_contracts:
            eligible_players = []
            for p in players:
                acquired = contract_data.get(p.name)
                if acquired is None:
                    # No acquisition data for this specific player -- can't
                    # judge their contract, so don't guess an expiration;
                    # keep them in the eligible pool.
                    eligible_players.append(p)
                    continue
                status = contract_status(p.name, acquired, current_season)
                if status.is_expired:
                    expired_names.append(p.name)
                else:
                    eligible_players.append(p)
            players = eligible_players
            note = (note + f" Contract check applied for season "
                    f"{current_season}: {len(expired_names)} player(s) have "
                    f"an expired 3-year contract and are excluded below "
                    f"(re-enter the draft pool).") if expired_names else (
                    note + f" Contract check applied for season "
                    f"{current_season}: no expired contracts found.")
        else:
            note = (note + " No contract data supplied -- cannot determine "
                    "which players' 3-year contracts have expired, so the "
                    "full roster below is shown as contract-eligible pending "
                    "real acquisition-date tracking.")

    if not rankings:
        # We know the cap, but have no value signal to rank against --
        # report the full eligible pool and let Christopher decide, rather
        # than picking an arbitrary "top N" (e.g. roster order, which
        # carries no actual value information).
        return KeeperGuidance(
            league_id=league_id, is_keeper_league=True, max_keepers=max_keepers,
            decided_by=policy["decided_by"],
            selection_deadline=policy["selection_deadline"],
            note=(note + " No ranking signal supplied -- showing "
                  "the full roster as keeper-eligible; add FANTASYPROS_API_KEY "
                  "for ranked suggestions."),
            other_eligible=[p.name for p in players],
            contract_expired=expired_names,
        )

    def _rank_of(p):
        return rankings.get(_norm(p.name), float("inf"))

    ranked = sorted(players, key=_rank_of)
    ranked_known = [p for p in ranked if _rank_of(p) != float("inf")]
    unranked = [p for p in ranked if _rank_of(p) == float("inf")]

    keep = ranked_known[:max_keepers]
    rest = ranked_known[max_keepers:] + unranked

    return KeeperGuidance(
        league_id=league_id, is_keeper_league=True, max_keepers=max_keepers,
        decided_by=policy["decided_by"],
        selection_deadline=policy["selection_deadline"],
        note=note,
        recommended_keeps=[p.name for p in keep],
        other_eligible=[p.name for p in rest],
        ranking_source=ranking_source,
        contract_expired=expired_names,
    )


def _norm(name: str) -> str:
    return name.strip().lower()
