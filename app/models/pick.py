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
from datetime import datetime, timezone

from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.league import League
    from app.models.season import Season
    from app.models.user import User
    from app.models.tournament import Tournament, TournamentEntry
    from app.models.golfer import Golfer


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

    # Viewonly join to TournamentEntry so the API can expose raw earnings_usd
    # alongside points_earned (which bakes in the multiplier at scoring time).
    entry: Mapped["TournamentEntry | None"] = relationship(
        "TournamentEntry",
        primaryjoin=(
            "and_(Pick.tournament_id == TournamentEntry.tournament_id, "
            "Pick.golfer_id == TournamentEntry.golfer_id)"
        ),
        foreign_keys="[TournamentEntry.tournament_id, TournamentEntry.golfer_id]",
        viewonly=True,
        uselist=False,
    )

    @property
    def earnings_usd(self) -> float | None:
        """Raw golfer earnings before the league multiplier is applied."""
        if self.entry is not None and self.entry.earnings_usd is not None:
            return float(self.entry.earnings_usd)
        return None

    @property
    def position(self) -> int | None:
        """Golfer's finishing (or current) position in the tournament."""
        return self.entry.finish_position if self.entry is not None else None

    @property
    def golfer_status(self) -> str | None:
        """Golfer's status in the tournament (e.g. 'CUT', 'WD', 'DQ'); None if active/finished normally."""
        return self.entry.status if self.entry is not None else None

    @property
    def is_tied(self) -> bool:
        """True when multiple golfers share this finish position."""
        return bool(self.entry and self.entry.is_tied)

    @property
    def is_locked(self) -> bool:
        """
        True once picking / changing this pick is no longer allowed.

        Lock rules (mirrors the backend validation in services/picks.py):
          - COMPLETED  → always locked (tournament is over)
          - IN_PROGRESS → locked once the golfer's Round 1 tee_time has passed,
                          or immediately if tee_time is null (safety: no data = locked)
          - SCHEDULED  → never locked here (deadline enforced by start_date check
                          in validate_new_pick / validate_pick_change)
        """
        from app.models.tournament import TournamentStatus

        if self.tournament.status == TournamentStatus.COMPLETED.value:
            return True
        if self.tournament.status == TournamentStatus.IN_PROGRESS.value:
            if self.entry is None or self.entry.tee_time is None:
                return True  # no tee_time when in_progress → locked (safety)
            return self.entry.tee_time <= datetime.now(timezone.utc)
        return False

    def __repr__(self) -> str:
        return (
            f"<Pick user={self.user_id} golfer={self.golfer_id} "
            f"tournament={self.tournament_id} points={self.points_earned}>"
        )
