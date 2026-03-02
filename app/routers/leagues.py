"""
Leagues router — /leagues/*

Endpoints:
  POST  /leagues                              Create a new league
  GET   /leagues/{slug}                       Get league details
  POST  /leagues/{slug}/join                  Join a league (authenticated users)
  GET   /leagues/{slug}/members               List all members
  PATCH /leagues/{slug}/members/{user_id}/role  Change a member's role (admin only)
  DELETE /leagues/{slug}/members/{user_id}    Remove a member (admin only)
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.dependencies import get_current_user, require_league_admin, require_league_member
from app.models import League, LeagueMember, LeagueMemberRole, Season, User
from app.schemas.league import LeagueCreate, LeagueMemberOut, LeagueOut, RoleUpdate

router = APIRouter(prefix="/leagues", tags=["leagues"])


@router.post("", response_model=LeagueOut, status_code=201)
def create_league(
    body: LeagueCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Create a new league. The creator is automatically made an admin and the
    first member. An active season for the current calendar year is created.
    """
    if db.query(League).filter_by(slug=body.slug).first():
        raise HTTPException(status_code=409, detail=f"A league with slug '{body.slug}' already exists")

    import datetime

    league = League(
        name=body.name,
        slug=body.slug,
        description=body.description,
        no_pick_penalty=body.no_pick_penalty,
        created_by=current_user.id,
    )
    db.add(league)
    db.flush()  # Get the league ID before adding related objects.

    # Auto-add creator as league admin.
    membership = LeagueMember(
        league_id=league.id,
        user_id=current_user.id,
        role=LeagueMemberRole.ADMIN.value,
    )
    db.add(membership)

    # Create the initial season for the current year.
    season = Season(league_id=league.id, year=datetime.date.today().year, is_active=True)
    db.add(season)

    db.commit()
    db.refresh(league)
    return league


@router.get("/{slug}", response_model=LeagueOut)
def get_league(
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_member),
):
    """Return league details. Requires membership."""
    league, _ = league_and_member
    return league


@router.post("/{slug}/join", response_model=LeagueMemberOut, status_code=201)
def join_league(
    slug: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Join a league by its slug.

    Any authenticated user can join. League admins can later remove members
    or restrict this via application-level invite codes (future feature).
    """
    league = db.query(League).filter_by(slug=slug).first()
    if not league:
        raise HTTPException(status_code=404, detail=f"League '{slug}' not found")

    existing = db.query(LeagueMember).filter_by(
        league_id=league.id, user_id=current_user.id
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="You are already a member of this league")

    membership = LeagueMember(
        league_id=league.id,
        user_id=current_user.id,
        role=LeagueMemberRole.MEMBER.value,
    )
    db.add(membership)
    db.commit()
    db.refresh(membership)

    # Load the user relationship for the response schema.
    membership.user = current_user
    return membership


@router.get("/{slug}/members", response_model=list[LeagueMemberOut])
def list_members(
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_member),
    db: Session = Depends(get_db),
):
    """List all members of a league. Requires membership."""
    league, _ = league_and_member
    return (
        db.query(LeagueMember)
        .filter_by(league_id=league.id)
        .options(joinedload(LeagueMember.user))
        .all()
    )


@router.patch("/{slug}/members/{user_id}/role", response_model=LeagueMemberOut)
def update_member_role(
    user_id: uuid.UUID,
    body: RoleUpdate,
    league_and_admin: tuple[League, LeagueMember] = Depends(require_league_admin),
    db: Session = Depends(get_db),
):
    """Change a member's role. Requires league admin."""
    league, _ = league_and_admin

    if body.role not in (LeagueMemberRole.ADMIN.value, LeagueMemberRole.MEMBER.value):
        raise HTTPException(status_code=400, detail="Role must be 'admin' or 'member'")

    membership = (
        db.query(LeagueMember)
        .filter_by(league_id=league.id, user_id=user_id)
        .options(joinedload(LeagueMember.user))
        .first()
    )
    if not membership:
        raise HTTPException(status_code=404, detail="Member not found")

    membership.role = body.role
    db.commit()
    db.refresh(membership)
    return membership


@router.delete("/{slug}/members/{user_id}", status_code=204)
def remove_member(
    user_id: uuid.UUID,
    league_and_admin: tuple[League, LeagueMember] = Depends(require_league_admin),
    db: Session = Depends(get_db),
):
    """Remove a member from a league. Requires league admin."""
    league, admin_membership = league_and_admin

    if user_id == admin_membership.user_id:
        raise HTTPException(status_code=400, detail="You cannot remove yourself from the league")

    membership = db.query(LeagueMember).filter_by(
        league_id=league.id, user_id=user_id
    ).first()
    if not membership:
        raise HTTPException(status_code=404, detail="Member not found")

    db.delete(membership)
    db.commit()
