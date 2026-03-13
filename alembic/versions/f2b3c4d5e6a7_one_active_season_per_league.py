"""one active season per league

Revision ID: f2b3c4d5e6a7
Revises: e5f1a9b2c3d4
Create Date: 2026-03-13

Adds a partial unique index so that at most one season per league can have
is_active = TRUE at the database level.  Previously only the application layer
(get_active_season dependency) enforced this; a direct DB write or a future
bug could silently create two active seasons for the same league.
"""

import sqlalchemy as sa
from alembic import op

revision = "f2b3c4d5e6a7"
down_revision = "e5f1a9b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_league_one_active_season",
        "seasons",
        ["league_id"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )


def downgrade() -> None:
    op.drop_index("uq_league_one_active_season", table_name="seasons")
