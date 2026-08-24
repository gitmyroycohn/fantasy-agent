"""
P1 bug fix (2026-08-24): waiver_recommendations surfaced a real Houston
relief pitcher named "Julio Rodriguez" with a Seattle batting star's
Statcast line (xwOBA, Barrel%) stapled onto his entry -- two different
real players sharing one normalized name, silently conflated. Same risk
class as the mlb/stats.py name-collision fix (2026-08-15), but in a
module that fix never touched: savant/client.py's leaderboard loaders,
which feed the free-agent waiver-pool enrichment path
(agent/decisions.py::get_filtered_waiver_adds -> _sav_enrich ->
enrich_with_savant).

Every loader used to do `result[key] = {...}` unconditionally per CSV row
-- last row silently wins on a name collision. Fix: _collision_safe_index()
tracks each name's distinct Savant player_id(s); a name backed by more
than one id (or any row with a missing/blank id, which can't prove two
rows are the same person) is dropped from the lookup entirely, so
batter_data()/pitcher_data() fall through to {} for that name instead of
returning the wrong player's stats.
"""
import savant.client as sav_mod
from savant.client import SavantClient


def _bat_xstats_row(name, player_id, ba="0.280"):
    return {
        "last_name, first_name": name,
        "player_id": player_id,
        "pa": "300",
        "ba": ba,
        "est_ba": "0.275",
        "est_ba_minus_ba_diff": "0.005",
        "woba": "0.350",
        "est_woba": "0.345",
        "est_woba_minus_woba_diff": "0.005",
    }


def _bat_ev_row(name, player_id, brl="12.0"):
    return {
        "last_name, first_name": name,
        "player_id": player_id,
        "avg_hit_speed": "90.0",
        "brl_percent": brl,
        "ev95percent": "40.0",
        "anglesweetspotpercent": "35.0",
    }


def test_two_different_players_same_name_excluded_from_batter_lookup(monkeypatch):
    # "Rodriguez, Julio" the Seattle batter (id 1) vs a namesake with a
    # DIFFERENT Savant player_id who happens to normalize identically.
    rows = [
        _bat_xstats_row("Rodriguez, Julio", "660271", ba="0.290"),
        _bat_xstats_row("Rodriguez, Julio", "999999", ba="0.240"),
    ]
    monkeypatch.setattr(sav_mod, "_fetch_csv", lambda url: rows)

    client = SavantClient(season=2026)
    data = client.batter_data("Julio Rodriguez")
    assert data == {}   # ambiguous -- must NOT silently return either player's line


def test_unique_batter_name_still_resolves(monkeypatch):
    rows = [_bat_xstats_row("Judge, Aaron", "592450", ba="0.310")]
    monkeypatch.setattr(sav_mod, "_fetch_csv", lambda url: rows)

    client = SavantClient(season=2026)
    data = client.batter_data("Aaron Judge")
    assert data["sv_ba"] == 0.310


def test_ev_barrel_lookup_also_excludes_collision(monkeypatch):
    rows = [
        _bat_ev_row("Rodriguez, Julio", "660271", brl="15.0"),
        _bat_ev_row("Rodriguez, Julio", "999999", brl="4.0"),
    ]
    monkeypatch.setattr(sav_mod, "_fetch_csv", lambda url: rows)

    client = SavantClient(season=2026)
    data = client.batter_data("Julio Rodriguez")
    # batter_data() merges xstats + EV; with no fake xstats rows queued
    # here, only the EV load matters -- it must still come back empty for
    # the collided name rather than leaking either barrel% value.
    assert "sv_barrel_pct" not in data


def test_pitcher_gets_pitcher_data_never_batter_fields(monkeypatch):
    # The regression as reported: a WaiverPlayer tagged RP must never end
    # up with batter-only Statcast keys (sv_barrel_pct / sv_xwoba) in its
    # enriched stats -- pitcher_data() only ever touches the pitcher xstats
    # loader, which doesn't parse those columns at all.
    from data.models import Player, WaiverPlayer
    from savant.client import enrich_with_savant

    pit_rows = []  # no pitcher xstats match -- realistic for an obscure arm
    monkeypatch.setattr(sav_mod, "_fetch_csv", lambda url: pit_rows)

    wp = WaiverPlayer(
        player=Player(id="1", name="Julio Rodriguez", position="RP", team="HOU"),
        add_rank=0,
    )
    client = SavantClient(season=2026)
    enrich_with_savant([wp], client)

    stats = wp.player.stats or {}
    assert "sv_barrel_pct" not in stats
    assert "sv_xwoba" not in stats


def test_missing_player_id_treated_as_ambiguous(monkeypatch):
    # If the CSV ever lacks a usable player_id, two rows sharing a name
    # can't be proven to be the same person -- fail safe (exclude) rather
    # than assume they're one player.
    rows = [
        _bat_xstats_row("Doe, Jane", "", ba="0.300"),
        _bat_xstats_row("Doe, Jane", "", ba="0.200"),
    ]
    monkeypatch.setattr(sav_mod, "_fetch_csv", lambda url: rows)

    client = SavantClient(season=2026)
    assert client.batter_data("Jane Doe") == {}
