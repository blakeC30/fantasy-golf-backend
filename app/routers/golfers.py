"""
Golfers router — /golfers/*

Golfers are global records populated by the scraper. The pick form queries
this endpoint to show available golfers for an upcoming tournament.

Endpoints:
  GET /golfers               List/search golfers
  GET /golfers/{id}          Get a single golfer
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models import Golfer, User
from app.schemas.golfer import GolferOut

router = APIRouter(prefix="/golfers", tags=["golfers"])


@router.get("", response_model=list[GolferOut])
def list_golfers(
    search: str | None = Query(default=None, description="Filter by name (case-insensitive substring)"),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    List all golfers, with optional name search.

    Used by the pick form to let users search for a golfer by name.
    Results are sorted by world_ranking (ascending — lower rank = better).
    """
    query = db.query(Golfer)
    if search:
        query = query.filter(Golfer.name.ilike(f"%{search}%"))
    return query.order_by(Golfer.world_ranking.asc().nulls_last()).all()


@router.get("/{golfer_id}", response_model=GolferOut)
def get_golfer(
    golfer_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from fastapi import HTTPException

    golfer = db.query(Golfer).filter_by(id=golfer_id).first()
    if not golfer:
        raise HTTPException(status_code=404, detail="Golfer not found")
    return golfer
