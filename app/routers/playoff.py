"""
Playoff router — /leagues/{league_id}/playoff/*

Endpoints:
  POST   /leagues/{league_id}/playoff/config                         Create playoff config (manager)
  GET    /leagues/{league_id}/playoff/config                         Get playoff config
  PATCH  /leagues/{league_id}/playoff/config                         Update playoff config (manager)
  GET    /leagues/{league_id}/playoff/bracket                        Full bracket view
  PATCH  /leagues/{league_id}/playoff/rounds/{round_id}              Assign tournament & draft window (manager)
  POST   /leagues/{league_id}/playoff/rounds/{round_id}/open         Open draft — auto-seeds round 1 (manager)
  POST   /leagues/{league_id}/playoff/rounds/{round_id}/resolve      Resolve draft → picks (manager)
  POST   /leagues/{league_id}/playoff/rounds/{round_id}/score        Score completed round (manager)
  POST   /leagues/{league_id}/playoff/rounds/{round_id}/advance      Advance bracket (manager)
  GET    /leagues/{league_id}/playoff/pods/{pod_id}                  Pod detail (members only)
  GET    /leagues/{league_id}/playoff/pods/{pod_id}/draft            Draft status for pod (members only)
  GET    /leagues/{league_id}/playoff/pods/{pod_id}/preferences      My preference list (member)
  PUT    /leagues/{league_id}/playoff/pods/{pod_id}/preferences      Submit/replace preferences (member)
  POST   /leagues/{league_id}/playoff/override                       Manual result override (manager)
  PATCH  /leagues/{league_id}/playoff/picks/{pick_id}               Revise golfer on a pick (manager)
"""

import math
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.dependencies import (
    get_active_season,
    get_current_user,
    get_league_or_404,
    require_league_manager,
    require_league_member,
)
from app.models import (
    League,
    LeagueMember,
    LeagueTournament,
    PlayoffConfig,
    PlayoffDraftPreference,
    PlayoffPick,
    PlayoffPod,
    PlayoffPodMember,
    PlayoffRound,
    Season,
    Tournament,
    User,
)
from app.models.tournament import TournamentStatus
from app.schemas.playoff import (
    BracketOut,
    BracketRoundOut,
    MyPlayoffPodOut,
    PlayoffConfigCreate,
    PlayoffConfigOut,
    PlayoffConfigUpdate,
    PlayoffDraftStatusOut,
    PlayoffPickOut,
    PlayoffPickSummary,
    PlayoffPodMemberDraftOut,
    PlayoffPodMemberOut,
    PlayoffPodOut,
    PlayoffPreferenceOut,
    PlayoffPreferenceSubmit,
    PlayoffPickRevise,
    PlayoffResultOverride,
    PlayoffRoundAssign,
    PlayoffRoundOut,
    PlayoffTournamentPickOut,
)
from app.services.playoff import (
    advance_bracket,
    any_r1_teed_off,
    first_r1_tee_time,
    open_round_draft,
    override_result,
    resolve_draft,
    score_round,
    submit_preferences,
)

router = APIRouter(prefix="/leagues", tags=["playoff"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_config_or_404(league_id: uuid.UUID, season_id: int, db: Session) -> PlayoffConfig:
    config = db.query(PlayoffConfig).filter_by(league_id=league_id, season_id=season_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Playoff config not found for this league/season")
    return config


def _get_pod_or_404(pod_id: int, db: Session) -> PlayoffPod:
    pod = (
        db.query(PlayoffPod)
        .options(
            joinedload(PlayoffPod.members).joinedload(PlayoffPodMember.user),
            joinedload(PlayoffPod.picks).joinedload(PlayoffPick.golfer),
            joinedload(PlayoffPod.playoff_round).joinedload(PlayoffRound.playoff_config),
        )
        .filter(PlayoffPod.id == pod_id)
        .first()
    )
    if not pod:
        raise HTTPException(status_code=404, detail="Pod not found")
    return pod


def _get_round_or_404(round_id: int, db: Session) -> PlayoffRound:
    round_obj = (
        db.query(PlayoffRound)
        .options(
            joinedload(PlayoffRound.pods).joinedload(PlayoffPod.members).joinedload(PlayoffPodMember.user),
            joinedload(PlayoffRound.pods).joinedload(PlayoffPod.picks).joinedload(PlayoffPick.golfer),
            joinedload(PlayoffRound.playoff_config),
            joinedload(PlayoffRound.tournament),
        )
        .filter(PlayoffRound.id == round_id)
        .first()
    )
    if not round_obj:
        raise HTTPException(status_code=404, detail="Round not found")
    return round_obj


def _build_pod_member_out(member: PlayoffPodMember) -> PlayoffPodMemberOut:
    return PlayoffPodMemberOut(
        id=member.id,
        user_id=member.user_id,
        display_name=member.user.display_name,
        seed=member.seed,
        draft_position=member.draft_position,
        total_points=member.total_points,
        is_eliminated=member.is_eliminated,
    )


def _build_pick_out(pick: PlayoffPick) -> PlayoffPickOut:
    return PlayoffPickOut(
        id=pick.id,
        pod_member_id=pick.pod_member_id,
        golfer_id=pick.golfer_id,
        golfer_name=pick.golfer.name,
        draft_slot=pick.draft_slot,
        points_earned=pick.points_earned,
        created_at=pick.created_at,
    )


def _build_pod_out(pod: PlayoffPod, config: PlayoffConfig, round_number: int, is_picks_visible: bool = True) -> PlayoffPodOut:
    idx = round_number - 1
    picks_per_player = config.picks_per_round[idx] if idx < len(config.picks_per_round) else config.picks_per_round[-1]
    total_slots = len(pod.members) * picks_per_player
    filled_slots = {p.draft_slot for p in pod.picks}
    active_slot: int | None = None
    if pod.status == "drafting":
        for slot in range(1, total_slots + 1):
            if slot not in filled_slots:
                active_slot = slot
                break

    return PlayoffPodOut(
        id=pod.id,
        bracket_position=pod.bracket_position,
        status=pod.status,
        winner_user_id=pod.winner_user_id,
        members=[_build_pod_member_out(m) for m in sorted(pod.members, key=lambda m: m.seed)],
        picks=[_build_pick_out(p) for p in sorted(pod.picks, key=lambda p: p.draft_slot)] if is_picks_visible else [],
        active_draft_slot=active_slot,
    )


def _build_bracket_round_out(round_obj: PlayoffRound, config: PlayoffConfig, is_picks_visible: bool = True) -> BracketRoundOut:
    tournament_name: str | None = None
    if round_obj.tournament:
        tournament_name = round_obj.tournament.name

    return BracketRoundOut(
        round_number=round_obj.round_number,
        status=round_obj.status,
        tournament_id=round_obj.tournament_id,
        tournament_name=tournament_name,
        draft_opens_at=round_obj.draft_opens_at,
        draft_resolved_at=round_obj.draft_resolved_at,
        pods=[_build_pod_out(pod, config, round_obj.round_number, is_picks_visible=is_picks_visible) for pod in sorted(round_obj.pods, key=lambda p: p.bracket_position)],
    )


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _count_eligible_playoff_tournaments(league_id: uuid.UUID, db: Session) -> int:
    """
    Count scheduled (future) league tournaments that are eligible as playoff rounds.

    Eligibility: tournament.status == 'scheduled' AND not the very next upcoming
    tournament (which is reserved for the current week's regular-season pick).
    """
    next_upcoming = (
        db.query(Tournament)
        .filter(Tournament.status == TournamentStatus.SCHEDULED.value)
        .order_by(Tournament.start_date.asc())
        .first()
    )
    next_upcoming_id = next_upcoming.id if next_upcoming else None

    query = (
        db.query(LeagueTournament)
        .filter_by(league_id=league_id)
        .join(LeagueTournament.tournament)
        .filter(Tournament.status == TournamentStatus.SCHEDULED.value)
    )
    if next_upcoming_id is not None:
        query = query.filter(Tournament.id != next_upcoming_id)

    return query.count()


def _required_rounds(playoff_size: int) -> int:
    """Return the number of playoff rounds required for a given bracket size."""
    if playoff_size == 32:
        return 4
    return int(math.log2(playoff_size))  # 2→1, 4→2, 8→3, 16→4


# ---------------------------------------------------------------------------
# Config endpoints
# ---------------------------------------------------------------------------


@router.post("/{league_id}/playoff/config", response_model=PlayoffConfigOut, status_code=201)
def create_playoff_config(
    body: PlayoffConfigCreate,
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_manager),
    season: Season = Depends(get_active_season),
    db: Session = Depends(get_db),
):
    """Create playoff configuration for the active season (manager only)."""
    league, _ = league_and_member

    existing = db.query(PlayoffConfig).filter_by(league_id=league.id, season_id=season.id).first()
    if existing:
        raise HTTPException(status_code=409, detail="Playoff config already exists for this season")

    # Validate that the current schedule has enough eligible future tournaments.
    if body.playoff_size > 0:
        required = _required_rounds(body.playoff_size)
        eligible = _count_eligible_playoff_tournaments(league.id, db)
        if eligible < required:
            raise HTTPException(
                status_code=422,
                detail=f"Schedule needs {required} future tournament(s) for a {body.playoff_size}-player bracket; {eligible} available",
            )

    config = PlayoffConfig(
        league_id=league.id,
        season_id=season.id,
        is_enabled=True,  # always enabled; playoffs are enabled by selecting playoff tournaments
        playoff_size=body.playoff_size,
        draft_style=body.draft_style,
        picks_per_round=body.picks_per_round,
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


@router.get("/{league_id}/playoff/config", response_model=PlayoffConfigOut)
def get_playoff_config(
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_member),
    season: Season = Depends(get_active_season),
    db: Session = Depends(get_db),
):
    """Get playoff config for the active season (members only)."""
    league, _ = league_and_member
    return _get_config_or_404(league.id, season.id, db)


@router.patch("/{league_id}/playoff/config", response_model=PlayoffConfigOut)
def update_playoff_config(
    body: PlayoffConfigUpdate,
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_manager),
    season: Season = Depends(get_active_season),
    db: Session = Depends(get_db),
):
    """Update playoff config (manager only). Cannot change once the bracket is active."""
    league, _ = league_and_member
    config = _get_config_or_404(league.id, season.id, db)

    if config.status != "pending":
        raise HTTPException(status_code=422, detail="Cannot modify config after the playoff bracket is active")

    if body.playoff_size is not None:
        if body.playoff_size > 0:
            required = _required_rounds(body.playoff_size)
            eligible = _count_eligible_playoff_tournaments(league.id, db)
            if eligible < required:
                raise HTTPException(
                    status_code=422,
                    detail=f"Schedule needs {required} future tournament(s) for a {body.playoff_size}-player bracket; {eligible} available",
                )
        config.playoff_size = body.playoff_size
    if body.draft_style is not None:
        config.draft_style = body.draft_style
    if body.picks_per_round is not None:
        config.picks_per_round = body.picks_per_round

    db.commit()
    db.refresh(config)
    return config


# ---------------------------------------------------------------------------
# Bracket view
# ---------------------------------------------------------------------------


@router.get("/{league_id}/playoff/bracket", response_model=BracketOut)
def get_bracket(
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_member),
    season: Season = Depends(get_active_season),
    db: Session = Depends(get_db),
):
    """
    Full bracket view — all rounds, pods, members, and picks (members only).
    """
    league, _ = league_and_member
    config = _get_config_or_404(league.id, season.id, db)

    # Eager-load everything in one query
    config_loaded = (
        db.query(PlayoffConfig)
        .options(
            joinedload(PlayoffConfig.rounds)
            .joinedload(PlayoffRound.pods)
            .joinedload(PlayoffPod.members)
            .joinedload(PlayoffPodMember.user),
            joinedload(PlayoffConfig.rounds)
            .joinedload(PlayoffRound.pods)
            .joinedload(PlayoffPod.picks)
            .joinedload(PlayoffPick.golfer),
            joinedload(PlayoffConfig.rounds)
            .joinedload(PlayoffRound.tournament),
        )
        .filter(PlayoffConfig.id == config.id)
        .first()
    )

    rounds_out = []
    for r in sorted(config_loaded.rounds, key=lambda r: r.round_number):
        is_picks_visible = True
        # Playoff rule: picks are hidden until the FIRST R1 tee time passes.
        # (Regular season hides until the LAST tee time — different rule.)
        if r.status == "locked" and r.tournament and r.tournament.status == "in_progress":
            is_picks_visible = any_r1_teed_off(db, r.tournament_id)
        rounds_out.append(_build_bracket_round_out(r, config_loaded, is_picks_visible=is_picks_visible))

    return BracketOut(playoff_config=config_loaded, rounds=rounds_out)


# ---------------------------------------------------------------------------
# Round management (manager)
# ---------------------------------------------------------------------------


@router.patch("/{league_id}/playoff/rounds/{round_id}", response_model=PlayoffRoundOut)
def assign_round_tournament(
    round_id: int,
    body: PlayoffRoundAssign,
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_manager),
    db: Session = Depends(get_db),
):
    """Assign a tournament and draft window to a round (manager only)."""
    league, _ = league_and_member
    round_obj = _get_round_or_404(round_id, db)

    # Verify this round belongs to a config for this league
    if round_obj.playoff_config.league_id != league.id:
        raise HTTPException(status_code=403, detail="Round does not belong to this league")

    round_obj.tournament_id = body.tournament_id
    round_obj.draft_opens_at = body.draft_opens_at
    db.commit()
    db.refresh(round_obj)
    return round_obj


@router.post("/{league_id}/playoff/rounds/{round_id}/open", response_model=PlayoffRoundOut)
def open_draft(
    round_id: int,
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_manager),
    db: Session = Depends(get_db),
):
    """Open the draft window for a round (manager only)."""
    league, _ = league_and_member
    round_obj = _get_round_or_404(round_id, db)

    if round_obj.playoff_config.league_id != league.id:
        raise HTTPException(status_code=403, detail="Round does not belong to this league")

    open_round_draft(db, round_obj)
    db.refresh(round_obj)
    return round_obj


@router.post("/{league_id}/playoff/rounds/{round_id}/resolve", response_model=PlayoffRoundOut)
def resolve_round_draft(
    round_id: int,
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_manager),
    db: Session = Depends(get_db),
):
    """
    Resolve the draft for a round: process all submitted preference lists
    into picks (manager only). Should be called after the tournament starts.
    """
    league, _ = league_and_member
    round_obj = _get_round_or_404(round_id, db)

    if round_obj.playoff_config.league_id != league.id:
        raise HTTPException(status_code=403, detail="Round does not belong to this league")

    resolve_draft(db, round_obj)
    db.refresh(round_obj)
    return round_obj


@router.post("/{league_id}/playoff/rounds/{round_id}/score", response_model=PlayoffRoundOut)
def score_playoff_round(
    round_id: int,
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_manager),
    db: Session = Depends(get_db),
):
    """
    Populate points_earned from tournament results (manager only).
    Call after the tournament completes.
    """
    league, _ = league_and_member
    round_obj = _get_round_or_404(round_id, db)

    if round_obj.playoff_config.league_id != league.id:
        raise HTTPException(status_code=403, detail="Round does not belong to this league")

    score_round(db, round_obj)
    db.refresh(round_obj)
    return round_obj


@router.post("/{league_id}/playoff/rounds/{round_id}/advance", response_model=PlayoffRoundOut)
def advance_playoff_bracket(
    round_id: int,
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_manager),
    db: Session = Depends(get_db),
):
    """
    Determine winners and populate next round's pods (manager only).
    Call after score_round.
    """
    league, _ = league_and_member
    round_obj = _get_round_or_404(round_id, db)

    if round_obj.playoff_config.league_id != league.id:
        raise HTTPException(status_code=403, detail="Round does not belong to this league")

    advance_bracket(db, round_obj)
    db.refresh(round_obj)
    return round_obj


# ---------------------------------------------------------------------------
# Pod endpoints (members)
# ---------------------------------------------------------------------------


@router.get("/{league_id}/playoff/pods/{pod_id}", response_model=PlayoffPodOut)
def get_pod_detail(
    pod_id: int,
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_member),
    db: Session = Depends(get_db),
):
    """Full pod detail — members, picks, and active draft slot (members only)."""
    league, _ = league_and_member
    pod = _get_pod_or_404(pod_id, db)

    # Verify pod belongs to this league
    if pod.playoff_round.playoff_config.league_id != league.id:
        raise HTTPException(status_code=403, detail="Pod does not belong to this league")

    config = pod.playoff_round.playoff_config
    return _build_pod_out(pod, config, pod.playoff_round.round_number)


@router.get("/{league_id}/playoff/pods/{pod_id}/draft", response_model=PlayoffDraftStatusOut)
def get_draft_status(
    pod_id: int,
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_member),
    db: Session = Depends(get_db),
):
    """
    Draft status for a pod: who has submitted their preference list and how many.
    Actual golfer rankings are hidden until the draft is resolved.
    """
    league, _ = league_and_member
    pod = _get_pod_or_404(pod_id, db)

    if pod.playoff_round.playoff_config.league_id != league.id:
        raise HTTPException(status_code=403, detail="Pod does not belong to this league")

    playoff_round = pod.playoff_round
    tournament = playoff_round.tournament

    # required_preference_count = pod_size * picks_per_round for this round
    config = playoff_round.playoff_config
    idx = playoff_round.round_number - 1
    ppr = config.picks_per_round[idx] if idx < len(config.picks_per_round) else config.picks_per_round[-1]
    pod_size = len(pod.members)
    required_preference_count: int | None = (pod_size * ppr) if pod_size > 0 else None

    members_out: list[PlayoffPodMemberDraftOut] = []
    for member in sorted(pod.members, key=lambda m: m.seed):
        pref_count = (
            db.query(PlayoffDraftPreference)
            .filter_by(pod_member_id=member.id)
            .count()
        )
        members_out.append(PlayoffPodMemberDraftOut(
            user_id=member.user_id,
            display_name=member.user.display_name,
            seed=member.seed,
            draft_position=member.draft_position,
            has_submitted=pref_count > 0,
            preference_count=pref_count,
        ))

    picks_out = [_build_pick_out(p) for p in sorted(pod.picks, key=lambda p: p.draft_slot)]

    # Hide resolved picks until the first Round 1 tee time has passed.
    # Playoff rule: once any golfer tees off, all pod picks become visible.
    if playoff_round.status == "locked" and tournament and tournament.status == "in_progress":
        if not any_r1_teed_off(db, tournament.id):
            picks_out = []

    # Deadline = first R1 tee time (the moment preferences lock).
    # Falls back to tournament.start_date midnight when tee times are not yet in the DB.
    from datetime import datetime, timezone
    deadline_dt: datetime | None = None
    if tournament:
        deadline_dt = first_r1_tee_time(db, tournament.id)
        if deadline_dt is None and tournament.start_date:
            deadline_dt = datetime.combine(
                tournament.start_date, datetime.min.time()
            ).replace(tzinfo=timezone.utc)

    return PlayoffDraftStatusOut(
        pod_id=pod.id,
        round_status=playoff_round.status,
        deadline=deadline_dt,
        required_preference_count=required_preference_count,
        members=members_out,
        resolved_picks=picks_out,
    )


@router.get("/{league_id}/playoff/pods/{pod_id}/preferences", response_model=list[PlayoffPreferenceOut])
def get_my_preferences(
    pod_id: int,
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_member),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get my submitted preference list for a pod."""
    league, _ = league_and_member
    pod = _get_pod_or_404(pod_id, db)

    if pod.playoff_round.playoff_config.league_id != league.id:
        raise HTTPException(status_code=403, detail="Pod does not belong to this league")

    pod_member = next((m for m in pod.members if m.user_id == current_user.id), None)
    if not pod_member:
        raise HTTPException(status_code=403, detail="You are not a member of this pod")

    prefs = (
        db.query(PlayoffDraftPreference)
        .filter_by(pod_member_id=pod_member.id)
        .order_by(PlayoffDraftPreference.rank)
        .all()
    )

    from app.models import Golfer
    return [
        PlayoffPreferenceOut(
            golfer_id=p.golfer_id,
            golfer_name=db.query(Golfer).filter_by(id=p.golfer_id).first().name,
            rank=p.rank,
        )
        for p in prefs
    ]


@router.put("/{league_id}/playoff/pods/{pod_id}/preferences", response_model=list[PlayoffPreferenceOut])
def submit_draft_preferences(
    pod_id: int,
    body: PlayoffPreferenceSubmit,
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_member),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Submit (or replace) the full ranked golfer preference list for this pod.
    Closes automatically at tournament start_date.
    """
    league, _ = league_and_member
    pod = _get_pod_or_404(pod_id, db)

    if pod.playoff_round.playoff_config.league_id != league.id:
        raise HTTPException(status_code=403, detail="Pod does not belong to this league")

    pod_member = next((m for m in pod.members if m.user_id == current_user.id), None)
    if not pod_member:
        raise HTTPException(status_code=403, detail="You are not a member of this pod")

    tournament_id = pod.playoff_round.tournament_id
    if tournament_id is None:
        raise HTTPException(status_code=422, detail="No tournament assigned to this round yet")

    new_prefs = submit_preferences(db, pod_member, body.golfer_ids, tournament_id)

    from app.models import Golfer
    return [
        PlayoffPreferenceOut(
            golfer_id=p.golfer_id,
            golfer_name=db.query(Golfer).filter_by(id=p.golfer_id).first().name,
            rank=p.rank,
        )
        for p in new_prefs
    ]


# ---------------------------------------------------------------------------
# Override (manager)
# ---------------------------------------------------------------------------


@router.post("/{league_id}/playoff/override", status_code=200)
def override_pod_result(
    body: PlayoffResultOverride,
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_manager),
    db: Session = Depends(get_db),
):
    """Manually set the winner of a pod, bypassing scoring (manager only)."""
    league, _ = league_and_member
    pod = _get_pod_or_404(body.pod_id, db)

    if pod.playoff_round.playoff_config.league_id != league.id:
        raise HTTPException(status_code=403, detail="Pod does not belong to this league")

    override_result(db, pod, body.winner_user_id)
    return {"detail": "Override applied"}


# ---------------------------------------------------------------------------
# My playoff pod context (member)
# ---------------------------------------------------------------------------


@router.get("/{league_id}/playoff/my-pod", response_model=MyPlayoffPodOut)
def get_my_playoff_pod(
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_member),
    current_user: User = Depends(get_current_user),
    season: Season = Depends(get_active_season),
    db: Session = Depends(get_db),
):
    """
    Lightweight context for the current user's active playoff pod.
    Used by Dashboard and MakePick to detect playoff weeks.
    Returns 200 in all cases (never 404 — absent config returns is_playoff_week=False).
    """
    from datetime import datetime, time, timezone

    _false = MyPlayoffPodOut(
        is_playoff_week=False,
        is_in_playoffs=False,
        active_pod_id=None,
        active_round_number=None,
        tournament_id=None,
        round_status=None,
        has_submitted=False,
        submitted_count=0,
        picks_per_round=None,
        required_preference_count=None,
        deadline=None,
    )

    league, _ = league_and_member

    config = db.query(PlayoffConfig).filter_by(league_id=league.id, season_id=season.id).first()
    if not config or config.playoff_size == 0:
        return _false

    # Find nearest upcoming league tournament (scheduled or in_progress)
    nearest = (
        db.query(Tournament)
        .join(LeagueTournament, LeagueTournament.tournament_id == Tournament.id)
        .filter(
            LeagueTournament.league_id == league.id,
            Tournament.status.in_(["scheduled", "in_progress"]),
        )
        .order_by(Tournament.start_date.asc())
        .first()
    )
    if not nearest:
        return _false

    # Check if nearest tournament is assigned to a playoff round (any non-completed status)
    active_round = (
        db.query(PlayoffRound)
        .filter(
            PlayoffRound.playoff_config_id == config.id,
            PlayoffRound.tournament_id == nearest.id,
            PlayoffRound.status.in_(["pending", "drafting", "locked"]),
        )
        .order_by(PlayoffRound.round_number.asc())
        .first()
    )
    if not active_round:
        return _false

    # It is a playoff week — check if current user is in a pod
    pod_member = (
        db.query(PlayoffPodMember)
        .join(PlayoffPod, PlayoffPodMember.pod_id == PlayoffPod.id)
        .filter(
            PlayoffPod.playoff_round_id == active_round.id,
            PlayoffPodMember.user_id == current_user.id,
        )
        .first()
    )
    if not pod_member:
        return MyPlayoffPodOut(
            is_playoff_week=True,
            is_in_playoffs=False,
            active_pod_id=None,
            active_round_number=active_round.round_number,
            tournament_id=nearest.id,
            round_status=active_round.status,
            has_submitted=False,
            submitted_count=0,
            picks_per_round=None,
            required_preference_count=None,
            deadline=None,
        )

    # User is in the playoffs
    pref_count = (
        db.query(PlayoffDraftPreference)
        .filter_by(pod_member_id=pod_member.id)
        .count()
    )

    idx = active_round.round_number - 1
    ppr = (
        config.picks_per_round[idx]
        if idx < len(config.picks_per_round)
        else config.picks_per_round[-1]
    )

    pod_size = (
        db.query(PlayoffPodMember)
        .filter_by(pod_id=pod_member.pod_id)
        .count()
    )
    required_preference_count = pod_size * ppr

    # Deadline = first R1 tee time; falls back to start_date midnight if not yet available.
    deadline_dt = first_r1_tee_time(db, nearest.id)
    if deadline_dt is None and nearest.start_date:
        deadline_dt = datetime.combine(
            nearest.start_date, time.min
        ).replace(tzinfo=timezone.utc)

    return MyPlayoffPodOut(
        is_playoff_week=True,
        is_in_playoffs=True,
        active_pod_id=pod_member.pod_id,
        active_round_number=active_round.round_number,
        tournament_id=nearest.id,
        round_status=active_round.status,
        has_submitted=pref_count > 0,
        submitted_count=pref_count,
        picks_per_round=ppr,
        required_preference_count=required_preference_count,
        deadline=deadline_dt,
    )


@router.get("/{league_id}/playoff/my-picks", response_model=list[PlayoffTournamentPickOut])
def get_my_playoff_picks(
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_member),
    current_user: User = Depends(get_current_user),
    season: Season = Depends(get_active_season),
    db: Session = Depends(get_db),
):
    """
    Returns the current user's playoff picks per tournament, for the MyPicks history page.
    Own picks are never hidden (no R1 tee time check here).
    """
    league, _ = league_and_member

    config = db.query(PlayoffConfig).filter_by(league_id=league.id, season_id=season.id).first()
    if not config:
        return []

    # All pod members for this user across all rounds in this config
    pod_members = (
        db.query(PlayoffPodMember)
        .join(PlayoffPod, PlayoffPodMember.pod_id == PlayoffPod.id)
        .join(PlayoffRound, PlayoffPod.playoff_round_id == PlayoffRound.id)
        .filter(
            PlayoffRound.playoff_config_id == config.id,
            PlayoffPodMember.user_id == current_user.id,
        )
        .options(
            joinedload(PlayoffPodMember.pod).joinedload(PlayoffPod.playoff_round),
        )
        .all()
    )

    result = []
    for pm in pod_members:
        round_obj = pm.pod.playoff_round
        if not round_obj.tournament_id:
            continue

        picks = (
            db.query(PlayoffPick)
            .filter_by(pod_member_id=pm.id)
            .options(joinedload(PlayoffPick.golfer))
            .order_by(PlayoffPick.draft_slot)
            .all()
        )

        result.append(PlayoffTournamentPickOut(
            tournament_id=round_obj.tournament_id,
            round_number=round_obj.round_number,
            status=round_obj.status,
            picks=[
                PlayoffPickSummary(
                    golfer_name=p.golfer.name,
                    points_earned=p.points_earned,
                )
                for p in picks
            ],
            total_points=pm.total_points,
        ))

    result.sort(key=lambda x: x.round_number)
    return result


# ---------------------------------------------------------------------------
# Revise pick (manager)
# ---------------------------------------------------------------------------


@router.patch("/{league_id}/playoff/picks/{pick_id}", response_model=PlayoffPickOut)
def revise_playoff_pick(
    pick_id: uuid.UUID,
    body: PlayoffPickRevise,
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_manager),
    db: Session = Depends(get_db),
):
    """Change the golfer on an existing playoff pick (manager only)."""
    from app.models import Golfer

    league, _ = league_and_member

    pick = (
        db.query(PlayoffPick)
        .options(
            joinedload(PlayoffPick.pod)
            .joinedload(PlayoffPod.playoff_round)
            .joinedload(PlayoffRound.playoff_config),
            joinedload(PlayoffPick.golfer),
        )
        .filter_by(id=pick_id)
        .first()
    )
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")

    if pick.pod.playoff_round.playoff_config.league_id != league.id:
        raise HTTPException(status_code=403, detail="Pick does not belong to this league")

    golfer = db.query(Golfer).filter_by(id=body.golfer_id).first()
    if not golfer:
        raise HTTPException(status_code=404, detail="Golfer not found")

    # No duplicate golfer within same pod
    conflict = (
        db.query(PlayoffPick)
        .filter(
            PlayoffPick.pod_id == pick.pod_id,
            PlayoffPick.golfer_id == body.golfer_id,
            PlayoffPick.id != pick_id,
        )
        .first()
    )
    if conflict:
        raise HTTPException(status_code=422, detail=f"{golfer.name} is already picked in this pod")

    pick.golfer_id = body.golfer_id
    pick.golfer = golfer
    pick.points_earned = None  # Reset — re-score will recalculate
    db.commit()
    db.refresh(pick)
    return _build_pick_out(pick)
