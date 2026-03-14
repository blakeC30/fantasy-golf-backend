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
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from app.models import (
    League,
    LeagueTournament,
    PlayoffConfig,
    PlayoffDraftPreference,
    PlayoffPick,
    PlayoffPod,
    PlayoffPodMember,
    PlayoffRound,
    Tournament,
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

    Called automatically by get_bracket when the regular-season schedule locks
    (exactly num_rounds scheduled tournaments remain — all regular-season ones
    have completed). Also callable by the manager via POST /rounds/{id}/open as
    an admin override.

    Raises HTTPException if already seeded or if conditions are not met.
    """
    existing_rounds = db.query(PlayoffRound).filter_by(playoff_config_id=config.id).count()
    if existing_rounds > 0:
        raise HTTPException(status_code=422, detail="Playoff bracket is already seeded")

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
    # Rounds start in "drafting" status — preferences open immediately upon seeding.
    round_objs: dict[int, PlayoffRound] = {}
    for i, row in enumerate(playoff_rows):
        r = PlayoffRound(
            playoff_config_id=config.id,
            round_number=i + 1,
            tournament_id=row.tournament_id,
            status="drafting",
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
            status="drafting",
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

    config.status = "active"
    config.seeded_at = datetime.now(timezone.utc)
    config.is_enabled = True
    db.commit()


# ---------------------------------------------------------------------------
# Preference window helpers
# ---------------------------------------------------------------------------

def first_r1_tee_time(db: Session, tournament_id) -> datetime | None:
    """
    Returns the earliest Round 1 tee time in the tournament field, or None if
    no tee times are available yet (field not synced).

    Used to determine when the playoff preference window closes: submissions
    are blocked once the first golfer in the field has teed off.
    """
    earliest = (
        db.query(sqlfunc.min(TournamentEntry.tee_time))
        .filter(
            TournamentEntry.tournament_id == tournament_id,
            TournamentEntry.tee_time.isnot(None),
        )
        .scalar()
    )
    if earliest is None:
        return None
    if earliest.tzinfo is None:
        earliest = earliest.replace(tzinfo=timezone.utc)
    return earliest


def any_r1_teed_off(db: Session, tournament_id) -> bool:
    """
    True if the FIRST Round 1 tee time has passed (i.e. any golfer has teed off).

    Used for playoff pick visibility: picks become visible to all pod members
    once the first golfer in the field tees off.

    Distinct from all_r1_teed_off() in picks.py, which waits for the LAST
    tee time — that is the rule for regular-season pick visibility.

    Returns False when no tee times are in the DB yet (field not synced),
    keeping picks hidden until data is available.
    """
    first_tee = first_r1_tee_time(db, tournament_id)
    if first_tee is None:
        return False
    return first_tee <= datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Round draft management
# ---------------------------------------------------------------------------

def open_round_draft(db: Session, playoff_round: PlayoffRound) -> None:
    """
    Admin override: explicitly transition a round and its pods to "drafting".

    In normal flow this is not needed — the bracket is auto-seeded (with all
    rounds starting as "drafting") when get_bracket detects the schedule has
    locked. This function exists as a manager safety valve for edge cases
    (e.g., the auto-seed hasn't run yet, or the round needs to be reopened).

    If the round is already "drafting" the call is a no-op.
    For rounds > 1, the previous round must be completed before calling.
    """
    # Already open — nothing to do.
    if playoff_round.status == "drafting":
        return

    if playoff_round.tournament_id is None:
        raise HTTPException(
            status_code=422,
            detail="Cannot open draft: no tournament assigned to this round",
        )

    # For round 2+: require the previous round's tournament to be completed.
    if playoff_round.round_number > 1:
        prev_round = (
            db.query(PlayoffRound)
            .filter_by(
                playoff_config_id=playoff_round.playoff_config_id,
                round_number=playoff_round.round_number - 1,
            )
            .first()
        )
        if prev_round is None:
            raise HTTPException(
                status_code=422,
                detail="Previous playoff round not found",
            )
        if prev_round.status != "completed":
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Cannot open round {playoff_round.round_number} preferences — "
                    f"round {prev_round.round_number} has not completed yet"
                ),
            )

    playoff_round.status = "drafting"
    for pod in playoff_round.pods:
        pod.status = "drafting"

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
    2. Preference window is still open: first R1 tee time has not yet passed
       (falls back to start_date when tee times are not yet in the DB)
    3. Exact required count: pod_size * picks_per_round
    4. No duplicate golfer_ids in the submitted list

    Note: golfers are NOT validated against the tournament field at submission time.
    Any golfer in the DB may be ranked. Non-field golfers are silently skipped
    at resolution time (resolve_draft).
    """
    # Load the playoff round through the pod
    pod = pod_member.pod
    playoff_round = pod.playoff_round

    if pod_member.is_eliminated:
        raise HTTPException(
            status_code=403,
            detail="You have been eliminated from the playoffs and cannot submit picks",
        )

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

    # Preference window closes when the first R1 tee time passes (rule: lock is
    # triggered by the very first tee time, not a specific golfer's tee time).
    # If tee times are not yet in the DB, fall back to tournament status.
    # Do NOT use start_date for the fallback — start_date is a calendar date
    # with no time component, and comparing it to now_utc.date() fires a day
    # early for US-timezone users (Wednesday night UTC = Thursday date).
    now_utc = datetime.now(timezone.utc)
    first_tee = first_r1_tee_time(db, tournament_id)
    if first_tee is not None:
        if first_tee <= now_utc:
            raise HTTPException(
                status_code=422,
                detail="Preference window is closed — the first golfer has already teed off",
            )
    else:
        if tournament.status in ("in_progress", "completed"):
            raise HTTPException(
                status_code=422,
                detail="Preference window is closed — the tournament has already started",
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

    # No field-membership validation at submission time (rule: any golfer in the DB
    # can be ranked at any time). Non-field golfers are silently skipped at resolution.

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
    Called by admin after the preference window closes (first R1 tee time passes).
    Processes all submitted preference lists in draft order.
    Players with no submitted list get no picks (earn $0).

    Field-membership check: golfers not in the official tournament field are
    silently skipped. If all of a member's preferences are ineligible, they
    receive no pick for that slot and earn $0.
    """
    if playoff_round.status != "drafting":
        raise HTTPException(
            status_code=422,
            detail="Round is not in drafting status",
        )

    # Guard: preferences cannot be resolved until the submission window has closed.
    # The window closes when the first Round 1 tee time passes.  If no tee times
    # are in the DB yet, fall back to tournament status — allow resolution if the
    # tournament is already in_progress or completed; block if still scheduled.
    if playoff_round.tournament_id is None:
        raise HTTPException(status_code=422, detail="No tournament assigned to this round")
    if not any_r1_teed_off(db, playoff_round.tournament_id):
        tournament = db.query(Tournament).filter_by(id=playoff_round.tournament_id).first()
        if tournament and tournament.status not in ("in_progress", "completed"):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Cannot resolve preferences yet — the submission window is still open. "
                    "Wait for the first golfer to tee off before resolving."
                ),
            )

    config = playoff_round.playoff_config

    # Pre-load tournament field as a set for efficient membership checks.
    # If the field has not been synced yet the set is empty and all preferences
    # are skipped — resolution should only be called once the field is available.
    field_golfer_ids: set[uuid.UUID] = {
        row.golfer_id
        for row in db.query(TournamentEntry.golfer_id)
        .filter_by(tournament_id=playoff_round.tournament_id)
        .all()
    }
    field_released = len(field_golfer_ids) > 0

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

            # Find best available pick from this player's preferences.
            # Skip golfers already claimed by a higher-priority slot AND golfers
            # not in the official tournament field (silently skipped per rule).
            picked_golfer_id = next(
                (
                    p.golfer_id for p in prefs
                    if p.golfer_id not in claimed
                    and (not field_released or p.golfer_id in field_golfer_ids)
                ),
                None,
            )

            if picked_golfer_id is None:
                # No list submitted, all preferences claimed, or all preferences
                # were for golfers not in the tournament field — no pick for this slot.
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

    No-pick penalty: applied once per unresolved pick slot (i.e., expected slots
    minus actual assigned picks). Uses league.no_pick_penalty — the same value
    as the regular season penalty, configurable by the league manager.
    """
    if playoff_round.status != "locked":
        raise HTTPException(
            status_code=422,
            detail=(
                "Round must be in locked status before scoring — "
                "resolve the draft first (POST /rounds/{id}/resolve)"
            ),
        )

    tournament = playoff_round.tournament
    if tournament is None:
        raise HTTPException(status_code=422, detail="No tournament assigned to this round")

    if tournament.status != "completed":
        raise HTTPException(
            status_code=422,
            detail="Tournament must be completed before scoring — wait for official results",
        )

    config = playoff_round.playoff_config
    league = db.query(League).filter_by(id=config.league_id).first()
    if not league:
        raise HTTPException(status_code=422, detail="League not found")

    no_pick_penalty = league.no_pick_penalty  # negative int, e.g. -50_000

    # How many picks each member is supposed to receive this round.
    idx = playoff_round.round_number - 1
    picks_per_player = (
        config.picks_per_round[idx]
        if idx < len(config.picks_per_round)
        else config.picks_per_round[-1]
    )

    # Use the league's per-tournament multiplier override if set; otherwise fall
    # back to the tournament's global multiplier. Mirrors score_picks() in the
    # scraper so playoff and regular-season scoring are consistent.
    lt = db.query(LeagueTournament).filter_by(
        league_id=config.league_id, tournament_id=tournament.id
    ).first()
    multiplier = lt.multiplier if lt and lt.multiplier is not None else tournament.multiplier

    # Validate that earnings are published for every assigned pick before modifying
    # any records.  TournamentEntry.earnings_usd is null until ESPN releases official
    # prize money; treating null as $0 would produce wrong scores and wrong winners.
    # If any earnings are missing, abort so the manager can try again later.
    for pod in playoff_round.pods:
        for member in pod.members:
            member_picks = (
                db.query(PlayoffPick)
                .filter_by(pod_id=pod.id, pod_member_id=member.id)
                .all()
            )
            for pick in member_picks:
                entry = (
                    db.query(TournamentEntry)
                    .filter_by(tournament_id=tournament.id, golfer_id=pick.golfer_id)
                    .first()
                )
                if entry is not None and entry.earnings_usd is None:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            "Earnings are not yet available for all golfers in this round. "
                            "Please wait for official tournament results to be published "
                            "and try again."
                        ),
                    )

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
                earnings = float(entry.earnings_usd) if entry and entry.earnings_usd is not None else 0.0
                pick.points_earned = earnings * multiplier
                total += pick.points_earned

            # Apply no-pick penalty for each slot that went unresolved.
            # This covers: no preference list submitted, all preferences claimed,
            # or all preferences were for golfers not in the tournament field.
            missed_slots = picks_per_player - len(member_picks)
            if missed_slots > 0:
                total += missed_slots * no_pick_penalty

            member.total_points = total

    db.commit()


# ---------------------------------------------------------------------------
# Winner determination
# ---------------------------------------------------------------------------

def _determine_pod_winner(pod: PlayoffPod) -> PlayoffPodMember:
    """
    Winner = eligible member with highest total_points.
    Tie-break: lower seed number (seed 1 beats seed 2 in a tie).
    Members with None total_points are treated as 0.

    Members marked is_eliminated=True before scoring begins (i.e., vacated slots
    from members who left the league mid-playoffs) are never eligible to win,
    regardless of how other members score.
    """
    eligible = [m for m in pod.members if not m.is_eliminated]
    if not eligible:
        raise HTTPException(
            status_code=422,
            detail=f"Pod {pod.id} has no eligible members — all slots are vacant",
        )
    members_sorted = sorted(
        eligible,
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
    if playoff_round.status != "locked":
        raise HTTPException(
            status_code=422,
            detail=(
                "Round must be in locked status to advance — "
                + (
                    "resolve the draft first (POST /rounds/{id}/resolve)."
                    if playoff_round.status == "drafting"
                    else "this round has already been advanced."
                )
            ),
        )

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
        if pod.winner_user_id is not None:
            # Winner was manually set by override_result before advance_bracket was
            # called.  Respect the override rather than recalculating from scores.
            winner = next(
                (m for m in pod.members if m.user_id == pod.winner_user_id),
                None,
            )
            if winner is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Pod {pod.id} has an override winner that is not a pod member — "
                        "please correct the override before advancing."
                    ),
                )
        else:
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
                    status="drafting",
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
        # Auto-open preference window for the next round — no manager action needed.
        next_round.status = "drafting"

    db.commit()


# ---------------------------------------------------------------------------
# Manager override
# ---------------------------------------------------------------------------

def override_result(db: Session, pod: PlayoffPod, winner_user_id: uuid.UUID) -> None:
    """
    Manager safety valve: manually set the winner of a pod.

    Only valid after:
      1. The playoff tournament has completed (individual pick revision is closed).
      2. The round has been scored (score_round has been run) so total_points are set.
      3. The bracket has NOT yet been advanced (round.status is still "locked").

    Once advance_bracket is called the result is permanently locked and cannot
    be overridden. Use the individual pick-revision endpoint (PATCH /picks/{id})
    to correct golfer assignments while the tournament is still in_progress.
    """
    playoff_round = pod.playoff_round

    # Rule: override only after the tournament has completed.
    tournament = (
        db.query(Tournament).filter_by(id=playoff_round.tournament_id).first()
        if playoff_round.tournament_id
        else None
    )
    if tournament is None or tournament.status != "completed":
        raise HTTPException(
            status_code=422,
            detail=(
                "Pod winner can only be overridden after the tournament has completed. "
                "Use individual pick revision while the tournament is in progress."
            ),
        )

    # Rule: round must be "locked" — draft resolved, bracket not yet advanced.
    if playoff_round.status != "locked":
        raise HTTPException(
            status_code=422,
            detail=(
                "Pod winner override is only available for rounds that have been resolved "
                "but not yet advanced. "
                + (
                    "Resolve the draft first (POST /rounds/{id}/resolve)."
                    if playoff_round.status == "drafting"
                    else "The bracket has already been advanced — this round is permanently locked."
                )
            ),
        )

    # Rule: scoring must have run — total_points must be set for all members
    # so that the manager can make an informed override decision.
    if any(m.total_points is None for m in pod.members):
        raise HTTPException(
            status_code=422,
            detail="Score the round first (POST /rounds/{id}/score) before overriding the winner.",
        )

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
        if member.user_id == winner_user_id:
            # Explicitly reset in case a previous override had marked this member eliminated.
            member.is_eliminated = False
        else:
            member.is_eliminated = True

    db.commit()
