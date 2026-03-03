"""
Users router — /users/*

Endpoints:
  GET   /users/me         Return the current user's profile
  PATCH /users/me         Update display name
  GET   /users/me/leagues Return all leagues the current user belongs to
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.dependencies import get_current_user
from app.models import League, LeagueMember, LeagueMemberStatus, User
from app.schemas.league import LeagueOut
from app.schemas.user import UserOut, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.patch("/me", response_model=UserOut)
def update_me(
    body: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.display_name is not None:
        current_user.display_name = body.display_name
    db.commit()
    db.refresh(current_user)
    return current_user


@router.get("/me/leagues", response_model=list[LeagueOut])
def get_my_leagues(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return all leagues where the current user is an approved member."""
    memberships = (
        db.query(LeagueMember)
        .filter_by(user_id=current_user.id, status=LeagueMemberStatus.APPROVED.value)
        .options(joinedload(LeagueMember.league))
        .all()
    )
    return [m.league for m in memberships]
