"""
Admin router — /admin/*

Platform-admin-only endpoints locked behind `require_platform_admin`.
Regular users and league admins cannot access these routes.

Endpoints:
  POST /admin/sync              Full sync for the current calendar year
  POST /admin/sync/{pga_tour_id}  Sync a single tournament by its ESPN event ID
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_platform_admin
from app.models import Tournament, User
from app.services.scraper import full_sync, sync_tournament

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/sync")
def trigger_full_sync(
    year: int | None = None,
    force: bool = Query(False, description="When true, delete all existing round data before re-syncing each tournament"),
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """
    Trigger a full PGA Tour data sync.

    Fetches the schedule for the given year (defaults to the current calendar
    year), upserts tournaments, then syncs fields and results for every
    in-progress or completed tournament.

    force=false (default): upsert only — existing round data is updated where
    new data is available but never cleared.
    force=true: delete all TournamentEntryRound rows for each tournament first,
    then re-fetch everything from ESPN.

    This runs the same logic as the daily scheduled job, so it's safe to call
    at any time. All upserts are idempotent.
    """
    target_year = year or date.today().year
    try:
        result = full_sync(db, target_year, force=force)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Sync failed: {exc}") from exc

    return result


@router.post("/sync/{pga_tour_id}")
def trigger_tournament_sync(
    pga_tour_id: str,
    force: bool = Query(False, description="When true, delete all existing round data before re-syncing"),
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """
    Sync a single tournament by its ESPN event ID (our pga_tour_id).

    force=false (default): upsert — only update fields where new data is available.
    force=true: delete all TournamentEntryRound rows for this tournament first,
    then re-fetch everything from ESPN. Use this when cached data is stale or wrong.
    """
    tournament = db.query(Tournament).filter_by(pga_tour_id=pga_tour_id).first()
    if not tournament:
        raise HTTPException(
            status_code=404,
            detail=f"Tournament '{pga_tour_id}' not found. Run /admin/sync first to populate the schedule.",
        )

    try:
        result = sync_tournament(db, pga_tour_id, force=force)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Sync failed: {exc}") from exc

    return result
