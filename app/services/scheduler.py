"""
APScheduler background job scheduler.

Runs two recurring jobs inside the FastAPI process — no Celery, no Redis,
no extra infrastructure needed.

Jobs
----
  daily_sync   (daily at 06:00 UTC)
    Fetches the PGA Tour schedule for the current year and syncs any
    in-progress or recently completed tournaments. Runs every day to catch
    mid-week results and schedule changes.

  monday_sync  (every Monday at 09:00 UTC)
    Runs after Sunday's final round. Same as daily_sync but on the day
    most likely to have newly completed tournament results.

Why BackgroundScheduler (not AsyncIOScheduler)?
------------------------------------------------
Our SQLAlchemy sessions and httpx calls are all synchronous. Running them
on the asyncio event loop would block it. BackgroundScheduler runs each
job in a thread from its own thread pool, completely separate from
asyncio — safe and simple.

Integration with FastAPI
------------------------
The scheduler is started and stopped via FastAPI's lifespan context manager
in app/main.py. This guarantees clean shutdown when the server receives
SIGTERM (e.g. from Kubernetes).
"""

import logging
from datetime import date

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger(__name__)

# Module-level scheduler instance — created once, shared by start/stop calls.
_scheduler = BackgroundScheduler(timezone="UTC")


def _run_full_sync() -> None:
    """
    Job function: import dependencies lazily to avoid circular imports at
    module load time (scheduler.py is imported by main.py before app is fully
    initialised).
    """
    from app.database import SessionLocal
    from app.services.scraper import full_sync

    year = date.today().year
    db = SessionLocal()
    try:
        result = full_sync(db, year)
        log.info("Scheduled sync complete: %s", result)
    except Exception as exc:
        log.error("Scheduled sync failed: %s", exc, exc_info=True)
    finally:
        db.close()


def start_scheduler() -> None:
    """
    Register all jobs and start the scheduler.

    Called once during application startup (FastAPI lifespan).
    Adding jobs here (not at module level) ensures they aren't registered
    during test collection, which would cause spurious DB connections.
    """
    if _scheduler.running:
        log.warning("Scheduler already running — skipping start")
        return

    # Daily at 06:00 UTC — catch overnight schedule updates and mid-week results.
    _scheduler.add_job(
        _run_full_sync,
        CronTrigger(hour=6, minute=0),
        id="daily_sync",
        replace_existing=True,
        misfire_grace_time=3600,  # if server was down, run within 1 hour of scheduled time
    )

    # Monday at 09:00 UTC — finalize Sunday's results a bit after they're posted.
    _scheduler.add_job(
        _run_full_sync,
        CronTrigger(day_of_week="mon", hour=9, minute=0),
        id="monday_sync",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    _scheduler.start()
    log.info("Scheduler started. Jobs: %s", [j.id for j in _scheduler.get_jobs()])


def stop_scheduler() -> None:
    """Stop the scheduler gracefully. Called during application shutdown."""
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("Scheduler stopped")
