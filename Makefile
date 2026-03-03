# Development commands for the fantasy-golf-backend.
# Run targets with: make <target>

# --reload-dir app restricts the file watcher to the app/ source directory.
# Without this, uvicorn watches .venv/, __pycache__/, etc. and restarts in
# an infinite loop as Python writes .pyc files during imports.
dev:
	uv run uvicorn app.main:app --reload --reload-dir app

test:
	TEST_DATABASE_URL=postgresql://fantasygolf:fantasygolf@localhost:5432/fantasygolf_test \
	uv run pytest tests/ -v

lint:
	uv run ruff check app/ tests/

.PHONY: dev test lint
