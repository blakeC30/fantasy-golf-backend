"""users_email_case_insensitive_unique

Replace the plain unique index on users.email with a functional unique index on
LOWER(email).  All auth code already normalises emails to lowercase before
writing, but the old index only caught exact-case duplicates.  The new index
makes the constraint database-enforced regardless of which code path inserts
the row.

Revision ID: k7l9m1n3o5p7
Revises: j6k8l0m2n4o6
Create Date: 2026-03-14

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "k7l9m1n3o5p7"
down_revision: Union[str, Sequence[str], None] = "j6k8l0m2n4o6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the old case-sensitive unique index created by SQLAlchemy.
    op.drop_index("ix_users_email", table_name="users")

    # Create a functional unique index so Postgres enforces uniqueness on the
    # lowercase form.  This blocks duplicates like "Alice@example.com" vs
    # "alice@example.com" even if application-level normalisation is bypassed.
    op.execute(
        "CREATE UNIQUE INDEX ix_users_email_lower ON users (LOWER(email))"
    )


def downgrade() -> None:
    op.drop_index("ix_users_email_lower", table_name="users")
    op.create_index("ix_users_email", "users", ["email"], unique=True)
