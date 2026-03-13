"""Tournament schemas."""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class GolferInFieldOut(BaseModel):
    """Golfer entry returned by GET /tournaments/{id}/field.

    Extends the base golfer fields with ``tee_time`` from the
    TournamentEntry row so the frontend can identify which golfers
    have already teed off when the tournament is in_progress.
    ``tee_time`` is None when tee times have not yet been assigned.
    """

    id: uuid.UUID
    pga_tour_id: str
    name: str
    world_ranking: int | None
    country: str | None
    tee_time: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


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

    all_r1_teed_off is True when every Round 1 tee time for an in-progress
    tournament has passed. The frontend uses this to hide the pick button when
    a member has no pick and the late-entry window has closed. Defaults to
    False (safe default — never hide the button unless we know all teed off).
    """

    effective_multiplier: float
    all_r1_teed_off: bool = False


# ---------------------------------------------------------------------------
# Leaderboard schemas
# ---------------------------------------------------------------------------

class RoundSummaryOut(BaseModel):
    round_number: int
    score: int | None
    score_to_par: int | None
    position: str | None
    tee_time: datetime | None
    is_playoff: bool = False
    thru: int | None = None
    started_on_back: bool | None = None

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
    # Team event fields — None for individual tournaments
    partner_name: str | None = None
    partner_golfer_id: str | None = None
    partner_golfer_pga_tour_id: str | None = None


class LeaderboardOut(BaseModel):
    tournament_id: str
    tournament_name: str
    tournament_status: str
    is_team_event: bool
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
