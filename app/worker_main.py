"""
SQS consumer entrypoint — worker container.

This process receives event messages from the SQS queue and runs the
corresponding finalization operations. It runs as a separate container
(alongside the scraper) so the two can scale independently.

Event types handled
-------------------
TOURNAMENT_IN_PROGRESS
    Published by sync_tournament() whenever a tournament is in_progress and
    there are unresolved playoff draft rounds linked to it. The handler calls
    resolve_draft() for each eligible round, but only if the first Round 1 tee
    time has already passed (any_r1_teed_off() == True). Safe to receive
    repeatedly — resolve_draft() is idempotent via the draft_resolved_at guard.

TOURNAMENT_COMPLETED
    Published by sync_schedule() when a tournament transitions to "completed".
    The handler runs the full finalization pipeline in order:
      1. score_picks()     — score all regular Pick records
      2. score_round()     — score all PlayoffPick records for linked playoff rounds
      3. advance_bracket() — advance the playoff bracket if all members are scored
    Each step is idempotent — safe to replay if SQS delivers the message more
    than once (standard queues guarantee at-least-once delivery).

Local development
-----------------
Run with LocalStack (see docker-compose.yml). The same code runs locally and
in production — only the AWS_ENDPOINT_URL env var differs.

    docker-compose up worker
"""

import logging
import signal
import sys

log = logging.getLogger(__name__)


def handle(message: dict) -> None:
    """Route a parsed SQS message body to the correct handler."""
    event_type = message.get("type")
    tournament_id = message.get("tournament_id")

    # Import here (not at top level) to avoid circular imports and to keep
    # module-level load fast during tests that import worker_main.
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        if event_type == "TOURNAMENT_IN_PROGRESS":
            if not tournament_id:
                log.warning("TOURNAMENT_IN_PROGRESS message missing tournament_id — skipping")
                return
            _handle_tournament_in_progress(db, tournament_id)
        elif event_type == "TOURNAMENT_COMPLETED":
            if not tournament_id:
                log.warning("TOURNAMENT_COMPLETED message missing tournament_id — skipping")
                return
            _handle_tournament_completed(db, tournament_id)
        else:
            # Unknown event type — log and let the message be deleted so it
            # doesn't clog the queue. Do not raise (that would retry).
            log.warning("Unknown SQS event type: %s — discarding", event_type)
    finally:
        db.close()


def _handle_tournament_in_progress(db, tournament_id: str) -> None:
    """
    Resolve playoff draft preferences for any round linked to this tournament
    that is still in "drafting" status and whose Round 1 tee times have passed.

    Called every ~5 minutes while live_score_sync is active (once per
    sync_tournament() call that publishes this event). resolve_draft() is
    idempotent — it exits immediately if draft_resolved_at is already set.
    """
    from app.models import PlayoffRound
    from app.services.playoff import any_r1_teed_off, resolve_draft

    rounds = (
        db.query(PlayoffRound)
        .filter(
            PlayoffRound.tournament_id == tournament_id,
            PlayoffRound.status == "drafting",
            PlayoffRound.draft_resolved_at.is_(None),
        )
        .all()
    )
    if not rounds:
        log.debug("TOURNAMENT_IN_PROGRESS: no unresolved playoff rounds for %s", tournament_id)
        return

    for playoff_round in rounds:
        if not any_r1_teed_off(db, playoff_round.tournament_id):
            log.debug(
                "TOURNAMENT_IN_PROGRESS: Round 1 not yet started for round %d — skipping",
                playoff_round.round_number,
            )
            continue
        log.info(
            "TOURNAMENT_IN_PROGRESS: resolving draft for playoff round %d (tournament=%s)",
            playoff_round.round_number,
            tournament_id,
        )
        try:
            resolve_draft(db, playoff_round)
            log.info(
                "TOURNAMENT_IN_PROGRESS: playoff round %d draft resolved",
                playoff_round.round_number,
            )
        except Exception as exc:
            db.rollback()
            log.error(
                "TOURNAMENT_IN_PROGRESS: failed to resolve round %d: %s",
                playoff_round.round_number,
                exc,
                exc_info=True,
            )
            raise  # re-raise so SQS retries this message


def _handle_tournament_completed(db, tournament_id: str) -> None:
    """
    Run the full finalization pipeline for a completed tournament:
      1. Score regular picks (score_picks)
      2. Score playoff round picks (score_round) for any linked "locked" playoff round
      3. Advance the bracket (advance_bracket) if all members are now scored

    Each step is idempotent — score_picks only processes picks where
    points_earned IS NULL, score_round checks status == "locked", and
    advance_bracket checks that all members are scored before advancing.
    """
    from app.models import PlayoffRound, Tournament
    from app.services.playoff import advance_bracket, score_round
    from app.services.scraper import score_picks

    tournament = db.query(Tournament).filter_by(id=tournament_id).first()
    if not tournament:
        log.warning("TOURNAMENT_COMPLETED: tournament %s not found — skipping", tournament_id)
        return

    # Step 1: Score regular picks.
    log.info("TOURNAMENT_COMPLETED: scoring regular picks for '%s'", tournament.name)
    try:
        count = score_picks(db, tournament)
        log.info("TOURNAMENT_COMPLETED: scored %d regular picks for '%s'", count, tournament.name)
    except Exception as exc:
        db.rollback()
        log.error("TOURNAMENT_COMPLETED: score_picks failed for '%s': %s", tournament.name, exc, exc_info=True)
        raise  # retry

    # Step 2: Score playoff round if one is linked to this tournament.
    playoff_round = (
        db.query(PlayoffRound)
        .filter_by(tournament_id=tournament_id, status="locked")
        .first()
    )
    if not playoff_round:
        log.debug("TOURNAMENT_COMPLETED: no locked playoff round for tournament %s", tournament_id)
        return

    log.info(
        "TOURNAMENT_COMPLETED: scoring playoff round %d for '%s'",
        playoff_round.round_number,
        tournament.name,
    )
    try:
        score_round(db, playoff_round)
        log.info(
            "TOURNAMENT_COMPLETED: playoff round %d scored",
            playoff_round.round_number,
        )
    except Exception as exc:
        db.rollback()
        log.error(
            "TOURNAMENT_COMPLETED: score_round failed for round %d: %s",
            playoff_round.round_number,
            exc,
            exc_info=True,
        )
        raise  # retry

    # Step 3: Advance the bracket (advance_bracket checks internally that all
    # members are scored before acting — safe to call unconditionally here).
    log.info(
        "TOURNAMENT_COMPLETED: attempting bracket advance for round %d",
        playoff_round.round_number,
    )
    try:
        advance_bracket(db, playoff_round)
        log.info(
            "TOURNAMENT_COMPLETED: bracket advanced past round %d",
            playoff_round.round_number,
        )
    except Exception as exc:
        db.rollback()
        log.error(
            "TOURNAMENT_COMPLETED: advance_bracket failed for round %d: %s",
            playoff_round.round_number,
            exc,
            exc_info=True,
        )
        raise  # retry


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _handle_sigterm(signum, frame):
    log.info("Worker received SIGTERM — shutting down")
    sys.exit(0)


if __name__ == "__main__":
    _configure_logging()
    signal.signal(signal.SIGTERM, _handle_sigterm)

    from app.services.sqs import consume
    consume(handle)
