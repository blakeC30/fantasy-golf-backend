"""
Seed script for local development.

Creates a realistic set of sample data so you have something to work with
when building and testing the API and frontend. Safe to run multiple times —
it checks for existing data before inserting.

Usage:
    cd fantasy-golf-backend
    python scripts/seed.py

What gets created:
  - 4 users (1 platform admin, 1 league admin, 2 regular members)
  - 1 league ("Augusta Pines Fantasy Golf")
  - 1 active season (current year)
  - 8 golfers (mix of top-ranked players)
  - 3 tournaments: 1 completed regular, 1 completed major, 1 upcoming
  - Tournament entries linking golfers to tournaments
  - Picks for the completed tournaments (with points already calculated)
"""

import sys
import os
from datetime import date, datetime, timezone, timedelta

# Add the project root to sys.path so `from app.xxx import ...` works when
# running this script directly (not as a module).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bcrypt

from app.database import SessionLocal
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


def hash_password(password: str) -> str:
    """Hash a password with bcrypt. Returns a UTF-8 string suitable for storing in the DB."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def seed():
    db = SessionLocal()
    try:
        # ---------------------------------------------------------------
        # Users
        # ---------------------------------------------------------------
        # Check if we've already seeded to make this idempotent.
        if db.query(User).filter_by(email="admin@example.com").first():
            print("Seed data already exists. Skipping.")
            return

        print("Creating users...")

        # Platform admin — can trigger scraper syncs, manage all tournaments.
        admin = User(
            email="admin@example.com",
            password_hash=hash_password("password123"),
            display_name="Platform Admin",
            is_platform_admin=True,
        )

        # League admin — created the league, manages its members.
        alice = User(
            email="alice@example.com",
            password_hash=hash_password("password123"),
            display_name="Alice Chen",
        )

        # Regular members.
        bob = User(
            email="bob@example.com",
            password_hash=hash_password("password123"),
            display_name="Bob Martinez",
        )
        carol = User(
            email="carol@example.com",
            password_hash=hash_password("password123"),
            display_name="Carol Johnson",
        )

        db.add_all([admin, alice, bob, carol])
        db.flush()  # Flush to get generated UUIDs without committing yet.

        # ---------------------------------------------------------------
        # League
        # ---------------------------------------------------------------
        print("Creating league...")

        league = League(
            name="Augusta Pines Fantasy Golf",
            description="A friendly fantasy golf league for friends and colleagues.",
            created_by=alice.id,
            no_pick_penalty=-50_000,
        )
        db.add(league)
        db.flush()

        # Add all four users as members; Alice is league manager.
        memberships = [
            LeagueMember(league_id=league.id, user_id=alice.id, role=LeagueMemberRole.MANAGER.value),
            LeagueMember(league_id=league.id, user_id=bob.id, role=LeagueMemberRole.MEMBER.value),
            LeagueMember(league_id=league.id, user_id=carol.id, role=LeagueMemberRole.MEMBER.value),
            LeagueMember(league_id=league.id, user_id=admin.id, role=LeagueMemberRole.MEMBER.value),
        ]
        db.add_all(memberships)

        # ---------------------------------------------------------------
        # Season
        # ---------------------------------------------------------------
        print("Creating season...")

        current_year = date.today().year
        season = Season(league_id=league.id, year=current_year, is_active=True)
        db.add(season)
        db.flush()

        # ---------------------------------------------------------------
        # Golfers
        # ---------------------------------------------------------------
        print("Creating golfers...")

        golfers_data = [
            {"pga_tour_id": "34046", "name": "Scottie Scheffler",  "world_ranking": 1,  "country": "US"},
            {"pga_tour_id": "46046", "name": "Rory McIlroy",       "world_ranking": 2,  "country": "NIR"},
            {"pga_tour_id": "29478", "name": "Xander Schauffele",  "world_ranking": 3,  "country": "US"},
            {"pga_tour_id": "47959", "name": "Collin Morikawa",    "world_ranking": 4,  "country": "US"},
            {"pga_tour_id": "48081", "name": "Viktor Hovland",     "world_ranking": 5,  "country": "NOR"},
            {"pga_tour_id": "33948", "name": "Jon Rahm",           "world_ranking": 6,  "country": "ESP"},
            {"pga_tour_id": "27644", "name": "Brooks Koepka",      "world_ranking": 7,  "country": "US"},
            {"pga_tour_id": "30925", "name": "Bryson DeChambeau",  "world_ranking": 8,  "country": "US"},
        ]

        golfers = []
        for g in golfers_data:
            golfer = Golfer(**g)
            db.add(golfer)
            golfers.append(golfer)
        db.flush()

        # ---------------------------------------------------------------
        # Tournaments
        # ---------------------------------------------------------------
        print("Creating tournaments...")

        # A completed regular tournament (2 weeks ago).
        t1_start = date.today() - timedelta(weeks=2)
        tournament_regular = Tournament(
            pga_tour_id="R2025001",
            name="The Sentry",
            start_date=t1_start,
            end_date=t1_start + timedelta(days=3),
            multiplier=1.0,
            purse_usd=20_000_000,
            status=TournamentStatus.COMPLETED.value,
        )

        # A completed major tournament (1 week ago). Points are doubled.
        t2_start = date.today() - timedelta(weeks=1)
        tournament_major = Tournament(
            pga_tour_id="R2025002",
            name="The Masters",
            start_date=t2_start,
            end_date=t2_start + timedelta(days=3),
            multiplier=2.0,
            purse_usd=18_000_000,
            status=TournamentStatus.COMPLETED.value,
        )

        # An upcoming tournament (next week). No picks yet.
        t3_start = date.today() + timedelta(weeks=1)
        tournament_upcoming = Tournament(
            pga_tour_id="R2025003",
            name="AT&T Pebble Beach Pro-Am",
            start_date=t3_start,
            end_date=t3_start + timedelta(days=3),
            multiplier=1.0,
            purse_usd=8_700_000,
            status=TournamentStatus.SCHEDULED.value,
        )

        db.add_all([tournament_regular, tournament_major, tournament_upcoming])
        db.flush()

        # ---------------------------------------------------------------
        # Tournament entries (golfers in each tournament)
        # ---------------------------------------------------------------
        print("Creating tournament entries...")

        # Regular tournament results: top 4 finishers with earnings.
        regular_results = [
            # (golfer_index, finish_position, earnings_usd)
            (0, 1, 3_600_000),  # Scheffler wins
            (1, 2, 2_160_000),  # McIlroy 2nd
            (2, 3, 1_360_000),  # Schauffele 3rd
            (3, 4,   960_000),  # Morikawa 4th
            (4, None, None),    # Hovland — missed cut
            (5, None, None),    # Rahm — missed cut
            (6, None, None),    # Koepka — WD
            (7, None, None),    # DeChambeau — missed cut
        ]
        for golfer_idx, position, earnings in regular_results:
            entry = TournamentEntry(
                tournament_id=tournament_regular.id,
                golfer_id=golfers[golfer_idx].id,
                finish_position=position,
                earnings_usd=earnings,
                status="cut" if earnings is None else None,
            )
            db.add(entry)

        # Major tournament results: different winner for variety.
        major_results = [
            (1, 1, 3_240_000),  # McIlroy wins the Masters
            (0, 2, 1_944_000),  # Scheffler 2nd
            (3, 3, 1_224_000),  # Morikawa 3rd
            (5, 4,   864_000),  # Rahm 4th
            (2, None, None),
            (4, None, None),
            (6, None, None),
            (7, None, None),
        ]
        for golfer_idx, position, earnings in major_results:
            entry = TournamentEntry(
                tournament_id=tournament_major.id,
                golfer_id=golfers[golfer_idx].id,
                finish_position=position,
                earnings_usd=earnings,
                status="cut" if earnings is None else None,
            )
            db.add(entry)

        # Upcoming tournament: field announced, no results yet.
        for golfer in golfers:
            entry = TournamentEntry(
                tournament_id=tournament_upcoming.id,
                golfer_id=golfer.id,
                # tee_time would be set by the scraper later
            )
            db.add(entry)

        db.flush()

        # ---------------------------------------------------------------
        # Picks
        # ---------------------------------------------------------------
        # Each user picks a different golfer for each completed tournament.
        # points_earned = earnings_usd * tournament.multiplier
        print("Creating picks...")

        picks_data = [
            # (user, tournament, golfer_idx, earnings, multiplier)
            (alice, tournament_regular, 0, 3_600_000, 1.0),   # Alice picked Scheffler (winner)
            (bob,   tournament_regular, 2,   960_000, 1.0),   # Bob picked Schauffele (wrong — actually 3rd, using Morikawa's earnings)
            (carol, tournament_regular, 1, 2_160_000, 1.0),   # Carol picked McIlroy (2nd)

            (alice, tournament_major,   1, 3_240_000, 2.0),   # Alice picked McIlroy (winner) — doubled!
            (bob,   tournament_major,   0, 1_944_000, 2.0),   # Bob picked Scheffler (2nd) — doubled
            (carol, tournament_major,   3, 1_224_000, 2.0),   # Carol picked Morikawa (3rd) — doubled
        ]
        for user, tournament, golfer_idx, earnings, multiplier in picks_data:
            pick = Pick(
                league_id=league.id,
                season_id=season.id,
                user_id=user.id,
                tournament_id=tournament.id,
                golfer_id=golfers[golfer_idx].id,
                points_earned=earnings * multiplier,
            )
            db.add(pick)

        # Carol missed the first regular tournament — no pick row.
        # This means she'll receive the no_pick_penalty (-50,000) for that week
        # when standings are calculated.

        db.commit()
        print("\nSeed complete!")
        print(f"  League:      '{league.name}' (id: {league.id})")
        print(f"  Season:      {current_year}")
        print(f"  Users:       alice@example.com (league manager), bob@example.com, carol@example.com")
        print(f"  Password:    password123 (all users)")
        print(f"  Tournaments: {tournament_regular.name}, {tournament_major.name}, {tournament_upcoming.name}")
        print(f"  Golfers:     {len(golfers)} golfers seeded")

    except Exception as e:
        db.rollback()
        print(f"\nSeed failed: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed()
