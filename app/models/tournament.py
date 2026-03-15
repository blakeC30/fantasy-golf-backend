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
    Boolean,
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
    from app.models.league_tournament import LeagueTournament
    from app.models.pick import Pick
    from app.models.pick_reminder import PickReminder


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

    # ESPN competition ID for this event. For most tournaments this equals
    # pga_tour_id, but team-format events (e.g. Zurich Classic) use a different
    # competition ID in the core API. Stored here so scraper calls use the
    # correct ID without re-fetching the scoreboard on every sync.
    competition_id: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # True for two-person team format tournaments (e.g. Zurich Classic).
    # Drives scraper routing: team events need roster expansion + officialAmount
    # earnings stat (divided by 2) instead of the standard per-athlete flow.
    is_team_event: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Set by the scraper at the very end of each sync_tournament call, after all
    # upserts and pick scoring are committed. The frontend polls this value to
    # detect when a sync has fully completed before refreshing the leaderboard.
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Relationships ---
    entries: Mapped[list["TournamentEntry"]] = relationship(
        back_populates="tournament"
    )
    picks: Mapped[list["Pick"]] = relationship(back_populates="tournament")
    league_tournaments: Mapped[list["LeagueTournament"]] = relationship(back_populates="tournament")
    pick_reminders: Mapped[list["PickReminder"]] = relationship(
        back_populates="tournament", cascade="all, delete-orphan"
    )

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

    # Round 1 (Thursday) tee time for this golfer (timezone-aware).
    # Null until the official tee sheet is released (usually Tuesday/Wednesday).
    # Once set, this value is never overwritten by later rounds' tee times.
    # Pick-locking logic: if now() >= tee_time, the pick is locked for the
    # entire tournament (not just Round 1 — picking closes when Thursday starts).
    tee_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Display leaderboard position computed from score_to_par totals (not ESPN's
    # sequential order). Tied golfers share the same number (T6 → finish_position=6).
    # Set during sync; null until the golfer has played at least one hole.
    finish_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # True when two or more golfers share this finish_position.
    is_tied: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false", default=False)

    # Prize money in whole USD dollars. Null until tournament completes.
    earnings_usd: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Earnings in whole USD dollars"
    )

    # Withdrawal, cut, disqualification, etc. Null while tournament is active.
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # For team events: the ESPN team competitor ID (e.g. "131066" for the team
    # "Novak/Griffin"). Used by score_picks to call the correct earnings endpoint:
    # /competitors/{team_competitor_id}/statistics rather than /competitors/{athlete_id}.
    # Null for individual (non-team) tournaments.
    team_competitor_id: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # --- Relationships ---
    tournament: Mapped["Tournament"] = relationship(back_populates="entries")
    golfer: Mapped["Golfer"] = relationship(back_populates="tournament_entries")
    rounds: Mapped[list["TournamentEntryRound"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<TournamentEntry tournament={self.tournament_id} "
            f"golfer={self.golfer_id} position={self.finish_position}>"
        )


class TournamentEntryRound(Base):
    """
    Per-round performance data for a golfer in a tournament.

    One row per (tournament_entry, round_number). Populated by the scraper
    using the ESPN core API /linescores endpoint, which provides full
    round-by-round breakdowns for completed and in-progress tournaments.

    The existing tournament_entries.tee_time column is unchanged — it holds
    the current round's tee time used by pick-locking logic. This table
    stores tee_time per round for historical display (e.g. showing users
    what time their golfer teed off in each round).

    ESPN field mappings
    -------------------
    round_number  ← linescores item "period"           (int, 1–4, 5+ playoff)
    tee_time      ← linescores item "teeTime"          (ISO 8601 UTC, nullable)
    score         ← linescores item "value"            (total strokes, nullable)
    score_to_par  ← linescores item "displayValue"     (string parsed to int:
                                                         "-2"→-2, "E"→0, "+1"→1)
    position      ← linescores item "currentPosition"  (int rank after round,
                                                         stored as string to allow
                                                         "T5" format if ESPN adds it)
    is_playoff    ← linescores item "isPlayoff"        (bool, default False)
    """

    __tablename__ = "tournament_entry_rounds"
    __table_args__ = (
        UniqueConstraint(
            "tournament_entry_id", "round_number", name="uq_entry_round_number"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tournament_entry_id: Mapped[int] = mapped_column(
        ForeignKey("tournament_entries.id"), nullable=False
    )

    # ESPN "period": 1–4 for standard rounds, 5+ for playoff rounds.
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # ESPN "teeTime": golfer's scheduled start time for this round (UTC).
    # Null until the tee sheet is released (usually Tuesday/Wednesday).
    tee_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ESPN "value": total strokes taken in this round (e.g. 68).
    # Null for rounds not yet played.
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ESPN "displayValue" parsed to int: "-2"→-2, "E"→0, "+1"→1.
    # Represents this round's score relative to par. Null until round completes.
    score_to_par: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ESPN "currentPosition" (integer rank) stored as a string.
    # Using String to allow positional strings like "T5" or "CUT" if ESPN
    # ever surfaces them here. Null until the round is complete.
    position: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # ESPN "isPlayoff": True for playoff rounds (period 5+). False for standard.
    is_playoff: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # Count of holes completed in this round (derived from nested linescores length).
    # Null for rounds that haven't started. 18 means the round is complete.
    # Used by the leaderboard to suppress partial round scores and power Today/Thru display.
    thru: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # True when the golfer's first hole played in this round was >= 10 (back-nine start).
    # Displayed as an asterisk next to the Thru number (e.g. "8*").
    started_on_back: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    entry: Mapped["TournamentEntry"] = relationship(back_populates="rounds")
