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
             /events/{id}/competitions/{competition_id}/competitors?limit=200
               → all golfers/teams in the field with finish order
             /competitions/{competition_id}/competitors/{team_id}/roster
               → individual athlete IDs for a team competitor (team events only)
             /events/{id}/competitions/{competition_id}/competitors/{competitor_id}/statistics
               → earnings for completed tournaments; team events use 'officialAmount'
               stat (divided by 2 for per-golfer share); individual events use 'amount'
             /events/{id}/competitions/{competition_id}/competitors/{competitor_id}/linescores
               → per-round data for each golfer: tee time, strokes, score-to-par,
               leaderboard position, and playoff flag. One call returns ALL rounds
               played so far, replacing the older /status endpoint (which only
               returned the current round's tee time). Stored in tournament_entry_rounds.
             /athletes/{athlete_id}
               → golfer name and country

  NOTE: For most tournaments, competition_id == pga_tour_id (event ID).
  Team-format events (e.g. Zurich Classic) use a DIFFERENT competition_id
  exposed in the scoreboard as competitions[0].id. The scraper stores this
  in Tournament.competition_id so subsequent calls use the correct ID.

Per-round data notes
--------------------
  The /linescores endpoint returns a paginated list of round objects for a
  competitor. Each item includes:
    period         → round number (1–4 regular, 5+ playoff)
    teeTime        → ISO 8601 UTC string for that round's start time
    value          → total strokes for the round (float, cast to int)
    displayValue   → score-to-par as string ("-2", "E", "+1") — parsed to int
    currentPosition→ leaderboard rank after this round (integer, stored as string)
    isPlayoff      → true for playoff rounds

  Tee times are only released Tuesday or Wednesday before the Thursday start.
  When linescores are empty or teeTime is absent, we store None and leave
  picks unlocked.

  tournament_entries.tee_time always holds Round 1's tee time and is never
  overwritten by later rounds. Pick-locking logic reads this field: once Round 1
  has started (tee_time <= now), the pick is locked for the entire tournament.

Note: The older summary endpoint (site.api.espn.com/...pga/summary?event=)
is no longer functional — it returns ESPN error code 2500 for all event IDs.
The core API endpoints above are the reliable replacement.
"""

import concurrent.futures
import logging
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from app.models import Golfer, LeagueTournament, Pick, Tournament, TournamentEntry, TournamentEntryRound, TournamentStatus

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


def _parse_score_to_par(display_value: str | None) -> int | None:
    """
    Convert ESPN's score-to-par display string to an integer.

    ESPN "displayValue" examples:
      "-2"  → -2   (under par)
      "E"   →  0   (even par)
      "+1"  → +1   (over par)
      "1"   →  1   (over par, no leading "+")

    Returns None if the value is absent or unparseable.
    """
    if not display_value:
        return None
    v = display_value.strip()
    if v.upper() == "E":
        return 0
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _fetch_competitor_rounds(
    pga_tour_id: str,
    competition_id: str,
    athlete_id: str,
) -> tuple[str, list[dict]]:
    """
    Fetch all per-round data for one competitor from the ESPN /linescores endpoint.

    ESPN linescores endpoint
    ------------------------
    URL:    /events/{pga_tour_id}/competitions/{competition_id}/competitors/{athlete_id}/linescores
    Returns a paginated list of round objects (one per round played).

    Per-round fields used
    ---------------------
    period         → round_number  (int, 1–4 standard, 5+ playoff)
    teeTime        → tee_time      (ISO 8601 UTC string, nullable)
    value          → score         (total strokes as float, cast to int, nullable)
    displayValue   → score_to_par  (string like "-2"/"E"/"+1", parsed to int, nullable)
    currentPosition→ position      (int rank after this round, stored as string, nullable)
    isPlayoff      → is_playoff    (bool, default False)

    The linescores array nested inside each round item contains hole-by-hole
    data (18 items per round). We do NOT store that level of detail — only the
    round summary fields listed above.

    This single endpoint call replaces the old /status endpoint call, which only
    returned the CURRENT round's tee time. The /linescores endpoint returns ALL
    rounds, giving us historical round data for display.

    Side-effect on tournament_entries.tee_time:
    The caller (upsert_field) reads the latest round's tee_time from the returned
    dicts and writes it back to tournament_entries.tee_time for pick-locking.

    Returns:
      (athlete_id, rounds) where rounds is a list of dicts ready to upsert into
      tournament_entry_rounds. An empty list means no linescores data available.
    """
    url = (
        f"{_CORE_API_BASE}/events/{pga_tour_id}"
        f"/competitions/{competition_id}/competitors/{athlete_id}/linescores"
    )
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                return athlete_id, []
            data = resp.json()
    except httpx.RequestError as exc:
        log.warning("Could not fetch linescores for athlete %s: %s", athlete_id, exc)
        return athlete_id, []

    rounds: list[dict] = []
    for item in data.get("items", []):
        # ESPN "period" is the round number. Skip items without a valid period.
        raw_period = item.get("period")
        if raw_period is None:
            continue
        try:
            round_number = int(raw_period)
        except (ValueError, TypeError):
            log.warning(
                "Unexpected period value for athlete %s: %r — skipping round",
                athlete_id, raw_period,
            )
            continue

        # Parse tee_time from ISO 8601 UTC string (e.g. "2026-03-05T13:45Z").
        tee_time_utc: datetime | None = None
        raw_tee_time = item.get("teeTime")
        if raw_tee_time:
            try:
                dt = datetime.fromisoformat(raw_tee_time.replace("Z", "+00:00"))
                tee_time_utc = dt.astimezone(timezone.utc)
            except (ValueError, TypeError):
                log.warning(
                    "Could not parse teeTime %r for athlete %s round %d",
                    raw_tee_time, athlete_id, round_number,
                )

        # ESPN "value" is strokes as a float (e.g. 70.0); cast to int.
        raw_value = item.get("value")
        score: int | None = None
        if raw_value is not None:
            try:
                score = int(float(raw_value))
            except (ValueError, TypeError):
                pass

        # ESPN "displayValue": score-to-par string for this round ("-2", "E", "+1").
        score_to_par = _parse_score_to_par(item.get("displayValue"))

        # ESPN "currentPosition": leaderboard rank after this round (integer).
        # Stored as a string to accommodate future positional formats (e.g. "T5").
        raw_pos = item.get("currentPosition")
        position: str | None = str(raw_pos) if raw_pos is not None else None

        # ESPN "isPlayoff": true only for playoff rounds.
        is_playoff = bool(item.get("isPlayoff", False))

        rounds.append({
            "round_number": round_number,
            "tee_time": tee_time_utc,
            "score": score,
            "score_to_par": score_to_par,
            "position": position,
            "is_playoff": is_playoff,
        })

    return athlete_id, rounds


def _fetch_tournament_data(
    pga_tour_id: str,
    known_golfer_ids: set[str] | None = None,
    fetch_round_data: bool = False,
) -> tuple[list[dict], list[dict]]:
    """
    Fetch the golfer field and finish order for one individual (non-team) tournament.

    Uses the ESPN core API competitors endpoint (event-specific, unlike the
    web scoreboard which ignores the event parameter). One request gets all
    competitor IDs and finish positions; athlete names are fetched concurrently
    for golfers not already cached in known_golfer_ids.

    Earnings are left as None — fetched on-demand in score_picks() for only
    the golfers users actually picked (1 API call per pick, not per field).

    When fetch_round_data=True, also fetches per-round data for each golfer
    from the /competitors/{id}/linescores endpoint concurrently. This returns
    all rounds played (tee time, strokes, score-to-par, position per round)
    and replaces the older /status-only tee time fetch. Enabled for both
    SCHEDULED (pre-tournament tee times) and IN_PROGRESS / COMPLETED tournaments
    (live and historical round scores).

    Args:
      pga_tour_id:       ESPN event ID for the tournament (also the competition ID
                         for individual tournaments).
      known_golfer_ids:  pga_tour_ids already in the DB; skips re-fetching them.
      fetch_round_data:  If True, fetch per-round linescores from the /linescores
                         sub-endpoint for every competitor. Adds ~N concurrent HTTP
                         calls where N is field size (~72-156). Defaults to False.

    Returns:
      golfers  — list of dicts ready to upsert as Golfer rows
      results  — list of dicts ready to upsert as TournamentEntry rows; each dict
                 includes a "rounds" key with a list of per-round dicts (may be
                 empty if fetch_round_data=False or no linescores available).
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

    all_athlete_ids = [str(c["id"]) for c in competitors if c.get("id")]
    known = known_golfer_ids or set()
    ids_to_fetch = [aid for aid in all_athlete_ids if aid not in known]

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

    # Step 3 (optional): fetch per-round linescores from the /linescores sub-endpoint.
    # For individual events competition_id == pga_tour_id.
    # rounds_by_athlete maps athlete_id → list of round dicts (may be empty).
    rounds_by_athlete: dict[str, list[dict]] = {}
    if fetch_round_data and all_athlete_ids:
        with concurrent.futures.ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
            futures_rd = {
                pool.submit(_fetch_competitor_rounds, pga_tour_id, pga_tour_id, aid): aid
                for aid in all_athlete_ids
            }
            for future in concurrent.futures.as_completed(futures_rd):
                try:
                    aid, rounds = future.result()
                    rounds_by_athlete[aid] = rounds
                except Exception as exc:
                    log.warning("Round data fetch failed: %s", exc)
        non_empty = sum(1 for rds in rounds_by_athlete.values() if rds)
        log.info(
            "Tournament %s: fetched round data for %d competitors (%d with rounds)",
            pga_tour_id, len(rounds_by_athlete), non_empty,
        )

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

        rounds = rounds_by_athlete.get(athlete_id, []) if fetch_round_data else []

        # Derive tee_time for tournament_entries.tee_time from Round 1 only.
        # Once Thursday starts, the pick is locked for the whole tournament —
        # we never overwrite this with a later round's tee time.
        current_tee_time: datetime | None = next(
            (rd["tee_time"] for rd in rounds if rd["round_number"] == 1 and rd["tee_time"] is not None),
            None,
        )

        results.append({
            "pga_tour_id": athlete_id,
            "finish_position": c.get("order"),
            "earnings_usd": None,
            "status": None,
            "tee_time": current_tee_time,
            "rounds": rounds,
            "team_competitor_id": None,
        })

    return golfers, results


def _fetch_team_roster(competition_id: str, team_competitor_id: str) -> list[str]:
    """
    Fetch the individual athlete IDs for one team competitor.

    The Zurich Classic (and any future team-format events) lists teams as
    competitors rather than individual golfers. This sub-endpoint expands a
    team into its individual player IDs so we can create proper Golfer rows.

    Returns a list of pga_tour_id strings (individual athlete IDs).
    """
    url = (
        f"{_CORE_API_BASE}/competitions/{competition_id}"
        f"/competitors/{team_competitor_id}/roster"
    )
    try:
        data = _get_json(url)
        return [str(e["playerId"]) for e in data.get("entries", []) if e.get("playerId")]
    except (httpx.HTTPError, httpx.RequestError) as exc:
        log.warning(
            "Could not fetch roster for team %s in competition %s: %s",
            team_competitor_id, competition_id, exc,
        )
        return []


def _fetch_team_field(
    pga_tour_id: str,
    competition_id: str,
    known_golfer_ids: set[str] | None = None,
    fetch_round_data: bool = False,
) -> tuple[list[dict], list[dict]]:
    """
    Fetch the individual golfer field for a team-format tournament.

    Team events (e.g. Zurich Classic) list team competitors instead of
    individual athletes. This function:
      1. Fetches all team competitors for the competition.
      2. Expands each team into its two individual athlete IDs via the roster
         sub-endpoint.
      3. Fetches athlete info (name, country) concurrently for new golfers.
      4. Optionally fetches per-round linescores from the /linescores sub-endpoint
         when fetch_round_data=True (all tournament states — provides tee times
         for upcoming rounds and scores/positions for completed rounds).
      5. Returns golfers + results lists with team_competitor_id set on each
         entry so score_picks can use the correct earnings endpoint later.

    Note on team event linescores: the /linescores endpoint uses the individual
    athlete_id as the competitor key (not the team_id), so the same
    _fetch_competitor_rounds helper works here. The competition_id used
    in the URL must be the team event's actual competition_id (may differ
    from pga_tour_id).

    Args:
      pga_tour_id:       ESPN event ID (used in earnings API URL).
      competition_id:    ESPN competition ID (may differ from pga_tour_id for
                         team events — stored in Tournament.competition_id).
      known_golfer_ids:  pga_tour_ids already in the DB; skips re-fetching them.
      fetch_round_data:  If True, fetch per-round linescores from the /linescores
                         sub-endpoint for every individual golfer. Defaults to False.

    Returns:
      golfers  — list of dicts (one per individual golfer, not per team)
      results  — list of dicts (one per individual golfer, with team_competitor_id
                 and a "rounds" key with a list of per-round dicts)
    """
    competitors_url = (
        f"{_CORE_API_BASE}/events/{pga_tour_id}"
        f"/competitions/{competition_id}/competitors"
    )
    data = _get_json(competitors_url, params={"limit": 200})
    team_competitors = data.get("items", [])

    if not team_competitors:
        log.warning("No team competitors found for tournament %s (competition %s)", pga_tour_id, competition_id)
        return [], []

    known = known_golfer_ids or set()

    # Expand each team into individual athlete IDs, preserving team_competitor_id.
    # team_entries: list of (athlete_id, team_competitor_id, finish_order)
    team_entries: list[tuple[str, str, int | None]] = []
    for team in team_competitors:
        team_id = str(team.get("id", ""))
        if not team_id:
            continue
        finish_order = team.get("order")
        athlete_ids = _fetch_team_roster(competition_id, team_id)
        for athlete_id in athlete_ids:
            team_entries.append((athlete_id, team_id, finish_order))

    # Fetch athlete info for golfers not already in DB.
    ids_to_fetch = [aid for aid, _, _ in team_entries if aid not in known]
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

    # Fetch per-round linescores for all individual golfers when requested.
    # For team events the /linescores URL uses the individual athlete_id, not the team_id.
    # rounds_by_athlete maps athlete_id → list of per-round dicts.
    rounds_by_athlete: dict[str, list[dict]] = {}
    if fetch_round_data and team_entries:
        all_athlete_ids = [aid for aid, _, _ in team_entries]
        with concurrent.futures.ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
            futures_rd = {
                pool.submit(_fetch_competitor_rounds, pga_tour_id, competition_id, aid): aid
                for aid in all_athlete_ids
            }
            for future in concurrent.futures.as_completed(futures_rd):
                try:
                    aid, rounds = future.result()
                    rounds_by_athlete[aid] = rounds
                except Exception as exc:
                    log.warning("Round data fetch failed: %s", exc)
        non_empty = sum(1 for rds in rounds_by_athlete.values() if rds)
        log.info(
            "Team tournament %s: fetched round data for %d golfers (%d with rounds)",
            pga_tour_id, len(rounds_by_athlete), non_empty,
        )

    log.info(
        "Team tournament %s: %d teams → %d individual golfers, %d new athlete fetches",
        pga_tour_id, len(team_competitors), len(team_entries), len(ids_to_fetch),
    )

    golfers: list[dict] = []
    results: list[dict] = []
    for athlete_id, team_id, finish_order in team_entries:
        info = athlete_info.get(athlete_id)
        golfers.append({
            "pga_tour_id": athlete_id,
            "name": info["name"] if info else None,
            "country": info["country"] if info else None,
        })

        rounds = rounds_by_athlete.get(athlete_id, []) if fetch_round_data else []

        # Derive tee_time for tournament_entries.tee_time from Round 1 only.
        # Once Thursday starts, the pick is locked for the whole tournament —
        # we never overwrite this with a later round's tee time.
        current_tee_time: datetime | None = next(
            (rd["tee_time"] for rd in rounds if rd["round_number"] == 1 and rd["tee_time"] is not None),
            None,
        )

        results.append({
            "pga_tour_id": athlete_id,
            "finish_position": finish_order,
            "earnings_usd": None,
            "status": None,
            "tee_time": current_tee_time,
            "rounds": rounds,
            "team_competitor_id": team_id,
        })

    return golfers, results


def _fetch_golfer_earnings(
    pga_tour_id: str,
    competitor_id: str,
    competition_id: str | None = None,
    is_team_event: bool = False,
) -> int | None:
    """
    Fetch prize earnings for one pick from the ESPN core API.

    Called by score_picks() only for golfers that have actual picks — keeps
    total API requests low (one per league member who submitted a pick).

    For individual tournaments:
      - competitor_id is the golfer's pga_tour_id
      - stat name is 'amount'
      - competition_id defaults to pga_tour_id

    For team tournaments (e.g. Zurich Classic):
      - competitor_id is the team's ESPN competitor ID (team_competitor_id)
      - stat name is 'officialAmount' (ESPN sets 'amount' to 0 for team events)
      - earnings are the TEAM's total purse; divide by 2 for per-golfer share
      - competition_id is the event's actual competition ID (stored in Tournament)

    Returns earnings in USD as an integer, or None if not found.
    """
    effective_competition_id = competition_id or pga_tour_id
    stats_url = (
        f"{_CORE_API_BASE}/events/{pga_tour_id}"
        f"/competitions/{effective_competition_id}/competitors/{competitor_id}/statistics"
    )
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
            resp = client.get(stats_url)
            if resp.status_code != 200:
                return None
            stats_data = resp.json()
    except httpx.RequestError as exc:
        log.warning("Could not fetch earnings for competitor %s: %s", competitor_id, exc)
        return None

    stat_name = "officialAmount" if is_team_event else "amount"

    for cat in stats_data.get("splits", {}).get("categories", []):
        for stat in cat.get("stats", []):
            if stat.get("name") == stat_name:
                raw = stat.get("value")
                if raw is not None:
                    try:
                        val = int(float(raw))
                        if val > 0:
                            # Team events: officialAmount is the team's combined prize;
                            # each golfer earns half.
                            return val // 2 if is_team_event else val
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

    For each event we also extract:
      competition_id  — the ESPN competition ID, which may differ from the event ID
                        for team-format events (e.g. Zurich Classic uses "11450")
      is_team_event   — True if the scoreboard lists type="team" competitors

    These two fields allow sync_tournament to use the correct API endpoints
    without re-fetching the scoreboard on every field sync.
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

        # Extract competition_id — for team events this differs from event_id.
        competition_id = str(comp.get("id") or event_id)

        # Detect team format: ESPN marks team-event competitors with type="team".
        competitors_sample = comp.get("competitors") or []
        is_team_event = bool(
            competitors_sample and competitors_sample[0].get("type") == "team"
        )

        results.append({
            "pga_tour_id": str(event_id),
            "competition_id": competition_id,
            "is_team_event": is_team_event,
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

    competition_id and is_team_event are set on creation and updated only if
    competition_id is not already set (safe to re-run; avoids overwriting
    manually corrected values).
    """
    created, updated = 0, 0
    for item in parsed:
        existing = db.query(Tournament).filter_by(pga_tour_id=item["pga_tour_id"]).first()
        if existing:
            existing.name = item["name"]
            existing.start_date = item["start_date"]
            existing.end_date = item["end_date"]
            existing.status = item["status"]
            # Only update team-event fields if not yet set (preserves manual corrections).
            if existing.competition_id is None:
                existing.competition_id = item.get("competition_id")
                existing.is_team_event = item.get("is_team_event", False)
            updated += 1
        else:
            db.add(Tournament(
                pga_tour_id=item["pga_tour_id"],
                competition_id=item.get("competition_id"),
                is_team_event=item.get("is_team_event", False),
                name=item["name"],
                start_date=item["start_date"],
                end_date=item["end_date"],
                status=item["status"],
                multiplier=item.get("multiplier", 1.0),
            ))
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
    For team events each result dict includes team_competitor_id, which is
    stored on the entry so score_picks can call the correct earnings endpoint.
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
            if result.get("team_competitor_id") is not None:
                entry.team_competitor_id = result["team_competitor_id"]
        else:
            entry = TournamentEntry(
                tournament_id=tournament.id,
                golfer_id=golfer.id,
                finish_position=result.get("finish_position"),
                earnings_usd=result.get("earnings_usd"),
                status=result.get("status"),
                tee_time=result.get("tee_time"),
                team_competitor_id=result.get("team_competitor_id"),
            )
            db.add(entry)
            entries_synced += 1

        # Upsert per-round data into tournament_entry_rounds.
        # Each round dict came from _fetch_competitor_rounds via the /linescores endpoint.
        # flush() ensures entry.id is set before we reference it as a FK.
        rounds = result.get("rounds", [])
        if rounds:
            db.flush()  # populate entry.id if this is a new entry
            for rd in rounds:
                round_row = db.query(TournamentEntryRound).filter_by(
                    tournament_entry_id=entry.id,
                    round_number=rd["round_number"],
                ).first()
                if round_row:
                    # Update all mutable fields — data may change while a tournament
                    # is in progress (scores finalize, position updates, etc.).
                    if rd.get("tee_time") is not None:
                        round_row.tee_time = rd["tee_time"]
                    if rd.get("score") is not None:
                        round_row.score = rd["score"]
                    if rd.get("score_to_par") is not None:
                        round_row.score_to_par = rd["score_to_par"]
                    if rd.get("position") is not None:
                        round_row.position = rd["position"]
                    round_row.is_playoff = rd.get("is_playoff", False)
                else:
                    db.add(TournamentEntryRound(
                        tournament_entry_id=entry.id,
                        round_number=rd["round_number"],
                        tee_time=rd.get("tee_time"),
                        score=rd.get("score"),
                        score_to_par=rd.get("score_to_par"),
                        position=rd.get("position"),
                        is_playoff=rd.get("is_playoff", False),
                    ))

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

    Team events (e.g. Zurich Classic):
      The earnings endpoint uses the team's ESPN competitor ID, not the
      individual golfer's ID. We look this up from TournamentEntry.team_competitor_id.
      ESPN reports team earnings under 'officialAmount'; we divide by 2 for
      each golfer's individual share.

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
            # Not stored yet — fetch from ESPN core API for this specific pick.
            # For team events, use the team_competitor_id as the competitor_id;
            # for individual events, use the golfer's own pga_tour_id.
            if tournament.is_team_event and entry and entry.team_competitor_id:
                competitor_id = entry.team_competitor_id
            else:
                golfer = db.query(Golfer).filter_by(id=pick.golfer_id).first()
                competitor_id = golfer.pga_tour_id if golfer else None

            if competitor_id:
                raw = _fetch_golfer_earnings(
                    tournament.pga_tour_id,
                    competitor_id,
                    competition_id=tournament.competition_id,
                    is_team_event=tournament.is_team_event,
                )
                if raw is not None:
                    earnings = float(raw)
                    # Persist so future calls skip the API hit.
                    if entry:
                        entry.earnings_usd = raw

        # Use the league's per-tournament multiplier override if set; otherwise
        # fall back to the tournament's global multiplier.
        lt = db.query(LeagueTournament).filter_by(
            league_id=pick.league_id, tournament_id=tournament.id
        ).first()
        effective_multiplier = (
            lt.multiplier if lt and lt.multiplier is not None else tournament.multiplier
        )
        pick.points_earned = (earnings or 0.0) * effective_multiplier
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

    Routes to _fetch_team_field for team-format tournaments (is_team_event=True)
    or _fetch_tournament_data for standard individual tournaments. After upserting
    golfers and entries, scores any pending picks if the tournament is completed.

    Per-round data (tee times, strokes, score-to-par, position) is fetched for
    all tournament states using the ESPN /linescores endpoint. This single call
    covers tee times for upcoming rounds (for pick-locking) and historical
    round scores for completed tournaments.

    Returns a summary dict with counts.
    """
    tournament = db.query(Tournament).filter_by(pga_tour_id=pga_tour_id).first()
    if not tournament:
        raise ValueError(f"Tournament with pga_tour_id '{pga_tour_id}' not found in DB. "
                         "Run sync_schedule first.")

    log.info("Syncing tournament '%s' (id=%s, team=%s)", tournament.name, pga_tour_id, tournament.is_team_event)

    # Fetch purse from the core event endpoint (site API scoreboard doesn't include it).
    try:
        event_data = _get_json(f"{_CORE_API_BASE}/events/{pga_tour_id}")
        raw_purse = event_data.get("purse")
        if raw_purse is not None:
            tournament.purse_usd = int(raw_purse)
            db.commit()
    except Exception as exc:
        log.warning("Could not fetch purse for %s: %s", pga_tour_id, exc)

    # Pass IDs of golfers already in DB so fetch functions skip re-fetching them.
    known_ids = {g.pga_tour_id for g in db.query(Golfer).all()}

    # Fetch per-round linescores for all tournament states:
    #   - SCHEDULED: gets tee times for upcoming rounds (pick-locking needs this).
    #   - IN_PROGRESS: gets live scores + positions for rounds already played.
    #   - COMPLETED: gets historical round-by-round data for display.
    # We fetch round data in all cases now because /linescores is the single
    # endpoint that covers both tee times and scores — no need to special-case.
    should_fetch_round_data = True

    try:
        if tournament.is_team_event:
            # Use the stored competition_id (may differ from pga_tour_id for team events).
            effective_competition_id = tournament.competition_id or pga_tour_id
            golfers, results = _fetch_team_field(
                pga_tour_id,
                effective_competition_id,
                known_golfer_ids=known_ids,
                fetch_round_data=should_fetch_round_data,
            )
        else:
            golfers, results = _fetch_tournament_data(
                pga_tour_id,
                known_golfer_ids=known_ids,
                fetch_round_data=should_fetch_round_data,
            )
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
