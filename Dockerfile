# Multi-target production Dockerfile
#
# Three build targets share the same base layer (deps + source):
#
#   api     → uvicorn HTTP server (default target)
#   scraper → APScheduler sync process
#   worker  → SQS consumer loop
#
# Build commands:
#   docker build --target api     -t fantasy-golf-backend  .
#   docker build --target scraper -t fantasy-golf-scraper  .
#   docker build --target worker  -t fantasy-golf-worker   .
#
# If no --target is specified, Docker builds the last defined stage (api).
# CI/CD builds all three targets from the same source tree.

# ─────────────────────────────────────────────────────────────────────────────
# Base: install dependencies + copy source
# Shared by api, scraper, and worker — changes here bust all caches.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

WORKDIR /app

# Copy uv directly from the official image — avoids pip entirely, keeps the
# image lean. The binary lives at /usr/local/bin/uv in the final image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# UV_SYSTEM_PYTHON=1 installs packages into the system Python (/usr/local/lib)
# rather than a virtualenv, so there is no .venv directory to conflict with
# volume mounts or COPY instructions.
ENV UV_SYSTEM_PYTHON=1

# Install dependencies before copying source so that code-only changes don't
# bust this expensive layer. Requires pyproject.toml + uv.lock both present.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy application source after deps — cheap layer on code changes.
COPY app/ ./app/

# Standard Python container best practices.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non-root user — running as root means a container escape = full host access.
# All app files are owned by appuser; system Python packages remain root-owned
# but are world-readable so the import chain still works.
RUN useradd --system --no-create-home appuser \
    && chown -R appuser:appuser /app
USER appuser

# ─────────────────────────────────────────────────────────────────────────────
# Scraper target: APScheduler sync process — no HTTP server
# ─────────────────────────────────────────────────────────────────────────────
FROM base AS scraper

# scraper_main.py starts the scheduler and blocks on signal.pause().
# No port exposed — this container only writes to the shared PostgreSQL DB
# and publishes SQS events.
CMD ["python", "-m", "app.scraper_main"]

# ─────────────────────────────────────────────────────────────────────────────
# Worker target: SQS consumer loop — no HTTP server
# ─────────────────────────────────────────────────────────────────────────────
FROM base AS worker

# worker_main.py polls SQS and handles TOURNAMENT_COMPLETED /
# TOURNAMENT_IN_PROGRESS events for playoff automation.
# Deploy at exactly 1 replica — handlers are idempotent but duplicate
# consumers waste work and can cause race conditions on bracket advancement.
CMD ["python", "-m", "app.worker_main"]

# ─────────────────────────────────────────────────────────────────────────────
# API target (default): uvicorn HTTP server
# ─────────────────────────────────────────────────────────────────────────────
FROM base AS api

EXPOSE 8000

# HEALTHCHECK uses Python's built-in urllib — no curl/wget needed on slim.
# start-period gives uvicorn time to start before the first check fires.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# 2 workers is appropriate for a t2.micro (1 vCPU). The scraper and worker
# run in separate containers so all uvicorn workers are free for HTTP requests.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
