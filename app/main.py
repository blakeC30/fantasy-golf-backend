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

# Configure the root logger so that application-level log.info() calls are
# visible in the container output alongside uvicorn's access log lines.
# Uvicorn configures its own loggers separately; this only affects app code.
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(name)s: %(message)s",
)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.limiter import limiter
from app.routers import admin, auth, golfers, leagues, picks, playoff, standings, tournaments, users

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    FastAPI lifespan: code before `yield` runs at startup, after `yield` at shutdown.

    This replaces the deprecated @app.on_event() decorators. Keeping startup
    and shutdown in one context manager makes the lifecycle explicit and
    ensures shutdown always runs even if startup raises an exception.

    NOTE: The ESPN sync scheduler is NOT started here. It runs in a separate
    scraper container (app/scraper_main.py) so that scraper failures cannot
    affect API availability, and the two can be deployed independently.
    Manual sync triggers remain available via POST /admin/sync.
    """
    log.info("Starting League Caddie API (environment=%s)", settings.ENVIRONMENT)
    if settings.ENVIRONMENT != "production":
        log.warning(
            "ENVIRONMENT is %r — refresh-token cookies will be sent without the "
            "Secure flag. Set ENVIRONMENT=production in production deployments.",
            settings.ENVIRONMENT,
        )
    else:
        if not settings.FRONTEND_URL.startswith("https://"):
            raise RuntimeError(
                f"FRONTEND_URL must start with 'https://' in production, got {settings.FRONTEND_URL!r}. "
                "Set the correct origin in your environment variables."
            )
    yield
    log.info("Shutting down League Caddie API")


app = FastAPI(
    title="League Caddie API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)

# Rate limiting — slowapi uses app.state.limiter to find the limiter instance.
# The exception handler converts RateLimitExceeded into a 429 JSON response.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

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
app.include_router(playoff.router, prefix=_PREFIX)


@app.get("/health")
def health():
    """Simple health check endpoint used by Kubernetes liveness probes."""
    return {"status": "ok"}
