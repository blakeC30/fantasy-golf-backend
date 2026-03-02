"""
Admin router — /admin/*

Platform-admin-only endpoints. These are locked behind `require_platform_admin`
and are not accessible to regular users or league admins.

Endpoints:
  POST /admin/sync   Manually trigger a PGA Tour data sync (stub — Phase 3)
"""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import require_platform_admin
from app.models import User

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/sync")
def trigger_sync(_: User = Depends(require_platform_admin)):
    """
    Manually trigger a PGA Tour data sync.

    Implemented in Phase 3 when the scraper service is built.
    """
    raise HTTPException(
        status_code=501,
        detail="Scraper not yet implemented — coming in Phase 3",
    )
