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
    GolferInFieldOut,
    LeaderboardEntryOut,
    LeaderboardOut,
    RoundSummaryOut,
    ScorecardOut,
    TournamentSyncStatusOut,
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


@router.get("/{tournament_id}/field", response_model=list[GolferInFieldOut])
def get_tournament_field(
    tournament_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return the golfers entered in a tournament's field with their Round 1 tee times.

    Used by the pick form to show which golfers are available to pick.
    Sorted by world_ranking (ascending — lower rank = better player).

    All non-withdrawn golfers are returned regardless of tournament status.
    The ``tee_time`` field on each entry lets the frontend grey out golfers
    whose tee time has already passed when the tournament is in_progress,
    giving the user clear visual feedback instead of a server-side error.

    WD (withdrawn) golfers are excluded — they cannot be picked and showing
    them would be confusing since they are no longer competing.
    """
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

    # Exclude withdrawn golfers — they are no longer competing and cannot be picked.
    entries = [e for e in entries if e.status != "WD"]

    return [
        GolferInFieldOut(
            id=e.golfer.id,
            pga_tour_id=e.golfer.pga_tour_id,
            name=e.golfer.name,
            world_ranking=e.golfer.world_ranking,
            country=e.golfer.country,
            tee_time=e.tee_time,
        )
        for e in entries
    ]


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

    stp_per_entry: dict[int, int | None] = {}  # entry.id → regulation total_stp (playoffs excluded)
    for entry in entries:
        scored = [r for r in entry.rounds if r.score_to_par is not None and not r.is_playoff]
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

    # Build separate stp counts for finishers vs. missed-cut players so that a
    # CUT/MDF/WD/DQ player at the same total STP as a finisher cannot create a
    # false tie (e.g. a CUT player at +10 should not mark the last finisher T73).
    finisher_stp_counts: Counter = Counter(
        stp_per_entry[e.id]
        for e in entries
        if e.status not in _BOTTOM_STATUSES and stp_per_entry[e.id] is not None
    )
    bottom_stp_counts: Counter = Counter(
        stp_per_entry[e.id]
        for e in entries
        if e.status in _BOTTOM_STATUSES and stp_per_entry[e.id] is not None
    )

    def _stp_counts_for(entry) -> Counter:
        return bottom_stp_counts if entry.status in _BOTTOM_STATUSES else finisher_stp_counts

    # Break ties resolved by a playoff.
    # Tied groups (same regulation stp) that include at least one entry with a
    # playoff round have their tie broken by the currentPosition ESPN recorded
    # after the final playoff round (winner = "1", runner-up = "2", etc.).
    # This gives the winner position 1 (not T1) and the loser position 2.
    playoff_tie_broken: set[int] = set()  # entry.id values whose tie was resolved
    tied_groups: dict[int, list] = {}
    for e in entries:
        if e.status in _BOTTOM_STATUSES:
            continue
        stp = stp_per_entry[e.id]
        if stp is not None and finisher_stp_counts.get(stp, 0) > 1:
            tied_groups.setdefault(stp, []).append(e)

    for stp, group in tied_groups.items():
        entries_with_playoff = [e for e in group if any(r.is_playoff for r in e.rounds)]
        if not entries_with_playoff:
            continue  # pure regulation tie — leave as-is

        def _playoff_pos(e) -> int:
            last = max((r for r in e.rounds if r.is_playoff), key=lambda r: r.round_number)
            try:
                return int(last.position) if last.position else 9999
            except (ValueError, TypeError):
                return 9999

        sorted_group = sorted(entries_with_playoff, key=_playoff_pos)
        base_pos = display_position[sorted_group[0].id]
        for offset, e in enumerate(sorted_group):
            display_position[e.id] = base_pos + offset
            playoff_tie_broken.add(e.id)

    # For team events, build a lookup from golfer_id → partner entry using team_competitor_id.
    partner_by_golfer_id: dict = {}  # golfer_id (UUID) → partner TournamentEntry
    if tournament.is_team_event:
        teams: dict[str, list] = {}
        for entry in entries:
            if entry.team_competitor_id:
                teams.setdefault(entry.team_competitor_id, []).append(entry)
        for team_entries in teams.values():
            if len(team_entries) == 2:
                e1, e2 = team_entries
                partner_by_golfer_id[e1.golfer_id] = e2
                partner_by_golfer_id[e2.golfer_id] = e1

    result_entries: list[LeaderboardEntryOut] = []
    for entry in entries:
        rounds_sorted = sorted(entry.rounds, key=lambda r: r.round_number)
        total_stp = stp_per_entry[entry.id]
        is_tied = (
            _stp_counts_for(entry).get(total_stp, 0) > 1 and entry.id not in playoff_tie_broken
        ) if total_stp is not None else False
        # made_cut: true only for active/finished players (no special status).
        # This drives the single "Cut Line" divider in the UI — everyone with
        # a notable status (CUT, WD, MDF, DQ) appears below the divider.
        made_cut = entry.status not in _BOTTOM_STATUSES
        partner = partner_by_golfer_id.get(entry.golfer_id)
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
                partner_name=partner.golfer.name if partner else None,
                partner_golfer_id=str(partner.golfer_id) if partner else None,
                partner_golfer_pga_tour_id=partner.golfer.pga_tour_id if partner else None,
            )
        )

    return LeaderboardOut(
        tournament_id=str(tournament_id),
        tournament_name=tournament.name,
        tournament_status=tournament.status,
        is_team_event=tournament.is_team_event,
        last_synced_at=tournament.last_synced_at,
        entries=result_entries,
    )


@router.get("/{tournament_id}/sync-status", response_model=TournamentSyncStatusOut)
def get_sync_status(
    tournament_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Lightweight endpoint returning only the sync timestamp for a tournament.

    Intended for polling: the frontend calls this every ~30 s while a tournament
    is in_progress and, when last_synced_at changes, invalidates the full
    leaderboard query.  Much cheaper than fetching the full leaderboard on each tick.
    """
    tournament = db.query(Tournament).filter_by(id=tournament_id).first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")
    return TournamentSyncStatusOut(
        tournament_id=str(tournament_id),
        tournament_status=tournament.status,
        last_synced_at=tournament.last_synced_at,
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
