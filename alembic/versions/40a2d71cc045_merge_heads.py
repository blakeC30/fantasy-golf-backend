"""merge heads

Revision ID: 40a2d71cc045
Revises: f1a4b7c9e2d3, f2b3c4d5e6a7
Create Date: 2026-03-13 03:32:42.478315

"""
from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = '40a2d71cc045'
down_revision: str | Sequence[str] | None = ('f1a4b7c9e2d3', 'f2b3c4d5e6a7')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
