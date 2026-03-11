"""
Playoff seed script — adds a playoff bracket on top of the existing seed data.

Requires seed.py to have been run first (depends on Augusta Pines league,
members, and THE PLAYERS Championship being in the schedule).

Safe to re-run — checks for existing playoff config before inserting.

Usage:
    cd fantasy-golf-backend
    python scripts/seed_playoff.py

What gets created:
  - PlayoffConfig (4-player, 2 picks per round, snake draft)
  - PlayoffRound 1 assigned to THE PLAYERS Championship (status=pending)
  - 2 PlayoffPods:
      Pod 1: Blake (seed 1, draft pos 1) vs Bob (seed 2, draft pos 2)
      Pod 2: Carol (seed 3, draft pos 1) vs Dave (seed 4, draft pos 2)
  - Eve is in the league but NOT in any pod → tests "Playoff Week, not in playoffs"

Testing workflow:
  1. Open the round (as Blake/manager):
       POST /leagues/{league_id}/playoff/rounds/{round_id}/open
     → Round goes to "drafting"; Dashboard/MakePick flip to playoff mode

  2. Submit preferences (as each pod member) through the UI

  3. Resolve draft (as Blake/manager):
       POST /leagues/{league_id}/playoff/rounds/{round_id}/resolve
     → Round goes to "locked"; picks assigned by draft algorithm

  4. Simulate tournament start (picks still hidden — no tee times yet):
       docker compose exec db psql -U postgres fantasygolf_dev -c \\
         "UPDATE tournaments SET status='in_progress'
           WHERE id='<players_tournament_id>';"

  5. Reveal picks (simulate all R1 teed off):
       docker compose exec db psql -U postgres fantasygolf_dev -c \\
         "UPDATE tournament_entry_rounds
             SET tee_time = NOW() - INTERVAL '3 hours'
           WHERE round_number = 1
             AND tournament_entry_id IN (
               SELECT id FROM tournament_entries
                WHERE tournament_id = '<players_tournament_id>'
             );"

  6. Score and complete (as manager):
       POST /leagues/{league_id}/playoff/rounds/{round_id}/score
       UPDATE tournaments SET status='completed' WHERE id='<players_tournament_id>';
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models import (
    League,
    LeagueMember,
    LeagueMemberRole,
    LeagueMemberStatus,
    Season,
    Tournament,
    User,
)
from app.models.league_tournament import LeagueTournament
from app.models.playoff import (
    PlayoffConfig,
    PlayoffPod,
    PlayoffPodMember,
    PlayoffRound,
)


def seed_playoff():
    db = SessionLocal()
    try:
        # ---------------------------------------------------------------
        # Find the Augusta Pines league and its members
        # ---------------------------------------------------------------
        league = db.query(League).filter_by(name="Augusta Pines Fantasy Golf").first()
        if not league:
            print("Augusta Pines league not found. Run scripts/seed.py first.")
            return

        season = db.query(Season).filter_by(league_id=league.id, is_active=True).first()
        if not season:
            print("Active season not found. Run scripts/seed.py first.")
            return

        blake = db.query(User).filter_by(email="chambersbw@gmail.com").first()
        bob   = db.query(User).filter_by(email="bob@example.com").first()
        carol = db.query(User).filter_by(email="carol@example.com").first()
        dave  = db.query(User).filter_by(email="dave@example.com").first()

        for name, user in [("blake", blake), ("bob", bob), ("carol", carol), ("dave", dave)]:
            if not user:
                print(f"{name} not found. Run scripts/seed.py first.")
                return

        # ---------------------------------------------------------------
        # Idempotency check
        # ---------------------------------------------------------------
        existing = db.query(PlayoffConfig).filter_by(
            league_id=league.id, season_id=season.id
        ).first()
        if existing:
            print("Playoff seed data already exists.")
            _print_summary(db, league, existing)
            return

        # ---------------------------------------------------------------
        # Find THE PLAYERS Championship and ensure it's in the schedule
        # ---------------------------------------------------------------
        players = db.query(Tournament).filter(
            Tournament.name.ilike("%PLAYERS%")
        ).order_by(Tournament.start_date.asc()).first()

        if not players:
            print("THE PLAYERS Championship not found in DB.")
            return

        lt = db.query(LeagueTournament).filter_by(
            league_id=league.id, tournament_id=players.id
        ).first()
        if not lt:
            db.add(LeagueTournament(league_id=league.id, tournament_id=players.id))
            db.flush()
            print(f"Added {players.name} to Augusta Pines schedule.")

        # ---------------------------------------------------------------
        # Ensure Blake is manager of Augusta Pines
        # ---------------------------------------------------------------
        membership = db.query(LeagueMember).filter_by(
            league_id=league.id, user_id=blake.id
        ).first()
        if not membership:
            db.add(LeagueMember(
                league_id=league.id,
                user_id=blake.id,
                role=LeagueMemberRole.MANAGER.value,
                status=LeagueMemberStatus.APPROVED.value,
            ))
            db.flush()
        elif membership.role != LeagueMemberRole.MANAGER.value:
            membership.role = LeagueMemberRole.MANAGER.value
            db.flush()

        # ---------------------------------------------------------------
        # Playoff config
        # ---------------------------------------------------------------
        config = PlayoffConfig(
            league_id=league.id,
            season_id=season.id,
            is_enabled=True,
            playoff_size=4,
            draft_style="snake",
            picks_per_round=[2],  # 2 golfer picks assigned per round
            status="active",
        )
        db.add(config)
        db.flush()

        # ---------------------------------------------------------------
        # Round 1 — THE PLAYERS Championship, status=pending
        # (Manager opens it through the UI to start testing)
        # ---------------------------------------------------------------
        round1 = PlayoffRound(
            playoff_config_id=config.id,
            round_number=1,
            tournament_id=players.id,
            status="pending",
        )
        db.add(round1)
        db.flush()

        # Pod 1: Blake (seed 1) vs Bob (seed 2)
        pod1 = PlayoffPod(playoff_round_id=round1.id, bracket_position=1, status="pending")
        db.add(pod1)
        db.flush()
        db.add(PlayoffPodMember(pod_id=pod1.id, user_id=blake.id, seed=1, draft_position=1))
        db.add(PlayoffPodMember(pod_id=pod1.id, user_id=bob.id,   seed=2, draft_position=2))

        # Pod 2: Carol (seed 3) vs Dave (seed 4)
        pod2 = PlayoffPod(playoff_round_id=round1.id, bracket_position=2, status="pending")
        db.add(pod2)
        db.flush()
        db.add(PlayoffPodMember(pod_id=pod2.id, user_id=carol.id, seed=3, draft_position=1))
        db.add(PlayoffPodMember(pod_id=pod2.id, user_id=dave.id,  seed=4, draft_position=2))

        db.commit()

        print("\nPlayoff seed complete!")
        _print_summary(db, league, config)

    except Exception as e:
        db.rollback()
        print(f"\nPlayoff seed failed: {e}")
        raise
    finally:
        db.close()


def _print_summary(db, league: League, config: PlayoffConfig):
    players = db.query(Tournament).filter(
        Tournament.name.ilike("%PLAYERS%")
    ).order_by(Tournament.start_date.asc()).first()

    round1 = db.query(PlayoffRound).filter_by(
        playoff_config_id=config.id, round_number=1
    ).first()

    print(f"\n  League:     '{league.name}'  id={league.id}")
    print(f"  Playoff:    config id={config.id}  status={config.status}")
    print(f"  Round 1:    id={round1.id if round1 else '?'}  tournament={players.name if players else '?'}  status={round1.status if round1 else '?'}")
    print()
    print("  Pod 1:  chambersbw@gmail.com (seed 1) vs bob@example.com (seed 2)")
    print("  Pod 2:  carol@example.com (seed 3) vs dave@example.com (seed 4)")
    print("  Eve (eve@example.com) — in league, NOT in bracket (tests non-playoff-member UI)")
    print()
    print("  Passwords: password123  (all except chambersbw@gmail.com if OAuth account)")
    print()
    print("  To start testing — open the draft as Blake (manager):")
    print(f"    POST /leagues/{league.id}/playoff/rounds/{round1.id if round1 else '<round_id>'}/open")
    print()
    print("  SQL shortcuts:")
    if players:
        print(f"    -- Set tournament in_progress (picks hidden):")
        print(f"    UPDATE tournaments SET status='in_progress' WHERE id='{players.id}';")
        print(f"    -- Reveal picks (all R1 teed off):")
        print(f"    UPDATE tournament_entry_rounds SET tee_time = NOW() - INTERVAL '3 hours'")
        print(f"      WHERE round_number=1 AND tournament_entry_id IN (")
        print(f"        SELECT id FROM tournament_entries WHERE tournament_id='{players.id}');")
        print(f"    -- Reset tournament to scheduled:")
        print(f"    UPDATE tournaments SET status='scheduled' WHERE id='{players.id}';")


if __name__ == "__main__":
    seed_playoff()
