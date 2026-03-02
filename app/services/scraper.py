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
     parse_schedule_response() takes the scoreboard JSON and returns clean
     tournament dicts. No DB access, so it's trivial to unit test.

  2. Database (upsert functions):
     upsert_tournaments(), upsert_field(), score_picks() take parsed dicts
     and write to the DB using SQLAlchemy sessions.

High-level orchestration functions (sync_schedule, sync_tournament,
full_sync) combine both layers and are what the scheduler and admin
endpoint call.

ESPN API endpoints used
-----------------------
  Schedule:  https://site.api.espn.com/apis/site/v2/sports/golf/pga/scoreboard
             ?dates={YYYY}  → all events for that calendar year

  Core API:  https://sports.core.api.espn.com/v2/sports/golf/leagues/pga/...
             /events/{id}/competitions/{id}/competitors?limit=200
               → all golfers in the field with finish order
             /events/{id}/competitions/{id}/competitors/{athlete_id}/statistics
               → earnings for completed tournaments
             /athletes/{athlete_id}
               → golfer name and country

Note: The older summary endpoint (site.api.espn.com/...pga/summary?event=)
is no longer functional — it returns ESPN error code 2500 for all event IDs.
The core API endpoints above are the reliable replacement.
"""

import concurrent.futures
import logging
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app.models import Golfer, Pick, Tournament, TournamentEntry, TournamentStatus

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ESPN API constants
# ---------------------------------------------------------------------------
_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/golf/pga/scoreboard"
_CORE_API_BASE = "https://sports.core.api.espn.com/v2/sports/golf/leagues/pga"
_REQUEST_TIMEOUT = 30.0  # seconds


# ---------------------------------------------------------------------------
# HTTP helpers
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


_FETCH_WORKERS = 5  # concurrent threads for athlete lookups


def _fetch_athlete_info(athlete_id: str) -> dict:
    """
    Fetch one golfer's display name and country from the ESPN core API.
    Returns a dict with pga_tour_id, name, country. Safe to call concurrently.
    """
    url = f"{_CORE_API_BASE}/athletes/{athlete_id}"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                d = resp.json()
                return {
                    "pga_tour_id": str(athlete_id),
                    "name": d.get("displayName", "Unknown"),
                    "country": d.get("citizenship") or None,
                }
    except httpx.RequestError as exc:
        log.warning("Could not fetch athlete %s: %s", athlete_id, exc)
    return {"pga_tour_id": str(athlete_id), "name": "Unknown", "country": None}


def _fetch_tournament_data(
    pga_tour_id: str,
    known_golfer_ids: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Fetch the golfer field and finish order for one tournament.

    Uses the ESPN core API competitors endpoint (event-specific, unlike the
    web scoreboard which ignores the event parameter). One request gets all
    competitor IDs and finish positions; athlete names are fetched concurrently
    for golfers not already cached in known_golfer_ids.

    Earnings are left as None — fetched on-demand in score_picks() for only
    the golfers users actually picked (1 API call per pick, not per field).

    Args:
      pga_tour_id:       ESPN event ID for the tournament.
      known_golfer_ids:  pga_tour_ids already in the DB; skips re-fetching them.

    Returns:
      golfers  — list of dicts ready to upsert as Golfer rows
      results  — list of dicts ready to upsert as TournamentEntry rows
    """
    # Step 1: one request for the full competitor list (IDs + finish order).
    competitors_url = (
        f"{_CORE_API_BASE}/events/{pga_tour_id}"
        f"/competitions/{pga_tour_id}/competitors"
    )
    data = _get_json(competitors_url, params={"limit": 200})
    competitors = data.get("items", [])

    if not competitors:
        log.warning("No competitors found for tournament %s", pga_tour_id)
        return [], []

    known = known_golfer_ids or set()
    ids_to_fetch = [
        str(c["id"]) for c in competitors
        if c.get("id") and str(c["id"]) not in known
    ]

    # Step 2: fetch athlete info only for golfers not already in DB.
    athlete_info: dict[str, dict] = {}
    if ids_to_fetch:
        with concurrent.futures.ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
            futures = {pool.submit(_fetch_athlete_info, aid): aid for aid in ids_to_fetch}
            for future in concurrent.futures.as_completed(futures):
                try:
                    info = future.result()
                    athlete_info[info["pga_tour_id"]] = info
                except Exception as exc:
                    log.warning("Athlete fetch failed: %s", exc)

    log.info(
        "Tournament %s: %d competitors, %d new athlete fetches",
        pga_tour_id, len(competitors), len(ids_to_fetch),
    )

    golfers: list[dict] = []
    results: list[dict] = []
    for c in competitors:
        athlete_id = str(c.get("id", ""))
        if not athlete_id:
            continue

        # Use freshly fetched info, or pass name=None for known golfers
        # (upsert_field will skip updating them).
        info = athlete_info.get(athlete_id)
        golfers.append({
            "pga_tour_id": athlete_id,
            "name": info["name"] if info else None,
            "country": info["country"] if info else None,
        })
        results.append({
            "pga_tour_id": athlete_id,
            "finish_position": c.get("order"),
            "earnings_usd": None,
            "status": None,
            "tee_time": None,
        })

    return golfers, results


def _fetch_golfer_earnings(pga_tour_id: str, athlete_id: str) -> int | None:
    """
    Fetch prize earnings for one golfer in one tournament from the ESPN core API.

    Called by score_picks() only for golfers that have actual picks — keeps
    total API requests low (one per league member who submitted a pick).
    Returns earnings in USD as an integer, or None if not found.
    """
    stats_url = (
        f"{_CORE_API_BASE}/events/{pga_tour_id}"
        f"/competitions/{pga_tour_id}/competitors/{athlete_id}/statistics"
    )
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
            resp = client.get(stats_url)
            if resp.status_code != 200:
                return None
            stats_data = resp.json()
    except httpx.RequestError as exc:
        log.warning("Could not fetch earnings for athlete %s: %s", athlete_id, exc)
        return None

    for cat in stats_data.get("splits", {}).get("categories", []):
        for stat in cat.get("stats", []):
            if stat.get("name") == "amount":
                raw = stat.get("value")
                if raw is not None:
                    try:
                        val = int(float(raw))
                        return val if val > 0 else None
                    except (ValueError, TypeError):
                        pass
    return None


# ---------------------------------------------------------------------------
# Parsing helpers  (pure — no DB access, easy to unit test)
# ---------------------------------------------------------------------------

def _map_espn_status(espn_status_name: str) -> str:
    """Convert ESPN status string to our TournamentStatus enum value."""
    return {
        "STATUS_SCHEDULED": TournamentStatus.SCHEDULED.value,
        "STATUS_IN_PROGRESS": TournamentStatus.IN_PROGRESS.value,
        "STATUS_FINAL": TournamentStatus.COMPLETED.value,
        # Treat cancelled events as completed so they don't surface as "upcoming"
        # in the pick form and don't get included in the next-scheduled sync.
        "STATUS_CANCELED": TournamentStatus.COMPLETED.value,
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
        # name=None means the golfer was already in DB (known_golfer_ids cache hit);
        # skip updating to avoid overwriting good data with None.
        golfer = db.query(Golfer).filter_by(pga_tour_id=g["pga_tour_id"]).first()
        if golfer:
            if g["name"] is not None:
                golfer.name = g["name"]
            if g.get("country") is not None:
                golfer.country = g["country"]
        else:
            golfer = Golfer(
                pga_tour_id=g["pga_tour_id"],
                name=g["name"] or "Unknown",
                country=g.get("country"),
            )
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

    For each pick we need the golfer's prize earnings. We first check the
    TournamentEntry row (may already have earnings from a previous sync), and
    fall back to fetching from the ESPN core API. This keeps requests minimal:
    one API call per pick, and only for picks that haven't been scored yet.

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

        earnings: float | None = None

        if entry and entry.earnings_usd:
            # Already stored from a previous sync — use it directly.
            earnings = float(entry.earnings_usd)
        else:
            # Not stored yet — fetch from ESPN core API for this specific golfer.
            golfer = db.query(Golfer).filter_by(id=pick.golfer_id).first()
            if golfer:
                raw = _fetch_golfer_earnings(tournament.pga_tour_id, golfer.pga_tour_id)
                if raw is not None:
                    earnings = float(raw)
                    # Persist so future calls skip the API hit.
                    if entry:
                        entry.earnings_usd = raw

        pick.points_earned = (earnings or 0.0) * tournament.multiplier
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
    Sync the field and results for a single tournament using the ESPN core API.

    Fetches all competitors concurrently (golfer names + earnings), upserts
    golfers and tournament entries, then scores picks if the tournament is
    completed. Returns a summary dict with counts.
    """
    tournament = db.query(Tournament).filter_by(pga_tour_id=pga_tour_id).first()
    if not tournament:
        raise ValueError(f"Tournament with pga_tour_id '{pga_tour_id}' not found in DB. "
                         "Run sync_schedule first.")

    log.info("Syncing tournament '%s' (id=%s)", tournament.name, pga_tour_id)

    # Pass IDs of golfers already in DB so _fetch_tournament_data skips re-fetching them.
    known_ids = {g.pga_tour_id for g in db.query(Golfer).all()}

    try:
        golfers, results = _fetch_tournament_data(pga_tour_id, known_golfer_ids=known_ids)
    except (httpx.HTTPError, httpx.RequestError) as exc:
        log.error("Failed to fetch field for %s: %s", pga_tour_id, exc)
        raise

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
      2. For each IN_PROGRESS or COMPLETED tournament, sync its field + results.
      3. Also sync the single next SCHEDULED tournament so the pick form has
         a golfer list to show.

    This is what the scheduler calls daily and what /admin/sync triggers.
    """
    schedule_result = sync_schedule(db, year)

    # Sync field + results for active or finished tournaments.
    active_statuses = {TournamentStatus.IN_PROGRESS.value, TournamentStatus.COMPLETED.value}
    tournaments_to_sync = (
        db.query(Tournament)
        .filter(Tournament.status.in_(active_statuses))
        .all()
    )

    # Also sync the soonest upcoming scheduled tournament so the pick form works.
    next_scheduled = (
        db.query(Tournament)
        .filter(Tournament.status == TournamentStatus.SCHEDULED.value)
        .order_by(Tournament.start_date.asc())
        .first()
    )
    if next_scheduled and next_scheduled not in tournaments_to_sync:
        tournaments_to_sync = list(tournaments_to_sync) + [next_scheduled]

    tournaments = tournaments_to_sync

    tournament_results = []
    errors = []

    for t in tournaments:
        # Capture identity info before any DB operation so logging still works
        # even if the session rolls back and expires these attributes.
        t_id = t.pga_tour_id
        t_name = t.name
        try:
            result = sync_tournament(db, t_id)
            tournament_results.append(result)
        except Exception as exc:
            # A failed flush invalidates the current transaction. Roll it back
            # so subsequent iterations start with a clean session state.
            db.rollback()
            log.error("Failed to sync tournament '%s': %s", t_name, exc)
            errors.append({"pga_tour_id": t_id, "name": t_name, "error": str(exc)})

    return {
        "year": year,
        "schedule": schedule_result,
        "tournaments_synced": len(tournament_results),
        "errors": errors,
    }
