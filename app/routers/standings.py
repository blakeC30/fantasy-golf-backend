"""
Standings router — /leagues/{league_id}/standings

Endpoints:
  GET /leagues/{league_id}/standings   Current season standings for the league
"""

from fastapi import APIRouter, Depends

from app.dependencies import get_active_season, require_league_member
from app.models import League, LeagueMember, Season
from app.schemas.standings import StandingsResponse, StandingsRow
from app.services.scoring import calculate_standings
from app.database import get_db
from sqlalchemy.orm import Session

router = APIRouter(prefix="/leagues/{league_id}/standings", tags=["standings"])


@router.get("", response_model=StandingsResponse)
def get_standings(
    league_and_member: tuple[League, LeagueMember] = Depends(require_league_member),
    season: Season = Depends(get_active_season),
    db: Session = Depends(get_db),
):
    """
    Return the current season standings for all league members.

    Points are calculated from completed tournament picks. Members who missed
    a tournament receive the league's no_pick_penalty for that week.
    Rows are sorted best-to-worst (highest total_points first).
    """
    league, _ = league_and_member
    raw_rows = calculate_standings(db, league, season)

    rows = [
        StandingsRow(
            rank=i + 1,
            user_id=row["user_id"],
            display_name=row["display_name"],
            total_points=row["total_points"],
            pick_count=row["pick_count"],
            missed_count=row["missed_count"],
        )
        for i, row in enumerate(raw_rows)
    ]

    return StandingsResponse(
        league_id=league.id,
        season_year=season.year,
        rows=rows,
    )
