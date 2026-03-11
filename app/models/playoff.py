"""
Playoff models.

Five tables covering the full playoff lifecycle:
  playoff_configs       — settings per league per season
  playoff_rounds        — one row per bracket round, with assigned tournament
  playoff_pods          — one pod/matchup per round
  playoff_pod_members   — which players are in which pod
  playoff_picks         — golfer picks within a pod draft
  playoff_draft_preferences — ranked preference lists submitted during draft window
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, UniqueConstraint, func
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.golfer import Golfer
    from app.models.league import League
    from app.models.season import Season
    from app.models.tournament import Tournament
    from app.models.user import User


class PlayoffConfig(Base):
    __tablename__ = "playoff_configs"
    __table_args__ = (
        UniqueConstraint("league_id", "season_id", name="uq_playoff_config_league_season"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    league_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("leagues.id"), nullable=False)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    playoff_size: Mapped[int] = mapped_column(Integer, nullable=False, default=16, server_default="16")
    draft_style: Mapped[str] = mapped_column(String(30), nullable=False, default="snake", server_default="snake")
    picks_per_round: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=lambda: [2, 4], server_default="[2,4]")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    seeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    rounds: Mapped[list["PlayoffRound"]] = relationship(back_populates="playoff_config", cascade="all, delete-orphan")


class PlayoffRound(Base):
    __tablename__ = "playoff_rounds"
    __table_args__ = (
        UniqueConstraint("playoff_config_id", "round_number", name="uq_playoff_round_config_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    playoff_config_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("playoff_configs.id"), nullable=False)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    tournament_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tournaments.id"), nullable=True)
    draft_opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    draft_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    playoff_config: Mapped["PlayoffConfig"] = relationship(back_populates="rounds")
    tournament: Mapped["Tournament | None"] = relationship()
    pods: Mapped[list["PlayoffPod"]] = relationship(back_populates="playoff_round", cascade="all, delete-orphan")


class PlayoffPod(Base):
    __tablename__ = "playoff_pods"
    __table_args__ = (
        UniqueConstraint("playoff_round_id", "bracket_position", name="uq_playoff_pod_round_position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    playoff_round_id: Mapped[int] = mapped_column(ForeignKey("playoff_rounds.id"), nullable=False)
    bracket_position: Mapped[int] = mapped_column(Integer, nullable=False)
    winner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    playoff_round: Mapped["PlayoffRound"] = relationship(back_populates="pods")
    members: Mapped[list["PlayoffPodMember"]] = relationship(back_populates="pod", cascade="all, delete-orphan")
    picks: Mapped[list["PlayoffPick"]] = relationship(back_populates="pod", cascade="all, delete-orphan")
    draft_preferences: Mapped[list["PlayoffDraftPreference"]] = relationship(back_populates="pod", cascade="all, delete-orphan")
    winner: Mapped["User | None"] = relationship(foreign_keys=[winner_user_id])


class PlayoffPodMember(Base):
    __tablename__ = "playoff_pod_members"
    __table_args__ = (
        UniqueConstraint("pod_id", "user_id", name="uq_playoff_pod_member"),
        UniqueConstraint("pod_id", "draft_position", name="uq_playoff_pod_draft_position"),
        UniqueConstraint("pod_id", "seed", name="uq_playoff_pod_seed"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pod_id: Mapped[int] = mapped_column(ForeignKey("playoff_pods.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    draft_position: Mapped[int] = mapped_column(Integer, nullable=False)
    total_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_eliminated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    pod: Mapped["PlayoffPod"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship()
    draft_preferences: Mapped[list["PlayoffDraftPreference"]] = relationship(back_populates="pod_member", cascade="all, delete-orphan")
    picks: Mapped[list["PlayoffPick"]] = relationship(back_populates="pod_member")


class PlayoffPick(Base):
    __tablename__ = "playoff_picks"
    __table_args__ = (
        UniqueConstraint("pod_id", "golfer_id", name="uq_playoff_pick_pod_golfer"),
        UniqueConstraint("pod_id", "pod_member_id", "draft_slot", name="uq_playoff_pick_pod_member_slot"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pod_id: Mapped[int] = mapped_column(ForeignKey("playoff_pods.id"), nullable=False)
    pod_member_id: Mapped[int] = mapped_column(ForeignKey("playoff_pod_members.id"), nullable=False)
    golfer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("golfers.id"), nullable=False)
    tournament_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tournaments.id"), nullable=False)
    draft_slot: Mapped[int] = mapped_column(Integer, nullable=False)
    points_earned: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    pod: Mapped["PlayoffPod"] = relationship(back_populates="picks")
    pod_member: Mapped["PlayoffPodMember"] = relationship(back_populates="picks")
    golfer: Mapped["Golfer"] = relationship()
    tournament: Mapped["Tournament"] = relationship()


class PlayoffDraftPreference(Base):
    __tablename__ = "playoff_draft_preferences"
    __table_args__ = (
        UniqueConstraint("pod_member_id", "golfer_id", name="uq_pref_member_golfer"),
        UniqueConstraint("pod_member_id", "rank", name="uq_pref_member_rank"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pod_id: Mapped[int] = mapped_column(ForeignKey("playoff_pods.id"), nullable=False)
    pod_member_id: Mapped[int] = mapped_column(ForeignKey("playoff_pod_members.id"), nullable=False)
    golfer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("golfers.id"), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    pod: Mapped["PlayoffPod"] = relationship(back_populates="draft_preferences")
    pod_member: Mapped["PlayoffPodMember"] = relationship(back_populates="draft_preferences")
    golfer: Mapped["Golfer"] = relationship()
