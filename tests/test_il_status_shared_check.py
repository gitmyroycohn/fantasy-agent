"""
P1 bug fix (2026-08-24): hitting_matchups had no IL check at all and could
recommend START for a player confirmed on the 10-day IL (Juan Soto, IL
since 7/25) that daily_decisions correctly flagged -- because the two
tools never shared this logic.

Fix: mlb.injuries.annotate_roster_injuries() is now the single shared IL
check both tools call (hitting_matchups directly in mcp_server.py;
daily_decisions via agent/decisions.py::_add_lineup_advice, which now
builds its il_norms set through this function instead of a bare
set(fetch_active_il().keys())). It also now cross-checks team (same risk
class as the Franklin Arias name-collision bug, 2026-08-13) before
trusting a name-only match against the IL list, since fetch_active_il() is
keyed by norm_name alone.
"""
from mlb.injuries import annotate_roster_injuries
from data.models import Player, RosterSlot


def _slot(name, team, status="A", slot="OF"):
    return RosterSlot(player=Player(id="1", name=name, position="OF",
                                     team=team, status=status), slot=slot)


def test_confirmed_il_player_is_flagged():
    roster = [_slot("Juan Soto", "NYM")]
    active_il = {
        "juansoto": {"name": "Juan Soto", "team": "NYM", "il_type": "10-Day IL",
                     "date": "2026-07-25"},
    }
    flagged = annotate_roster_injuries(roster, active_il)
    assert len(flagged) == 1
    assert flagged[0]["player_name"] == "Juan Soto"
    assert flagged[0]["il_type"] == "10-Day IL"


def test_healthy_player_not_flagged():
    roster = [_slot("Aaron Judge", "NYY")]
    active_il = {
        "juansoto": {"name": "Juan Soto", "team": "NYM", "il_type": "10-Day IL",
                     "date": "2026-07-25"},
    }
    assert annotate_roster_injuries(roster, active_il) == []


def test_name_collision_across_teams_is_not_flagged():
    # A different real player shares a normalized name with someone on the
    # IL list, but plays for a different team -- must NOT be flagged as
    # injured (same risk class as the Franklin Arias bug: name-only
    # matching across teams).
    roster = [_slot("Chris Young", "BOS")]
    active_il = {
        "chrisyoung": {"name": "Chris Young", "team": "KC", "il_type": "15-Day IL",
                       "date": "2026-08-01"},
    }
    assert annotate_roster_injuries(roster, active_il) == []


def test_team_alias_forms_still_match():
    # CBS's own pro_team field can return the short MLB-native abbreviation
    # ("SD") while fetch_active_il()'s team comes from a different MLB API
    # response shape -- canonical_team() must reconcile both.
    roster = [_slot("Fernando Tatis Jr.", "SD")]
    active_il = {
        "fernandotatisjr": {"name": "Fernando Tatis Jr.", "team": "SDP",
                            "il_type": "10-Day IL", "date": "2026-08-10"},
    }
    flagged = annotate_roster_injuries(roster, active_il)
    assert len(flagged) == 1
