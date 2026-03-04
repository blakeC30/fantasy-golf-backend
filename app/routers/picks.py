"""
Picks router — /leagues/{league_id}/picks/*

Endpoints:
  POST  /leagues/{league_id}/picks                          Submit a pick for the active season
  GET   /leagues/{league_id}/picks/mine                     My picks for the active season
  GET   /leagues/{league_id}/picks                          All picks (completed tournaments only)
  GET   /leagues/{league_id}/picks/tournament/{t_id}        Pick breakdown for one tournament
  PATCH /leagues/{league_id}/picks/{pick_id}                Change the golfer on an existing pick
"""

import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.dependencies import (
    get_active_season,
    get_current_user,
    require_league_member,
)
from app.models import (
    Golfer,
    League,
    LeagueMember,
    LeagueMemberStatus,
    LeagueTournament,
    Pick,
    Season,
    TournamentEntry,
    TournamentStatus,
    User,
)
from app.schemas.pick import PickCreate, PickOut, PickUpdate
from app.services.picks import validate_new_pick, validate_pick_change


# ---------------------------------------------------------------------------
# Response schemas for the tournament picks summary endpoint
# ---------------------------------------------------------------------------

class PickerInfo(BaseModel):
    user_id: str
    display_name: str
    points_earned: float | None


class GolferPickGroup(BaseModel):
    golfer_id: str
    golfer_name: str
    pick_count: int
    pickers: list[PickerInfo]


class NoPicker(BaseModel):
    user_id: str
    display_name: str


class WinnerInfo(BaseModel):
    golfer_name: str
    pick_count: int  # 0 if no league member picked the winner


class TournamentPicksSummary(BaseModel):
    tournament_status: str
    member_count: int
    picks_by_golfer: list[GolferPickGroup]   # sorted by pick_count desc
    no_pick_members: list[NoPicker]
    winner: WinnerInfo | None  # None for non-completed tournaments

router = APIRouter(prefix="/leagues/{league_id}/picks", tags=["picks"])


def _picks_with_relations(query):
    """Eagerly load golfer and tournament so they're available for the schema."""
    return query.options(
        joinedload(Pick.golfer),
        joinedload(Pick.tournament),
    )


@router.post("", response_model=PickOut, status_code=201)
def submit_pick(
    body: PickCreate,
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_member),
    season: Season = Depends(get_active_season),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Submit a pick for an upcoming tournament.

    Validates:
    - Tournament is SCHEDULED and start_date is in the future
    - Golfer is in the tournament field
    - User hasn't picked this golfer this season (no-repeat rule)
    - User doesn't already have a pick for this tournament
    """
    league, _ = league_and_member

    validate_new_pick(
        db,
        league_id=league.id,
        season=season,
        user_id=current_user.id,
        tournament_id=body.tournament_id,
        golfer_id=body.golfer_id,
    )

    pick = Pick(
        league_id=league.id,
        season_id=season.id,
        user_id=current_user.id,
        tournament_id=body.tournament_id,
        golfer_id=body.golfer_id,
    )
    db.add(pick)
    db.commit()

    return (
        _picks_with_relations(db.query(Pick))
        .filter_by(id=pick.id)
        .first()
    )


@router.get("/mine", response_model=list[PickOut])
def get_my_picks(
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_member),
    season: Season = Depends(get_active_season),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the current user's picks for the active season."""
    league, _ = league_and_member
    return (
        _picks_with_relations(
            db.query(Pick).filter_by(
                league_id=league.id,
                season_id=season.id,
                user_id=current_user.id,
            )
        )
        .all()
    )


@router.get("", response_model=list[PickOut])
def get_all_picks(
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_member),
    season: Season = Depends(get_active_season),
    db: Session = Depends(get_db),
):
    """
    Return all picks for completed tournaments in the active season.

    Picks for in-progress or upcoming tournaments are withheld to prevent
    members from copying each other's choices.
    """
    league, _ = league_and_member
    return (
        _picks_with_relations(
            db.query(Pick)
            .filter_by(league_id=league.id, season_id=season.id)
            .join(Pick.tournament)
            .filter_by(status=TournamentStatus.COMPLETED.value)
        )
        .all()
    )


@router.get("/tournament/{tournament_id}", response_model=TournamentPicksSummary)
def get_tournament_picks_summary(
    tournament_id: uuid.UUID,
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_member),
    db: Session = Depends(get_db),
):
    """
    Return pick breakdown for a specific tournament.

    Picks are hidden while status=scheduled to prevent copying before the
    tournament begins. Once in_progress or completed, all member picks are
    shown grouped by golfer, along with members who submitted no pick.
    """
    league, _ = league_and_member

    lt = (
        db.query(LeagueTournament)
        .filter_by(league_id=league.id, tournament_id=tournament_id)
        .first()
    )
    if not lt:
        raise HTTPException(404, "Tournament not in this league's schedule")

    tournament = lt.tournament
    if tournament.status == TournamentStatus.SCHEDULED.value:
        raise HTTPException(403, "Picks are revealed once the tournament begins")

    picks = (
        db.query(Pick)
        .filter_by(league_id=league.id, tournament_id=tournament_id)
        .options(joinedload(Pick.golfer), joinedload(Pick.user))
        .all()
    )

    members = (
        db.query(LeagueMember)
        .filter_by(league_id=league.id, status=LeagueMemberStatus.APPROVED.value)
        .options(joinedload(LeagueMember.user))
        .all()
    )

    golfer_map: dict[str, dict] = defaultdict(
        lambda: {"golfer_id": None, "golfer_name": None, "pickers": []}
    )
    picker_ids: set[uuid.UUID] = set()

    for pick in picks:
        gid = str(pick.golfer_id)
        golfer_map[gid]["golfer_id"] = gid
        golfer_map[gid]["golfer_name"] = pick.golfer.name
        golfer_map[gid]["pickers"].append(
            PickerInfo(
                user_id=str(pick.user_id),
                display_name=pick.user.display_name,
                points_earned=pick.points_earned,
            )
        )
        picker_ids.add(pick.user_id)

    picks_by_golfer = sorted(
        [
            GolferPickGroup(
                golfer_id=v["golfer_id"],
                golfer_name=v["golfer_name"],
                pick_count=len(v["pickers"]),
                pickers=v["pickers"],
            )
            for v in golfer_map.values()
        ],
        key=lambda g: -g.pick_count,
    )

    no_pick_members = [
        NoPicker(user_id=str(m.user_id), display_name=m.user.display_name)
        for m in members
        if m.user_id not in picker_ids
    ]

    # For completed tournaments, find the actual winner (finish_position=1)
    winner: WinnerInfo | None = None
    if tournament.status == TournamentStatus.COMPLETED.value:
        top_entry = (
            db.query(TournamentEntry)
            .filter_by(tournament_id=tournament_id, finish_position=1)
            .options(joinedload(TournamentEntry.golfer))
            .first()
        )
        if top_entry:
            pick_count = sum(
                1 for g in picks_by_golfer
                if g.golfer_id == str(top_entry.golfer_id)
            )
            winner = WinnerInfo(
                golfer_name=top_entry.golfer.name,
                pick_count=pick_count,
            )

    return TournamentPicksSummary(
        tournament_status=tournament.status,
        member_count=len(members),
        picks_by_golfer=picks_by_golfer,
        no_pick_members=no_pick_members,
        winner=winner,
    )


@router.patch("/{pick_id}", response_model=PickOut)
def change_pick(
    pick_id: uuid.UUID,
    body: PickUpdate,
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_member),
    season: Season = Depends(get_active_season),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Change the golfer on an existing pick.

    The pick must belong to the current user. Lock rules:
    - SCHEDULED: allowed until tournament.start_date
    - IN_PROGRESS: allowed until the new golfer's tee_time passes
    - COMPLETED: never allowed
    """
    league, _ = league_and_member

    pick = (
        _picks_with_relations(db.query(Pick))
        .filter_by(id=pick_id, league_id=league.id, user_id=current_user.id)
        .first()
    )
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")

    validate_pick_change(
        db,
        pick=pick,
        new_golfer_id=body.golfer_id,
        season=season,
        league_id=league.id,
        user_id=current_user.id,
    )

    pick.golfer_id = body.golfer_id
    db.commit()

    return (
        _picks_with_relations(db.query(Pick))
        .filter_by(id=pick.id)
        .first()
    )
