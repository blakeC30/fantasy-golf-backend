"""
Tournament and TournamentEntry models.

A Tournament represents a single PGA Tour event in a given week. Tournaments
are populated by the scraper and cover the full season schedule.

TournamentEntry is the join table between a Tournament and the Golfers who
played in it. After the tournament ends, each entry records that golfer's
finish position and earnings — this is the raw data our scoring service uses.

Key design note: `multiplier` replaces a simple `is_major` boolean.
  - Standard tournament: multiplier = 1.0  → points = earnings × 1.0
  - Major tournament:    multiplier = 2.0  → points = earnings × 2.0
  - Future flexibility:  any float value works (e.g. 1.5 for a special event)

Pick-lock rules (enforced in the API layer, schema supports them here):
  - A pick can be CHANGED until the picked golfer's `tee_time` has passed.
  - If `tee_time` is null but the tournament is `in_progress`, the pick is
    also considered locked (belt-and-suspenders safety for missing tee times).
  - New picks follow the original rule: must be submitted before start_date.

TournamentStatus tracks the lifecycle of a tournament so the scraper and
frontend know what to do with each event.
"""

import enum
import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.golfer import Golfer
    from app.models.pick import Pick


class TournamentStatus(str, enum.Enum):
    """
    Lifecycle of a PGA Tour event.

    Using `str` as a base makes the enum JSON-serializable and lets us
    store values as plain strings in the database (avoids PostgreSQL ENUM
    type, which requires a migration to add new values).
    """

    SCHEDULED = "scheduled"      # Future event; field not yet announced
    IN_PROGRESS = "in_progress"  # Currently being played
    COMPLETED = "completed"      # Final results are official


class Tournament(Base):
    __tablename__ = "tournaments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Stable external identifier from the PGA Tour / ESPN API. Used for upserts.
    pga_tour_id: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)

    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)

    # Scoring multiplier. Default 1.0 (standard event). Set to 2.0 for majors.
    # Using Float instead of Numeric here because rounding to the cent is not
    # important for a multiplier — it's always a simple value like 1.0 or 2.0.
    multiplier: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=1.0,
        server_default="1.0",
    )

    # Total prize pool in USD. Informational — not used in scoring.
    purse_usd: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # "scheduled" | "in_progress" | "completed" stored as a string.
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=TournamentStatus.SCHEDULED.value,
        server_default=TournamentStatus.SCHEDULED.value,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # --- Relationships ---
    entries: Mapped[list["TournamentEntry"]] = relationship(
        back_populates="tournament"
    )
    picks: Mapped[list["Pick"]] = relationship(back_populates="tournament")

    def __repr__(self) -> str:
        return f"<Tournament name={self.name!r} status={self.status}>"


class TournamentEntry(Base):
    """
    One golfer's participation record in one tournament.

    Created when the field is announced (finish_position/earnings/tee_time
    are null at that point). Updated by the scraper as tee times are released
    and again after the tournament ends with official results.
    """

    __tablename__ = "tournament_entries"
    __table_args__ = (
        # A golfer can only appear once per tournament.
        UniqueConstraint("tournament_id", "golfer_id", name="uq_entry_tournament_golfer"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    tournament_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tournaments.id"), nullable=False
    )
    golfer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("golfers.id"), nullable=False
    )

    # Round-1 tee time for this golfer (timezone-aware).
    # Null until the official tee sheet is released (usually Tuesday/Wednesday).
    # The API uses this to lock pick changes: if now() >= tee_time, the pick
    # is locked and can no longer be changed for that tournament.
    tee_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Null until the tournament ends and official results are published.
    finish_position: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Prize money in whole USD dollars. Null until tournament completes.
    earnings_usd: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Earnings in whole USD dollars"
    )

    # Withdrawal, cut, disqualification, etc. Null while tournament is active.
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # --- Relationships ---
    tournament: Mapped["Tournament"] = relationship(back_populates="entries")
    golfer: Mapped["Golfer"] = relationship(back_populates="tournament_entries")

    def __repr__(self) -> str:
        return (
            f"<TournamentEntry tournament={self.tournament_id} "
            f"golfer={self.golfer_id} position={self.finish_position}>"
        )
