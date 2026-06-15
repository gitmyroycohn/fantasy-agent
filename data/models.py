from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Player:
    id: str
    name: str
    position: str          # primary position string e.g. "SP", "OF"
    team: str = ""
    status: str = "A"      # A=Active, DL=IL, etc.
    stats: dict = field(default_factory=dict)   # live/projected stats keyed by stat name

    @property
    def positions(self) -> list[str]:
        """Multi-position eligibility split on '/'."""
        return [p.strip() for p in self.position.split("/") if p.strip()]


@dataclass
class RosterSlot:
    player: Player
    slot: str
    is_starting: bool = False


@dataclass
class Team:
    id: str
    name: str
    owner: str = ""
    roster: list = field(default_factory=list)

    def players(self) -> list[Player]:
        return [rs.player for rs in self.roster]


@dataclass
class CategoryStanding:
    category: str
    my_value: float
    opp_value: float    # H2H: opponent's value; Roto: 0.0 (unused)
    winning: bool
    gap: float = 0.0
    rank: int = 0       # Roto: current rank (1=best); 0 if H2H
    rotopts: int = 0    # Roto: current roto points; 0 if H2H
    dif: int = 0        # Roto: rank change since last period


@dataclass
class Matchup:
    week: int
    opponent_name: str = ""
    opponent_id: str = ""
    category_standings: list = field(default_factory=list)
    cats_winning: int = 0
    cats_losing: int = 0
    cats_tied: int = 0


@dataclass
class WaiverPlayer:
    """A player available on the waiver wire / free agent pool."""
    player: Player
    add_rank: int = 0           # lower = higher priority
    ownership_pct: float = 0.0
    on_waivers: bool = False    # True = waiver claim required; False = free add
