"""Tournament schemas."""

import uuid
from datetime import date

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
