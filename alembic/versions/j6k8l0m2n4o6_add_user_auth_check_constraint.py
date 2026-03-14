"""add_user_auth_check_constraint

Adds a database-level CHECK constraint to the users table ensuring that at
least one of password_hash or google_id is non-null.  This enforces the
invariant that every user account has at least one auth method, even if the
application layer is bypassed via a direct DB write.

Revision ID: j6k8l0m2n4o6
Revises: i5j7k9l1m3n5
Create Date: 2026-03-14

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "j6k8l0m2n4o6"
down_revision: Union[str, Sequence[str], None] = "i5j7k9l1m3n5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_users_has_auth_method",
        "users",
        sa.text("password_hash IS NOT NULL OR google_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_has_auth_method", "users", type_="check")
