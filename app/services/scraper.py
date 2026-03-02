"""
PGA Tour data scraper using the ESPN unofficial sports API.

Why ESPN? It requires no API key, has been stable for years, and returns
JSON — no HTML scraping needed. The downside is it's unofficial and
undocumented, so the response shape can change without notice. All
parsing is written defensively (.get() everywhere, sensible defaults).

Architecture
------------
The functions here are split into two clear layers:

  1. Parsing (pure functions):
     parse_schedule_response() and parse_summary_response() take raw API
     JSON and return clean dicts. No DB access, so they're trivial to unit
     test with fixture data.

  2. Database (upsert functions):
     upsert_tournaments(), upsert_field(), upsert_results(), score_picks()
     take the parsed dicts and write to the DB using SQLAlchemy sessions.

High-level orchestration functions (sync_schedule, sync_tournament,
full_sync) combine both layers and are what the scheduler and admin
endpoint call.

ESPN API endpoints used
-----------------------
  Schedule: https://site.api.espn.com/apis/site/v2/sports/golf/pga/scoreboard
            ?dates={YYYY}  → returns all events for that calendar year

  Summary:  https://site.api.espn.com/apis/site/v2/sports/golf/pga/summary
            ?event={espnEventId}  → leaderboard + field for one tournament
"""

import logging
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from app.models import Golfer, Pick, Season, Tournament, TournamentEntry, TournamentStatus

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ESPN API constants
# ---------------------------------------------------------------------------
_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/golf/pga/scoreboard"
_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/golf/pga/summary"
_REQUEST_TIMEOUT = 30.0  # seconds


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _get_json(url: str, params: dict | None = None) -> dict:
    """
    Make a synchronous GET request and return parsed JSON.

    Uses a short-lived httpx.Client (connection pooling within one call).
    Raises httpx.HTTPStatusError on 4xx/5xx, httpx.RequestError on network failure.
    """
    with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
        resp = client.get(url, params=params or {})
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Parsing helpers  (pure — no DB access, easy to unit test)
# ---------------------------------------------------------------------------

def _map_espn_status(espn_status_name: str) -> str:
    """Convert ESPN status string to our TournamentStatus enum value."""
    return {
        "STATUS_SCHEDULED": TournamentStatus.SCHEDULED.value,
        "STATUS_IN_PROGRESS": TournamentStatus.IN_PROGRESS.value,
        "STATUS_FINAL": TournamentStatus.COMPLETED.value,
    }.get(espn_status_name, TournamentStatus.SCHEDULED.value)


def _parse_date(date_str: str | None) -> date | None:
    """Parse an ESPN ISO timestamp ('2025-04-10T10:00Z') to a Python date."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def parse_schedule_response(data: dict) -> list[dict]:
    """
    Extract tournament records from an ESPN scoreboard API response.

    ESPN wraps events under either data['events'] or data['leagues'][i]['events'].
    We check both. Returns a list of dicts ready to be upserted as Tournament rows.
    """
    # Collect raw events from whichever nesting ESPN uses.
    raw_events: list[dict] = data.get("events", [])
    if not raw_events:
        for league in data.get("leagues", []):
            raw_events.extend(league.get("events", []))

    results = []
    for event in raw_events:
        event_id = event.get("id")
        if not event_id:
            continue

        # The competition object holds precise start/end dates.
        competitions = event.get("competitions") or [{}]
        comp = competitions[0]

        status_name = (
            event.get("status", {}).get("type", {}).get("name", "STATUS_SCHEDULED")
        )

        start_date = _parse_date(comp.get("startDate") or event.get("date"))
        end_date = _parse_date(comp.get("endDate"))
        if not start_date:
            continue
        if not end_date:
            end_date = start_date + timedelta(days=3)

        results.append({
            "pga_tour_id": str(event_id),
            "name": event.get("name") or event.get("shortName", "Unknown Tournament"),
            "start_date": start_date,
            "end_date": end_date,
            "status": _map_espn_status(status_name),
            # multiplier defaults to 1.0; platform admin sets 2.0 for majors manually
            # (ESPN doesn't label which events are majors in a machine-readable way)
            "multiplier": 1.0,
        })

    return results


def parse_summary_response(data: dict) -> tuple[list[dict], list[dict]]:
    """
    Extract golfer profiles and tournament results from an ESPN summary response.

    Returns:
      golfers  — list of dicts for upserting Golfer rows
      results  — list of dicts for upserting TournamentEntry rows
    """
    golfers: list[dict] = []
    results: list[dict] = []

    for entry in data.get("leaderboard", []):
        athlete = entry.get("athlete") or {}
        athlete_id = athlete.get("id")
        if not athlete_id:
            continue

        # Country from the flag object (alt text or countryCode)
        flag = athlete.get("flag") or {}
        country = flag.get("alt") or flag.get("countryCode") or None

        golfers.append({
            "pga_tour_id": str(athlete_id),
            "name": athlete.get("displayName", "Unknown"),
            "country": country,
        })

        # Earnings are in statistics array
        earnings: int | None = None
        for stat in entry.get("statistics") or []:
            if stat.get("name") in ("earnings", "prize"):
                raw = stat.get("value")
                if raw is not None:
                    try:
                        earnings = int(float(raw))
                    except (ValueError, TypeError):
                        pass
                break

        # Finish position from sortOrder (1 = winner)
        position: int | None = None
        raw_pos = entry.get("sortOrder")
        if raw_pos is not None:
            try:
                position = int(raw_pos)
            except (ValueError, TypeError):
                pass

        # Status: active (made cut) vs cut / WD / DQ
        entry_status: str | None = None
        raw_status = entry.get("status")
        if isinstance(raw_status, str) and raw_status.lower() in ("cut", "wd", "mdf", "dq"):
            entry_status = raw_status.lower()

        # Tee time for pick-lock enforcement
        tee_time: datetime | None = None
        raw_tee = entry.get("teeTime")
        if raw_tee:
            try:
                tee_time = datetime.fromisoformat(raw_tee.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        results.append({
            "pga_tour_id": str(athlete_id),  # FK link to golfer
            "finish_position": position,
            "earnings_usd": earnings,
            "status": entry_status,
            "tee_time": tee_time,
        })

    return golfers, results


# ---------------------------------------------------------------------------
# Database upsert helpers
# ---------------------------------------------------------------------------

def upsert_tournaments(db: Session, parsed: list[dict]) -> tuple[int, int]:
    """
    Upsert Tournament rows. Returns (created, updated).

    Only mutable fields (name, end_date, status) are updated on an existing
    row. multiplier is NOT overwritten because platform admins set it manually
    for majors and we don't want a sync to reset it.
    """
    created, updated = 0, 0
    for item in parsed:
        existing = db.query(Tournament).filter_by(pga_tour_id=item["pga_tour_id"]).first()
        if existing:
            existing.name = item["name"]
            existing.start_date = item["start_date"]
            existing.end_date = item["end_date"]
            existing.status = item["status"]
            updated += 1
        else:
            db.add(Tournament(**item))
            created += 1
    db.commit()
    return created, updated


def upsert_field(
    db: Session,
    tournament: Tournament,
    golfers: list[dict],
    results: list[dict],
) -> tuple[int, int]:
    """
    Upsert Golfer and TournamentEntry rows for the tournament's field.
    Returns (golfers_synced, entries_synced).

    results is a parallel list to golfers (same pga_tour_id key links them).
    """
    results_by_id = {r["pga_tour_id"]: r for r in results}

    golfers_synced = 0
    entries_synced = 0

    for g in golfers:
        # Upsert golfer profile.
        golfer = db.query(Golfer).filter_by(pga_tour_id=g["pga_tour_id"]).first()
        if golfer:
            golfer.name = g["name"]
            if g.get("country"):
                golfer.country = g["country"]
        else:
            golfer = Golfer(pga_tour_id=g["pga_tour_id"], name=g["name"], country=g.get("country"))
            db.add(golfer)
        db.flush()  # ensure golfer.id is populated

        # Upsert tournament entry.
        entry = db.query(TournamentEntry).filter_by(
            tournament_id=tournament.id, golfer_id=golfer.id
        ).first()

        result = results_by_id.get(g["pga_tour_id"], {})

        if entry:
            if result.get("finish_position") is not None:
                entry.finish_position = result["finish_position"]
            if result.get("earnings_usd") is not None:
                entry.earnings_usd = result["earnings_usd"]
            if result.get("status") is not None:
                entry.status = result["status"]
            if result.get("tee_time") is not None:
                entry.tee_time = result["tee_time"]
        else:
            entry = TournamentEntry(
                tournament_id=tournament.id,
                golfer_id=golfer.id,
                finish_position=result.get("finish_position"),
                earnings_usd=result.get("earnings_usd"),
                status=result.get("status"),
                tee_time=result.get("tee_time"),
            )
            db.add(entry)
            entries_synced += 1

        golfers_synced += 1

    db.commit()
    return golfers_synced, entries_synced


def score_picks(db: Session, tournament: Tournament) -> int:
    """
    Calculate and store points_earned for all picks in a completed tournament.

    Called after results are synced. Finds every Pick across all leagues
    for this tournament, looks up the golfer's earnings, and computes:
      points_earned = earnings_usd * tournament.multiplier

    If the golfer missed the cut (no earnings), points_earned = 0.
    Returns the number of picks scored.
    """
    if tournament.status != TournamentStatus.COMPLETED.value:
        log.warning("score_picks called on non-completed tournament %s", tournament.name)
        return 0

    picks = db.query(Pick).filter_by(tournament_id=tournament.id).all()
    count = 0

    for pick in picks:
        entry = db.query(TournamentEntry).filter_by(
            tournament_id=tournament.id, golfer_id=pick.golfer_id
        ).first()

        if entry and entry.earnings_usd:
            pick.points_earned = float(entry.earnings_usd) * tournament.multiplier
        else:
            # Missed cut or WD — earns 0 (not the penalty; penalty is for no pick at all)
            pick.points_earned = 0.0

        count += 1

    db.commit()
    log.info("Scored %d picks for '%s'", count, tournament.name)
    return count


# ---------------------------------------------------------------------------
# High-level sync functions (HTTP + DB)
# ---------------------------------------------------------------------------

def sync_schedule(db: Session, year: int) -> dict:
    """
    Fetch the PGA Tour schedule for a calendar year and upsert tournaments.
    Returns a summary dict with counts.
    """
    log.info("Syncing schedule for year %d", year)
    try:
        data = _get_json(_SCOREBOARD_URL, params={"dates": str(year)})
    except httpx.HTTPError as exc:
        log.error("Failed to fetch schedule: %s", exc)
        raise

    parsed = parse_schedule_response(data)
    created, updated = upsert_tournaments(db, parsed)

    log.info("Schedule sync: %d created, %d updated", created, updated)
    return {"year": year, "tournaments_created": created, "tournaments_updated": updated}


def sync_tournament(db: Session, pga_tour_id: str) -> dict:
    """
    Sync the field and results for a single tournament.

    Fetches the ESPN summary for the event, upserts golfers and entries,
    then scores picks if the tournament is completed.
    Returns a summary dict with counts.
    """
    tournament = db.query(Tournament).filter_by(pga_tour_id=pga_tour_id).first()
    if not tournament:
        raise ValueError(f"Tournament with pga_tour_id '{pga_tour_id}' not found in DB. "
                         "Run sync_schedule first.")

    log.info("Syncing tournament '%s' (id=%s)", tournament.name, pga_tour_id)
    try:
        data = _get_json(_SUMMARY_URL, params={"event": pga_tour_id})
    except httpx.HTTPError as exc:
        log.error("Failed to fetch summary for %s: %s", pga_tour_id, exc)
        raise

    golfers, results = parse_summary_response(data)
    golfers_synced, entries_synced = upsert_field(db, tournament, golfers, results)

    # Re-query to get the latest status after upsert.
    db.refresh(tournament)
    picks_scored = 0
    if tournament.status == TournamentStatus.COMPLETED.value:
        picks_scored = score_picks(db, tournament)

    log.info(
        "Tournament sync '%s': %d golfers, %d new entries, %d picks scored",
        tournament.name, golfers_synced, entries_synced, picks_scored,
    )
    return {
        "pga_tour_id": pga_tour_id,
        "name": tournament.name,
        "golfers_synced": golfers_synced,
        "entries_synced": entries_synced,
        "picks_scored": picks_scored,
    }


def full_sync(db: Session, year: int) -> dict:
    """
    Run a complete sync for an entire year:
      1. Fetch the schedule and upsert all tournaments.
      2. For each tournament that is IN_PROGRESS or COMPLETED, sync its field.

    This is what the scheduler calls daily and what /admin/sync triggers.
    """
    schedule_result = sync_schedule(db, year)

    # Only sync field/results for active or finished tournaments.
    # SCHEDULED tournaments don't have a field yet.
    active_statuses = {TournamentStatus.IN_PROGRESS.value, TournamentStatus.COMPLETED.value}
    tournaments = (
        db.query(Tournament)
        .filter(Tournament.status.in_(active_statuses))
        .all()
    )

    tournament_results = []
    errors = []

    for t in tournaments:
        try:
            result = sync_tournament(db, t.pga_tour_id)
            tournament_results.append(result)
        except Exception as exc:
            log.error("Failed to sync tournament '%s': %s", t.name, exc)
            errors.append({"pga_tour_id": t.pga_tour_id, "name": t.name, "error": str(exc)})

    return {
        "year": year,
        "schedule": schedule_result,
        "tournaments_synced": len(tournament_results),
        "errors": errors,
    }
