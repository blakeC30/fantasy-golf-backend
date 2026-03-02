"""
Pydantic schemas for request validation and response serialization.

Schemas are separate from SQLAlchemy models:
  - Models = database shape (tables, columns, relationships)
  - Schemas = API shape (what goes in/out of HTTP requests)

Keeping them separate lets us control exactly what data is exposed through
the API without leaking internal model details.
"""

from app.schemas.auth import GoogleAuthRequest, LoginRequest, RegisterRequest, TokenResponse
from app.schemas.golfer import GolferOut
from app.schemas.league import LeagueCreate, LeagueMemberOut, LeagueOut, RoleUpdate
from app.schemas.pick import PickCreate, PickOut, PickUpdate
from app.schemas.standings import StandingsResponse, StandingsRow
from app.schemas.tournament import TournamentOut
from app.schemas.user import UserOut, UserUpdate

__all__ = [
    "RegisterRequest",
    "LoginRequest",
    "GoogleAuthRequest",
    "TokenResponse",
    "UserOut",
    "UserUpdate",
    "LeagueCreate",
    "LeagueOut",
    "LeagueMemberOut",
    "RoleUpdate",
    "TournamentOut",
    "GolferOut",
    "PickCreate",
    "PickUpdate",
    "PickOut",
    "StandingsRow",
    "StandingsResponse",
]
