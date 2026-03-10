"""add_is_tied_to_tournament_entries

Revision ID: c9d3f2a8e5b1
Revises: 1be05745ead6
Create Date: 2026-03-09

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c9d3f2a8e5b1'
down_revision: str | Sequence[str] | None = '1be05745ead6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'tournament_entries',
        sa.Column('is_tied', sa.Boolean(), server_default='false', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('tournament_entries', 'is_tied')
