"""
PickReminder model — tracks one email reminder per (league, season, tournament).

The Wednesday APScheduler job creates one row per league-tournament pair for
tournaments starting in the coming week. Sending is tracked via sent_at; failures
via attempt_count / failed_at. The UNIQUE constraint guarantees idempotency —
calling create_pick_reminders twice never creates duplicate rows.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.league import League
    from app.models.season import Season
    from app.models.tournament import Tournament


class PickReminder(Base):
    __tablename__ = "pick_reminders"
    __table_args__ = (
        # One reminder per league per season per tournament — enforces idempotency
        # at the DB level even if the scheduler job runs multiple times.
        UniqueConstraint("league_id", "season_id", "tournament_id", name="uq_pick_reminders"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id", ondelete="CASCADE"), nullable=False
    )
    # Season.id is an Integer (autoincrement), not a UUID.
    season_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("seasons.id", ondelete="CASCADE"), nullable=False
    )
    tournament_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False
    )

    # When the reminder was (or should have been) sent.
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Set once the email was successfully delivered to SES.
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Set permanently after max_attempts consecutive SES failures.
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Incremented on each SES call attempt (success or failure).
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # --- Relationships ---
    league: Mapped["League"] = relationship(back_populates="pick_reminders")
    season: Mapped["Season"] = relationship(back_populates="pick_reminders")
    tournament: Mapped["Tournament"] = relationship(back_populates="pick_reminders")

    def __repr__(self) -> str:
        return (
            f"<PickReminder league={self.league_id} tournament={self.tournament_id} "
            f"sent={self.sent_at is not None}>"
        )
