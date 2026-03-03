"""
Scoring service.

Calculates season standings from picks stored in the database.

Scoring rules:
  - points_earned = golfer_earnings_usd * tournament.multiplier
  - If a user has no pick row for a completed tournament → league.no_pick_penalty is applied
  - Standings are sorted by total_points descending (highest wins)

This module contains pure calculation logic with no HTTP concerns. It can be
called from both the standings router and the scraper (when finalizing results).
"""

import datetime

from sqlalchemy.orm import Session, joinedload

from app.models import League, LeagueMember, LeagueMemberStatus, LeagueTournament, Pick, Season, Tournament, TournamentStatus


def calculate_standings(db: Session, league: League, season: Season) -> list[dict]:
    """
    Return standings rows for a league season, sorted best to worst.

    Each row is a dict with:
      user_id, display_name, total_points, pick_count, missed_count
    """
    # Only count tournaments the league admin explicitly added to the schedule
    # AND that have completed. This lets leagues start mid-season and handles
    # weeks with multiple simultaneous events.
    scheduled_ids_subq = (
        db.query(LeagueTournament.tournament_id)
        .filter(LeagueTournament.league_id == league.id)
        .subquery()
    )
    season_tournaments = (
        db.query(Tournament)
        .filter(
            Tournament.id.in_(scheduled_ids_subq),
            Tournament.status == TournamentStatus.COMPLETED.value,
            Tournament.start_date >= datetime.date(season.year, 1, 1),
            Tournament.start_date <= datetime.date(season.year, 12, 31),
        )
        .all()
    )
    completed_ids = {t.id for t in season_tournaments}

    # Only approved members appear in standings — pending requests are excluded.
    members = (
        db.query(LeagueMember)
        .filter_by(league_id=league.id, status=LeagueMemberStatus.APPROVED.value)
        .options(joinedload(LeagueMember.user))
        .all()
    )

    if not completed_ids:
        # Season hasn't started yet — everyone tied at 0.
        return [
            {
                "user_id": m.user_id,
                "display_name": m.user.display_name,
                "total_points": 0.0,
                "pick_count": 0,
                "missed_count": 0,
            }
            for m in members
        ]

    # Load all settled picks (points already calculated) for this league/season.
    picks = (
        db.query(Pick)
        .filter(
            Pick.league_id == league.id,
            Pick.season_id == season.id,
            Pick.tournament_id.in_(completed_ids),
            Pick.points_earned.is_not(None),
        )
        .all()
    )

    # Index picks by user for O(1) lookup.
    picks_by_user: dict = {}
    for pick in picks:
        picks_by_user.setdefault(pick.user_id, []).append(pick)

    standings = []
    for member in members:
        user_picks = picks_by_user.get(member.user_id, [])
        picked_ids = {p.tournament_id for p in user_picks}
        total = sum(p.points_earned for p in user_picks)  # type: ignore[misc]

        missed = completed_ids - picked_ids
        total += len(missed) * league.no_pick_penalty

        standings.append(
            {
                "user_id": member.user_id,
                "display_name": member.user.display_name,
                "total_points": total,
                "pick_count": len(picked_ids),
                "missed_count": len(missed),
            }
        )

    standings.sort(key=lambda x: x["total_points"], reverse=True)
    return standings
