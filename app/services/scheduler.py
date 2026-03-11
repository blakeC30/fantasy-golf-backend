"""
APScheduler background job scheduler — scraper container only.

This module is imported by app/scraper_main.py (the scraper container
entrypoint). It is NOT imported by app/main.py — the API container runs
without a scheduler. Keeping them separate means scraper failures cannot
take down the API, and the two containers can be deployed independently.

Jobs
----
  schedule_sync       (daily at 06:00 UTC)
    Fetches the full PGA Tour season schedule and upserts tournaments.
    Always runs — status transitions, rescheduling, and cancellations need
    to be reflected promptly.

  field_sync_d2       (daily at 14:00 UTC)
    Syncs the field for any tournament starting in exactly 2 days (fields
    are typically announced Mon–Tue, 2 days before the Thursday start).
    Anchored to start_date, not a fixed weekday, so non-Thursday starts
    (alternate events, rain-delayed reschedules) are handled correctly.

  field_sync_d1       (daily at 18:00 UTC)
    Syncs the field for any tournament starting tomorrow. Catches late
    additions, alternates, and withdrawals.

  field_sync_d0       (daily at 11:00 UTC)
    Syncs the field for any tournament starting today. This is the critical
    run — tee times are confirmed by now, and locked picks depend on them.

  live_score_sync     (every 10 minutes, all days)
    Syncs live scores whenever a tournament is in_progress AND the current
    UTC time falls within the expected play window. The play window is
    derived from actual tee times stored in the DB (timezone-agnostic),
    with a wide conservative fallback if tee times haven't been loaded yet.

    Critically: there is NO day-of-week restriction. If a tournament is
    in_progress on Monday due to a weather delay or playoff carryover,
    live sync continues automatically.

  results_finalization  (daily at 09:00, 15:00, and 21:00 UTC)
    Finds any completed tournament with unscored picks and runs score_picks().
    Runs three times per day so it catches any finish time on any day of the
    week — a Monday afternoon finish is caught by the 21:00 UTC run; a
    Tuesday finish is caught the next morning.

Why BackgroundScheduler (not AsyncIOScheduler)?
-----------------------------------------------
SQLAlchemy sessions and httpx calls are synchronous. Running them on the
asyncio event loop would block it. BackgroundScheduler runs each job in a
thread from its own thread pool, completely separate from asyncio — safe
and correct.

Play window algorithm
---------------------
For live_score_sync, "should we run now?" is computed as follows:

  1. Query tournament_entry_rounds for tee times where:
       - tournament_id = active in_progress tournament
       - date(tee_time) = today (UTC)
       - tee_time IS NOT NULL

  2a. If tee times exist:
        play_start = min(tee_times) - 30 min  (pre-round buffer)
        play_end   = max(tee_times) + 5 hours (generous finish buffer)
        → skip if current UTC time is outside [play_start, play_end]

  2b. If no tee times in DB yet (field not synced, or tee times unreleased):
        Use wide fallback: [10:00 UTC, 07:00 UTC next day]
        Covers US East (earliest tee ~10:30 UTC) through Hawaii (latest
        finish ~06:00 UTC). Wide enough for any PGA Tour location worldwide.
"""

import logging
from datetime import date, datetime, time as dt_time, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger(__name__)

# Module-level scheduler — created once, shared by start/stop calls.
_scheduler = BackgroundScheduler(timezone="UTC")


# ---------------------------------------------------------------------------
# Play window helper
# ---------------------------------------------------------------------------

def _is_within_play_window(db, tournament) -> bool:
    """
    Return True if the current UTC time is within today's expected play window
    for the given in_progress tournament.

    Uses stored tee times from tournament_entry_rounds when available; falls
    back to a wide conservative window that covers all PGA Tour time zones.
    """
    # Lazy imports to avoid circular imports at module load time.
    from sqlalchemy import func
    from app.models import TournamentEntry, TournamentEntryRound

    now_utc = datetime.now(tz=timezone.utc)
    today_utc = now_utc.date()

    tee_time_rows = (
        db.query(TournamentEntryRound.tee_time)
        .join(TournamentEntry, TournamentEntryRound.tournament_entry_id == TournamentEntry.id)
        .filter(
            TournamentEntry.tournament_id == tournament.id,
            TournamentEntryRound.tee_time.isnot(None),
            func.date(TournamentEntryRound.tee_time) == today_utc,
        )
        .limit(500)  # cap the scan — we only need min/max
        .all()
    )

    if tee_time_rows:
        raw_times = [row.tee_time for row in tee_time_rows if row.tee_time is not None]
        if raw_times:
            # Normalise to UTC-aware datetimes (SQLAlchemy may return naive or aware).
            aware_times = [
                t if t.tzinfo is not None else t.replace(tzinfo=timezone.utc)
                for t in raw_times
            ]
            play_start = min(aware_times) - timedelta(minutes=30)
            play_end = max(aware_times) + timedelta(hours=5)
            in_window = play_start <= now_utc <= play_end
            log.debug(
                "Play window from DB tee times: [%s – %s], now=%s, in_window=%s",
                play_start.strftime("%H:%M UTC"),
                play_end.strftime("%H:%M UTC"),
                now_utc.strftime("%H:%M UTC"),
                in_window,
            )
            return in_window

    # Fallback: tee times not yet in the DB (field not released yet, or this is
    # the first sync of the day). Use a wide window that safely covers every PGA
    # Tour location:
    #   - US East summer (UTC-4): first tee ~10:30 UTC, last finish ~00:00 UTC
    #   - Hawaii (UTC-10):        first tee ~16:30 UTC, last finish ~06:00 UTC
    # So [10:00 UTC, 07:00 UTC next day] covers the full range worldwide.
    wide_start = datetime.combine(today_utc, dt_time(10, 0), tzinfo=timezone.utc)
    wide_end = datetime.combine(today_utc + timedelta(days=1), dt_time(7, 0), tzinfo=timezone.utc)
    in_window = wide_start <= now_utc <= wide_end
    log.debug(
        "No tee times in DB — wide fallback window [10:00–07:00 UTC], in_window=%s",
        in_window,
    )
    return in_window


# ---------------------------------------------------------------------------
# Job functions
# ---------------------------------------------------------------------------

def _run_schedule_sync() -> None:
    """Daily at 06:00 UTC: fetch and upsert the full PGA Tour season schedule."""
    from app.database import SessionLocal
    from app.services.scraper import sync_schedule

    db = SessionLocal()
    try:
        year = date.today().year
        result = sync_schedule(db, year)
        log.info("Schedule sync complete: %s", result)
    except Exception as exc:
        log.error("Schedule sync failed: %s", exc, exc_info=True)
    finally:
        db.close()


def _run_field_sync(days_before_start: int) -> None:
    """
    Sync the field for any tournament whose start_date is `days_before_start`
    days from today.

    Anchors to start_date rather than a fixed weekday so that non-Thursday
    starts (alternate events, rain-rescheduled events) are handled correctly.
    Handles simultaneous events in the same week (e.g. two-event weeks).
    """
    from app.database import SessionLocal
    from app.models import Tournament, TournamentStatus
    from app.services.scraper import sync_tournament

    db = SessionLocal()
    try:
        target_date = date.today() + timedelta(days=days_before_start)
        tournaments = (
            db.query(Tournament)
            .filter(
                Tournament.status == TournamentStatus.SCHEDULED.value,
                Tournament.start_date == target_date,
            )
            .all()
        )
        if not tournaments:
            log.debug("Field sync (d%d): no tournament starting on %s", days_before_start, target_date)
            return
        for tournament in tournaments:
            log.info(
                "Field sync (d%d): syncing field for '%s' (start=%s)",
                days_before_start,
                tournament.name,
                target_date,
            )
            sync_tournament(db, tournament.pga_tour_id)
    except Exception as exc:
        log.error("Field sync (d%d) failed: %s", days_before_start, exc, exc_info=True)
    finally:
        db.close()


def _run_live_score_sync() -> None:
    """
    Every 10 minutes: sync live scores for any in_progress tournament,
    but only if the current time falls within the computed play window.

    No day-of-week restriction — if a tournament is in_progress on Monday
    due to a weather delay or playoff carryover, this job continues running.

    Safety guard: if a tournament's end_date is more than 3 days in the past
    but its status is still in_progress, it means ESPN hasn't reported
    STATUS_FINAL yet (or schedule_sync encountered an error). We skip it and
    log a warning. The next daily schedule_sync will correct the status once
    ESPN updates. 3 days gives enough buffer for Monday playoff holes and
    Tuesday weather-delay finishes.
    """
    from app.database import SessionLocal
    from app.models import Tournament, TournamentStatus
    from app.services.scraper import sync_tournament

    db = SessionLocal()
    try:
        active_tournaments = (
            db.query(Tournament)
            .filter_by(status=TournamentStatus.IN_PROGRESS.value)
            .all()
        )
        if not active_tournaments:
            return  # Nothing to do — skip silently (fires every 10 min)

        today_utc = datetime.now(tz=timezone.utc).date()

        for tournament in active_tournaments:
            # Safety guard: skip tournaments whose end_date is stale.
            if tournament.end_date and (today_utc - tournament.end_date).days > 3:
                log.warning(
                    "Live sync: skipping '%s' — end_date %s is %d days ago but status is "
                    "still in_progress. schedule_sync will correct once ESPN updates.",
                    tournament.name,
                    tournament.end_date,
                    (today_utc - tournament.end_date).days,
                )
                continue

            if not _is_within_play_window(db, tournament):
                log.debug("Live sync: outside play window for '%s', skipping", tournament.name)
                continue
            log.info("Live sync: syncing scores for in_progress '%s'", tournament.name)
            sync_tournament(db, tournament.pga_tour_id)
    except Exception as exc:
        log.error("Live score sync failed: %s", exc, exc_info=True)
    finally:
        db.close()


def _run_results_finalization() -> None:
    """
    Daily at 09:00, 15:00, 21:00 UTC: score picks for any completed tournament
    that still has unscored picks.

    Runs three times per day so it catches any finish time on any day of the
    week without relying on Monday-specific logic:
      - 09:00 UTC: catches Sunday finishes (official earnings posted overnight)
      - 15:00 UTC: catches Monday morning finishes or mid-morning corrections
      - 21:00 UTC: catches Monday afternoon finishes or late-posted results
    """
    from app.database import SessionLocal
    from app.models import Pick, Tournament, TournamentStatus
    from app.services.scraper import score_picks

    db = SessionLocal()
    try:
        # Find completed tournaments where at least one pick has no score yet.
        tournaments_needing_scoring = (
            db.query(Tournament)
            .join(Pick, Pick.tournament_id == Tournament.id)
            .filter(
                Tournament.status == TournamentStatus.COMPLETED.value,
                Pick.points_earned.is_(None),
            )
            .distinct()
            .all()
        )
        if not tournaments_needing_scoring:
            log.debug("Results finalization: no tournaments with unscored picks")
            return

        for tournament in tournaments_needing_scoring:
            log.info(
                "Results finalization: scoring picks for completed '%s'",
                tournament.name,
            )
            try:
                count = score_picks(db, tournament)
                log.info(
                    "Results finalization: scored %d picks for '%s'",
                    count,
                    tournament.name,
                )
            except Exception as exc:
                # Roll back the failed transaction so the next tournament can proceed.
                db.rollback()
                log.error(
                    "Results finalization: failed to score '%s': %s",
                    tournament.name,
                    exc,
                    exc_info=True,
                )
    except Exception as exc:
        log.error("Results finalization failed: %s", exc, exc_info=True)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------

def start_scheduler() -> None:
    """
    Register all sync jobs and start the scheduler.

    Called once from app/scraper_main.py during scraper container startup.
    Jobs are registered here (not at module level) so they aren't active
    during test collection, which would cause spurious DB connections.
    """
    if _scheduler.running:
        log.warning("Scheduler already running — skipping start")
        return

    # ── 1. Schedule sync ──────────────────────────────────────────────────
    # Daily at 06:00 UTC. Runs year-round; handles status transitions,
    # rescheduling, and newly announced tournaments.
    _scheduler.add_job(
        _run_schedule_sync,
        CronTrigger(hour=6, minute=0),
        id="schedule_sync",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # ── 2. Field syncs (3 runs per tournament week) ───────────────────────
    # Anchored to start_date, not a fixed weekday. Each job checks whether
    # any tournament starts exactly N days from today.

    # d2: initial field release (typically 2 days before start)
    _scheduler.add_job(
        lambda: _run_field_sync(days_before_start=2),
        CronTrigger(hour=14, minute=0),
        id="field_sync_d2",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # d1: catch late changes, alternates, and withdrawals
    _scheduler.add_job(
        lambda: _run_field_sync(days_before_start=1),
        CronTrigger(hour=18, minute=0),
        id="field_sync_d1",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # d0: morning-of-start — tee times are confirmed by now (critical for pick-locking)
    _scheduler.add_job(
        lambda: _run_field_sync(days_before_start=0),
        CronTrigger(hour=11, minute=0),
        id="field_sync_d0",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # ── 3. Live score sync ────────────────────────────────────────────────
    # Fires every 10 minutes. The job itself decides whether to do real work
    # by checking: (a) is there an in_progress tournament? (b) are we within
    # the computed play window? If either check fails, the job exits in <1ms.
    # No day-of-week restriction — handles Monday/Tuesday weather carryovers.
    _scheduler.add_job(
        _run_live_score_sync,
        CronTrigger(minute="*/10"),
        id="live_score_sync",
        replace_existing=True,
        misfire_grace_time=300,  # 5-minute grace (shorter — freshness matters)
        max_instances=1,         # prevent overlapping runs if ESPN is slow
    )

    # ── 4. Results finalization ───────────────────────────────────────────
    # 3× daily to catch any finish time on any day of the week.
    # No day-of-week restriction — a Tuesday finish is caught the next morning.
    _scheduler.add_job(
        _run_results_finalization,
        CronTrigger(hour="9,15,21", minute=0),
        id="results_finalization",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    _scheduler.start()
    log.info("Scraper scheduler started. Jobs: %s", [j.id for j in _scheduler.get_jobs()])


def stop_scheduler() -> None:
    """Stop the scheduler gracefully. Called during scraper container shutdown."""
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("Scraper scheduler stopped")
