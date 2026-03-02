"""
Tournaments router — /tournaments/*

Tournaments are global (not league-specific) — the same PGA Tour events appear
in all leagues. Data is populated by the scraper (Phase 3).

Endpoints:
  GET /tournaments          List tournaments (filterable by status)
  GET /tournaments/{id}     Get a single tournament with its field
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models import Tournament, TournamentStatus, User
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
