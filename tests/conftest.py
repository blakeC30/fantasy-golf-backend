"""
Pytest configuration and shared fixtures.

Tests run against a real PostgreSQL test database (`fantasygolf_test`).
This avoids SQLite compatibility issues with PostgreSQL-specific types
(UUID, etc.) and ensures tests mirror production behavior.

Requires:
  - PostgreSQL running: docker compose up postgres -d
  - Test DB created once:
      docker compose exec postgres psql -U fantasygolf -d fantasygolf_dev \
        -c "CREATE DATABASE fantasygolf_test;"

Isolation strategy: all tables are TRUNCATED after every test, giving each
test function a completely clean slate without needing rollback gymnastics.
"""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://fantasygolf:fantasygolf@localhost:5432/fantasygolf_test",
)

from app.database import get_db  # noqa: E402
from app.main import app  # noqa: E402
# Importing Base from app.models (not app.models.base) triggers the __init__.py,
# which imports every model class and registers them all with Base.metadata.
from app.models import Base  # noqa: E402

engine = create_engine(TEST_DB_URL)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="session", autouse=True)
def create_tables():
    """Create all tables once per test session; drop at the end."""
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def clean_db(create_tables):
    """
    Truncate all tables after every test for a clean slate.

    TRUNCATE ... CASCADE handles FK dependencies. RESTART IDENTITY resets
    auto-increment sequences so IDs don't leak between tests.
    """
    yield
    table_names = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
    with engine.connect() as conn:
        conn.execute(text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE"))
        conn.commit()


@pytest.fixture
def db(create_tables):
    """Yield a SQLAlchemy session for direct DB access within a test."""
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db):
    """
    FastAPI TestClient connected to the test database.

    Overrides the `get_db` dependency so every HTTP request made through
    `client` uses `fantasygolf_test` instead of the dev database.
    Each request still gets its own session (matches prod behavior).
    """
    def _override_get_db():
        s = TestingSessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Shared auth fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def registered_user(client):
    """Register a test user and return the access token."""
    resp = client.post("/api/v1/auth/register", json={
        "email": "test@example.com",
        "password": "password123",
        "display_name": "Test User",
    })
    assert resp.status_code == 201, resp.json()
    return resp.json()["access_token"]


@pytest.fixture
def auth_headers(registered_user):
    """Return an Authorization header dict for the test user."""
    return {"Authorization": f"Bearer {registered_user}"}
