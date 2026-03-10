# Multi-target production Dockerfile
#
# Two build targets share the same base layer (deps + source):
#
#   api     → uvicorn HTTP server (default target)
#   scraper → APScheduler sync process (no HTTP server)
#
# Build commands:
#   docker build --target api     -t fantasy-golf-backend  .
#   docker build --target scraper -t fantasy-golf-scraper  .
#
# If no --target is specified, Docker builds the last defined stage (api).
# CI/CD builds both targets from the same source tree.

# ─────────────────────────────────────────────────────────────────────────────
# Base: install dependencies + copy source
# Shared by both the api and scraper targets — changes here bust both caches.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

WORKDIR /app

# Install uv — fast Python package manager used by this project.
RUN pip install uv --quiet

# UV_SYSTEM_PYTHON=1 installs into the system Python rather than a virtualenv,
# so the app source can be mounted/copied without shadowing a .venv directory.
ENV UV_SYSTEM_PYTHON=1

# Install dependencies first (before copying source) so that code-only changes
# don't bust this expensive layer. Requires pyproject.toml + uv.lock both present.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy application source after deps so code changes are a cheap layer.
COPY app/ ./app/

# Standard Python container best practices.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ─────────────────────────────────────────────────────────────────────────────
# Scraper target: APScheduler sync process — no HTTP server
# ─────────────────────────────────────────────────────────────────────────────
FROM base AS scraper

# scraper_main.py starts the scheduler and blocks on signal.pause().
# No port exposed — this container only writes to the shared PostgreSQL DB.
CMD ["python", "-m", "app.scraper_main"]

# ─────────────────────────────────────────────────────────────────────────────
# API target (default): uvicorn HTTP server
# ─────────────────────────────────────────────────────────────────────────────
FROM base AS api

EXPOSE 8000

# 2 workers is appropriate for a t2.micro (1 vCPU). The scraper runs separately
# so the API workers are fully available for request handling.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
