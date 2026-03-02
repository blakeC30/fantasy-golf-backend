"""
LeagueTournament model — join table between leagues and tournaments.

A league admin selects which PGA Tour tournaments their league will
participate in. This handles two common scenarios:
  1. Multiple events in one week — the admin picks which event counts.
  2. Mid-season start — tournaments before the league started are excluded.

Picks and standings are scoped to a league's selected tournaments only.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class LeagueTournament(Base):
    __tablename__ = "league_tournaments"
    __table_args__ = (UniqueConstraint("league_id", "tournament_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    league_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leagues.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tournament_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tournaments.id", ondelete="CASCADE"),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # --- Relationships ---
    league: Mapped["League"] = relationship(back_populates="league_tournaments")  # type: ignore[name-defined]
    tournament: Mapped["Tournament"] = relationship(back_populates="league_tournaments")  # type: ignore[name-defined]

    def __repr__(self) -> str:
        return f"<LeagueTournament league={self.league_id} tournament={self.tournament_id}>"
