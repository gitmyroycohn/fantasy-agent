"""
fantasypros/draft_board.py

Shared draft-board builder for all 3 CBS football leagues, re-scoring
FantasyPros' consensus market data through each league's OWN scoring rules
instead of trusting FantasyPros' generic points_ppr aggregate.

Why this exists as one module instead of three near-duplicate scripts:
hard_chargers and east_coast were originally built as separate one-off
analysis scripts (build_hc_draft_board.py, build_sfflf_draft_board.py) --
both fetched the same two FantasyPros endpoints and differed only in which
scoring function/profile they applied. This consolidates that shared
fetch+rank+CSV logic into one place, driven by config/leagues.yaml's
`scoring_profile` field, so a 4th league (or a scoring rule change) is a
config lookup, not a new script to maintain.

Two genuinely different re-scoring paths exist, both dispatched from here:
  - standard_ppr / standard_ppr_strict (hard_chargers, east_coast): CBS-style
    flat per-play rates. FantasyPros' raw stat fields are remapped to CBS's
    own field names (Recpt, RuYd, PaTD, etc.) and run through
    sports.football.scoring.score_player().
  - sfflf_tiered (f_league): tiered/position-dependent scoring that doesn't
    fit a flat-rate table at all. sports.football.scoring.estimate_sfflf_points()
    already consumes FantasyPros' own field names directly (pass_yds,
    rush_tds, ...) -- no remapping needed for this path.

KNOWN LIMITATIONS (carried forward from the original one-off scripts, not
fixed here since fixing them means better source data, not better code):
  - "FL" (fumbles lost) is mapped from FantasyPros' aggregate "fumbles"
    field, which may represent total fumbles rather than fumbles lost
    specifically -- not verified against FantasyPros' own docs.
  - Two-point conversions: FantasyPros' season projection only exposes one
    aggregate "2pt_tds" figure (no breakdown by pass/rush/rec), so the CBS
    remapping path assigns 100% of it to "Pa2P". This underweights Re2P/Ru2P
    for standard_ppr leagues (minor -- 2pt conversions are rare events) and
    doesn't affect the sfflf path (its two_point tier is uniform per type
    already, keyed as "2pt_tds" directly).
  - K/DST are never scored here -- FantasyPros' generic season projection
    doesn't carry the granular categories any of the 3 leagues' real K/DST
    formulas need (made-FG-by-distance list, sacks, INTs, points allowed
    tiers). Callers should treat K/DST as generic-consensus-only in any
    draft guide built from this data, same as before this module existed.
  - Rankings availability: FantasyPros doesn't publish a league-specific
    consensus board for any of these 3 formats, so ADP/ECR (a PPR-market
    consensus) is used as the sole AVAILABILITY signal for all 3 leagues --
    reasonable since other managers draft off generic market consensus
    regardless of any one league's own scoring rules. It's only the VALUE
    side (the re-scored points) that's made league-specific here.
"""
from __future__ import annotations

from sports.football.scoring import score_player, estimate_sfflf_points

# league_cfg["scoring_profile"] values that go through the CBS-style flat-rate
# path (score_player() with a remapped stats dict) vs. sfflf's own estimator.
_FLAT_RATE_PROFILES = {"standard_ppr", "standard_ppr_strict"}
_TIERED_PROFILES = {"sfflf_tiered"}

_SCORABLE_POSITIONS = ("QB", "RB", "WR", "TE")


def _norm(name: str) -> str:
    return (name or "").strip().lower()


def _fp_stats_to_cbs_fields(stats: dict) -> dict:
    """Remap FantasyPros' raw projection stat fields to CBS's own field
    names, for the standard_ppr / standard_ppr_strict score_player() path.

    See this module's docstring for the two documented limitations here
    (fumbles-lost assumption, 2pt-conversion type not broken out).
    """
    return {
        "Recpt": stats.get("rec_rec", 0) or 0,
        "RuYd":  stats.get("rush_yds", 0) or 0,
        "ReYd":  stats.get("rec_yds", 0) or 0,
        "PaYd":  stats.get("pass_yds", 0) or 0,
        "PaTD":  stats.get("pass_tds", 0) or 0,
        "RuTD":  stats.get("rush_tds", 0) or 0,
        "ReTD":  stats.get("rec_tds", 0) or 0,
        "PaInt": stats.get("pass_ints", 0) or 0,
        "FL":    stats.get("fumbles", 0) or 0,
        "Pa2P":  stats.get("2pt_tds", 0) or 0,
    }


def _score_one(position: str, stats: dict, scoring_profile: str) -> float | None:
    """Score one player's FantasyPros projection stats under the given
    league scoring_profile. Returns None for positions/profiles this
    module doesn't compute (K/DST, or an unrecognized profile)."""
    if position not in _SCORABLE_POSITIONS:
        return None
    if scoring_profile in _FLAT_RATE_PROFILES:
        mapped = _fp_stats_to_cbs_fields(stats)
        return score_player(mapped, position, scoring_profile)
    if scoring_profile in _TIERED_PROFILES:
        return estimate_sfflf_points(stats, position)
    return None


def build_draft_board(league_cfg: dict, fp_client, season: int = 2026,
                       exclude_players: set[str] | None = None) -> list[dict]:
    """Build one league's draft board: FantasyPros consensus ADP (realistic
    pick-availability order) cross-referenced with each player's season
    projection, re-scored under this league's own scoring_profile.

    Args:
        league_cfg: one league entry from config/leagues.yaml's `football`
                    list (must have a "scoring_profile" key).
        fp_client:  an authenticated FantasyProsClient.
        season:     NFL season year to project (default 2026).
        exclude_players: player names to drop entirely before ranking --
                    e.g. this league's predicted keepers (see
                    agent/football_decisions.py::predicted_keepers()). A
                    kept player doesn't re-enter the live draft pool, so
                    excluding them here (rather than just flagging them)
                    means league_rank/rank_delta for everyone else are
                    computed against the ACTUAL available pool, not a pool
                    that still counts players who can't be drafted.
                    Name matching is case/whitespace-insensitive (same
                    normalization as the FantasyPros<->projection join
                    below); unmatched names are silently ignored rather than
                    treated as an error, since a keeper name that doesn't
                    match FantasyPros' naming is a data-quality note, not a
                    reason to fail the whole board. Omit or pass None/empty
                    for no filtering (the previous, unfiltered behavior).

    Returns a list of row dicts, sorted by ecr_rank ascending:
        ecr_rank, name, position, team,
        league_points, league_rank, rank_delta
    league_points/league_rank/rank_delta are None for players this module
    doesn't score (K/DST, or no projection match) -- see module docstring.
    rank_delta = ecr_rank - league_rank (positive = this league's scoring
    values the player MORE than the generic market does).
    """
    scoring_profile = league_cfg.get("scoring_profile")
    if not scoring_profile:
        raise ValueError(
            f"League {league_cfg.get('id', '?')!r} has no scoring_profile "
            "set in config/leagues.yaml -- cannot build a scored draft board."
        )

    rankings = fp_client.nfl_consensus_rankings(position="ALL", season=season,
                                                 scoring="PPR", rank_type="ADP")
    if not rankings:
        rankings = fp_client.nfl_consensus_rankings(position="ALL", season=season,
                                                     scoring="PPR", rank_type="ROS")

    projections = fp_client.nfl_projections(position="ALL", season=season, scoring="PPR")
    proj_by_name = {_norm(p.get("name", "")): p for p in projections}

    exclude_norm = {_norm(n) for n in (exclude_players or ())}

    rows = []
    for r in rankings:
        name = r.get("player_name", "")
        if exclude_norm and _norm(name) in exclude_norm:
            continue
        pos_rank = r.get("pos_rank", "")
        position = "".join(ch for ch in pos_rank if ch.isalpha()) if pos_rank else ""
        ecr_rank = r.get("rank_ecr")
        team = r.get("player_team_id", "")

        proj = proj_by_name.get(_norm(name))
        league_points = None
        if proj:
            stats = proj.get("stats", {}) or {}
            pts = _score_one(position, stats, scoring_profile)
            if pts is not None:
                league_points = round(pts, 1)

        rows.append({
            "ecr_rank": ecr_rank,
            "name": name,
            "position": position,
            "team": team,
            "league_points": league_points,
        })

    scored = [r for r in rows if r["league_points"] is not None]
    scored.sort(key=lambda r: -r["league_points"])
    for i, r in enumerate(scored, 1):
        r["league_rank"] = i
        r["rank_delta"] = (r["ecr_rank"] - i) if r["ecr_rank"] else None

    unscored = [r for r in rows if r["league_points"] is None]
    for r in unscored:
        r["league_rank"] = None
        r["rank_delta"] = None

    return sorted(scored + unscored, key=lambda r: (r["ecr_rank"] is None, r["ecr_rank"]))


def write_draft_board_csv(rows: list[dict], out_path: str) -> None:
    import csv
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ecr_rank", "name", "position", "team",
                                          "league_points", "league_rank", "rank_delta"])
        w.writeheader()
        for r in rows:
            w.writerow(r)
