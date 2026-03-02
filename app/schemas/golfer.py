"""Golfer schemas."""

import uuid

from pydantic import BaseModel, ConfigDict


class GolferOut(BaseModel):
    id: uuid.UUID
    pga_tour_id: str
    name: str
    world_ranking: int | None
    country: str | None

    model_config = ConfigDict(from_attributes=True)
