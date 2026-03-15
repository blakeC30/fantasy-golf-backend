"""
Pick reminder service — Wednesday email reminders for unpicked league members.

The Wednesday APScheduler job calls `create_and_send_pick_reminders(db)` once per
week. It:
  1. Finds all scheduled PGA tournaments starting within the next 7 days.
  2. For each tournament, finds all leagues that have it in their schedule.
  3. Skips any league with no active season (out-of-season — silent skip).
  4. Creates a PickReminder row (idempotent — UNIQUE constraint prevents duplicates).
  5. Immediately sends emails to all approved league members who:
       - have not yet submitted a pick for this tournament in the active season, AND
       - have pick_reminders_enabled = True.
  6. Marks reminder sent_at on full success; increments attempt_count on failure.
     After max_attempts failures, sets failed_at so the row is permanently skipped.

The email CTA adjusts based on whether the pick window is currently open:
  - If the tournament is in_progress OR is the globally-next scheduled tournament
    with no other tournament in_progress, the window is open → "Submit your pick".
  - Otherwise the window is not yet open → "Picks open soon" message (no CTA link).
"""

import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def _is_pick_window_open(db: Session, tournament) -> bool:
    """
    Return True if members can currently submit picks for this tournament.

    Mirrors the frontend pickWindowOpen logic:
      - Tournament is in_progress → always open (until R1 tees off)
      - No globally in_progress tournament AND this is the globally-next
        scheduled tournament → open
    """
    from app.models import Tournament, TournamentStatus

    if tournament.status == TournamentStatus.IN_PROGRESS.value:
        return True

    has_global_in_progress = (
        db.query(Tournament)
        .filter(Tournament.status == TournamentStatus.IN_PROGRESS.value)
        .first()
    ) is not None

    if has_global_in_progress:
        return False

    globally_next = (
        db.query(Tournament)
        .filter(Tournament.status == TournamentStatus.SCHEDULED.value)
        .order_by(Tournament.start_date.asc())
        .first()
    )
    return globally_next is not None and tournament.id == globally_next.id


def _send_reminder_for_league(
    db: Session,
    reminder,
    tournament,
    pick_window_open: bool,
) -> tuple[int, int]:
    """
    Send pick reminder emails to all unpicked, opted-in, approved members of
    the reminder's league for the given tournament + season.

    Returns (sent_count, skipped_count).
    Raises on SES error so the caller can handle attempt_count / failed_at.
    """
    from app.models import LeagueMember, LeagueMemberStatus, Pick, User
    from app.services.email import send_pick_reminder_email

    league_id = reminder.league_id
    season_id = reminder.season_id
    tournament_id = tournament.id

    # All approved members of this league.
    members = (
        db.query(LeagueMember)
        .filter_by(league_id=league_id, status=LeagueMemberStatus.APPROVED.value)
        .all()
    )

    sent = 0
    skipped = 0
    for member in members:
        user: User = db.get(User, member.user_id)
        if user is None:
            skipped += 1
            continue

        # Respect opt-out preference.
        if not user.pick_reminders_enabled:
            skipped += 1
            continue

        # Skip users who already submitted a pick.
        already_picked = (
            db.query(Pick)
            .filter_by(
                league_id=league_id,
                season_id=season_id,
                user_id=member.user_id,
                tournament_id=tournament_id,
            )
            .first()
        )
        if already_picked:
            skipped += 1
            continue

        send_pick_reminder_email(
            to_email=user.email,
            display_name=user.display_name,
            league_name=reminder.league.name,
            league_id=str(league_id),
            tournament_name=tournament.name,
            start_date=tournament.start_date.strftime("%B %-d"),
            pick_window_open=pick_window_open,
        )
        sent += 1

    return sent, skipped


def create_and_send_pick_reminders(db: Session) -> dict:
    """
    Entry point called by the Wednesday APScheduler job.

    Finds all scheduled PGA tournaments starting within the next 7 days,
    creates PickReminder rows for each affected active-season league,
    then immediately sends emails to unpicked members.

    Returns a summary dict: {"sent": int, "failed": int, "skipped": int, "errors": list}.
    """
    from app.models import (
        LeagueTournament,
        PickReminder,
        Season,
        Tournament,
        TournamentStatus,
    )

    now_utc = datetime.now(tz=timezone.utc)
    today = now_utc.date()
    window_end = today + timedelta(days=7)

    # Tournaments starting in the next 7 days that are still scheduled.
    upcoming = (
        db.query(Tournament)
        .filter(
            Tournament.status == TournamentStatus.SCHEDULED.value,
            Tournament.start_date >= today,
            Tournament.start_date <= window_end,
        )
        .all()
    )

    if not upcoming:
        log.info("Pick reminders: no tournaments starting in the next 7 days")
        return {"sent": 0, "failed": 0, "skipped": 0, "errors": []}

    total_sent = 0
    total_failed = 0
    total_skipped = 0
    errors: list[str] = []

    for tournament in upcoming:
        # Find leagues that have this tournament in their schedule.
        league_ids = [
            row.league_id
            for row in db.query(LeagueTournament.league_id)
            .filter(LeagueTournament.tournament_id == tournament.id)
            .all()
        ]
        if not league_ids:
            continue

        pick_window_open = _is_pick_window_open(db, tournament)

        for league_id in league_ids:
            # Only process leagues with an active season.
            season: Season | None = (
                db.query(Season)
                .filter_by(league_id=league_id, is_active=True)
                .first()
            )
            if season is None:
                log.debug(
                    "Pick reminders: league %s has no active season — skipping tournament '%s'",
                    league_id,
                    tournament.name,
                )
                continue

            # Confirm the tournament is part of this season's schedule
            # (already confirmed via LeagueTournament — re-check is belt-and-suspenders).
            # Create reminder row (idempotent — UNIQUE constraint prevents duplicate rows).
            reminder: PickReminder | None = (
                db.query(PickReminder)
                .filter_by(
                    league_id=league_id,
                    season_id=season.id,
                    tournament_id=tournament.id,
                )
                .first()
            )

            if reminder is None:
                reminder = PickReminder(
                    id=uuid.uuid4(),
                    league_id=league_id,
                    season_id=season.id,
                    tournament_id=tournament.id,
                    scheduled_at=now_utc,
                )
                db.add(reminder)
                db.flush()  # populate id before use

            # Skip if already sent successfully.
            if reminder.sent_at is not None:
                log.debug(
                    "Pick reminders: already sent for league=%s tournament='%s' — skipping",
                    league_id,
                    tournament.name,
                )
                continue

            # Skip if permanently failed.
            if reminder.failed_at is not None:
                log.warning(
                    "Pick reminders: permanently failed reminder for league=%s tournament='%s' — skipping",
                    league_id,
                    tournament.name,
                )
                continue

            # Re-check active season in case it was deactivated between creation and send.
            if not season.is_active:
                log.info(
                    "Pick reminders: season deactivated for league=%s — skipping send",
                    league_id,
                )
                continue

            try:
                sent, skipped = _send_reminder_for_league(
                    db, reminder, tournament, pick_window_open
                )
                reminder.sent_at = now_utc
                reminder.attempt_count += 1
                db.commit()
                total_sent += sent
                total_skipped += skipped
                log.info(
                    "Pick reminders: league=%s tournament='%s' sent=%d skipped=%d",
                    league_id,
                    tournament.name,
                    sent,
                    skipped,
                )
            except Exception as exc:
                db.rollback()
                reminder.attempt_count += 1
                if reminder.attempt_count >= reminder.max_attempts:
                    reminder.failed_at = now_utc
                    reminder.error_message = str(exc)
                    log.error(
                        "Pick reminders: permanently failed for league=%s tournament='%s' after %d attempts: %s",
                        league_id,
                        tournament.name,
                        reminder.attempt_count,
                        exc,
                    )
                else:
                    log.warning(
                        "Pick reminders: attempt %d/%d failed for league=%s tournament='%s': %s",
                        reminder.attempt_count,
                        reminder.max_attempts,
                        league_id,
                        tournament.name,
                        exc,
                    )
                db.commit()
                total_failed += 1
                errors.append(f"league={league_id} tournament={tournament.name}: {exc}")

    return {"sent": total_sent, "failed": total_failed, "skipped": total_skipped, "errors": errors}
