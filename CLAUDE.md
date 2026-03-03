# Fantasy Golf Backend

FastAPI + Python + SQLAlchemy 2.0 app. See the root `CLAUDE.md` for project-wide rules and domain logic.

## Tech

- **FastAPI** — async HTTP framework, automatic OpenAPI at `/api/v1/docs` (DEBUG mode only)
- **SQLAlchemy 2.0** — ORM with `Mapped` / `mapped_column` typed columns
- **Alembic** — migrations (see Migration section below)
- **PostgreSQL** — primary DB
- **httpx** — sync HTTP client for ESPN API calls
- **APScheduler** (`BackgroundScheduler`) — daily sync jobs in thread pool, started in FastAPI lifespan
- **Ruff** — linting + formatting
- **pytest** — test runner

## Directory Structure

```
app/
├── main.py           # App init, router registration, CORS, lifespan (scheduler)
├── config.py         # Pydantic BaseSettings — reads .env; singleton `settings`
├── database.py       # SQLAlchemy engine + SessionLocal + get_db() dependency
├── dependencies.py   # FastAPI dependency functions (auth chain, league access chain)
├── models/           # SQLAlchemy ORM models
│   ├── user.py       # User
│   ├── league.py     # League, LeagueMember, LeagueMemberStatus, Season
│   ├── tournament.py # Tournament, TournamentEntry, TournamentStatus
│   ├── golfer.py     # Golfer
│   ├── pick.py       # Pick
│   └── league_tournament.py  # LeagueTournament (join table)
├── schemas/          # Pydantic request/response schemas
│   ├── auth.py       # RegisterRequest, LoginRequest, GoogleAuthRequest, TokenResponse
│   ├── user.py       # UserOut, UserUpdate
│   ├── league.py     # LeagueCreate/Update/Out, LeagueMemberOut, RoleUpdate,
│   │                 #   LeagueJoinPreview, LeagueRequestOut
│   ├── tournament.py # TournamentOut
│   ├── golfer.py     # GolferOut
│   ├── pick.py       # PickCreate, PickUpdate, PickOut
│   └── standings.py  # StandingsRow, StandingsResponse
├── routers/
│   ├── auth.py       # /auth/*
│   ├── users.py      # /users/*
│   ├── leagues.py    # /leagues/*
│   ├── tournaments.py# /tournaments/*
│   ├── golfers.py    # /golfers/*
│   ├── picks.py      # /leagues/{league_id}/picks/*
│   ├── standings.py  # /leagues/{league_id}/standings
│   └── admin.py      # /admin/* (platform admin only)
└── services/
    ├── auth.py       # hash_password, verify_password, create/decode JWT tokens, verify_google_id_token
    ├── picks.py      # validate_new_pick(), validate_pick_change() — raises HTTPException
    ├── scoring.py    # calculate_standings() — returns list[dict]
    ├── scraper.py    # ESPN API client, upsert functions, full_sync / sync_tournament
    └── scheduler.py  # APScheduler setup — starts on FastAPI startup, runs scraper jobs

alembic/
└── versions/         # Migration files — see Migration section
tests/
├── conftest.py       # Test DB setup, fixtures (client, db, auth_headers, registered_user)
├── test_auth.py
├── test_picks.py
├── test_scraper.py
└── test_scoring.py
```

## API Endpoints

All routes are prefixed with `/api/v1`.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| POST | `/auth/register` | — | Returns access_token |
| POST | `/auth/login` | — | Sets httpOnly refresh_token cookie |
| POST | `/auth/google` | — | Google ID token → JWT pair |
| POST | `/auth/refresh` | cookie | Returns new access_token |
| POST | `/auth/logout` | token | Clears refresh cookie |
| GET | `/users/me` | token | Current user profile |
| PATCH | `/users/me` | token | Update display_name |
| GET | `/users/me/leagues` | token | User's approved leagues |
| POST | `/leagues` | token | Create league (creator → manager) |
| GET | `/leagues/join/{invite_code}` | token | Preview league (no side effects) |
| GET | `/leagues/my-requests` | token | User's pending requests |
| POST | `/leagues/join/{invite_code}` | token | Submit join request |
| GET | `/leagues/{league_id}` | member | League details |
| PATCH | `/leagues/{league_id}` | manager | Update name/description/penalty |
| GET | `/leagues/{league_id}/members` | member | Approved members only |
| PATCH | `/leagues/{league_id}/members/{user_id}/role` | manager | |
| DELETE | `/leagues/{league_id}/members/{user_id}` | manager | |
| GET | `/leagues/{league_id}/requests` | manager | Pending join requests |
| POST | `/leagues/{league_id}/requests/{user_id}/approve` | manager | |
| DELETE | `/leagues/{league_id}/requests/me` | token | User withdraws own request |
| DELETE | `/leagues/{league_id}/requests/{user_id}` | manager | Deny request |
| GET | `/leagues/{league_id}/tournaments` | member | League's selected tournaments (returns `LeagueTournamentOut` with `effective_multiplier`) |
| PUT | `/leagues/{league_id}/tournaments` | manager | Atomically replace schedule; body: `{tournaments: [{tournament_id, multiplier?}]}` |
| GET | `/tournaments` | token | All/filtered by status |
| GET | `/tournaments/{id}` | token | Tournament details |
| GET | `/tournaments/{id}/field` | token | Golfers in field |
| GET | `/golfers` | token | List/search golfers |
| GET | `/golfers/{id}` | token | Golfer details |
| POST | `/leagues/{league_id}/picks` | member | Submit pick |
| GET | `/leagues/{league_id}/picks/mine` | member | My picks this season |
| GET | `/leagues/{league_id}/picks` | member | All picks (completed tournaments only) |
| PATCH | `/leagues/{league_id}/picks/{pick_id}` | member | Change golfer |
| GET | `/leagues/{league_id}/standings` | member | Season standings |
| POST | `/admin/sync` | platform_admin | Full ESPN data sync |
| POST | `/admin/sync/{pga_tour_id}` | platform_admin | Sync single tournament |

**CRITICAL — FastAPI route ordering**: Literal path segments must be defined BEFORE parameterized ones. Example in `leagues.py`:
```python
# These must come BEFORE /{league_id} and /{league_id}/requests/{user_id}
@router.get("/join/{invite_code}")
@router.get("/my-requests")
@router.delete("/{league_id}/requests/me")   # before /{league_id}/requests/{user_id}
```

## Dependency Chain

```
get_current_user          ← validates JWT access token from Authorization header
  └─ require_platform_admin   ← checks is_platform_admin
  └─ get_league_or_404    ← looks up league by league_id
       └─ require_league_member   ← checks approved membership
            └─ require_league_manager   ← checks manager role
  └─ get_active_season    ← gets active season for league
```

FastAPI caches dependency results within a single request — each runs once even if multiple route params depend on it.

## DB Session Pattern

```python
def my_route(db: Session = Depends(get_db)):
    obj = db.query(Model).filter_by(...).first()
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj
```

Always call `db.commit()` explicitly. Never rely on auto-commit. Use `db.refresh(obj)` after insert to load server-generated fields (id, created_at).

## Models

### Key Column Types
- PKs: `UUID` with `default=uuid4`
- Auto-increment PKs (join tables): `Integer`, `autoincrement=True`
- Timestamps: `DateTime(timezone=True)`, `server_default=func.now()`
- Status enums: stored as plain strings (`String(20)`), not PostgreSQL ENUMs

### Schema Summary

| Table | Key Columns |
|-------|-------------|
| `users` | id (UUID), email (unique), password_hash (nullable), google_id (nullable), display_name, is_platform_admin |
| `leagues` | id (UUID), name, invite_code (unique, 16-char token), is_public, no_pick_penalty (default=-50000) |
| `league_members` | league_id, user_id, role ("manager"\|"member"), status ("pending"\|"approved") |
| `seasons` | league_id, year (int), is_active; UNIQUE(league_id, year) |
| `tournaments` | pga_tour_id (unique), name, start_date, end_date, multiplier (float, default=1.0), status, competition_id (nullable), is_team_event (bool) |
| `tournament_entries` | tournament_id, golfer_id, tee_time, earnings_usd, finish_position, team_competitor_id (nullable) |
| `golfers` | pga_tour_id (unique), name, world_ranking, country |
| `picks` | league_id, season_id, user_id, tournament_id, golfer_id, points_earned (nullable); UNIQUE(league_id, season_id, user_id, tournament_id) |
| `league_tournaments` | league_id, tournament_id, multiplier (float nullable); UNIQUE(league_id, tournament_id) |

### Points Formula
```
effective_multiplier = league_tournaments.multiplier  (if not NULL)
                     ?? tournament.multiplier           (global default)
points_earned = tournament_entry.earnings_usd × effective_multiplier
```
`tournament.multiplier` is the global default (1.0 standard, 2.0 majors, 1.5 The Players). League managers can override per-tournament via `league_tournaments.multiplier`. NULL means inherit the global default. `score_picks` resolves `effective_multiplier` per pick by looking up the `LeagueTournament` row.

## Migrations

**We do NOT run `alembic upgrade head` inside Docker.** Apply via `psql` directly:

```bash
docker exec fantasygolf-postgres-1 psql -U fantasygolf -d fantasygolf_dev -c "
  -- your DDL here
  UPDATE alembic_version SET version_num = '<new_revision>';
"
```

Existing migration files (in order):
1. `99fbdae03d30` — initial schema
2. `6ae0425f23c9` — expand golfer.country to 100 chars
3. `b721c01b567f` — add league_tournaments table
4. `a3f9c2b1d8e5` — remove slug, add invite_code
5. `1be05745ead6` — add invite_code, is_public, member status
6. `b7d4e1f2a9c3` — add is_team_event, competition_id, team_competitor_id
7. `c4e8a2f1b9d6` — rename admin role → manager
8. `d2e5f8a3c1b7` — add `league_tournaments.multiplier` (per-league override)

New migrations still go in `alembic/versions/` with correct `down_revision` chaining, but are applied manually via psql.

## Scraper

ESPN unofficial API — no auth required, but undocumented and may change.

- `sync_schedule(db, year)` — fetch PGA Tour schedule for a year, upsert Tournaments
- `sync_tournament(db, pga_tour_id)` — sync field + score picks; routes to team or individual path based on `is_team_event`
- `full_sync(db, year)` — sync schedule then all in-progress/completed + next scheduled tournament
- `score_picks(db, tournament)` — populate `picks.points_earned` for completed tournament

**Team events (Zurich Classic):** `competition_id` on Tournament may differ from `pga_tour_id`. Earnings fetched via `team_competitor_id` (stored on TournamentEntry), then divided by 2 for per-golfer share.

Scraper jobs run daily at 06:00 UTC (schedule sync) and Monday 09:00 UTC (finalize results). Manual trigger via `POST /admin/sync`.

## Testing

```bash
# Run all tests
docker compose exec backend python -m pytest tests/ -v

# Run specific file
docker compose exec backend python -m pytest tests/test_scoring.py -v
```

Test DB: `fantasygolf_test` (separate from dev). Fixtures in `conftest.py` truncate tables after every test. Key fixtures: `client` (FastAPI TestClient), `db` (SQLAlchemy session), `auth_headers` (Authorization header dict), `registered_user` (creates user + returns token).

## Error Handling

```python
raise HTTPException(status_code=422, detail="Tournament is not in this league's schedule")
```

- Use `422` for business rule violations (invalid pick, wrong status, etc.)
- Use `404` for resource not found
- Use `403` for authorization failures
- Use `401` for authentication failures
- Services raise `HTTPException` directly — routers don't need try/catch for expected failures
