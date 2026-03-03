"""
League and LeagueMember models.

A League is an independent fantasy golf group. Multiple leagues can exist on
the platform simultaneously — each has its own members, seasons, and standings.

LeagueMember is the join table between User and League. It also stores the
user's role and membership status:
  - role:   "admin" | "member"
  - status: "pending" (awaiting admin approval) | "approved" (active member)

Joining a private league creates a "pending" membership. The admin then
approves or denies via the /leagues/{league_id}/requests endpoints. Public leagues
(is_public=True) auto-approve on join — but all leagues are currently created
as private; the is_public field exists for future use.

Invite flow: each league has a unique invite_code (random 22-char URL-safe
string). Admins share this code as a join link: /join/{invite_code}. The code
doesn't change unless the admin explicitly regenerates it.
"""

import enum
import secrets
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    # Only imported during type-checking (mypy/pyright), not at runtime.
    # This avoids circular imports while keeping the type checker happy.
    from app.models.league_tournament import LeagueTournament
    from app.models.pick import Pick
    from app.models.season import Season
    from app.models.user import User


class LeagueMemberRole(str, enum.Enum):
    """
    A user's role within a specific league.
    Inheriting from str makes the enum JSON-serializable and lets SQLAlchemy
    store it as a plain string in the database.
    """
    ADMIN = "admin"    # Can manage members, settings, and trigger syncs
    MEMBER = "member"  # Can view and submit picks


class LeagueMemberStatus(str, enum.Enum):
    """
    Membership lifecycle state.

    PENDING  — user has submitted a join request; awaiting admin action.
    APPROVED — admin accepted the request; user is a full active member.

    Denied requests are simply deleted (not stored with a "denied" status)
    to keep the table small and not confuse future join attempts.
    """
    PENDING = "pending"
    APPROVED = "approved"


class League(Base):
    __tablename__ = "leagues"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Unique, unguessable token used in the invite URL (/join/{invite_code}).
    # Generated once at creation; admins share this URL with prospective members.
    # Stored as a 22-character URL-safe base64 string (128 bits of entropy).
    invite_code: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        nullable=False,
        index=True,
        default=lambda: secrets.token_urlsafe(16),
        # server_default covers rows added directly to the DB (e.g. migrations).
        # gen_random_uuid()::text isn't pretty but it's unguessable and unique.
        server_default=text("gen_random_uuid()::text"),
    )

    # When True, join requests are auto-approved; no admin action required.
    # When False (default), every join request must be approved by an admin.
    # The UI always creates private leagues for now; this field is here for
    # future public-league functionality.
    is_public: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

    # The user who created the league. They are automatically made an admin.
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Points applied to a user's season total when they miss a week (no pick
    # submitted before the tournament starts). Negative by convention.
    # Stored as an integer because earnings are in whole dollars.
    # Default matches the league's house rule: -50,000.
    no_pick_penalty: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=-50_000,
        server_default="-50000",
    )

    # --- Relationships ---
    created_by_user: Mapped["User"] = relationship(
        back_populates="created_leagues",
        foreign_keys=[created_by],
    )
    members: Mapped[list["LeagueMember"]] = relationship(back_populates="league")
    seasons: Mapped[list["Season"]] = relationship(back_populates="league")
    picks: Mapped[list["Pick"]] = relationship(back_populates="league")
    league_tournaments: Mapped[list["LeagueTournament"]] = relationship(back_populates="league")

    def __repr__(self) -> str:
        return f"<League id={self.id} name={self.name!r}>"


class LeagueMember(Base):
    __tablename__ = "league_members"
    __table_args__ = (
        # A user can only appear once per league (covers both pending and approved).
        UniqueConstraint("league_id", "user_id", name="uq_league_member"),
    )

    # Integer primary key is fine here — this is an internal join table.
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    league_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leagues.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)

    # Stored as a plain string ("admin" or "member") using the enum's value.
    role: Mapped[str] = mapped_column(
        String(20),
        default=LeagueMemberRole.MEMBER.value,
        server_default=LeagueMemberRole.MEMBER.value,
        nullable=False,
    )

    # "pending" until an admin approves the join request; then "approved".
    # server_default="approved" so existing rows (admin-created) stay valid.
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=LeagueMemberStatus.APPROVED.value,
        server_default=LeagueMemberStatus.APPROVED.value,
    )

    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # --- Relationships ---
    league: Mapped["League"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="league_memberships")

    def __repr__(self) -> str:
        return f"<LeagueMember league={self.league_id} user={self.user_id} role={self.role!r} status={self.status!r}>"
