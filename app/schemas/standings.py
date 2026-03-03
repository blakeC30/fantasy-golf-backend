"""Standings schemas."""

import uuid

from pydantic import BaseModel


class StandingsRow(BaseModel):
    rank: int
    is_tied: bool  # True when two or more players share this rank (e.g. T2)
    user_id: uuid.UUID
    display_name: str
    total_points: float
    pick_count: int
    missed_count: int


class StandingsResponse(BaseModel):
    league_id: uuid.UUID
    season_year: int
    rows: list[StandingsRow]
