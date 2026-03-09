"""replace tournament_entry_round_times with tournament_entry_rounds

Revision ID: f1a4b7c9e2d3
Revises: e3f7a1c2d9b8
Create Date: 2026-03-08

Drops the narrowly-scoped tournament_entry_round_times table (which stored
only tee times per round) and replaces it with tournament_entry_rounds, which
stores full per-round performance data for each golfer in a tournament.

New columns come directly from the ESPN core API /linescores endpoint:
  - score          ← linescores item "value" (strokes for the round)
  - score_to_par   ← linescores item "displayValue" parsed to int ("-2" → -2)
  - position       ← linescores item "currentPosition" (leaderboard rank after round)
  - tee_time       ← linescores item "teeTime" (ISO 8601 UTC, nullable)
  - is_playoff     ← linescores item "isPlayoff" (true for playoff holes)

The existing tournament_entries.tee_time column is unchanged — it continues
to hold the current round's tee time for pick-locking logic.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "f1a4b7c9e2d3"
down_revision = "e3f7a1c2d9b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("tournament_entry_round_times")

    op.create_table(
        "tournament_entry_rounds",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tournament_entry_id", sa.Integer(), nullable=False),
        # ESPN linescores "period" field: 1–4 for standard rounds, 5+ for playoffs.
        sa.Column("round_number", sa.Integer(), nullable=False),
        # ESPN linescores "teeTime": ISO 8601 UTC string → timezone-aware datetime.
        # Nullable: tee times aren't released until Tuesday/Wednesday before Thursday start.
        sa.Column("tee_time", sa.DateTime(timezone=True), nullable=True),
        # ESPN linescores "value": total strokes taken this round (e.g. 68).
        # Nullable: absent for rounds not yet played.
        sa.Column("score", sa.Integer(), nullable=True),
        # ESPN linescores "displayValue": score-to-par string ("-2", "E", "+1")
        # stored as an integer (E → 0). Nullable until round completes.
        sa.Column("score_to_par", sa.Integer(), nullable=True),
        # ESPN linescores "currentPosition": leaderboard rank after this round (integer).
        # Stored as a string to accommodate ties notation (e.g. "T5") if ESPN
        # ever provides that directly. Nullable until round completes.
        sa.Column("position", sa.String(10), nullable=True),
        # ESPN linescores "isPlayoff": true for playoff rounds (period 5+).
        # Defaults to false for standard rounds.
        sa.Column(
            "is_playoff",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        sa.ForeignKeyConstraint(
            ["tournament_entry_id"],
            ["tournament_entries.id"],
            name="fk_entry_rounds_entry_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tournament_entry_id", "round_number", name="uq_entry_round_number"
        ),
    )


def downgrade() -> None:
    op.drop_table("tournament_entry_rounds")

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
