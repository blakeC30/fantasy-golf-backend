"""
FastAPI application entry point.

This file:
  1. Defines the lifespan context manager (startup/shutdown hooks)
  2. Creates the FastAPI app instance
  3. Configures CORS (Cross-Origin Resource Sharing)
  4. Registers all routers under /api/v1
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import admin, auth, golfers, leagues, picks, standings, tournaments, users

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    FastAPI lifespan: code before `yield` runs at startup, after `yield` at shutdown.

    This replaces the deprecated @app.on_event() decorators. Keeping startup
    and shutdown in one context manager makes the lifecycle explicit and
    ensures shutdown always runs even if startup raises an exception.
    """
    log.info("Starting Fantasy Golf API")
    from app.services.scheduler import start_scheduler
    start_scheduler()

    yield

    log.info("Shutting down Fantasy Golf API")
    from app.services.scheduler import stop_scheduler
    stop_scheduler()


app = FastAPI(
    title="Fantasy Golf API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)

# ---------------------------------------------------------------------------
# CORS middleware
# ---------------------------------------------------------------------------
# allow_origins: which frontend URLs are allowed to call the API.
# allow_credentials=True: required for the browser to send httpOnly cookies
#   (refresh tokens). Must also set specific origins — cannot use "*" with credentials.
# allow_methods/headers: needed for preflight OPTIONS requests.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
# All routes are prefixed with /api/v1 so we can evolve the API later without
# breaking existing clients.
_PREFIX = "/api/v1"

app.include_router(auth.router, prefix=_PREFIX)
app.include_router(users.router, prefix=_PREFIX)
app.include_router(leagues.router, prefix=_PREFIX)
app.include_router(tournaments.router, prefix=_PREFIX)
app.include_router(golfers.router, prefix=_PREFIX)
app.include_router(picks.router, prefix=_PREFIX)
app.include_router(standings.router, prefix=_PREFIX)
app.include_router(admin.router, prefix=_PREFIX)


@app.get("/health")
def health():
    """Simple health check endpoint used by Kubernetes liveness probes."""
    return {"status": "ok"}
