"""
Admin router — /admin/*

Platform-admin-only endpoints locked behind `require_platform_admin`.
Regular users and league admins cannot access these routes.

Endpoints:
  POST /admin/sync              Full sync for the current calendar year
  POST /admin/sync/{pga_tour_id}  Sync a single tournament by its ESPN event ID
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_platform_admin
from app.models import Tournament, User
from app.services.scraper import full_sync, sync_tournament

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/sync")
def trigger_full_sync(
    year: int | None = None,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """
    Trigger a full PGA Tour data sync.

    Fetches the schedule for the given year (defaults to the current calendar
    year), upserts tournaments, then syncs fields and results for every
    in-progress or completed tournament.

    This runs the same logic as the daily scheduled job, so it's safe to call
    at any time. All upserts are idempotent.
    """
    target_year = year or date.today().year
    try:
        result = full_sync(db, target_year)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Sync failed: {exc}") from exc

    return result


@router.post("/sync/{pga_tour_id}")
def trigger_tournament_sync(
    pga_tour_id: str,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """
    Sync a single tournament by its ESPN event ID (our pga_tour_id).

    Useful when a specific tournament finishes and you want to immediately
    update results and score picks without waiting for the scheduled job.
    """
    tournament = db.query(Tournament).filter_by(pga_tour_id=pga_tour_id).first()
    if not tournament:
        raise HTTPException(
            status_code=404,
            detail=f"Tournament '{pga_tour_id}' not found. Run /admin/sync first to populate the schedule.",
        )

    try:
        result = sync_tournament(db, pga_tour_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Sync failed: {exc}") from exc

    return result
