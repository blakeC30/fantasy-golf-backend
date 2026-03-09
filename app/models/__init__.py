"""
Import all models here so that:
  1. They are registered with Base.metadata before Alembic runs.
  2. Anywhere in the app that does `from app.models import User` works cleanly.
"""

from app.models.base import Base
from app.models.user import User
from app.models.league import League, LeagueMember, LeagueMemberRole, LeagueMemberStatus
from app.models.season import Season
from app.models.golfer import Golfer
from app.models.tournament import Tournament, TournamentEntry, TournamentEntryRound, TournamentStatus
from app.models.pick import Pick
from app.models.league_tournament import LeagueTournament

__all__ = [
    "Base",
    "User",
    "League",
    "LeagueMember",
    "LeagueMemberRole",
    "LeagueMemberStatus",
    "Season",
    "Golfer",
    "Tournament",
    "TournamentEntry",
    "TournamentEntryRound",
    "TournamentStatus",
    "Pick",
    "LeagueTournament",
]
