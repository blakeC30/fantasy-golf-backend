"""
Declarative base for all SQLAlchemy models.

Every model inherits from Base, which gives it the ability to map Python
class attributes to database columns. Base.metadata holds the full registry
of all tables — Alembic uses this to compare models against the real database
and generate migrations.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
