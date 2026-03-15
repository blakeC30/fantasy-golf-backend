"""
Pick validation service.

All business rules for submitting or changing a pick live here. The router
calls these functions and handles the HTTPException they raise.

Separating validation from routing makes the logic testable without HTTP.

Rules enforced:
  1. New picks: tournament must be SCHEDULED, or IN_PROGRESS with the chosen
     golfer's tee_time still in the future (first-day late entry).
  2. Deadline: tournament.start_date must be in the future for SCHEDULED picks.
  3. Pick-change lock: if IN_PROGRESS, the new golfer's tee_time must not
     have passed. If tee_time is null when IN_PROGRESS, pick is locked.
     Exception: if the current pick's golfer has no Round 1 TournamentEntryRound
     data (they never teed off), the change is allowed as long as the new golfer
     hasn't teed off. WD status is not required — ESPN sometimes omits it for
     pre-event scratches replaced in the field before the tournament starts.
  4. Golfer must be entered in the tournament (TournamentEntry must exist).
  5. No-repeat rule: golfer not already picked this season in this league.
  6. One pick per tournament per user per season per league.
  7. Picks for a SCHEDULED tournament are blocked if any IN_PROGRESS tournament
     exists in the league's schedule (previous tournament must complete first).
"""

import uuid
from datetime import date, datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from app.models import Golfer, LeagueTournament, Pick, PlayoffConfig, PlayoffRound, Season, Tournament, TournamentEntry, TournamentStatus


def validate_new_pick(
    db: Session,
    league_id: uuid.UUID,
    season: Season,
    user_id: uuid.UUID,
    tournament_id: uuid.UUID,
    golfer_id: uuid.UUID,
) -> None:
    """
    Validate all rules for a new pick submission.
    Raises HTTPException with an informative message on any failure.
    """
    tournament = db.query(Tournament).filter_by(id=tournament_id).first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")

    # League schedule check: the admin must have explicitly added this tournament.
    in_schedule = db.query(LeagueTournament).filter_by(
        league_id=league_id, tournament_id=tournament_id
    ).first()
    if not in_schedule:
        raise HTTPException(
            status_code=422,
            detail="This tournament is not in your league's schedule",
        )

    # Block regular picks for playoff-designated tournaments. Playoff rounds use
    # a preference/draft mechanism (PlayoffPick rows), not regular Pick records.
    # Allowing a regular pick here would create a ghost record that doesn't count
    # in standings but would still consume the golfer under the no-repeat rule.
    playoff_round = (
        db.query(PlayoffRound)
        .join(PlayoffConfig, PlayoffRound.playoff_config_id == PlayoffConfig.id)
        .filter(
            PlayoffConfig.league_id == league_id,
            PlayoffRound.tournament_id == tournament_id,
        )
        .first()
    )
    if playoff_round:
        raise HTTPException(
            status_code=422,
            detail="This is a playoff tournament — submit your ranked preferences via the playoff bracket instead",
        )

    if tournament.status == TournamentStatus.COMPLETED.value:
        raise HTTPException(status_code=400, detail="Tournament is already completed")

    # Picks for a scheduled (upcoming) tournament are only allowed once the global
    # PGA Tour schedule is clear: no tournament anywhere is in progress, and the
    # most recently completed tournament's earnings have been published by ESPN.
    # A league may skip PGA events; the real-world constraint (standings must be
    # settled) applies globally regardless of the league's own schedule.
    if tournament.status == TournamentStatus.SCHEDULED.value:
        # Block while any PGA tournament globally is still in progress.
        active = (
            db.query(Tournament)
            .filter(Tournament.status == TournamentStatus.IN_PROGRESS.value)
            .first()
        )
        if active:
            raise HTTPException(
                status_code=400,
                detail=f"Picks for this tournament are not available until '{active.name}' completes",
            )

        # Block if the pick target is not the globally-next scheduled PGA tournament.
        # Members cannot pick ahead — the league's pick target must align with what
        # would naturally be next on the PGA Tour schedule.
        globally_next = (
            db.query(Tournament)
            .filter(Tournament.status == TournamentStatus.SCHEDULED.value)
            .order_by(Tournament.start_date.asc())
            .first()
        )
        if globally_next and tournament.id != globally_next.id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Picks are not yet available. '{globally_next.name}' has not been "
                    "played yet — picks open once the global PGA schedule catches up."
                ),
            )

        # Block if the globally last-completed tournament's earnings haven't been
        # published by ESPN yet.  score_picks() caches earnings in
        # TournamentEntry.earnings_usd; if the field is synced but no earnings exist,
        # official prize money hasn't been released and standings would be incorrect.
        last_completed = (
            db.query(Tournament)
            .filter(Tournament.status == TournamentStatus.COMPLETED.value)
            .order_by(Tournament.start_date.desc())
            .first()
        )
        if last_completed:
            entries_exist = (
                db.query(TournamentEntry)
                .filter(TournamentEntry.tournament_id == last_completed.id)
                .first()
            )
            any_earnings = (
                db.query(TournamentEntry)
                .filter(
                    TournamentEntry.tournament_id == last_completed.id,
                    TournamentEntry.earnings_usd.isnot(None),
                )
                .first()
            )
            if entries_exist and not any_earnings:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Results for '{last_completed.name}' are still being finalized. "
                        "Please try again once official earnings are published."
                    ),
                )

    if tournament.status not in (
        TournamentStatus.SCHEDULED.value,
        TournamentStatus.IN_PROGRESS.value,
    ):
        raise HTTPException(
            status_code=400,
            detail="Picks can only be submitted for upcoming or live tournaments",
        )

    golfer = db.query(Golfer).filter_by(id=golfer_id).first()
    if not golfer:
        raise HTTPException(status_code=404, detail="Golfer not found")

    # Determine whether the official field has been released for this tournament.
    # The field is considered released as soon as any TournamentEntry rows exist.
    # Before release, any known golfer can be picked (they may or may not play).
    field_released = (
        db.query(TournamentEntry).filter_by(tournament_id=tournament_id).first()
        is not None
    )

    entry: TournamentEntry | None = None
    if field_released:
        entry = db.query(TournamentEntry).filter_by(
            tournament_id=tournament_id, golfer_id=golfer_id
        ).first()
        if not entry:
            raise HTTPException(
                status_code=400,
                detail="Golfer is not entered in this tournament",
            )

    if tournament.status == TournamentStatus.SCHEDULED.value:
        now = datetime.now(timezone.utc)
        if entry is not None and entry.tee_time is not None:
            # Tee times are published — use the golfer's actual R1 tee time as the
            # deadline, matching the rule exactly: "the pick locks when the picked
            # golfer's Round 1 tee time passes." This unblocks late picks on
            # tournament day when the scraper hasn't yet flipped status to IN_PROGRESS.
            if entry.tee_time <= now:
                raise HTTPException(
                    status_code=400,
                    detail="Pick deadline has passed — golfer has already teed off",
                )
        else:
            # Tee times not yet available — fall back to start_date as the proxy.
            # start_date is a calendar date (no time component), so comparing it to
            # date.today() (server UTC) is correct: once the tournament day arrives,
            # picks are blocked until tee times are synced and the specific golfer's
            # time can be checked.
            if tournament.start_date <= date.today():
                raise HTTPException(
                    status_code=400,
                    detail="Pick deadline has passed — the tournament has already started",
                )
    else:
        # IN_PROGRESS: field must be released and the golfer must not have teed off.
        now = datetime.now(timezone.utc)
        if not field_released or entry is None or entry.tee_time is None or entry.tee_time <= now:
            raise HTTPException(
                status_code=400,
                detail="Pick deadline has passed — golfer has already teed off or tee time is unavailable",
            )

    # No-repeat: has this golfer already been picked this season?
    repeated = (
        db.query(Pick)
        .filter_by(
            league_id=league_id,
            season_id=season.id,
            user_id=user_id,
            golfer_id=golfer_id,
        )
        .first()
    )
    if repeated:
        raise HTTPException(
            status_code=400,
            detail=f"You have already picked {golfer.name} this season",
        )

    # One pick per tournament.
    duplicate = (
        db.query(Pick)
        .filter_by(
            league_id=league_id,
            season_id=season.id,
            user_id=user_id,
            tournament_id=tournament_id,
        )
        .first()
    )
    if duplicate:
        raise HTTPException(status_code=400, detail="You have already submitted a pick for this tournament")


def validate_pick_change(
    db: Session,
    pick: Pick,
    new_golfer_id: uuid.UUID,
    season: Season,
    league_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """
    Validate changing the golfer on an existing pick.
    Raises HTTPException on any failure.
    """
    tournament = pick.tournament

    if tournament.status == TournamentStatus.COMPLETED.value:
        raise HTTPException(status_code=400, detail="Tournament is already completed — pick cannot be changed")

    # Determine whether the official field has been released for this tournament.
    field_released = (
        db.query(TournamentEntry).filter_by(tournament_id=tournament.id).first()
        is not None
    )

    if tournament.status == TournamentStatus.IN_PROGRESS.value:
        # Check whether the current pick is locked before allowing a change.
        # pick.is_locked returns False when the current golfer withdrew before tee-off,
        # which is the exception that allows a swap even during an in-progress tournament.
        if pick.is_locked:
            raise HTTPException(
                status_code=400,
                detail="Pick is locked — your golfer has already teed off",
            )

        # Validate the new golfer: must be in the field and not yet teed off.
        entry = db.query(TournamentEntry).filter_by(
            tournament_id=tournament.id, golfer_id=new_golfer_id
        ).first()
        if not entry:
            raise HTTPException(status_code=400, detail="Golfer is not entered in this tournament")

        now = datetime.now(timezone.utc)
        if entry.tee_time is None or entry.tee_time <= now:
            raise HTTPException(
                status_code=400,
                detail="Pick is locked — golfer has already teed off or tee time is unavailable",
            )
    else:
        # SCHEDULED: apply the same start_date deadline as a new pick.
        if tournament.start_date <= date.today():
            raise HTTPException(status_code=400, detail="Pick deadline has passed")

        # Only enforce the field check if entries have been released.
        if field_released:
            entry = db.query(TournamentEntry).filter_by(
                tournament_id=tournament.id, golfer_id=new_golfer_id
            ).first()
            if not entry:
                raise HTTPException(status_code=400, detail="Golfer is not entered in this tournament")

    # No-repeat: new golfer can't already be used this season (excluding this pick's golfer).
    existing = (
        db.query(Pick)
        .filter_by(
            league_id=league_id,
            season_id=season.id,
            user_id=user_id,
            golfer_id=new_golfer_id,
        )
        .filter(Pick.id != pick.id)
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=400,
            detail="You have already picked this golfer this season",
        )


def all_r1_teed_off(db: Session, tournament_id) -> bool:
    """True if the last Round 1 tee time in the tournament field has passed.

    Uses TournamentEntry.tee_time — the same source used for pick locking — so
    this function is consistent with validate_pick_change / Pick.is_locked.

    Returns False if no tee times are in the DB yet (field not synced), keeping
    picks hidden until data is available.
    """
    now_utc = datetime.now(tz=timezone.utc)
    last_tee_time = (
        db.query(sqlfunc.max(TournamentEntry.tee_time))
        .filter(
            TournamentEntry.tournament_id == tournament_id,
            TournamentEntry.tee_time.isnot(None),
        )
        .scalar()
    )
    if last_tee_time is None:
        return False
    if last_tee_time.tzinfo is None:
        last_tee_time = last_tee_time.replace(tzinfo=timezone.utc)
    return last_tee_time <= now_utc
