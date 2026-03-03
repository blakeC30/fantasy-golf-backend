"""add league_tournament multiplier

Revision ID: d2e5f8a3c1b7
Revises: c4e8a2f1b9d6
Create Date: 2026-03-03

Adds a per-league multiplier override to league_tournaments. NULL means
"inherit from tournament.multiplier" (the global default). This allows
league managers to set different point weights per tournament, e.g.
keeping The Players at 1.5× while a different league uses 1.0×.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "d2e5f8a3c1b7"
down_revision = "c4e8a2f1b9d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "league_tournaments",
        sa.Column("multiplier", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("league_tournaments", "multiplier")
