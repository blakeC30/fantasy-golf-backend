"""
Database engine and session management.

SQLAlchemy uses two key concepts:
  - Engine:  the low-level connection pool to the database.
  - Session: a unit of work — all queries within one request share a session,
             which is committed or rolled back together.

`get_db` is a FastAPI dependency (added in Phase 2) that hands a fresh session
to each request and closes it automatically when the request finishes.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings

# pool_pre_ping=True sends a lightweight "ping" before using any connection
# from the pool. This prevents errors if the database has restarted since
# the connection was first created.
engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)

# SessionLocal is a factory. Calling SessionLocal() creates a new Session object.
# autocommit=False means we have to explicitly commit changes.
# autoflush=False means SQLAlchemy won't send SQL to the DB until we ask it to.
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """
    FastAPI dependency that provides a database session per request.

    Usage in a route:
        @router.get("/example")
        def example(db: Session = Depends(get_db)):
            ...

    The `yield` makes this a generator — the code after `yield` runs after the
    route finishes, guaranteed even if an exception is raised.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
