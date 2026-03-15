# Fantasy Golf League Website

## Project Purpose

A web application to modernize a private fantasy golf league that currently runs on spreadsheets and Google Forms. The goal is to replace manual tracking with a real website that handles picks, scoring, and standings automatically.

After the first season (proving it works for the private league), the vision is to open it to the public and potentially monetize it.

## Fantasy Golf Rules

- Each player's score starts at 0 at the beginning of the season.
- Each week, a player picks one golfer competing in that week's PGA tournament.
- Points earned = dollars earned by the chosen golfer that week.
- **No repeat golfers**: once a golfer is used in a season, that golfer cannot be picked again.
- **Tournament multiplier**: each tournament has a `multiplier` (float, default `1.0`). Points earned = golfer earnings × multiplier. Majors use `2.0`. Other special events may use other values.
- Points accumulate week over week throughout the season.
- The player with the most accumulated points at the end of the season wins.

### Playoff Member Departure Rules

When a member **leaves or is removed** during a season with an active playoff config:

**Case A — Regular season still in progress** (last non-playoff tournament in the league schedule has NOT yet completed):
- The playoff bracket size auto-shrinks to the largest valid bracket size (power of 2: 2, 4, 8, 16, 32) that fits the remaining member count.
- Example: 8-member playoff, one member leaves → 7 remain → bracket reduces to 4.
- If fewer than 2 members remain, the playoff size is set to 0 (disabled).
- The `picks_per_round` array is left unchanged; the manager may adjust it manually.

**Case B — Regular season has ended** (last non-playoff tournament in the league schedule is completed, schedule is locked):
- The bracket size stays the same — the departed member's slot becomes a bye.
- Their pod slot is marked `is_eliminated=True` so it can never win.
- Their pending picks and preference lists are deleted so all their draft slots score as no-picks.
- Completed rounds (already scored and advanced) are not touched — prior-round history is permanent.

## Tech Stack

### Frontend
- React + TypeScript + Vite
- Tailwind CSS for styling
- Zustand for state management
- React Query (TanStack Query) for data fetching
- React Router for navigation

### Backend
- FastAPI + Python
- SQLAlchemy 2.0 ORM
- Alembic for database migrations
- PostgreSQL database
- Auth: email/password (bcrypt) OR Google OAuth (ID token flow) — both issue the same JWT pair
- JWT (access token 15 min + refresh token 7 days, httpOnly cookie)
- `google-auth` Python library for verifying Google ID tokens server-side
- APScheduler for scheduled scraping jobs
- httpx for async HTTP (ESPN unofficial API for PGA Tour data)
- Ruff for linting/formatting
- pytest for tests

### Infrastructure
- Docker (multi-stage builds for small images)
- K3s (lightweight Kubernetes — NOT EKS, which costs $72/month)
- Helm 3 for Kubernetes package management
- AWS: EC2 t2.micro — all services (frontend, backend, Postgres) run here, no RDS
- Postgres runs inside K3s as a Deployment with a PersistentVolumeClaim (data on EBS)
- AWS ECR for container image registry
- Dev and Production namespaces on the same K3s cluster

### CI/CD
- GitHub Actions (free — repo is public)
- Pipeline: lint+test → build Docker images → push to ECR → helm deploy
- PRs: test only. Push to `dev`: deploy to dev namespace. Push to `main`: deploy to prod.

## Subagent Delegation

This monorepo has two distinct sub-projects. When a task is scoped to one of them, delegate to the appropriate subagent rather than working directly in the root conversation.

### When to delegate

| Task involves… | Delegate to |
|---|---|
| Any file under `fantasy-golf-frontend/` | **frontend subagent** — cd into `fantasy-golf-frontend/`, follow `fantasy-golf-frontend/CLAUDE.md` |
| Any file under `fantasy-golf-backend/` | **backend subagent** — cd into `fantasy-golf-backend/`, follow `fantasy-golf-backend/CLAUDE.md` |
| Cross-cutting (e.g. new feature touching both) | Spawn **both** subagents in parallel; coordinate via this root CLAUDE.md |
| Infrastructure only (`helm/`, `docker-compose.yml`, `.github/`) | Work in root; no subagent needed |

### How to delegate

Use the `Agent` tool with `subagent_type: general-purpose` and set the working directory context in the prompt. Always pass:
- The full task description
- Relevant file paths
- Any cross-project constraints (e.g. API contract changes the backend is making that the frontend must match)

The subagent should read the subfolder CLAUDE.md at the start of its task to pick up conventions.

---

## CLAUDE.md Maintenance

This project has three CLAUDE.md files — keep all three current:

- `CLAUDE.md` (this file) — project-wide rules, domain logic, tech stack, guiding principles
- `fantasy-golf-frontend/CLAUDE.md` — frontend-specific: routes, components, hooks, React Query keys, styling conventions
- `fantasy-golf-backend/CLAUDE.md` — backend-specific: endpoints, models, dependency chain, migration process, scraper

**When to update them:**
- New page, component, or hook added → update `fantasy-golf-frontend/CLAUDE.md`
- New endpoint, model column, or schema added → update `fantasy-golf-backend/CLAUDE.md`
- New migration applied → add it to the migration list in `fantasy-golf-backend/CLAUDE.md`
- New React Query cache key introduced → add it to the cache key table in `fantasy-golf-frontend/CLAUDE.md`
- Tech stack or architecture decision changes → update this file

Update the relevant CLAUDE.md as part of the same task, not as a separate follow-up.

## Guiding Principles

### Cost — Top Priority Constraint
This project must be as close to free as possible to run. Every infrastructure and tooling decision must be evaluated through the lens of cost. Prefer:
- Free tiers (AWS Free Tier, GitHub Actions free minutes, etc.)
- Lightweight resource usage (small instance sizes, minimal replicas)
- Open-source tools over paid SaaS
- Self-hosted solutions where they save money without adding excessive complexity

**Do not suggest or implement anything that will result in ongoing cloud costs without explicitly flagging the cost and getting approval.**

### Learning-Oriented
The developer is learning frontend, backend, infrastructure, and cloud fundamentals. Prefer:
- Clear, conventional approaches over clever shortcuts
- Patterns that a professional engineer would use in production
- Explanations alongside implementation choices when appropriate
- **After every change, briefly explain what was done and why in plain terms** — what problem it solves, what pattern it uses, and any important trade-offs. Keep it concise but educational.

### Security
- Follow OWASP best practices
- Never expose secrets or credentials
- Sanitize all user inputs
- Authentication and authorization must be properly implemented

### Code Quality
- Keep code simple and readable
- Avoid over-engineering
- Write tests for meaningful logic
- Don't add features beyond what is currently needed

## Multi-League Architecture
- The app supports multiple independent leagues from day one
- Each league has its own members, picks, seasons, and standings
- Users create a platform account and can join multiple leagues
- League admins manage members via invite links

## Project Structure

```
FantasyGolf/
├── CLAUDE.md
├── docker-compose.yml            # Local development
├── helm/
│   └── fantasy-golf/             # Helm chart for K8s deployments
├── .github/
│   └── workflows/ci-cd.yml       # GitHub Actions pipeline
├── fantasy-golf-frontend/        # React + TypeScript app
└── fantasy-golf-backend/         # FastAPI + Python app
    ├── app/
    │   ├── main.py
    │   ├── config.py
    │   ├── database.py
    │   ├── dependencies.py
    │   ├── models/
    │   ├── schemas/
    │   ├── routers/
    │   └── services/
    ├── alembic/
    └── tests/
```

## Implementation Phases (ordered)
0. Foundation & project setup
1. Database schema & migrations
2. Backend core (FastAPI, auth, API)
5. Docker (containerize early)
3. Web scraping (ESPN API + APScheduler)
4. Frontend (React pages and components)
6. Helm charts (test locally with k3d)
8. AWS setup (EC2, ECR — no RDS)
7. CI/CD pipeline (GitHub Actions)
9. Dev/prod environments (K8s namespaces)

## Environments

- **Dev**: K8s `dev` namespace, `fantasygolf_dev` database, deployed from `dev` branch
- **Production**: K8s `prod` namespace, `fantasygolf_prod` database, deployed from `main` branch
- **Local**: docker-compose (postgres on :5432, backend on :8000, frontend on :5173)
