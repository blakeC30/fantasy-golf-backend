"""remove_slug_from_leagues

Revision ID: a3f9c2b1d8e5
Revises: 1be05745ead6
Create Date: 2026-03-02 00:00:00.000000

Removes the slug column from leagues. Leagues are now identified by their
UUID primary key in all API routes and frontend navigation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3f9c2b1d8e5"
down_revision: str | Sequence[str] | None = "1be05745ead6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_leagues_slug", table_name="leagues")
    op.drop_column("leagues", "slug")


def downgrade() -> None:
    op.add_column(
        "leagues",
        sa.Column("slug", sa.String(length=100), nullable=True),
    )
    op.create_index("ix_leagues_slug", "leagues", ["slug"], unique=True)
