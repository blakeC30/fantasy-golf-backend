"""
Seed script for local development.

Creates realistic sample data using real PGA Tour tournament history that
already exists in the database (scraped data). Safe to re-run — checks for
existing data before inserting.

Usage:
    cd fantasy-golf-backend
    python scripts/seed.py

What gets created:
  - 5 users: Blake (you, manager), Bob, Carol, Dave, Eve
  - 1 league: "Augusta Pines Fantasy Golf"
  - 1 active season (current year)
  - League schedule: last 8 completed tournaments + THE PLAYERS Championship
  - Picks for each member across the 8 completed tournaments (real earnings)
  - No pick for Eve in week 1 (tests no-pick penalty)

Picks use actual golfer earnings from TournamentEntry — no made-up numbers.
"""

import sys
import os
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bcrypt

from app.database import SessionLocal
from app.models import (
    Golfer,
    League,
    LeagueMember,
    LeagueMemberRole,
    LeagueMemberStatus,
    Pick,
    Season,
    Tournament,
    TournamentEntry,
    TournamentStatus,
    User,
)
from app.models.league_tournament import LeagueTournament


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def get_or_create_user(db, email: str, display_name: str, is_platform_admin: bool = False) -> User:
    """Return existing user or create a new one with password123."""
    user = db.query(User).filter_by(email=email).first()
    if not user:
        user = User(
            email=email,
            password_hash=hash_password("password123"),
            display_name=display_name,
            is_platform_admin=is_platform_admin,
        )
        db.add(user)
        db.flush()
        print(f"  Created user: {email}")
    else:
        print(f"  Found existing user: {email}")
    return user


def get_earnings(db, tournament_id, golfer_id) -> float | None:
    """Look up real earnings from TournamentEntry."""
    entry = db.query(TournamentEntry).filter_by(
        tournament_id=tournament_id, golfer_id=golfer_id
    ).first()
    return entry.earnings_usd if entry else None


def seed():
    db = SessionLocal()
    try:
        # ---------------------------------------------------------------
        # Idempotency check
        # ---------------------------------------------------------------
        if db.query(League).filter_by(name="Augusta Pines Fantasy Golf").first():
            print("Augusta Pines league already exists. Skipping.")
            return

        # ---------------------------------------------------------------
        # Users
        # ---------------------------------------------------------------
        print("\nUsers:")
        # Blake is you — use existing Google OAuth account if present
        blake = get_or_create_user(db, "chambersbw@gmail.com", "Blake Chambers")
        bob   = get_or_create_user(db, "bob@example.com",   "Bob Martinez")
        carol = get_or_create_user(db, "carol@example.com", "Carol Johnson")
        dave  = get_or_create_user(db, "dave@example.com",  "Dave Thompson")
        eve   = get_or_create_user(db, "eve@example.com",   "Eve Wilson")
        _     = get_or_create_user(db, "admin@example.com", "Platform Admin", is_platform_admin=True)

        # ---------------------------------------------------------------
        # League
        # ---------------------------------------------------------------
        print("\nLeague:")
        league = League(
            name="Augusta Pines Fantasy Golf",
            description="A friendly fantasy golf league for testing.",
            created_by=blake.id,
            no_pick_penalty=-50_000,
        )
        db.add(league)
        db.flush()
        print(f"  Created: {league.name} (id: {league.id})")

        members = [
            LeagueMember(league_id=league.id, user_id=blake.id, role=LeagueMemberRole.MANAGER.value, status=LeagueMemberStatus.APPROVED.value),
            LeagueMember(league_id=league.id, user_id=bob.id,   role=LeagueMemberRole.MEMBER.value,  status=LeagueMemberStatus.APPROVED.value),
            LeagueMember(league_id=league.id, user_id=carol.id, role=LeagueMemberRole.MEMBER.value,  status=LeagueMemberStatus.APPROVED.value),
            LeagueMember(league_id=league.id, user_id=dave.id,  role=LeagueMemberRole.MEMBER.value,  status=LeagueMemberStatus.APPROVED.value),
            LeagueMember(league_id=league.id, user_id=eve.id,   role=LeagueMemberRole.MEMBER.value,  status=LeagueMemberStatus.APPROVED.value),
        ]
        db.add_all(members)

        # ---------------------------------------------------------------
        # Season
        # ---------------------------------------------------------------
        season = Season(league_id=league.id, year=date.today().year, is_active=True)
        db.add(season)
        db.flush()

        # ---------------------------------------------------------------
        # Tournaments — look up by PGA Tour ID (scraped data)
        # ---------------------------------------------------------------
        print("\nTournaments:")
        # PGA Tour IDs for the 8 most recent completed events + THE PLAYERS
        pga_ids = [
            "401811929",  # The American Express        (2026-01-22)
            "401811930",  # Sony Open in Hawaii          (2026-01-15) -- skip, use below
            "401811931",  # Farmers Insurance Open       (2026-01-29)
            "401811932",  # WM Phoenix Open              (2026-02-05)
            "401811933",  # AT&T Pebble Beach Pro-Am     (2026-02-12)
            "401811934",  # The Genesis Invitational     (2026-02-19)
            "401811935",  # Cognizant Classic            (2026-02-26)
            "401811936",  # Arnold Palmer Invitational   (2026-03-05)
            "401811937",  # THE PLAYERS Championship     (2026-03-12) ← playoff week
        ]

        # Resolve by name instead since we confirmed these names exist
        tournament_names = [
            "The American Express",
            "Farmers Insurance Open",
            "WM Phoenix Open",
            "AT&T Pebble Beach Pro-Am",
            "The Genesis Invitational",
            "Cognizant Classic in The Palm Beaches",
            "Arnold Palmer Invitational pres. by Mastercard",
            "THE PLAYERS Championship",
        ]

        tournaments: dict[str, Tournament] = {}
        for name in tournament_names:
            t = db.query(Tournament).filter(Tournament.name.ilike(f"%{name.split(' ')[0]}%{name.split(' ')[-1]}%")).first()
            if not t:
                # Try exact name prefix
                t = db.query(Tournament).filter(Tournament.name.ilike(f"{name[:20]}%")).first()
            if t:
                tournaments[name] = t
                db.add(LeagueTournament(league_id=league.id, tournament_id=t.id))
                print(f"  Scheduled: {t.name} ({t.start_date}, status={t.status})")
            else:
                print(f"  WARNING: Not found: {name}")

        db.flush()

        # ---------------------------------------------------------------
        # Golfers — resolve by name from DB
        # ---------------------------------------------------------------
        def golfer(name: str) -> Golfer:
            g = db.query(Golfer).filter_by(name=name).first()
            if not g:
                raise ValueError(f"Golfer not found: {name}")
            return g

        scheffler  = golfer("Scottie Scheffler")
        mcilroy    = golfer("Rory McIlroy")
        morikawa   = golfer("Collin Morikawa")
        bhatia     = golfer("Akshay Bhatia")
        aberg      = golfer("Ludvig Åberg")
        bridgeman  = golfer("Jacob Bridgeman")
        rose       = golfer("Justin Rose")
        gotterup   = golfer("Chris Gotterup")
        matsuyama  = golfer("Hideki Matsuyama")
        echavarria = golfer("Nico Echavarria")
        minwoo     = golfer("Min Woo Lee")
        straka     = golfer("Sepp Straka")
        jason_day  = golfer("Jason Day")
        si_woo     = golfer("Si Woo Kim")
        lowry      = golfer("Shane Lowry")
        berger     = golfer("Daniel Berger")
        castillo   = golfer("Ricky Castillo")
        mccarty    = golfer("Matt McCarty")
        coody      = golfer("Pierceson Coody")
        fleetwood  = golfer("Tommy Fleetwood")
        kitayama   = golfer("Kurt Kitayama")
        t_moore    = golfer("Taylor Moore")
        blanchet   = golfer("Chandler Blanchet")
        gerard     = golfer("Ryan Gerard")
        hisatsune  = golfer("Ryo Hisatsune")
        cam_young  = golfer("Cameron Young")
        adam_scott = golfer("Adam Scott")
        clanton    = golfer("Luke Clanton")

        # ---------------------------------------------------------------
        # Picks — (user, tournament_name, golfer) — earnings from real DB
        # No-repeat rule respected per user.
        # ---------------------------------------------------------------
        print("\nPicks:")

        def make_pick(user, t_name, golfer_obj):
            t = tournaments.get(t_name)
            if not t:
                print(f"  SKIP pick (tournament not found): {t_name}")
                return
            earnings = get_earnings(db, t.id, golfer_obj.id)
            pts = (earnings or 0) * t.multiplier
            pick = Pick(
                league_id=league.id,
                season_id=season.id,
                user_id=user.id,
                tournament_id=t.id,
                golfer_id=golfer_obj.id,
                points_earned=pts,
                # earnings_usd is a hybrid property derived from TournamentEntry — not set directly
            )
            db.add(pick)

        # Blake — strong season, picked mostly winners
        make_pick(blake, "The American Express",                          scheffler)   # W  $1,656,000
        make_pick(blake, "Farmers Insurance Open",                        rose)        # W  $1,728,000
        make_pick(blake, "WM Phoenix Open",                               matsuyama)   # 2  $1,046,400
        make_pick(blake, "AT&T Pebble Beach Pro-Am",                      morikawa)    # W  $3,600,000
        make_pick(blake, "The Genesis Invitational",                      mcilroy)     # T2 $1,800,000
        make_pick(blake, "Cognizant Classic in The Palm Beaches",         echavarria)  # W  $1,728,000
        make_pick(blake, "Arnold Palmer Invitational pres. by Mastercard",bhatia)      # W  $4,000,000
        # Puerto Rico — Blake skips (no pick)

        # Bob — solid, a few misses
        make_pick(bob, "The American Express",                          jason_day)   # T2 $616,400
        make_pick(bob, "Farmers Insurance Open",                        si_woo)      # T2 $726,400
        make_pick(bob, "WM Phoenix Open",                               gotterup)    # W  $1,728,000
        make_pick(bob, "AT&T Pebble Beach Pro-Am",                      straka)      # T2 $1,760,000
        make_pick(bob, "The Genesis Invitational",                      bridgeman)   # W  $4,000,000
        make_pick(bob, "Cognizant Classic in The Palm Beaches",         lowry)       # T2 $726,400
        make_pick(bob, "Arnold Palmer Invitational pres. by Mastercard",berger)      # 2  $2,200,000

        # Carol — mixed results
        make_pick(carol, "The American Express",                          mccarty)     # T2 $616,400
        make_pick(carol, "Farmers Insurance Open",                        coody)       # T2 $726,400
        make_pick(carol, "WM Phoenix Open",                               scheffler)   # T3 $439,680
        make_pick(carol, "AT&T Pebble Beach Pro-Am",                      fleetwood)   # T4 $877,500
        make_pick(carol, "The Genesis Invitational",                      kitayama)    # T2 $1,800,000
        make_pick(carol, "Cognizant Classic in The Palm Beaches",         t_moore)     # T2 $726,400
        make_pick(carol, "Arnold Palmer Invitational pres. by Mastercard",aberg)       # T3 $1,200,000

        # Dave — trailing the pack
        make_pick(dave, "The American Express",                          gerard)      # T2 $616,400
        make_pick(dave, "Farmers Insurance Open",                        hisatsune)   # T2 $726,400
        make_pick(dave, "WM Phoenix Open",                               bhatia)      # T3 $439,680
        make_pick(dave, "AT&T Pebble Beach Pro-Am",                      scheffler)   # T4 $877,500
        make_pick(dave, "The Genesis Invitational",                      adam_scott)  # 4  $1,000,000
        make_pick(dave, "Cognizant Classic in The Palm Beaches",         echavarria)  # W  $1,728,000
        make_pick(dave, "Arnold Palmer Invitational pres. by Mastercard",cam_young)   # 3  $1,200,000

        # Eve — no-pick penalty in week 1, otherwise decent
        # (no pick for The American Express — triggers no_pick_penalty)
        make_pick(eve, "Farmers Insurance Open",                        rose)        # W  $1,728,000  (same as Blake, different week is fine — different user)
        make_pick(eve, "WM Phoenix Open",                               si_woo)      # T3 $439,680
        make_pick(eve, "AT&T Pebble Beach Pro-Am",                      minwoo)      # T2 $1,760,000
        make_pick(eve, "The Genesis Invitational",                      mcilroy)     # T2 $1,800,000
        make_pick(eve, "Cognizant Classic in The Palm Beaches",         blanchet)    # T2 $726,400  (Smotherman?)
        make_pick(eve, "Arnold Palmer Invitational pres. by Mastercard",berger)      # 2  $2,200,000

        db.commit()
        print(f"\nSeed complete!")
        print(f"  League id:   {league.id}")
        print(f"  Season:      {season.year}")
        print(f"  Members:     chambersbw@gmail.com (manager), bob, carol, dave, eve")
        print(f"  Passwords:   password123 (all except chambersbw if already OAuth)")
        print(f"  Tournaments: {len(tournaments)} in schedule")

    except Exception as e:
        db.rollback()
        print(f"\nSeed failed: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed()
