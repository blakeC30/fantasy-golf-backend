# Fantasy Golf — Backend

FastAPI + Python backend for the Fantasy Golf League platform. Handles authentication, league management, picks, standings, live score syncing from ESPN, and playoff bracket automation.

---

## Table of Contents

- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Local Development](#local-development)
- [Configuration](#configuration)
- [API Reference](#api-reference)
- [Database Schema](#database-schema)
- [Migrations](#migrations)
- [Scraper & Scheduler](#scraper--scheduler)
- [SQS Worker](#sqs-worker)
- [Authentication](#authentication)
- [Playoff System](#playoff-system)
- [Testing](#testing)
- [Error Handling](#error-handling)
- [Security](#security)

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| HTTP framework | FastAPI 0.115+ |
| ASGI server | Uvicorn (with standard extras) |
| ORM | SQLAlchemy 2.0 (typed `Mapped` columns) |
| Migrations | Alembic (manually applied — see [Migrations](#migrations)) |
| Database | PostgreSQL 15 |
| Auth | bcrypt + python-jose (JWT HS256) + google-auth (Google OAuth) |
| HTTP client | httpx (ESPN API calls) |
| Scheduler | APScheduler `BackgroundScheduler` (time-driven scraper jobs) |
| Event queue | AWS SQS (LocalStack locally, real SQS in production) |
| Email | AWS SES via boto3 (LocalStack locally, real SES in production) |
| Rate limiting | slowapi (per-IP, per-endpoint) |
| Linting / formatting | Ruff |
| Testing | pytest + pytest-asyncio |
| Package manager | uv |

**Python version:** 3.12+

---

## Project Structure

```
fantasy-golf-backend/
├── Dockerfile               # Multi-target production build (api / scraper / worker)
├── Dockerfile.dev           # Development hot-reload container
├── pyproject.toml           # Dependencies and Ruff config
├── app/
│   ├── main.py              # FastAPI app init, CORS, lifespan, router registration
│   ├── config.py            # Pydantic BaseSettings — reads .env, singleton `settings`
│   ├── database.py          # SQLAlchemy engine, SessionLocal, get_db()
│   ├── dependencies.py      # Dependency chain: auth → league → member → manager
│   ├── limiter.py           # slowapi rate-limiter instance (shared across routers)
│   ├── scraper_main.py      # Scraper container entrypoint — starts APScheduler
│   ├── worker_main.py       # Worker container entrypoint — SQS consumer loop
│   │
│   ├── models/              # SQLAlchemy ORM models
│   │   ├── base.py                      # Declarative base
│   │   ├── user.py                      # User
│   │   ├── password_reset_token.py      # PasswordResetToken (forgot-password)
│   │   ├── league.py                    # League, LeagueMember
│   │   ├── season.py                    # Season
│   │   ├── tournament.py                # Tournament, TournamentEntry, TournamentStatus
│   │   ├── tournament_entry_rounds.py   # TournamentEntryRound (per-round detail)
│   │   ├── golfer.py                    # Golfer
│   │   ├── pick.py                      # Pick (regular season)
│   │   ├── league_tournament.py         # LeagueTournament (per-league schedule + multiplier)
│   │   └── playoff.py                   # PlayoffConfig, PlayoffRound, PlayoffPod,
│   │                                    # PlayoffPodMember, PlayoffPick, PlayoffDraftPreference
│   │
│   ├── schemas/             # Pydantic request/response schemas
│   │   ├── auth.py          # RegisterRequest, LoginRequest, GoogleAuthRequest,
│   │   │                    # TokenResponse, ForgotPasswordRequest, ResetPasswordRequest
│   │   ├── user.py          # UserOut, UserUpdate
│   │   ├── league.py        # LeagueCreate/Update/Out, LeagueMemberOut, RoleUpdate,
│   │   │                    # LeagueJoinPreview, LeagueRequestOut
│   │   ├── tournament.py    # TournamentOut, LeagueTournamentOut, GolferInFieldOut,
│   │   │                    # LeaderboardOut, SyncStatusOut
│   │   ├── golfer.py        # GolferOut
│   │   ├── pick.py          # PickCreate, PickUpdate, PickOut
│   │   ├── standings.py     # StandingsRow, StandingsResponse
│   │   └── playoff.py       # PlayoffConfigOut, PlayoffBracketOut, PlayoffPodOut,
│   │                        # PlayoffPickOut, PlayoffPreferenceIn/Out, ...
│   │
│   ├── routers/             # FastAPI route handlers
│   │   ├── auth.py          # /auth/*
│   │   ├── users.py         # /users/*
│   │   ├── leagues.py       # /leagues/*
│   │   ├── tournaments.py   # /tournaments/*
│   │   ├── golfers.py       # /golfers/*
│   │   ├── picks.py         # /leagues/{id}/picks/*
│   │   ├── standings.py     # /leagues/{id}/standings
│   │   ├── playoff.py       # /leagues/{id}/playoff/*
│   │   └── admin.py         # /admin/* (platform admin only)
│   │
│   └── services/            # Business logic
│       ├── auth.py          # Password hashing, JWT, Google token verification,
│       │                    # password reset token lifecycle
│       ├── email.py         # SES email sending (reset link)
│       ├── picks.py         # Pick validation: no-repeat rule, tee-time locking
│       ├── scoring.py       # calculate_standings()
│       ├── scraper.py       # ESPN API client, tournament/field/score syncs
│       ├── scheduler.py     # APScheduler job definitions
│       ├── sqs.py           # boto3 SQS publish/consume wrapper
│       └── playoff.py       # Bracket seeding, draft resolution, scoring, advancement
│
├── alembic/
│   ├── env.py
│   └── versions/            # 21 migration files — applied manually (see Migrations)
│
└── tests/
    ├── conftest.py           # Test DB, fixtures (client, db, auth_headers, registered_user)
    ├── test_auth.py
    ├── test_picks.py
    ├── test_scraper.py
    └── test_scoring.py
```

---

## Local Development

### Prerequisites

- Python 3.12+
- Docker + Docker Compose
- `uv` (install with `pip install uv`)

### 1. Copy the environment file

```bash
cd fantasy-golf-backend
cp .env.example .env   # then edit with your values (see Configuration below)
```

At minimum, set `GOOGLE_CLIENT_ID` if you want Google OAuth locally. Everything else has sensible defaults for local dev.

### 2. Start the full stack

From the **project root** (where `docker-compose.yml` lives):

```bash
docker compose up
```

This brings up:
- `postgres` — PostgreSQL on port **5432**
- `localstack` — LocalStack (SQS + SES emulation) on port **4566**
- `backend` — FastAPI API on port **8000** (hot-reload)
- `scraper` — APScheduler process (no port)
- `worker` — SQS consumer process (no port)
- `frontend` — Vite dev server on port **5173**

Or start only the backend stack:

```bash
docker compose up postgres localstack backend
```

### 3. Apply migrations

Migrations are applied manually (see [Migrations](#migrations)):

```bash
docker exec fantasygolf-postgres-1 psql -U fantasygolf -d fantasygolf_dev -c "
  -- paste DDL from migration file here
  UPDATE alembic_version SET version_num = '<revision_id>';
"
```

### 4. Interactive API docs

Available at **http://localhost:8000/api/v1/docs** when `DEBUG=True` (default in dev).

### 5. Trigger a data sync

After starting the stack, seed tournament data from ESPN:

```bash
curl -X POST http://localhost:8000/api/v1/admin/sync \
  -H "Authorization: Bearer <your_access_token>"
```

Or target a specific tournament by its ESPN `pga_tour_id`.

### 6. Test password reset flow locally

Because LocalStack intercepts SES, no real emails are sent. The reset URL is always logged to the backend container's stdout:

```
INFO: Password reset URL for user@example.com: http://localhost:5173/reset-password?token=...
```

Copy that URL and paste it in your browser.

---

## Configuration

All settings live in `app/config.py` (Pydantic `BaseSettings`). Values are read from the `.env` file at startup.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `DATABASE_URL` | str | `postgresql://fantasygolf:fantasygolf@localhost:5432/fantasygolf_dev` | PostgreSQL connection string |
| `SECRET_KEY` | str | `change-this-...` | JWT signing key — **must be changed in production** |
| `ENVIRONMENT` | str | `development` | `"development"` or `"production"` — controls Secure cookie flag |
| `DEBUG` | bool | `True` | Enables `/docs` and `/redoc` OpenAPI endpoints |
| `FRONTEND_URL` | str | `http://localhost:5173` | CORS allowed origin — must be `https://` in production |
| `GOOGLE_CLIENT_ID` | str | `""` | Google OAuth client ID — empty disables Google auth |
| `RESET_TOKEN_EXPIRE_HOURS` | int | `1` | Password reset token TTL (hours) |
| `AWS_REGION` | str | `us-east-1` | AWS region for SES and SQS |
| `AWS_ACCESS_KEY_ID` | str | `""` | AWS credentials — empty = use EC2 IAM role in production |
| `AWS_SECRET_ACCESS_KEY` | str | `""` | AWS credentials — empty = use EC2 IAM role in production |
| `AWS_ENDPOINT_URL` | str | `""` | Override endpoint URL — set to `http://localstack:4566` in docker-compose |
| `SES_FROM_EMAIL` | str | `noreply@league-caddie.com` | Verified SES sender address |
| `SQS_QUEUE_URL` | str | `""` | SQS queue URL — if empty, publish is a no-op (safe for native dev without LocalStack) |

### Production differences

- `ENVIRONMENT=production` → cookies get `Secure=True` (HTTPS only)
- `DEBUG=False` → `/docs` and `/redoc` are disabled
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` left empty — EC2 IAM instance profile provides credentials automatically
- `AWS_ENDPOINT_URL` left empty — boto3 uses real AWS endpoints

---

## API Reference

All routes are prefixed with `/api/v1`.

### Auth

| Method | Path | Auth | Rate | Notes |
|--------|------|------|------|-------|
| POST | `/auth/register` | — | 5/hr | Create account, auto-login; returns access token |
| POST | `/auth/login` | — | 10/min | Email + password → access token + httpOnly refresh cookie |
| POST | `/auth/google` | — | 10/min | Google ID token → JWT pair; links account if email exists |
| POST | `/auth/refresh` | cookie | — | Exchange refresh cookie for new access token |
| POST | `/auth/logout` | token | — | Clear refresh cookie |
| POST | `/auth/forgot-password` | — | 3/hr | Send reset email; always returns 200 (no email enumeration) |
| POST | `/auth/reset-password` | — | 10/hr | Validate token, set new password, auto-login |

### Users

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET | `/users/me` | token | Current user profile |
| PATCH | `/users/me` | token | Update `display_name` |
| GET | `/users/me/leagues` | token | All approved leagues for the authenticated user |

### Leagues

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| POST | `/leagues` | token | Create league (creator becomes manager) |
| GET | `/leagues/join/{invite_code}` | token | Preview league before joining (no side effects) |
| GET | `/leagues/my-requests` | token | User's pending join requests across all leagues |
| POST | `/leagues/join/{invite_code}` | token | Submit join request |
| GET | `/leagues/{league_id}` | member | League details |
| PATCH | `/leagues/{league_id}` | manager | Update name, no-pick penalty |
| GET | `/leagues/{league_id}/members` | member | List approved members |
| PATCH | `/leagues/{league_id}/members/{user_id}/role` | manager | Change member role (`manager` / `member`) |
| DELETE | `/leagues/{league_id}/members/{user_id}` | manager | Remove member |
| DELETE | `/leagues/{league_id}/members/me` | member | Leave league |
| GET | `/leagues/{league_id}/requests` | manager | Pending join requests |
| POST | `/leagues/{league_id}/requests/{user_id}/approve` | manager | Approve join request |
| DELETE | `/leagues/{league_id}/requests/{user_id}` | manager | Deny / delete request |
| DELETE | `/leagues/{league_id}/requests/me` | token | Withdraw own pending request |
| GET | `/leagues/{league_id}/tournaments` | member | League schedule — returns `LeagueTournamentOut` with `effective_multiplier` and `all_r1_teed_off` |
| PUT | `/leagues/{league_id}/tournaments` | manager | Atomically replace schedule; body: `{tournaments: [{tournament_id, multiplier?}]}` |

### Tournaments

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET | `/tournaments` | token | List tournaments; optional `?status=scheduled\|in_progress\|completed` |
| GET | `/tournaments/{id}` | token | Tournament details (name, dates, purse, multiplier, status) |
| GET | `/tournaments/{id}/field` | token | Golfers in field — `GolferInFieldOut[]` with `tee_time`; WD golfers excluded |
| GET | `/tournaments/{id}/leaderboard` | token | Full leaderboard with per-round scores; includes `last_synced_at` |
| GET | `/tournaments/{id}/sync-status` | token | Lightweight: `{tournament_id, tournament_status, last_synced_at}` — poll every 30s |

### Golfers

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET | `/golfers` | token | List / search golfers (substring match on name, sorted by `world_ranking`) |
| GET | `/golfers/{id}` | token | Golfer detail (name, country, world_ranking, pga_tour_id) |

### Picks

All paths are relative to `/leagues/{league_id}`.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| POST | `/picks` | member | Submit pick for a tournament; enforces no-repeat rule and tee-time lock |
| GET | `/picks/mine` | member | My picks for the active season |
| GET | `/picks` | member | All members' picks for completed tournaments |
| GET | `/picks/tournament/{tournament_id}` | member | Pick breakdown for one tournament |
| PATCH | `/picks/{pick_id}` | member | Change golfer (only if pick is not locked) |
| PUT | `/picks/admin-override` | manager | Manager: upsert or delete any member's pick (bypasses lock; no-repeat rule still enforced) |

### Standings

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET | `/leagues/{league_id}/standings` | member | Season standings — rank, points, pick count, missed picks |

### Playoff

All paths are relative to `/leagues/{league_id}`.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| POST | `/playoff/config` | manager | Create playoff config for active season |
| GET | `/playoff/config` | member | Get playoff config |
| PATCH | `/playoff/config` | manager | Update config (only while `status=pending`) |
| GET | `/playoff/bracket` | member | Full bracket — all rounds, pods, picks |
| POST | `/playoff/rounds/{round_id}/open` | manager | Admin override: open draft for a round |
| POST | `/playoff/rounds/{round_id}/resolve` | manager | Resolve preferences → picks |
| POST | `/playoff/rounds/{round_id}/score` | manager | Score round from tournament results |
| POST | `/playoff/rounds/{round_id}/advance` | manager | Determine winners, create next-round pods |
| GET | `/playoff/pods/{pod_id}` | member | Pod detail — members, picks, visibility flag |
| GET | `/playoff/pods/{pod_id}/draft` | member | Draft status — who submitted, resolved picks |
| GET | `/playoff/pods/{pod_id}/preferences` | member | My ranked preference list for this pod |
| PUT | `/playoff/pods/{pod_id}/preferences` | member | Submit or replace ranked preference list |
| POST | `/playoff/override` | manager | Manually override pod winner |
| GET | `/playoff/my-pod` | member | Lightweight context: `{is_playoff_week, pod_id?, ...}` — always 200 |
| GET | `/playoff/my-picks` | member | My playoff picks per tournament (all rounds in active season) |

### Admin

| Method | Path | Auth | Rate | Notes |
|--------|------|------|------|-------|
| POST | `/admin/sync` | platform_admin | 5/hr | Full ESPN sync; optional `?year=2024&force=true` |
| POST | `/admin/sync/{pga_tour_id}` | platform_admin | 10/hr | Sync single tournament; optional `?force=true` |

### Health

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET | `/health` | — | Returns `{"status": "ok"}` — used by Docker and K8s healthchecks |

---

### Route Ordering Note

FastAPI matches routes in declaration order. Literal path segments **must** be defined before parameterized ones in the same prefix:

```python
# leagues.py — correct order
@router.get("/join/{invite_code}")  # before /{league_id}
@router.get("/my-requests")          # before /{league_id}
@router.get("/{league_id}")
@router.delete("/{league_id}/requests/me")      # before /{league_id}/requests/{user_id}
@router.delete("/{league_id}/requests/{user_id}")
```

---

## Database Schema

### Key Conventions

- **UUIDs** for user-facing entities (users, leagues, picks, golfers); `default=uuid.uuid4`
- **Auto-increment integers** for join tables and internal records (league_members, tournament_entries, playoff pods)
- **Timestamps** stored as `DateTime(timezone=True)` with `server_default=func.now()`
- **Status columns** stored as plain strings (`String(20)`), not PostgreSQL ENUMs — avoids migration pain
- **Multipliers** stored as floats (1.0 = standard, 1.5 = The Players, 2.0 = majors)

### Tables

| Table | Key Columns | Notes |
|-------|------------|-------|
| `users` | `id` (UUID), `email` (unique, lowercased), `password_hash` (nullable), `google_id` (unique, nullable), `display_name`, `is_platform_admin` | CHECK constraint: at least one of `password_hash` / `google_id` must be set |
| `password_reset_tokens` | `id` (UUID), `user_id` (FK→users CASCADE), `token_hash` (SHA-256, indexed), `expires_at`, `used_at` (nullable), `created_at` | Single-use, 1-hour TTL; only hash stored |
| `leagues` | `id` (UUID), `name`, `invite_code` (unique, 22-char URL-safe token), `is_public` (default false), `no_pick_penalty` (default −50000), `created_by` (FK→users) | Invite code is the join mechanism |
| `league_members` | `league_id`, `user_id`, `role` (manager\|member), `status` (pending\|approved), `joined_at` | PK = (league_id, user_id) |
| `seasons` | `id` (auto-inc), `league_id`, `year`, `is_active` | UNIQUE(league_id, year); one active season per league |
| `tournaments` | `id` (UUID), `pga_tour_id` (unique, ESPN event ID), `name`, `start_date`, `end_date`, `status` (scheduled\|in_progress\|completed), `multiplier` (float, default 1.0), `purse_usd`, `competition_id` (nullable, team events), `is_team_event`, `last_synced_at` | Populated by scraper from ESPN |
| `tournament_entries` | `id` (auto-inc), `tournament_id`, `golfer_id`, `status` (WD\|CUT\|MDF\|DQ\|null), `tee_time` (R1), `earnings_usd`, `finish_position`, `is_tied`, `team_competitor_id` | UNIQUE(tournament_id, golfer_id) |
| `tournament_entry_rounds` | `id` (auto-inc), `tournament_entry_id`, `round_number`, `tee_time` (UTC), `score`, `score_to_par`, `position`, `is_playoff` | UNIQUE(tournament_entry_id, round_number) |
| `golfers` | `id` (UUID), `pga_tour_id` (unique, ESPN athlete ID), `name`, `world_ranking`, `country` (100 chars) | Populated by scraper |
| `picks` | `id` (UUID), `league_id`, `season_id`, `user_id`, `tournament_id`, `golfer_id`, `points_earned` (nullable), `submitted_at` | UNIQUE(league_id, season_id, user_id, tournament_id); points set by scoring service |
| `league_tournaments` | `league_id`, `tournament_id`, `multiplier` (nullable) | PK = (league_id, tournament_id); NULL multiplier = inherit global |
| `playoff_configs` | `id` (UUID), `league_id`, `season_id`, `is_enabled`, `playoff_size` (power of 2), `draft_style` (snake\|linear\|top_seed_priority), `picks_per_round` (JSON int array), `status` (pending\|seeded\|complete), `seeded_at` | UNIQUE(league_id, season_id) |
| `playoff_rounds` | `id` (auto-inc), `playoff_config_id`, `round_number`, `tournament_id`, `draft_opens_at`, `draft_resolved_at`, `status` (pending\|drafting\|locked\|scored\|advanced) | UNIQUE(playoff_config_id, round_number) |
| `playoff_pods` | `id` (auto-inc), `playoff_round_id`, `bracket_position`, `winner_user_id`, `status` | UNIQUE(playoff_round_id, bracket_position) |
| `playoff_pod_members` | `id` (auto-inc), `pod_id`, `user_id`, `seed`, `draft_position`, `total_points`, `is_eliminated` | UNIQUE(pod_id, user_id) |
| `playoff_picks` | `id` (UUID), `pod_id`, `pod_member_id`, `golfer_id`, `tournament_id`, `draft_slot`, `points_earned` | UNIQUE(pod_id, golfer_id) — one golfer per pod |
| `playoff_draft_preferences` | `id` (UUID), `pod_id`, `pod_member_id`, `golfer_id`, `rank` | UNIQUE(pod_member_id, golfer_id) — ranked list per member |

### Points Formula

```
effective_multiplier = league_tournaments.multiplier   (if NOT NULL)
                     ?? tournament.multiplier            (global default)

points_earned = tournament_entry.earnings_usd × effective_multiplier
```

League managers can override the per-tournament multiplier at the league level (`league_tournaments.multiplier`). `NULL` means inherit the global default from `tournament.multiplier`.

---

## Migrations

Alembic manages migrations, but **they are NOT run automatically inside Docker**. Apply each migration manually via psql:

```bash
docker exec fantasygolf-postgres-1 psql -U fantasygolf -d fantasygolf_dev -c "
  -- Paste DDL from the migration file here
  UPDATE alembic_version SET version_num = '<new_revision_id>';
"
```

New migration files still go in `alembic/versions/` with the correct `down_revision` chain, but they are always applied manually.

### Migration History

| # | Revision | Description |
|---|----------|-------------|
| 1 | `99fbdae03d30` | Initial schema (users, leagues, seasons, tournaments, golfers, picks) |
| 2 | `6ae0425f23c9` | Expand `golfer.country` to 100 chars |
| 3 | `b721c01b567f` | Add `league_tournaments` table |
| 4 | `a3f9c2b1d8e5` | Remove `league.slug`, add `invite_code` |
| 5 | `1be05745ead6` | Rename invite_code, add `is_public`, add member `status` column |
| 6 | `b7d4e1f2a9c3` | Add `is_team_event`, `competition_id`, `team_competitor_id` to tournaments |
| 7 | `c4e8a2f1b9d6` | Rename league role `admin` → `manager` |
| 8 | `d2e5f8a3c1b7` | Add `league_tournaments.multiplier` (per-league multiplier override) |
| 9 | `e3f7a1c2d9b8` | Add `tournament_entry_round_times` table (initial per-round tee times) |
| 10 | `f1a4b7c9e2d3` | Replace `tournament_entry_round_times` with `tournament_entry_rounds` (full per-round data) |
| 11 | `c9d3f2a8e5b1` | Add `tournament_entries.is_tied` |
| 12 | `a1b2c3d4e5f6` | Add all playoff tables |
| 13 | `d4f6a2e8b1c9` | Add `league_tournaments.is_playoff` (later dropped) |
| 14 | `e5f1a9b2c3d4` | Drop `league_tournaments.is_playoff` — playoff rounds now auto-assigned |
| 15 | `e5g9b3c7f2a1` | Replace `round1_picks_per_player` + `subsequent_picks_per_player` with `picks_per_round` (JSON array) |
| 16 | `g3h5i7j9k1l2` | Drop `leagues.description` column |
| 17 | `h4i6j8k0l2m3` | Add `tournaments.last_synced_at` (UTC timestamp) |
| 18 | `i5j7k9l1m3n5` | Add `password_reset_tokens` table |
| 19 | `j6k8l0m2n4o6` | Add CHECK constraint `ck_users_has_auth_method` on `users` |
| 20 | `k7l9m1n3o5p7` | Replace case-sensitive email index with `LOWER(email)` functional unique index |
| 21 | `40a2d71cc045` | Merge heads (resolve branched migration history) |

---

## Scraper & Scheduler

The scraper runs in its own container (`python -m app.scraper_main`). It uses the **ESPN unofficial API** (no auth required) to keep tournament, field, and score data current.

### ESPN API Integration

The scraper fetches data from ESPN's internal APIs. These are undocumented and may change, but have been stable for years.

**Primary functions in `app/services/scraper.py`:**

| Function | What it does |
|----------|-------------|
| `sync_schedule(db, year)` | Fetch PGA Tour schedule; upsert Tournaments; trim post-Tour-Championship rows; publish `TOURNAMENT_COMPLETED` SQS events for status transitions |
| `sync_tournament(db, pga_tour_id)` | Sync field + per-round data + earnings; routes to team or individual path based on `is_team_event`; publishes `TOURNAMENT_IN_PROGRESS` while unresolved playoff rounds exist |
| `full_sync(db, year)` | `sync_schedule` + sync all in-progress and completed tournaments + the next scheduled one |
| `score_picks(db, tournament)` | Populate `picks.points_earned` from earnings × effective multiplier for all picks in this tournament |

**Tour Championship cutoff:** The Tour Championship is the last valid fantasy season event. Any ESPN events starting after it ends are filtered out of the schedule and any that slipped into the DB are cleaned up automatically.

**Team events (e.g. Zurich Classic):** `competition_id` may differ from `pga_tour_id`. Earnings are fetched via each golfer's `team_competitor_id` and divided by 2 for the per-golfer share.

### APScheduler Jobs

All jobs run in the scraper container (`BackgroundScheduler`). Scheduling is **status-driven, not calendar-driven** — no hardcoded weekdays.

| Job ID | Trigger | When it runs |
|--------|---------|--------------|
| `schedule_sync` | Always | Daily at 06:00 UTC |
| `field_sync_d2` | Tournament starts in 2 days | Daily at 14:00 UTC |
| `field_sync_d1` | Tournament starts tomorrow | Daily at 18:00 UTC |
| `field_sync_d0` | Tournament starts today | Daily at 11:00 UTC — tee times are now confirmed |
| `live_score_sync` | Tournament `in_progress` | Every 5 minutes, within the active play window |
| `results_finalization` | Completed tournament + unscored picks | Daily at 09:00, 15:00, 21:00 UTC (safety net) |

**Live sync play window:** Computed from `tournament_entry_rounds.tee_time` values in the DB. If tee times are available: window = `(min tee time − 30 min)` to `(max tee time + 8 hours)`. If no tee times yet: fallback window `[10:00–07:00 UTC next day]` covers all PGA Tour time zones (US East through Hawaii). Monday weather carryovers continue syncing automatically.

---

## SQS Worker

The worker runs in its own container (`python -m app.worker_main`). It consumes events from the SQS queue and handles operations that are event-driven rather than clock-driven.

### Events

| Event | Published by | Handler action |
|-------|-------------|----------------|
| `TOURNAMENT_IN_PROGRESS` | `sync_tournament()` every 5 min while in_progress + unresolved playoff rounds | `resolve_draft()` — assign picks from preference lists once `any_r1_teed_off()` is True |
| `TOURNAMENT_COMPLETED` | `sync_schedule()` on status transition | `score_picks()` → `score_round()` → `advance_bracket()` in sequence |

**All handlers are idempotent** — SQS at-least-once delivery is safe:
- `resolve_draft()` exits immediately if `draft_resolved_at` is already set
- `score_picks()` only processes picks where `points_earned IS NULL`
- `score_round()` checks `status == "locked"` before proceeding
- `advance_bracket()` verifies all pod members are scored before advancing

### Queue Configuration

- Visibility timeout: 120 seconds (prevents two worker pods from processing the same message simultaneously)
- Max receive count: 3 — after 3 failures, message moves to the dead-letter queue (DLQ)
- Long polling: 20-second wait per receive call (reduces empty-receive API costs)

**DLQ monitoring:** A non-zero DLQ depth means a finalization step failed permanently and requires manual investigation.

**Local dev:** LocalStack emulates SQS. The queue is created automatically by `localstack-init/create-queues.sh` on startup. If `SQS_QUEUE_URL` is unset, publish calls are silently skipped.

**Production:** EC2 instance profile provides credentials. Queue names: `fantasy-golf-events` / `fantasy-golf-events-dlq`.

---

## Authentication

### Flow Overview

The app supports two auth methods — both issue the same JWT pair:

1. **Email + password** — bcrypt-hashed password stored in `users.password_hash`
2. **Google OAuth** — Google ID token verified server-side; `google_id` stored in `users.google_id`

A user account can have both methods linked (e.g., registered with email, later signed in with Google using the same email address — the accounts merge automatically).

### JWT Tokens

| Token | Lifetime | Storage |
|-------|----------|---------|
| Access token | 15 minutes | Memory (JavaScript) — sent as `Authorization: Bearer <token>` header |
| Refresh token | 7 days | httpOnly cookie — never accessible from JavaScript |

Cookie security flags:
- `httpOnly=True` — JS cannot read the refresh token
- `Secure=True` (production only) — only sent over HTTPS
- `SameSite=Lax` — CSRF protection

### Password Reset Flow

1. User submits email to `POST /auth/forgot-password`
2. If user exists and has a password (not Google-only), a `PasswordResetToken` is created:
   - Raw token: `secrets.token_urlsafe(32)` (URL-safe, 256 bits of entropy)
   - Only the SHA-256 hash is stored in the DB
   - Token expires in 1 hour; single-use (`used_at` set on redemption)
3. AWS SES sends an email with the reset link. **The reset URL is always logged to the backend console** for local dev testing.
4. User submits new password to `POST /auth/reset-password` — token is validated, password is updated, token is consumed, and the user is auto-logged in.

The response is always `200` regardless of whether the email exists, preventing email enumeration.

### Dependency Chain

```
get_current_user              ← validates JWT access token from Authorization header
  └─ require_platform_admin   ← checks is_platform_admin flag
  └─ get_league_or_404        ← looks up League by league_id path param
       └─ require_league_member    ← checks approved membership in league
            └─ require_league_manager   ← checks manager role
  └─ get_active_season        ← loads the active Season for the league
```

FastAPI caches dependency results within a single request — each dependency runs at most once even if multiple route parameters depend on it.

---

## Playoff System

The playoff system is fully automated once configured. The league manager sets the configuration; the system handles seeding, draft resolution, scoring, and bracket advancement.

### Configuration

Managers set:
- `playoff_size` — number of qualifying players (0, 2, 4, 8, 16, or 32)
- `draft_style` — how draft order is determined within a pod (`snake`, `linear`, `top_seed_priority`)
- `picks_per_round` — JSON array of integers, one per round (e.g. `[2, 1]` = 2 picks in R1, 1 pick thereafter)

### Bracket Seeding

The bracket is seeded automatically when all three conditions are met:
1. All regular-season tournaments have completed
2. Only playoff-round tournaments remain as "scheduled"
3. Official earnings from the last regular-season tournament have been published

No manager action is required. Bracket position and pod assignments are determined by regular-season standings.

### Playoff Rounds

Each round follows this lifecycle:
1. **Drafting** — members submit ranked preference lists (opens automatically when bracket is seeded for R1, or when previous round advances)
2. **Locked** — picks are resolved from preferences once the first Round 1 tee time passes (`resolve_draft`)
3. **Scored** — points are populated from tournament earnings (`score_round`)
4. **Advanced** — pod winners determined, next round's pods created (`advance_bracket`)

### Pick Visibility

Regular season and playoff picks follow different visibility rules:
- **Regular season**: all members' picks hidden until **all** Round 1 tee times have passed
- **Playoff**: picks hidden until **any** Round 1 tee time has passed (first golfer tees off)

The `is_picks_visible` flag on `PlayoffPodOut` communicates this to the frontend. The backend never sends hidden picks in the API response — they are filtered server-side.

---

## Testing

### Setup

Create the test database once:

```bash
docker compose exec postgres psql -U fantasygolf -d fantasygolf_dev \
  -c "CREATE DATABASE fantasygolf_test;"
```

### Running Tests

```bash
# All tests
docker compose exec backend python -m pytest tests/ -v

# Specific file
docker compose exec backend python -m pytest tests/test_auth.py -v

# Specific test
docker compose exec backend python -m pytest tests/test_picks.py::test_no_repeat_rule -v

# With coverage
docker compose exec backend python -m pytest tests/ --cov=app --cov-report=term-missing
```

### Test Fixtures (`conftest.py`)

| Fixture | Scope | What it provides |
|---------|-------|-----------------|
| `create_tables` | session | Create / drop all tables once per test session |
| `clean_db` | function | `TRUNCATE ... CASCADE` after every test (auto-use) |
| `db` | function | SQLAlchemy session connected to test database |
| `client` | function | FastAPI `TestClient` with `get_db` dependency overridden to use test DB |
| `registered_user` | function | Creates a test user, returns the access token |
| `auth_headers` | function | `{"Authorization": "Bearer <token>"}` dict for authenticated requests |

### Test Files

| File | What it covers |
|------|---------------|
| `test_auth.py` | Register, login, Google OAuth, refresh, logout, forgot-password, reset-password, rate limits |
| `test_picks.py` | Pick submission, no-repeat rule, tee-time locking, pick change, manager override |
| `test_scraper.py` | ESPN API response parsing, field sync, earnings sync, team events |
| `test_scoring.py` | `calculate_standings()`, multiplier math, no-pick penalty, playoff scoring |

---

## Error Handling

Services raise `HTTPException` directly with descriptive `detail` strings. Routers do not need try/catch for expected business rule failures — FastAPI handles `HTTPException` and returns a JSON response automatically.

```python
# Example from services/picks.py
raise HTTPException(status_code=422, detail="Golfer has already been picked this season")
```

### HTTP Status Code Conventions

| Code | When used |
|------|-----------|
| `200` | Successful request |
| `201` | Resource created |
| `204` | Success, no content (e.g. logout) |
| `400` | Malformed request |
| `401` | Authentication required or failed (invalid / expired token) |
| `403` | Authorization failed (not a member, not a manager, pending status) |
| `404` | Resource not found |
| `409` | Conflict (duplicate email on register, duplicate pick) |
| `422` | Business rule violation (wrong tournament status, repeat golfer, schedule locked) |
| `429` | Rate limit exceeded |
| `500` | Unexpected server error |
| `501` | Not implemented (Google OAuth requested but `GOOGLE_CLIENT_ID` is empty) |

---

## Security

- **Passwords** hashed with bcrypt (cost factor auto-determined by library)
- **JWTs** signed with HS256; access tokens are short-lived (15 min)
- **Refresh tokens** stored in httpOnly cookies — never accessible to JavaScript
- **Google OAuth** verified server-side using `google-auth` library; ID token never trusted without verification
- **Password reset tokens** use `secrets.token_urlsafe(32)` for the raw token; only the SHA-256 hash is stored in the DB; single-use with 1-hour expiry
- **Email normalization** — all emails lowercased before storage and lookup; enforced at DB level with a `LOWER(email)` functional unique index
- **Auth method constraint** — DB CHECK constraint ensures every user has at least one auth method (`password_hash IS NOT NULL OR google_id IS NOT NULL`)
- **No email enumeration** — `POST /auth/forgot-password` always returns `200`
- **CORS** — restricted to `FRONTEND_URL` only (never `*`); `allow_credentials=True` for cookies
- **Rate limiting** — per-IP limits on auth and write endpoints via slowapi
- **Non-root container** — production Docker image runs as a non-root `appuser`
- **Secure cookies** — `Secure=True` in production (HTTPS only), `SameSite=Lax` (CSRF protection)
- **AWS credentials** — never stored in env vars in production; EC2 IAM instance profile provides them automatically
