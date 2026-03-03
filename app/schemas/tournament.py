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


class LeagueTournamentOut(TournamentOut):
    """TournamentOut extended with the league's effective multiplier.

    effective_multiplier resolves to the league's per-tournament override if set,
    falling back to the global tournament.multiplier. Frontend uses this value
    to display and pre-populate the multiplier picker in the manage page.
    """

    effective_multiplier: float
