"""
Season model.

A Season represents a single competitive year within a league. Each league
runs one season per calendar year. Picks and standings are scoped to a season
— points don't carry over from one year to the next.

The UniqueConstraint ensures a league can't accidentally have two active
seasons for the same year.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.league import League
    from app.models.pick import Pick


class Season(Base):
    __tablename__ = "seasons"
    __table_args__ = (
        # Each league can only have one season per year.
        UniqueConstraint("league_id", "year", name="uq_league_season_year"),
        # Each league can only have one active season at a time.
        # Partial unique index — only applies to rows where is_active = TRUE.
        Index(
            "uq_league_one_active_season",
            "league_id",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    league_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leagues.id"), nullable=False)

    # Calendar year, e.g. 2025.
    year: Mapped[int] = mapped_column(Integer, nullable=False)

    # Only one season should be active at a time per league.
    # Inactive seasons are historical — useful for viewing past standings.
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # --- Relationships ---
    league: Mapped["League"] = relationship(back_populates="seasons")
    picks: Mapped[list["Pick"]] = relationship(back_populates="season")

    def __repr__(self) -> str:
        return f"<Season league={self.league_id} year={self.year} active={self.is_active}>"
