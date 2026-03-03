"""League and league membership schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.user import UserOut


class LeagueCreate(BaseModel):
    name: str
    description: str | None = None
    # Default matches the house rule; league admin can override on creation.
    no_pick_penalty: int = -50_000


class LeagueOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
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
    """Used by league admins to change a member's role."""
    role: str  # "admin" or "member"
