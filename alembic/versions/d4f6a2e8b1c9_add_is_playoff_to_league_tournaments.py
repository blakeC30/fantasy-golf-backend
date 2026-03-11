"""add is_playoff to league_tournaments

Revision ID: d4f6a2e8b1c9
Revises: a1b2c3d4e5f6
Create Date: 2026-03-10

"""

from alembic import op
import sqlalchemy as sa

revision = "d4f6a2e8b1c9"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "league_tournaments",
        sa.Column(
            "is_playoff",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("league_tournaments", "is_playoff")
