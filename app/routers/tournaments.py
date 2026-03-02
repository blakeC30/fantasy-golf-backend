"""
Tournaments router — /tournaments/*

Tournaments are global (not league-specific) — the same PGA Tour events appear
in all leagues. Data is populated by the scraper (Phase 3).

Endpoints:
  GET /tournaments              List tournaments (filterable by status)
  GET /tournaments/{id}         Get a single tournament
  GET /tournaments/{id}/field   Golfers entered in the tournament (for pick form)
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.dependencies import get_current_user
from app.models import Golfer, Tournament, TournamentEntry, TournamentStatus, User
from app.schemas.golfer import GolferOut
from app.schemas.tournament import TournamentOut

router = APIRouter(prefix="/tournaments", tags=["tournaments"])


@router.get("", response_model=list[TournamentOut])
def list_tournaments(
    status: str | None = Query(default=None, description="Filter by status: scheduled, in_progress, completed"),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    List tournaments, optionally filtered by status.

    Default (no filter) returns all tournaments sorted by start_date descending
    so the most recent/upcoming appear first.
    """
    query = db.query(Tournament)

    if status is not None:
        valid = {s.value for s in TournamentStatus}
        if status not in valid:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status '{status}'. Must be one of: {', '.join(valid)}",
            )
        query = query.filter(Tournament.status == status)

    return query.order_by(Tournament.start_date.desc()).all()


@router.get("/{tournament_id}", response_model=TournamentOut)
def get_tournament(
    tournament_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tournament = db.query(Tournament).filter_by(id=tournament_id).first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")
    return tournament


@router.get("/{tournament_id}/field", response_model=list[GolferOut])
def get_tournament_field(
    tournament_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return the golfers entered in a tournament's field.

    Used by the pick form to show which golfers are available to pick.
    Sorted by world_ranking (ascending — lower rank = better player).
    """
    tournament = db.query(Tournament).filter_by(id=tournament_id).first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")

    entries = (
        db.query(TournamentEntry)
        .filter_by(tournament_id=tournament_id)
        .options(joinedload(TournamentEntry.golfer))
        .join(TournamentEntry.golfer)
        .order_by(Golfer.world_ranking.asc().nulls_last())
        .all()
    )
    return [e.golfer for e in entries]
