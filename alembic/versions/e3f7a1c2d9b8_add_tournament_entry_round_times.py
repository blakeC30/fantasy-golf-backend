"""add tournament_entry_round_times table

Revision ID: e3f7a1c2d9b8
Revises: d2e5f8a3c1b7
Create Date: 2026-03-08

Adds tournament_entry_round_times to store per-round tee times for each
golfer in a tournament. Previously only the current round's tee time was
kept (in tournament_entries.tee_time), overwritten on each scraper run.
This table preserves a row per (entry, round_number) so historical per-round
tee times are available for display or analysis.

The existing tournament_entries.tee_time column is unchanged — it continues
to hold the current round's tee time used by pick-locking logic.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "e3f7a1c2d9b8"
down_revision = "d2e5f8a3c1b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tournament_entry_round_times",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tournament_entry_id", sa.Integer(), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("tee_time", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tournament_entry_id"],
            ["tournament_entries.id"],
            name="fk_entry_round_times_entry_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tournament_entry_id", "round_number", name="uq_entry_round"
        ),
    )


def downgrade() -> None:
    op.drop_table("tournament_entry_round_times")
