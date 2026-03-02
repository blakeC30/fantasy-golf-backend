"""
Golfer model.

A Golfer is a professional golfer on the PGA Tour. Golfer records are
populated and kept up-to-date by the scraper (Phase 3). The pga_tour_id
is the stable external identifier — it lets us match incoming scraper data
to the right row even if a golfer's name changes (name normalization,
diacritics, etc.).

world_ranking is updated periodically. It's stored here for display
purposes and doesn't affect scoring.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Golfer(Base):
    __tablename__ = "golfers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # The PGA Tour's own identifier for this golfer. Used by the scraper to
    # match incoming data to existing records (upsert). Indexed for fast lookups.
    pga_tour_id: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(150), nullable=False)

    # Nullable — we may not always have ranking data at the time of insert.
    world_ranking: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Two-letter country code (e.g. "US", "ZA", "SE"). Nullable for safety.
    country: Mapped[str | None] = mapped_column(String(10), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # --- Relationships ---
    tournament_entries: Mapped[list["TournamentEntry"]] = relationship(
        back_populates="golfer"
    )
    picks: Mapped[list["Pick"]] = relationship(back_populates="golfer")

    def __repr__(self) -> str:
        return f"<Golfer name={self.name!r} ranking={self.world_ranking}>"
