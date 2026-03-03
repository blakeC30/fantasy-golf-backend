"""add_team_event_fields

Revision ID: b7d4e1f2a9c3
Revises: a3f9c2b1d8e5
Create Date: 2026-03-02

Adds support for team-format tournaments (e.g. Zurich Classic):
  - tournaments.competition_id     — ESPN competition ID (may differ from pga_tour_id for team events)
  - tournaments.is_team_event      — True for two-person team format tournaments
  - tournament_entries.team_competitor_id — ESPN team competitor ID, used to fetch earnings for team events
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7d4e1f2a9c3"
down_revision: Union[str, Sequence[str], None] = "a3f9c2b1d8e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tournaments", sa.Column("competition_id", sa.String(50), nullable=True))
    op.add_column(
        "tournaments",
        sa.Column("is_team_event", sa.Boolean, nullable=False, server_default="false"),
    )
    op.add_column(
        "tournament_entries",
        sa.Column("team_competitor_id", sa.String(50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tournament_entries", "team_competitor_id")
    op.drop_column("tournaments", "is_team_event")
    op.drop_column("tournaments", "competition_id")
