"""
Pick model.

A Pick is the core action in the game: one league member chooses one golfer
for one tournament in a season. After the tournament ends, `points_earned`
is populated by the scoring service.

Business rules enforced here (via UniqueConstraint) and in the API layer:
  1. One pick per user per tournament per season per league.
     → UniqueConstraint on (league_id, season_id, user_id, tournament_id)
  2. No repeat golfers within a season for the same user in the same league.
     → Enforced in the picks service (not a simple DB constraint — requires
        a query to check existing picks for the season).
  3. Picks lock at tournament start_date.
     → Enforced in the API layer by comparing submitted_at to tournament.start_date.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Pick(Base):
    __tablename__ = "picks"
    __table_args__ = (
        # One pick per user per tournament per season per league.
        UniqueConstraint(
            "league_id",
            "season_id",
            "user_id",
            "tournament_id",
            name="uq_pick_league_season_user_tournament",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), nullable=False
    )
    season_id: Mapped[int] = mapped_column(
        ForeignKey("seasons.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    tournament_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tournaments.id"), nullable=False
    )
    golfer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("golfers.id"), nullable=False
    )

    # Null until the tournament is complete and the scoring service runs.
    # Formula: golfer earnings_usd * tournament.multiplier
    points_earned: Mapped[float | None] = mapped_column(Float, nullable=True)

    # When the user submitted the pick. The API rejects picks where
    # submitted_at would be after the tournament's start_date.
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # --- Relationships ---
    league: Mapped["League"] = relationship(back_populates="picks")
    season: Mapped["Season"] = relationship(back_populates="picks")
    user: Mapped["User"] = relationship(back_populates="picks")
    tournament: Mapped["Tournament"] = relationship(back_populates="picks")
    golfer: Mapped["Golfer"] = relationship(back_populates="picks")

    def __repr__(self) -> str:
        return (
            f"<Pick user={self.user_id} golfer={self.golfer_id} "
            f"tournament={self.tournament_id} points={self.points_earned}>"
        )
