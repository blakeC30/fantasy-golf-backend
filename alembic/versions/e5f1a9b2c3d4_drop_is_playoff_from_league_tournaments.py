"""drop is_playoff from league_tournaments

Revision ID: e5f1a9b2c3d4
Revises: d4f6a2e8b1c9
Create Date: 2026-03-11

Playoffs no longer use explicit per-row flags — the last N scheduled tournaments
in the league's schedule are automatically used as playoff rounds.
"""

import sqlalchemy as sa
from alembic import op

revision = "e5f1a9b2c3d4"
down_revision = "d4f6a2e8b1c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("league_tournaments", "is_playoff")


def downgrade() -> None:
    op.add_column(
        "league_tournaments",
        sa.Column("is_playoff", sa.Boolean(), nullable=False, server_default="false"),
    )
