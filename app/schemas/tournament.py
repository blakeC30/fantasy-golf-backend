"""Tournament schemas."""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class TournamentOut(BaseModel):
    id: uuid.UUID
    pga_tour_id: str
    name: str
    start_date: date
    end_date: date
    multiplier: float
    purse_usd: int | None
    status: str
    is_team_event: bool

    model_config = ConfigDict(from_attributes=True)


class LeagueTournamentOut(TournamentOut):
    """TournamentOut extended with the league's effective multiplier.

    effective_multiplier resolves to the league's per-tournament override if set,
    falling back to the global tournament.multiplier. Frontend uses this value
    to display and pre-populate the multiplier picker in the manage page.
    """

    effective_multiplier: float


# ---------------------------------------------------------------------------
# Leaderboard schemas
# ---------------------------------------------------------------------------

class RoundSummaryOut(BaseModel):
    round_number: int
    score: int | None
    score_to_par: int | None
    position: str | None
    tee_time: datetime | None

    model_config = ConfigDict(from_attributes=True)


class LeaderboardEntryOut(BaseModel):
    golfer_id: str
    golfer_name: str
    golfer_pga_tour_id: str
    golfer_country: str | None
    finish_position: int | None
    is_tied: bool
    made_cut: bool
    status: str | None
    earnings_usd: int | None
    total_score_to_par: int | None
    rounds: list[RoundSummaryOut]


class LeaderboardOut(BaseModel):
    tournament_id: str
    tournament_name: str
    tournament_status: str
    entries: list[LeaderboardEntryOut]


# ---------------------------------------------------------------------------
# Scorecard schemas
# ---------------------------------------------------------------------------

HoleResult = Literal["eagle", "birdie", "par", "bogey", "double_bogey", "triple_plus"]


class HoleScoreOut(BaseModel):
    hole: int
    par: int | None
    score: int | None
    score_to_par: int | None
    result: HoleResult | None


class ScorecardOut(BaseModel):
    golfer_id: str
    round_number: int
    holes: list[HoleScoreOut]
    total_score: int | None
    total_score_to_par: int | None
