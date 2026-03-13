"""add thru and started_on_back to tournament_entry_rounds

Revision ID: c7e2a1f9b4d3
Revises: 40a2d71cc045
Create Date: 2026-03-13

Adds:
  - tournament_entry_rounds.thru          INTEGER NULL  (holes completed in the round)
  - tournament_entry_rounds.started_on_back BOOLEAN NULL (first hole >= 10)

These fields are populated by the scraper from the ESPN /linescores nested
hole array and enable the live leaderboard to:
  1. Suppress in-progress round scores from R-columns (show only when thru=18)
  2. Display "Today" (current round score-to-par) and "Thru" (holes completed)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7e2a1f9b4d3"
down_revision: str | Sequence[str] | None = "40a2d71cc045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tournament_entry_rounds", sa.Column("thru", sa.Integer(), nullable=True))
    op.add_column("tournament_entry_rounds", sa.Column("started_on_back", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("tournament_entry_rounds", "started_on_back")
    op.drop_column("tournament_entry_rounds", "thru")
