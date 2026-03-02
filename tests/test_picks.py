"""
Tests for pick submission and validation.

These test the full HTTP flow through the picks router.
Requires docker compose up postgres -d and a seeded database.
"""

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models import (
    Golfer,
    League,
    LeagueMember,
    LeagueMemberRole,
    Pick,
    Season,
    Tournament,
    TournamentEntry,
    TournamentStatus,
    User,
)
from app.services.auth import hash_password


# ---------------------------------------------------------------------------
# Helpers to create test fixtures directly in the DB
# ---------------------------------------------------------------------------

def make_user(db: Session, email: str, display_name: str = "Test") -> User:
    user = User(email=email, password_hash=hash_password("pass"), display_name=display_name)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_league(db: Session, creator: User) -> tuple[League, Season]:
    league = League(
        name="Test League",
        slug=f"test-{uuid.uuid4().hex[:8]}",
        created_by=creator.id,
    )
    db.add(league)
    db.flush()  # flush so league.id is populated before we reference it below

    db.add(LeagueMember(league_id=league.id, user_id=creator.id, role=LeagueMemberRole.ADMIN.value))
    season = Season(league_id=league.id, year=date.today().year, is_active=True)
    db.add(season)
    db.commit()
    db.refresh(league)
    db.refresh(season)
    return league, season


def make_golfer(db: Session, name: str = "Test Golfer") -> Golfer:
    golfer = Golfer(pga_tour_id=f"T{uuid.uuid4().hex[:6]}", name=name)
    db.add(golfer)
    db.commit()
    db.refresh(golfer)
    return golfer


def make_tournament(db: Session, days_from_now: int = 3) -> Tournament:
    start = date.today() + timedelta(days=days_from_now)
    t = Tournament(
        pga_tour_id=f"R{uuid.uuid4().hex[:6]}",
        name="Test Open",
        start_date=start,
        end_date=start + timedelta(days=3),
        status=TournamentStatus.SCHEDULED.value,
        multiplier=1.0,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def add_golfer_to_tournament(db: Session, tournament: Tournament, golfer: Golfer) -> TournamentEntry:
    entry = TournamentEntry(tournament_id=tournament.id, golfer_id=golfer.id)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSubmitPick:
    def test_submit_pick_success(self, client, auth_headers, db):
        user = db.query(User).filter_by(email="test@example.com").first()
        league, season = make_league(db, user)
        golfer = make_golfer(db)
        tournament = make_tournament(db, days_from_now=7)
        add_golfer_to_tournament(db, tournament, golfer)

        resp = client.post(
            f"/api/v1/leagues/{league.slug}/picks",
            headers=auth_headers,
            json={"tournament_id": str(tournament.id), "golfer_id": str(golfer.id)},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["golfer_id"] == str(golfer.id)
        assert data["tournament_id"] == str(tournament.id)
        assert data["points_earned"] is None  # Not set until tournament ends

    def test_cannot_pick_same_golfer_twice_per_season(self, client, auth_headers, db):
        user = db.query(User).filter_by(email="test@example.com").first()
        league, season = make_league(db, user)
        golfer = make_golfer(db)

        t1 = make_tournament(db, days_from_now=7)
        t2 = make_tournament(db, days_from_now=14)
        add_golfer_to_tournament(db, t1, golfer)
        add_golfer_to_tournament(db, t2, golfer)

        # First pick succeeds.
        resp1 = client.post(
            f"/api/v1/leagues/{league.slug}/picks",
            headers=auth_headers,
            json={"tournament_id": str(t1.id), "golfer_id": str(golfer.id)},
        )
        assert resp1.status_code == 201

        # Same golfer for a different tournament should fail.
        resp2 = client.post(
            f"/api/v1/leagues/{league.slug}/picks",
            headers=auth_headers,
            json={"tournament_id": str(t2.id), "golfer_id": str(golfer.id)},
        )
        assert resp2.status_code == 400
        assert "already picked" in resp2.json()["detail"].lower()

    def test_cannot_pick_after_deadline(self, client, auth_headers, db):
        user = db.query(User).filter_by(email="test@example.com").first()
        league, season = make_league(db, user)
        golfer = make_golfer(db)

        # Tournament that started yesterday.
        past_tournament = make_tournament(db, days_from_now=-1)
        add_golfer_to_tournament(db, past_tournament, golfer)

        resp = client.post(
            f"/api/v1/leagues/{league.slug}/picks",
            headers=auth_headers,
            json={"tournament_id": str(past_tournament.id), "golfer_id": str(golfer.id)},
        )
        assert resp.status_code == 400
        assert "deadline" in resp.json()["detail"].lower()

    def test_cannot_pick_golfer_not_in_tournament(self, client, auth_headers, db):
        user = db.query(User).filter_by(email="test@example.com").first()
        league, season = make_league(db, user)
        golfer = make_golfer(db)
        tournament = make_tournament(db, days_from_now=7)
        # Intentionally NOT adding golfer to tournament.

        resp = client.post(
            f"/api/v1/leagues/{league.slug}/picks",
            headers=auth_headers,
            json={"tournament_id": str(tournament.id), "golfer_id": str(golfer.id)},
        )
        assert resp.status_code == 400
        assert "not entered" in resp.json()["detail"].lower()

    def test_non_member_cannot_pick(self, client, db):
        """A user who is not a league member gets 403."""
        creator = make_user(db, "creator@example.com")
        league, _ = make_league(db, creator)
        golfer = make_golfer(db)
        tournament = make_tournament(db, days_from_now=7)
        add_golfer_to_tournament(db, tournament, golfer)

        # Register and login as a different user.
        client.post("/api/v1/auth/register", json={
            "email": "outsider@example.com",
            "password": "pass",
            "display_name": "Outsider",
        })
        login = client.post("/api/v1/auth/login", json={
            "email": "outsider@example.com",
            "password": "pass",
        })
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        resp = client.post(
            f"/api/v1/leagues/{league.slug}/picks",
            headers=headers,
            json={"tournament_id": str(tournament.id), "golfer_id": str(golfer.id)},
        )
        assert resp.status_code == 403
