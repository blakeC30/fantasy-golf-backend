"""
FastAPI dependency functions.

Dependencies are reusable "building blocks" injected into route handlers via
`Depends(...)`. FastAPI runs them before the route, handles errors they raise,
and caches results within a single request (so a dependency called from two
places only executes once per request).

Dependency chain for a protected league-manager route:
  route
    └── require_league_manager(league_id, db, current_user)
          └── require_league_member(league_id, db, current_user)
                └── get_current_user(token, db)
                      └── get_db()
"""

import uuid

from fastapi import Cookie, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import League, LeagueMember, LeagueMemberRole, LeagueMemberStatus, Season, User
from app.services.auth import decode_access_token

# HTTPBearer extracts "Bearer <token>" from the Authorization header.
# auto_error=False lets us return a cleaner 401 instead of FastAPI's default.
_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    """
    Validate the JWT access token from the Authorization header.
    Returns the authenticated User or raises HTTP 401.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        payload = decode_access_token(credentials.credentials)
        user_id = uuid.UUID(payload["sub"])
    except (JWTError, ValueError, KeyError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User account not found")

    return user


def require_platform_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """Allow only platform admins (can trigger scraper syncs, manage tournaments)."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin access required")
    return current_user


def get_league_or_404(league_id: uuid.UUID, db: Session = Depends(get_db)) -> League:
    """
    Look up a league by its UUID primary key.

    FastAPI automatically injects `league_id` from the route path parameter
    (e.g. `/leagues/{league_id}/...`). This dependency is reused by member and
    admin checks so the league is only fetched once per request.
    """
    league = db.query(League).filter_by(id=league_id).first()
    if not league:
        raise HTTPException(status_code=404, detail=f"League '{league_id}' not found")
    return league


def require_league_member(
    league: League = Depends(get_league_or_404),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> tuple[League, LeagueMember]:
    """
    Verify the current user is a member of the league.
    Returns (league, membership) for use in route handlers.
    """
    membership = (
        db.query(LeagueMember)
        .filter_by(
            league_id=league.id,
            user_id=current_user.id,
            status=LeagueMemberStatus.APPROVED.value,
        )
        .first()
    )
    if not membership:
        # Check if there's a pending request so we can give a clearer error.
        pending = db.query(LeagueMember).filter_by(
            league_id=league.id, user_id=current_user.id
        ).first()
        if pending:
            raise HTTPException(
                status_code=403,
                detail="Your join request is pending league manager approval",
            )
        raise HTTPException(status_code=403, detail="You are not a member of this league")
    return league, membership


def require_league_manager(
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_member),
) -> tuple[League, LeagueMember]:
    """
    Verify the current user is a league manager.
    Chains on require_league_member so the membership is checked first.
    """
    league, membership = league_and_member
    if membership.role != LeagueMemberRole.MANAGER.value:
        raise HTTPException(status_code=403, detail="League manager access required")
    return league, membership


def get_active_season(
    league: League = Depends(get_league_or_404),
    db: Session = Depends(get_db),
) -> Season:
    """Return the active season for the league, or 404 if none exists."""
    season = db.query(Season).filter_by(league_id=league.id, is_active=True).first()
    if not season:
        raise HTTPException(status_code=404, detail="No active season for this league")
    return season


def get_refresh_token_user(
    refresh_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User:
    """
    Validate the refresh token from the httpOnly cookie.
    Used exclusively by the POST /auth/refresh endpoint.
    """
    from app.services.auth import decode_refresh_token

    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token provided")

    try:
        payload = decode_refresh_token(refresh_token)
        user_id = uuid.UUID(payload["sub"])
    except (JWTError, ValueError, KeyError):
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User account not found")

    return user
