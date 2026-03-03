"""Rename league member role 'admin' to 'manager'.

The league leader role was renamed from 'admin' to 'manager' to distinguish
it clearly from the platform-level admin role (is_platform_admin on users).

Revision ID: c4e8a2f1b9d6
Revises: b7d4e1f2a9c3
Create Date: 2026-03-03
"""

from alembic import op

revision = "c4e8a2f1b9d6"
down_revision = "b7d4e1f2a9c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE league_members SET role = 'manager' WHERE role = 'admin'")


def downgrade() -> None:
    op.execute("UPDATE league_members SET role = 'admin' WHERE role = 'manager'")
