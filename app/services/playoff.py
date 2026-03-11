"""
Playoff service — all playoff business logic.

Key functions:
  seed_playoff(db, config)                  → Create rounds/pods/pod_members from standings
  generate_draft_order(style, n, picks)     → Returns list of draft_position values per slot
  get_active_slot(db, pod_id, total_slots)  → Returns next unfilled slot number
  open_round_draft(db, round_obj)           → Transition round from pending → drafting
  submit_preferences(db, pod_member, ids, tournament_id)  → Replace player's preference list (atomic)
  resolve_draft(db, playoff_round)          → Admin-triggered: process preferences → picks
  score_round(db, playoff_round)            → Populate points_earned from TournamentEntry
  advance_bracket(db, playoff_round)        → Set winners, create next-round pods
  override_result(db, pod, winner_user_id)  → Manager manual result override
"""

import math
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import (
    PlayoffConfig,
    PlayoffDraftPreference,
    PlayoffPick,
    PlayoffPod,
    PlayoffPodMember,
    PlayoffRound,
    TournamentEntry,
)
from app.services.scoring import calculate_standings


# ---------------------------------------------------------------------------
# Draft order generation
# ---------------------------------------------------------------------------

def generate_draft_order(style: str, n: int, picks: int) -> list[int]:
    """
    Returns a list of draft_position values (length = n * picks).
    Each element is the draft_position of the player who picks in that slot.
    Slot index (0-based) maps to draft_position.

    style: "snake" | "linear" | "top_seed_priority"
    n: number of players in the pod
    picks: number of picks per player
    """
    if style == "snake":
        order = []
        for round_idx in range(picks):
            positions = list(range(1, n + 1))
            if round_idx % 2 == 1:
                positions = list(reversed(positions))
            order.extend(positions)
        return order
    elif style == "linear":
        order = []
        for _ in range(picks):
            order.extend(range(1, n + 1))
        return order
    elif style == "top_seed_priority":
        order = []
        for draft_position in range(1, n + 1):
            order.extend([draft_position] * picks)
        return order
    else:
        raise ValueError(f"Unknown draft style: {style!r}")


# ---------------------------------------------------------------------------
# Active slot computation
# ---------------------------------------------------------------------------

def get_active_slot(db: Session, pod_id: int, total_slots: int) -> int | None:
    """
    Returns the next unfilled slot number, or None if the draft is complete.
    """
    filled_slots = (
        db.query(PlayoffPick.draft_slot)
        .filter(PlayoffPick.pod_id == pod_id)
        .all()
    )
    filled_set = {row.draft_slot for row in filled_slots}
    for slot in range(1, total_slots + 1):
        if slot not in filled_set:
            return slot
    return None  # All slots filled


# ---------------------------------------------------------------------------
# Pod seeding helpers
# ---------------------------------------------------------------------------

def assign_pod(seed: int, num_pods: int) -> int:
    """
    Returns the 1-indexed pod (bracket_position) for a given seed.
    Works for pods-of-4 brackets (e.g. round 1 of the 32-player bracket).

    Seeds are split into four "tiers" of num_pods each.
    Tier 1: seeds 1..P         (top seeds, straight order)
    Tier 2: seeds P+1..2P      (second tier, reversed)
    Tier 3: seeds 2P+1..3P     (third tier, same direction as tier 1)
    Tier 4: seeds 3P+1..4P     (bottom seeds, reversed)
    """
    tier_size = num_pods  # = playoff_size // 4
    tier = (seed - 1) // tier_size  # 0-indexed tier: 0, 1, 2, 3
    position_in_tier = (seed - 1) % tier_size  # 0-indexed within tier

    if tier % 2 == 0:
        # Tiers 0 and 2: pod number = position_in_tier + 1
        return position_in_tier + 1
    else:
        # Tiers 1 and 3: pod number is reversed
        return tier_size - position_in_tier


def assign_pod_2(seed: int, num_pods: int) -> int:
    """
    Standard bracket seeding for head-to-head (pods of 2).

    Seed 1 faces the lowest seed (pod 1), seed 2 faces the second-lowest, etc.

    Example for 8 players (4 pods):
      seed 1 → pod 1, seed 8 → pod 1  (1 vs 8)
      seed 2 → pod 2, seed 7 → pod 2  (2 vs 7)
      seed 3 → pod 3, seed 6 → pod 3  (3 vs 6)
      seed 4 → pod 4, seed 5 → pod 4  (4 vs 5)
    """
    n = num_pods * 2
    if seed <= num_pods:
        return seed
    else:
        return n + 1 - seed


def _normalize_draft_positions(db: Session, round_obj: PlayoffRound) -> None:
    """
    Re-sort draft_positions by seed in all pods of the given round.
    Called after advance_bracket() adds winners to next-round pods, to ensure
    draft_position reflects the sorted seed order within each pod.
    """
    for pod in round_obj.pods:
        members_sorted = sorted(pod.members, key=lambda m: m.seed)
        for i, member in enumerate(members_sorted):
            member.draft_position = i + 1


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def seed_playoff(db: Session, config: PlayoffConfig) -> None:
    """
    Seed the playoff bracket from current season standings.

    Auto-selects the last N scheduled (future) tournaments in the league's
    schedule as playoff rounds, where N is derived from playoff_size.
    Tournaments are assigned to rounds in ascending start_date order.

    Raises HTTPException on any validation failure.
    """
    if config.status != "pending":
        raise HTTPException(status_code=422, detail="Playoff is already seeded or active")

    from app.models import League, LeagueTournament, Season, Tournament as TournamentModel
    from app.models.tournament import TournamentStatus

    league = db.query(League).filter_by(id=config.league_id).first()
    season = db.query(Season).filter_by(id=config.season_id).first()

    if not league or not season:
        raise HTTPException(status_code=404, detail="League or season not found")

    standings = calculate_standings(db, league=league, season=season)

    playoff_size = config.playoff_size

    if len(standings) < playoff_size:
        raise HTTPException(
            status_code=422,
            detail=f"Not enough members to fill the bracket. Need {playoff_size}, have {len(standings)}",
        )

    # New bracket structure:
    # - All sizes 2/4/8/16: pods of 2, num_rounds = log2(playoff_size)
    # - Size 32: round 1 pods of 4 (8 pods), subsequent rounds pods of 2; 4 rounds total
    if playoff_size == 32:
        pod_size = 4
        num_rounds = 4
    else:
        pod_size = 2
        num_rounds = int(math.log2(playoff_size))

    # Auto-pick the last num_rounds scheduled (future) league tournaments.
    scheduled_rows = (
        db.query(LeagueTournament)
        .filter_by(league_id=config.league_id)
        .join(LeagueTournament.tournament)
        .filter(TournamentModel.status == TournamentStatus.SCHEDULED.value)
        .order_by(TournamentModel.start_date.asc())
        .all()
    )
    playoff_rows = scheduled_rows[-num_rounds:] if len(scheduled_rows) >= num_rounds else []

    if len(playoff_rows) != num_rounds:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Need at least {num_rounds} future tournament(s) in the schedule for a "
                f"{playoff_size}-player bracket; {len(scheduled_rows)} available"
            ),
        )

    seeded_members = standings[:playoff_size]

    # Create ALL rounds with tournament IDs assigned in date order.
    round_objs: dict[int, PlayoffRound] = {}
    for i, row in enumerate(playoff_rows):
        r = PlayoffRound(
            playoff_config_id=config.id,
            round_number=i + 1,
            tournament_id=row.tournament_id,
            status="pending",
        )
        db.add(r)
        round_objs[i + 1] = r

    db.flush()

    # Round 1 pods.
    num_pods_round1 = playoff_size // pod_size
    round1 = round_objs[1]
    pod_map: dict[int, PlayoffPod] = {}
    for bp in range(1, num_pods_round1 + 1):
        pod = PlayoffPod(
            playoff_round_id=round1.id,
            bracket_position=bp,
            status="pending",
        )
        db.add(pod)
        pod_map[bp] = pod

    db.flush()

    for i, standing in enumerate(seeded_members):
        seed = i + 1
        if pod_size == 4:
            pod = pod_map[assign_pod(seed, num_pods_round1)]
        else:
            pod = pod_map[assign_pod_2(seed, num_pods_round1)]
        db.add(PlayoffPodMember(
            pod_id=pod.id,
            user_id=standing["user_id"],
            seed=seed,
            draft_position=0,  # temporary; set after sorting below
        ))

    db.flush()

    # Set draft_position within each pod (1 = top seed, 2 = second seed, etc.)
    for pod in pod_map.values():
        db.refresh(pod)
        for i, member in enumerate(sorted(pod.members, key=lambda m: m.seed)):
            member.draft_position = i + 1

    config.status = "seeded"
    config.seeded_at = datetime.now(timezone.utc)
    config.is_enabled = True
    db.commit()


# ---------------------------------------------------------------------------
# Round draft management
# ---------------------------------------------------------------------------

def open_round_draft(db: Session, playoff_round: PlayoffRound) -> None:
    """
    Transition a round and all its pods from pending → drafting.

    For round 1 with a pending (unseeded) config, seeding is performed
    automatically first, based on current standings and the league's
    selected playoff tournaments.
    """
    config = playoff_round.playoff_config

    # Auto-seed when opening round 1 for the first time.
    if playoff_round.round_number == 1 and config.status == "pending":
        seed_playoff(db, config)
        db.refresh(playoff_round)

    if playoff_round.tournament_id is None:
        raise HTTPException(
            status_code=422,
            detail="Cannot open draft: no tournament assigned to this round",
        )

    playoff_round.status = "drafting"
    for pod in playoff_round.pods:
        pod.status = "drafting"

    if config.status == "seeded":
        config.status = "active"

    db.commit()


# ---------------------------------------------------------------------------
# Preference submission
# ---------------------------------------------------------------------------

def submit_preferences(
    db: Session,
    pod_member: PlayoffPodMember,
    golfer_ids: list[uuid.UUID],
    tournament_id: uuid.UUID,
) -> list[PlayoffDraftPreference]:
    """
    Atomically replace a player's full ranked preference list.

    Validates:
    1. The round is in 'drafting' status
    2. Tournament has not yet started (now < tournament.start_date)
    3. All golfer_ids exist in the tournament field
    4. No duplicate golfer_ids in the submitted list
    """
    # Load the playoff round through the pod
    pod = pod_member.pod
    playoff_round = pod.playoff_round

    if playoff_round.status not in ("pending", "drafting"):
        raise HTTPException(
            status_code=422,
            detail="Draft is not open for this round",
        )

    # Validate tournament has not started
    from app.models import Tournament
    tournament = db.query(Tournament).filter_by(id=tournament_id).first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")

    now_date = datetime.now(timezone.utc).date()
    if now_date >= tournament.start_date:
        raise HTTPException(
            status_code=422,
            detail="Tournament has already started; preference submission is closed",
        )

    # Validate exact required count: pod_size * picks_per_round
    config = playoff_round.playoff_config
    idx = playoff_round.round_number - 1
    ppr = config.picks_per_round[idx] if idx < len(config.picks_per_round) else config.picks_per_round[-1]
    pod_size = len(pod.members)
    required_count = pod_size * ppr
    if len(golfer_ids) != required_count:
        raise HTTPException(
            status_code=422,
            detail=f"You must rank exactly {required_count} golfers ({pod_size} players × {ppr} picks each)",
        )

    # Validate no duplicates in the submitted list
    if len(golfer_ids) != len(set(golfer_ids)):
        raise HTTPException(status_code=422, detail="Duplicate golfer IDs in preference list")

    # Validate all golfers are in the tournament field (skip if field not yet released)
    field_released = db.query(TournamentEntry).filter_by(tournament_id=tournament_id).limit(1).first() is not None
    if field_released:
        for golfer_id in golfer_ids:
            entry = (
                db.query(TournamentEntry)
                .filter_by(tournament_id=tournament_id, golfer_id=golfer_id)
                .first()
            )
            if not entry:
                raise HTTPException(
                    status_code=422,
                    detail=f"Golfer {golfer_id} is not in the tournament field",
                )

    # Delete all existing preferences for this pod_member (atomic replace)
    db.query(PlayoffDraftPreference).filter_by(pod_member_id=pod_member.id).delete()

    # Insert new preferences in order (index 0 → rank 1)
    new_prefs = []
    for rank, golfer_id in enumerate(golfer_ids, start=1):
        pref = PlayoffDraftPreference(
            pod_id=pod_member.pod_id,
            pod_member_id=pod_member.id,
            golfer_id=golfer_id,
            rank=rank,
        )
        db.add(pref)
        new_prefs.append(pref)

    db.commit()
    for pref in new_prefs:
        db.refresh(pref)

    return new_prefs


# ---------------------------------------------------------------------------
# Draft resolution
# ---------------------------------------------------------------------------

def resolve_draft(db: Session, playoff_round: PlayoffRound) -> None:
    """
    Called by admin after tournament.start_date.
    Processes all submitted preference lists in draft order.
    Players with no submitted list get no picks (earn $0).
    """
    if playoff_round.status != "drafting":
        raise HTTPException(
            status_code=422,
            detail="Round is not in drafting status",
        )

    config = playoff_round.playoff_config

    for pod in playoff_round.pods:
        idx = playoff_round.round_number - 1
        picks_per_player = config.picks_per_round[idx] if idx < len(config.picks_per_round) else config.picks_per_round[-1]
        total_slots = len(pod.members) * picks_per_player

        slot_order = generate_draft_order(
            style=config.draft_style,
            n=len(pod.members),
            picks=picks_per_player,
        )  # Returns list of draft_positions, one per slot

        claimed: set[uuid.UUID] = set()

        for slot_number, draft_position in enumerate(slot_order, start=1):
            member = next(m for m in pod.members if m.draft_position == draft_position)

            prefs = (
                db.query(PlayoffDraftPreference)
                .filter_by(pod_member_id=member.id)
                .order_by(PlayoffDraftPreference.rank)
                .all()
            )

            # Find best available pick from this player's preferences
            picked_golfer_id = next(
                (p.golfer_id for p in prefs if p.golfer_id not in claimed),
                None,
            )

            if picked_golfer_id is None:
                # No list submitted or all preferences claimed — no pick for this slot
                continue

            db.add(PlayoffPick(
                pod_id=pod.id,
                pod_member_id=member.id,
                golfer_id=picked_golfer_id,
                tournament_id=playoff_round.tournament_id,
                draft_slot=slot_number,
            ))
            claimed.add(picked_golfer_id)

        db.commit()

    playoff_round.draft_resolved_at = datetime.now(timezone.utc)
    playoff_round.status = "locked"
    db.commit()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_round(db: Session, playoff_round: PlayoffRound) -> None:
    """
    Populate points_earned for all playoff_picks in this round and
    update playoff_pod_members.total_points.
    Called by admin after the assigned tournament completes.
    """
    tournament = playoff_round.tournament
    if tournament is None:
        raise HTTPException(status_code=422, detail="No tournament assigned to this round")

    multiplier = tournament.multiplier  # Use tournament's global multiplier

    for pod in playoff_round.pods:
        for member in pod.members:
            member_picks = (
                db.query(PlayoffPick)
                .filter_by(pod_id=pod.id, pod_member_id=member.id)
                .all()
            )
            total = 0.0
            for pick in member_picks:
                entry = (
                    db.query(TournamentEntry)
                    .filter_by(tournament_id=tournament.id, golfer_id=pick.golfer_id)
                    .first()
                )
                earnings = entry.earnings_usd if entry and entry.earnings_usd else 0
                pick.points_earned = earnings * multiplier
                total += pick.points_earned
            member.total_points = total

    db.commit()


# ---------------------------------------------------------------------------
# Winner determination
# ---------------------------------------------------------------------------

def _determine_pod_winner(pod: PlayoffPod) -> PlayoffPodMember:
    """
    Winner = member with highest total_points.
    Tie-break: lower seed number (seed 1 beats seed 2 in a tie).
    Members with None total_points are treated as 0.
    """
    members_sorted = sorted(
        pod.members,
        key=lambda m: (-(m.total_points or 0.0), m.seed),
    )
    return members_sorted[0]


# ---------------------------------------------------------------------------
# Bracket advancement
# ---------------------------------------------------------------------------

def advance_bracket(db: Session, playoff_round: PlayoffRound) -> None:
    """
    After scoring is complete for a round, determine winners and populate
    the next round's pods.
    """
    # Validate all pods are scored (winner determinable)
    for pod in playoff_round.pods:
        if any(m.total_points is None for m in pod.members):
            raise HTTPException(
                status_code=422,
                detail=f"Pod {pod.id} has unscored members — run score_round first",
            )

    config = playoff_round.playoff_config
    next_round = (
        db.query(PlayoffRound)
        .filter_by(
            playoff_config_id=playoff_round.playoff_config_id,
            round_number=playoff_round.round_number + 1,
        )
        .first()
    )

    for pod in playoff_round.pods:
        winner = _determine_pod_winner(pod)
        pod.winner_user_id = winner.user_id
        pod.status = "completed"

        # Mark all non-winners as eliminated
        for member in pod.members:
            if member.user_id != winner.user_id:
                member.is_eliminated = True

        if next_round:
            next_bracket_position = math.ceil(pod.bracket_position / 2)
            next_pod = (
                db.query(PlayoffPod)
                .filter_by(
                    playoff_round_id=next_round.id,
                    bracket_position=next_bracket_position,
                )
                .first()
            )
            if not next_pod:
                next_pod = PlayoffPod(
                    playoff_round_id=next_round.id,
                    bracket_position=next_bracket_position,
                    status="pending",
                )
                db.add(next_pod)
                db.flush()

            # Assign winner to next pod with their seed
            existing_seed = next(m for m in pod.members if m.user_id == winner.user_id).seed
            member_count_in_next = (
                db.query(PlayoffPodMember)
                .filter_by(pod_id=next_pod.id)
                .count()
            )
            next_member = PlayoffPodMember(
                pod_id=next_pod.id,
                user_id=winner.user_id,
                seed=existing_seed,
                draft_position=member_count_in_next + 1,  # temporary; re-sorted below
            )
            db.add(next_member)

    playoff_round.status = "completed"

    if next_round:
        # Flush so the new pod members are visible for re-sort
        db.flush()
        _normalize_draft_positions(db, next_round)

    db.commit()


# ---------------------------------------------------------------------------
# Manager override
# ---------------------------------------------------------------------------

def override_result(db: Session, pod: PlayoffPod, winner_user_id: uuid.UUID) -> None:
    """
    Manager safety valve: manually set the winner of a pod.
    Bypasses all scoring logic.
    """
    # Validate the winner is actually a member of this pod
    winner_member = next(
        (m for m in pod.members if m.user_id == winner_user_id),
        None,
    )
    if not winner_member:
        raise HTTPException(
            status_code=422,
            detail="Specified user is not a member of this pod",
        )

    pod.winner_user_id = winner_user_id
    pod.status = "completed"

    for member in pod.members:
        if member.user_id != winner_user_id:
            member.is_eliminated = True

    db.commit()
