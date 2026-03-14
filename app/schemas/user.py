"""User schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class UserOut(BaseModel):
    """
    Safe public representation of a user.

    password_hash and google_id are intentionally excluded — never expose them.
    """
    id: uuid.UUID
    email: str
    display_name: str
    is_platform_admin: bool
    created_at: datetime

    # from_attributes=True tells Pydantic to read data from ORM object
    # attributes (e.g. user.email) instead of dict keys. Required when
    # returning SQLAlchemy model instances from FastAPI routes.
    model_config = ConfigDict(from_attributes=True)


class UserUpdate(BaseModel):
    """Fields the user is allowed to change about themselves."""
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
