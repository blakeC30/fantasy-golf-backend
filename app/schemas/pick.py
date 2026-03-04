"""Pick schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.golfer import GolferOut
from app.schemas.tournament import TournamentOut


class PickCreate(BaseModel):
    tournament_id: uuid.UUID
    golfer_id: uuid.UUID


class PickUpdate(BaseModel):
    """Change the golfer on an existing pick (before the pick locks)."""
    golfer_id: uuid.UUID


class PickOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    tournament_id: uuid.UUID
    golfer_id: uuid.UUID
    points_earned: float | None
    earnings_usd: float | None  # raw golfer earnings before multiplier
    submitted_at: datetime
    golfer: GolferOut
    tournament: TournamentOut

    model_config = ConfigDict(from_attributes=True)
