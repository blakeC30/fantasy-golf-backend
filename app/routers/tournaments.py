"""
Tournaments router — /tournaments/*

Tournaments are global (not league-specific) — the same PGA Tour events appear
in all leagues. Data is populated by the scraper (Phase 3).

Endpoints:
  GET /tournaments                                  List tournaments (filterable by status)
  GET /tournaments/{id}                             Get a single tournament
  GET /tournaments/{id}/field                       Golfers entered in the tournament (for pick form)
  GET /tournaments/{id}/leaderboard                 Full leaderboard with per-round summaries
  GET /tournaments/{id}/golfers/{gid}/scorecard     Hole-by-hole scorecard (ESPN on-demand)
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.dependencies import get_current_user
from app.models import Golfer, Tournament, TournamentEntry, TournamentStatus, User
from app.schemas.golfer import GolferOut
from app.schemas.tournament import (
    LeaderboardEntryOut,
    LeaderboardOut,
    RoundSummaryOut,
    ScorecardOut,
    TournamentOut,
)

router = APIRouter(prefix="/tournaments", tags=["tournaments"])


@router.get("", response_model=list[TournamentOut])
def list_tournaments(
    status: str | None = Query(default=None, description="Filter by status: scheduled, in_progress, completed"),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    List tournaments, optionally filtered by status.

    Default (no filter) returns all tournaments sorted by start_date descending
    so the most recent/upcoming appear first.
    """
    query = db.query(Tournament)

    if status is not None:
        valid = {s.value for s in TournamentStatus}
        if status not in valid:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status '{status}'. Must be one of: {', '.join(valid)}",
            )
        query = query.filter(Tournament.status == status)

    return query.order_by(Tournament.start_date.desc()).all()


@router.get("/{tournament_id}", response_model=TournamentOut)
def get_tournament(
    tournament_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tournament = db.query(Tournament).filter_by(id=tournament_id).first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")
    return tournament


@router.get("/{tournament_id}/field", response_model=list[GolferOut])
def get_tournament_field(
    tournament_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return the golfers entered in a tournament's field.

    Used by the pick form to show which golfers are available to pick.
    Sorted by world_ranking (ascending — lower rank = better player).

    When the tournament is IN_PROGRESS, only golfers who haven't teed off yet
    are returned (tee_time in the future and not withdrawn). This prevents the
    pick form from showing ineligible golfers.
    """
    from datetime import datetime, timezone

    tournament = db.query(Tournament).filter_by(id=tournament_id).first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")

    entries = (
        db.query(TournamentEntry)
        .filter_by(tournament_id=tournament_id)
        .options(joinedload(TournamentEntry.golfer))
        .join(TournamentEntry.golfer)
        .order_by(Golfer.world_ranking.asc().nulls_last())
        .all()
    )

    if tournament.status == TournamentStatus.IN_PROGRESS.value:
        now = datetime.now(timezone.utc)
        entries = [
            e for e in entries
            if e.tee_time is not None
            and e.tee_time > now
            and e.status != "WD"
        ]

    return [e.golfer for e in entries]


@router.get("/{tournament_id}/leaderboard", response_model=LeaderboardOut)
def get_leaderboard(
    tournament_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Full tournament leaderboard with per-round score summaries.

    Data is served entirely from the DB (tournament_entries + tournament_entry_rounds)
    so this endpoint is fast and doesn't require an ESPN API call.  Only available
    for in_progress and completed tournaments.
    """
    tournament = db.query(Tournament).filter_by(id=tournament_id).first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")
    if tournament.status == TournamentStatus.SCHEDULED.value:
        raise HTTPException(
            status_code=400,
            detail="Leaderboard is not available for scheduled tournaments",
        )

    entries = (
        db.query(TournamentEntry)
        .options(
            joinedload(TournamentEntry.golfer),
            joinedload(TournamentEntry.rounds),
        )
        .filter_by(tournament_id=tournament_id)
        .all()
    )

    # Compute total_score_to_par per entry (sum of per-round score_to_par values).
    # ESPN's finish_position (competitors.order) is always sequential and unique —
    # it does NOT repeat for tied golfers — so we ignore it and compute our own
    # display positions from the actual scores.
    from collections import Counter

    stp_per_entry: dict[int, int | None] = {}  # entry.id → total_stp
    for entry in entries:
        scored = [r for r in entry.rounds if r.score_to_par is not None]
        stp_per_entry[entry.id] = sum(r.score_to_par for r in scored) if scored else None

    _BOTTOM_STATUSES = {"WD", "CUT", "MDF", "DQ"}

    def _sort_tier(e) -> int:
        """0 = active/finished, 1 = missed cut (CUT/MDF), 2 = withdrew/DQ."""
        if e.status not in _BOTTOM_STATUSES:
            return 0
        if e.status in ("CUT", "MDF"):
            return 1
        return 2  # WD, DQ

    # Sort: active → CUT/MDF → WD/DQ; within each tier sort by total_stp then name.
    entries.sort(
        key=lambda e: (
            _sort_tier(e),
            stp_per_entry[e.id] is None,
            stp_per_entry[e.id] if stp_per_entry[e.id] is not None else 0,
            e.golfer.name,
        )
    )

    # Assign display positions: golfers sharing the same total_stp share the same rank.
    # E.g. two golfers at -12 ranked 3rd → both get display_position=3 (not 3 and 4).
    # We iterate the already-sorted list and track a running counter:
    # - when stp changes, the new rank = current index + 1 (1-based)
    # - when stp is the same, keep the rank from the first golfer in that group
    display_position: dict[int, int | None] = {}
    running_rank = 0
    prev_stp: object = object()  # sentinel — never equals any real stp
    active_count = 0  # counts active (non-bottom) entries seen so far
    for entry in entries:
        stp = stp_per_entry[entry.id]
        if entry.status in _BOTTOM_STATUSES or stp is None:
            display_position[entry.id] = None
        else:
            if stp != prev_stp:
                running_rank = active_count + 1
                prev_stp = stp
            display_position[entry.id] = running_rank
            active_count += 1

    stp_counts: Counter = Counter(v for v in stp_per_entry.values() if v is not None)

    result_entries: list[LeaderboardEntryOut] = []
    for entry in entries:
        rounds_sorted = sorted(entry.rounds, key=lambda r: r.round_number)
        total_stp = stp_per_entry[entry.id]
        is_tied = stp_counts.get(total_stp, 0) > 1 if total_stp is not None else False
        # made_cut: true only for active/finished players (no special status).
        # This drives the single "Cut Line" divider in the UI — everyone with
        # a notable status (CUT, WD, MDF, DQ) appears below the divider.
        made_cut = entry.status not in _BOTTOM_STATUSES
        result_entries.append(
            LeaderboardEntryOut(
                golfer_id=str(entry.golfer_id),
                golfer_name=entry.golfer.name,
                golfer_pga_tour_id=entry.golfer.pga_tour_id,
                golfer_country=entry.golfer.country,
                finish_position=display_position[entry.id],
                is_tied=is_tied,
                made_cut=made_cut,
                status=entry.status,
                earnings_usd=entry.earnings_usd,
                total_score_to_par=total_stp,
                rounds=[RoundSummaryOut.model_validate(r) for r in rounds_sorted],
            )
        )

    return LeaderboardOut(
        tournament_id=str(tournament_id),
        tournament_name=tournament.name,
        tournament_status=tournament.status,
        entries=result_entries,
    )


@router.get("/{tournament_id}/golfers/{golfer_id}/scorecard", response_model=ScorecardOut)
def get_scorecard(
    tournament_id: uuid.UUID,
    golfer_id: uuid.UUID,
    round: int = Query(1, ge=1, le=5, description="Round number (1–4 standard, 5 playoff)"),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Hole-by-hole scorecard for a golfer in a specific round, fetched live from ESPN.

    The ``holes`` list may be empty if ESPN does not include nested hole-level
    data for this round — callers should handle this gracefully.
    """
    from app.services.scraper import fetch_golfer_scorecard

    tournament = db.query(Tournament).filter_by(id=tournament_id).first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")

    golfer = db.query(Golfer).filter_by(id=golfer_id).first()
    if not golfer:
        raise HTTPException(status_code=404, detail="Golfer not found")

    result = fetch_golfer_scorecard(tournament, golfer, round)
    return ScorecardOut(**result)
