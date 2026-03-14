"""add_last_synced_at_to_tournaments

Revision ID: h4i6j8k0l2m3
Revises: g3h5i7j9k1l2
Create Date: 2026-03-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "h4i6j8k0l2m3"
down_revision: Union[str, Sequence[str], None] = "g3h5i7j9k1l2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tournaments",
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tournaments", "last_synced_at")
