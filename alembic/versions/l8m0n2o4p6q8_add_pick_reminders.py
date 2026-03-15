"""Add pick_reminders table and users.pick_reminders_enabled

Revision ID: l8m0n2o4p6q8
Revises: k7l9m1n3o5p7
Create Date: 2026-03-15

pick_reminders stores one row per (league, season, tournament) so the
Wednesday APScheduler job can track what has already been sent and retry
on SES failures without double-sending.

users.pick_reminders_enabled is the opt-out preference. Default TRUE so
all existing users receive reminders unless they explicitly disable them.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "l8m0n2o4p6q8"
down_revision: Union[str, Sequence[str], None] = "k7l9m1n3o5p7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # pick_reminders table
    # ------------------------------------------------------------------
    op.create_table(
        "pick_reminders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("league_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("leagues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("season_id", sa.Integer(), sa.ForeignKey("seasons.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tournament_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("league_id", "season_id", "tournament_id", name="uq_pick_reminders"),
    )

    # Partial index speeds up the Wednesday job's "find pending reminders" query.
    op.create_index(
        "ix_pick_reminders_pending",
        "pick_reminders",
        ["scheduled_at"],
        postgresql_where=sa.text("sent_at IS NULL AND failed_at IS NULL"),
    )

    # ------------------------------------------------------------------
    # users.pick_reminders_enabled — opt-out preference (default on)
    # ------------------------------------------------------------------
    op.add_column(
        "users",
        sa.Column(
            "pick_reminders_enabled",
            sa.Boolean(),
            nullable=False,
            server_default="true",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "pick_reminders_enabled")
    op.drop_index("ix_pick_reminders_pending", table_name="pick_reminders")
    op.drop_table("pick_reminders")
