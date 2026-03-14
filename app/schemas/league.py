"""League and league membership schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.schemas.user import UserOut


class LeagueCreate(BaseModel):
    name: str
    # Default matches the house rule; league manager can override on creation.
    no_pick_penalty: int = -50_000

    @field_validator("no_pick_penalty")
    @classmethod
    def penalty_must_be_non_positive(cls, v: int) -> int:
        if v > 0:
            raise ValueError("no_pick_penalty must be 0 or negative")
        if v < -500_000:
            raise ValueError("no_pick_penalty cannot exceed -500,000")
        return v


class LeagueUpdate(BaseModel):
    """Partial update for league settings. Only provided fields are changed."""
    name: str | None = None
    no_pick_penalty: int | None = None

    @field_validator("no_pick_penalty")
    @classmethod
    def penalty_must_be_non_positive(cls, v: int | None) -> int | None:
        if v is not None and v > 0:
            raise ValueError("no_pick_penalty must be 0 or negative")
        if v is not None and v < -500_000:
            raise ValueError("no_pick_penalty cannot exceed -500,000")
        return v


class LeagueOut(BaseModel):
    id: uuid.UUID
    name: str
    no_pick_penalty: int
    invite_code: str
    is_public: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class LeagueMemberOut(BaseModel):
    """A league member with their user details nested."""
    user_id: uuid.UUID
    league_id: uuid.UUID
    role: str
    status: str
    joined_at: datetime
    user: UserOut

    model_config = ConfigDict(from_attributes=True)


class RoleUpdate(BaseModel):
    """Used by league managers to change a member's role."""
    role: str  # "manager" or "member"


class LeagueJoinPreview(BaseModel):
    """League info shown to a user before they confirm a join request."""
    league_id: uuid.UUID
    name: str
    member_count: int
    # None = no relationship, "pending" = waiting for approval, "approved" = already a member
    user_status: str | None


class LeagueRequestOut(BaseModel):
    """A pending join request from the requesting user's perspective."""
    league_id: uuid.UUID
    league_name: str
    requested_at: datetime
