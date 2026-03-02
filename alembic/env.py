"""
Alembic environment configuration.

This file runs every time you invoke an `alembic` command. Its two jobs are:
  1. Tell Alembic WHERE the database is (pulled from our app settings, not
     hardcoded here — so dev, CI, and prod all use the right database
     automatically based on the DATABASE_URL environment variable).
  2. Tell Alembic WHAT the schema looks like (Base.metadata), which enables
     `--autogenerate` to diff the current database against our models and
     produce the correct migration.

Two modes:
  - Online: connects to a live database and runs migrations directly.
            Used by `alembic upgrade head` in normal operation.
  - Offline: generates SQL statements to stdout/a file without connecting.
             Useful for reviewing what will run before touching the database.
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# --- App imports ---
# Import settings so we can read DATABASE_URL from the environment.
from app.config import settings

# Import Base so Alembic knows the full schema for autogenerate.
# The models must be imported (below) before Base.metadata is passed to
# context.configure — otherwise the tables won't be registered yet.
from app.models.base import Base

# Import every model so SQLAlchemy registers them on Base.metadata.
# Alembic's autogenerate compares Base.metadata (what your code says the
# schema should look like) against the actual database (what it currently
# is) and generates the difference as migration operations.
import app.models  # noqa: F401  — side-effect import, registers all models

# ---------------------------------------------------------------------------

# The Alembic Config object provides access to alembic.ini values.
config = context.config

# Set up Python logging from alembic.ini (controls what Alembic prints).
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override the database URL from our application settings rather than from
# the hardcoded value in alembic.ini. This means the same alembic commands
# work in every environment — just set DATABASE_URL in the environment.
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

# Hand Alembic our schema metadata. This is what powers `--autogenerate`.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    Run migrations without connecting to the database.

    Produces SQL statements to stdout so you can review exactly what will
    execute before touching anything. Useful for change control reviews.

    Usage: alembic upgrade head --sql
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Detect column type changes (e.g. String(100) → String(200)) during
        # autogenerate. Without this, Alembic only detects added/removed columns.
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations against a live database connection.

    This is what `alembic upgrade head` calls in normal operation.
    NullPool ensures Alembic doesn't reuse connections from the app's pool —
    migrations run in their own clean transaction.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
