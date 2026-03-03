"""
Picks router — /leagues/{league_id}/picks/*

Endpoints:
  POST  /leagues/{league_id}/picks                Submit a pick for the active season
  GET   /leagues/{league_id}/picks/mine           My picks for the active season
  GET   /leagues/{league_id}/picks                All picks (completed tournaments only)
  PATCH /leagues/{league_id}/picks/{pick_id}      Change the golfer on an existing pick
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.dependencies import (
    get_active_season,
    get_current_user,
    require_league_member,
)
from app.models import League, LeagueMember, Pick, Season, TournamentStatus, User
from app.schemas.pick import PickCreate, PickOut, PickUpdate
from app.services.picks import validate_new_pick, validate_pick_change

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
