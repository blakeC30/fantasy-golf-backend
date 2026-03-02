"""
User model — a platform-level account.

A single user can belong to many leagues. Auth supports two methods:
  - Email/password: password_hash is set, google_id may be None.
  - Google OAuth:   google_id is set, password_hash may be None.
  - Both linked:    both fields are set (account linking).

At least one of password_hash or google_id must be non-null.
This constraint is enforced in the application layer (services/auth.py),
not at the database level, to keep the schema simple.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        # uuid4 generates a random UUID each time a new User is created in Python.
        default=uuid.uuid4,
    )
    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
    )
    # Nullable: Google-only accounts have no password.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Nullable: email/password accounts may not have a linked Google account.
    google_id: Mapped[str | None] = mapped_column(
        String(255),
        unique=True,
        nullable=True,
        index=True,
    )
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)

    # Platform admins can trigger scraper syncs and manage tournaments.
    # Regular users are not platform admins (they can be league admins, which is different).
    is_platform_admin: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
    )
    # server_default=func.now() means the DB sets this automatically on INSERT.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # --- Relationships ---
    # These let us write `user.picks` or `user.league_memberships` in Python
    # without writing extra queries.
    league_memberships: Mapped[list["LeagueMember"]] = relationship(back_populates="user")
    picks: Mapped[list["Pick"]] = relationship(back_populates="user")
    created_leagues: Mapped[list["League"]] = relationship(
        back_populates="created_by_user",
        foreign_keys="League.created_by",
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r}>"
