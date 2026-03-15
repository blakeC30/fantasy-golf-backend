"""Playoff schemas — request/response types for all playoff endpoints."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator


# ---------------------------------------------------------------------------
# Playoff Config
# ---------------------------------------------------------------------------

class PlayoffConfigCreate(BaseModel):
    playoff_size: int = 16
    draft_style: str = "snake"
    picks_per_round: list[int] = [2, 2]

    @field_validator("playoff_size")
    @classmethod
    def must_be_power_of_two(cls, v: int) -> int:
        if v not in (0, 2, 4, 8, 16, 32):
            raise ValueError("playoff_size must be 0 (disabled), 2, 4, 8, 16, or 32")
        return v

    @field_validator("draft_style")
    @classmethod
    def must_be_valid_style(cls, v: str) -> str:
        if v not in ("snake", "linear", "top_seed_priority"):
            raise ValueError("draft_style must be snake, linear, or top_seed_priority")
        return v

    @field_validator("picks_per_round")
    @classmethod
    def picks_must_be_positive(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("picks_per_round must not be empty")
        if any(n < 1 for n in v):
            raise ValueError("each value in picks_per_round must be at least 1")
        return v


class PlayoffConfigUpdate(BaseModel):
    playoff_size: int | None = None
    draft_style: str | None = None
    picks_per_round: list[int] | None = None

    @field_validator("playoff_size")
    @classmethod
    def must_be_power_of_two(cls, v: int | None) -> int | None:
        if v is not None and v not in (0, 2, 4, 8, 16, 32):
            raise ValueError("playoff_size must be 0 (disabled), 2, 4, 8, 16, or 32")
        return v

    @field_validator("picks_per_round")
    @classmethod
    def picks_must_be_positive(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return v
        if not v:
            raise ValueError("picks_per_round must not be empty")
        if any(n < 1 for n in v):
            raise ValueError("each value in picks_per_round must be at least 1")
        return v


class PlayoffConfigOut(BaseModel):
    id: uuid.UUID
    league_id: uuid.UUID
    season_id: int
    is_enabled: bool
    playoff_size: int
    draft_style: str
    picks_per_round: list[int]
    status: str
    seeded_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Playoff Round
# ---------------------------------------------------------------------------

class PlayoffRoundAssign(BaseModel):
    """Admin assigns a tournament and draft window to a round."""
    tournament_id: uuid.UUID
    draft_opens_at: datetime | None = None


class PlayoffRoundOut(BaseModel):
    id: int
    round_number: int
    tournament_id: uuid.UUID | None
    draft_opens_at: datetime | None
    draft_resolved_at: datetime | None
    status: str

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Playoff Pod
# ---------------------------------------------------------------------------

class PlayoffPodMemberOut(BaseModel):
    id: int
    user_id: uuid.UUID
    display_name: str
    seed: int
    draft_position: int
    total_points: float | None
    is_eliminated: bool

    model_config = ConfigDict(from_attributes=True)


class PlayoffPickOut(BaseModel):
    id: uuid.UUID
    pod_member_id: int
    golfer_id: uuid.UUID
    golfer_name: str
    draft_slot: int
    points_earned: float | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PlayoffPodOut(BaseModel):
    id: int
    bracket_position: int
    status: str
    winner_user_id: uuid.UUID | None
    members: list[PlayoffPodMemberOut]
    picks: list[PlayoffPickOut]
    active_draft_slot: int | None  # None when draft is complete or not started
    is_picks_visible: bool  # False = picks still hidden (any_r1_teed_off not yet reached)

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Bracket View
# ---------------------------------------------------------------------------

class BracketRoundOut(BaseModel):
    round_number: int
    status: str
    tournament_id: uuid.UUID | None
    tournament_name: str | None
    draft_opens_at: datetime | None
    draft_resolved_at: datetime | None
    pods: list[PlayoffPodOut]

    model_config = ConfigDict(from_attributes=True)


class BracketOut(BaseModel):
    playoff_config: PlayoffConfigOut
    rounds: list[BracketRoundOut]

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Draft Preferences
# ---------------------------------------------------------------------------

class PlayoffPreferenceSubmit(BaseModel):
    """Player submits their full ranked preference list (replaces any existing list)."""
    golfer_ids: list[uuid.UUID]  # Ordered list: index 0 = rank 1 (most preferred)


class PlayoffPreferenceOut(BaseModel):
    golfer_id: uuid.UUID
    golfer_name: str
    rank: int

    model_config = ConfigDict(from_attributes=True)


class PlayoffPodMemberDraftOut(BaseModel):
    user_id: uuid.UUID
    display_name: str
    seed: int
    draft_position: int
    has_submitted: bool
    preference_count: int

    model_config = ConfigDict(from_attributes=True)


class PlayoffDraftStatusOut(BaseModel):
    """Full draft state for a pod — what each player has submitted."""
    pod_id: int
    round_status: str  # drafting | locked
    deadline: datetime | None  # = tournament.start_date; None if no tournament assigned yet
    required_preference_count: int | None  # pod_size * picks_per_round; None until seeded
    members: list[PlayoffPodMemberDraftOut]  # includes has_submitted flag + preference count
    resolved_picks: list[PlayoffPickOut]  # empty until resolved

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Admin Override
# ---------------------------------------------------------------------------

class PlayoffResultOverride(BaseModel):
    pod_id: int
    winner_user_id: uuid.UUID


class PlayoffPickRevise(BaseModel):
    golfer_id: uuid.UUID


class MyPlayoffPodOut(BaseModel):
    """Lightweight context for the current user's active playoff pod — used by Dashboard/MakePick."""
    is_playoff_week: bool        # nearest scheduled/in_progress league tournament is a playoff round
    is_in_playoffs: bool         # current user has an active pod in that round
    active_pod_id: int | None
    active_round_number: int | None
    tournament_id: uuid.UUID | None
    round_status: str | None     # "drafting" | "locked" | None
    has_submitted: bool
    submitted_count: int         # how many golfers ranked so far
    picks_per_round: int | None
    required_preference_count: int | None  # pod_size * picks_per_round
    deadline: datetime | None


class PlayoffPickSummary(BaseModel):
    golfer_name: str
    points_earned: float | None


class PlayoffTournamentPickOut(BaseModel):
    """One playoff round's picks for a user — used by the MyPicks history page."""
    tournament_id: uuid.UUID
    round_number: int
    status: str                  # round status (drafting/locked/scoring/completed)
    picks: list[PlayoffPickSummary]  # empty if draft not yet resolved
    total_points: float | None
