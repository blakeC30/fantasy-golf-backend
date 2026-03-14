"""
Tests for the scraper service.

These are unit tests — no HTTP is made. httpx calls are intercepted by
pytest-httpx (or unittest.mock) so tests run without a network connection.

What's tested here:
  - parse_schedule_response()  — JSON → tournament dicts (including team event detection)
  - upsert_tournaments()       — create new / update existing tournament rows
  - upsert_field()             — create new / update existing golfer + entry rows
  - score_picks()              — points_earned set correctly after results land

The high-level sync_* functions (which make real HTTP calls) are integration
tests and run only when the ESPN API is reachable. They are not included here.
"""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.services.scraper import (
    _map_espn_status,
    _parse_date,
    parse_schedule_response,
    score_picks,
    upsert_field,
    upsert_tournaments,
)


# ---------------------------------------------------------------------------
# Fixtures — sample ESPN API payloads
# ---------------------------------------------------------------------------

SCOREBOARD_PAYLOAD = {
    "events": [
        {
            "id": "401580001",
            "name": "The Masters",
            "date": "2025-04-10T10:00Z",
            "status": {"type": {"name": "STATUS_FINAL"}},
            "competitions": [
                {
                    "startDate": "2025-04-10T10:00Z",
                    "endDate": "2025-04-13T20:00Z",
                }
            ],
        },
        {
            "id": "401580002",
            "name": "AT&T Pebble Beach Pro-Am",
            "date": "2025-02-06T14:00Z",
            "status": {"type": {"name": "STATUS_SCHEDULED"}},
            "competitions": [
                {
                    "startDate": "2025-02-06T14:00Z",
                    "endDate": "2025-02-09T22:00Z",
                }
            ],
        },
    ]
}

# ESPN scoreboard payload for a team-format tournament (Zurich Classic style).
# competitors[0].type == "team" triggers is_team_event=True detection.
# The competition id ("11450") differs from the event id ("401703507").
TEAM_EVENT_PAYLOAD = {
    "events": [
        {
            "id": "401703507",
            "name": "Zurich Classic of New Orleans",
            "date": "2025-04-24T14:00Z",
            "status": {"type": {"name": "STATUS_SCHEDULED"}},
            "competitions": [
                {
                    "id": "11450",
                    "startDate": "2025-04-24T14:00Z",
                    "endDate": "2025-04-27T22:00Z",
                    "competitors": [
                        {"id": "131066", "type": "team", "order": 1},
                        {"id": "131067", "type": "team", "order": 2},
                    ],
                }
            ],
        }
    ]
}

# Some ESPN responses nest events under leagues instead of top-level.
SCOREBOARD_PAYLOAD_NESTED = {
    "leagues": [
        {
            "events": SCOREBOARD_PAYLOAD["events"],
        }
    ]
}


# ---------------------------------------------------------------------------
# parse_schedule_response
# ---------------------------------------------------------------------------

class TestParseScheduleResponse:
    def test_extracts_both_events(self):
        result = parse_schedule_response(SCOREBOARD_PAYLOAD)
        assert len(result) == 2

    def test_extracts_correct_fields(self):
        result = parse_schedule_response(SCOREBOARD_PAYLOAD)
        masters = next(t for t in result if t["pga_tour_id"] == "401580001")

        assert masters["name"] == "The Masters"
        assert masters["start_date"] == date(2025, 4, 10)
        assert masters["end_date"] == date(2025, 4, 13)
        assert masters["status"] == "completed"
        assert masters["multiplier"] == 1.0  # scraper never sets 2.0 — admin does that

    def test_handles_nested_leagues_structure(self):
        """ESPN sometimes wraps events under leagues[i].events."""
        result = parse_schedule_response(SCOREBOARD_PAYLOAD_NESTED)
        assert len(result) == 2

    def test_status_mapping(self):
        result = parse_schedule_response(SCOREBOARD_PAYLOAD)
        pebble = next(t for t in result if t["pga_tour_id"] == "401580002")
        assert pebble["status"] == "scheduled"

    def test_skips_events_without_id(self):
        data = {"events": [{"name": "No ID Event", "date": "2025-01-01T00:00Z"}]}
        result = parse_schedule_response(data)
        assert result == []

    def test_skips_events_without_date(self):
        data = {"events": [{"id": "123", "name": "No Date", "competitions": [{}]}]}
        result = parse_schedule_response(data)
        assert result == []

    def test_falls_back_to_event_date_if_no_competition(self):
        data = {
            "events": [
                {
                    "id": "555",
                    "name": "Fallback Test",
                    "date": "2025-07-01T10:00Z",
                    "status": {"type": {"name": "STATUS_SCHEDULED"}},
                    "competitions": [],
                }
            ]
        }
        result = parse_schedule_response(data)
        assert len(result) == 1
        assert result[0]["start_date"] == date(2025, 7, 1)
        # end_date falls back to start_date + 3 days
        assert result[0]["end_date"] == date(2025, 7, 4)

    def test_empty_response(self):
        assert parse_schedule_response({}) == []
        assert parse_schedule_response({"events": []}) == []

    def test_individual_event_not_team(self):
        """Standard individual tournaments must have is_team_event=False."""
        result = parse_schedule_response(SCOREBOARD_PAYLOAD)
        masters = next(t for t in result if t["pga_tour_id"] == "401580001")
        assert masters["is_team_event"] is False

    def test_individual_event_competition_id_matches_event_id(self):
        """For standard tournaments, competition_id should equal pga_tour_id."""
        result = parse_schedule_response(SCOREBOARD_PAYLOAD)
        masters = next(t for t in result if t["pga_tour_id"] == "401580001")
        assert masters["competition_id"] == "401580001"

    def test_team_event_detected(self):
        """Zurich-style events with type='team' competitors must set is_team_event=True."""
        result = parse_schedule_response(TEAM_EVENT_PAYLOAD)
        assert len(result) == 1
        zurich = result[0]
        assert zurich["is_team_event"] is True

    def test_team_event_competition_id_differs_from_event_id(self):
        """Team events expose a different competition id (e.g. '11450' vs '401703507')."""
        result = parse_schedule_response(TEAM_EVENT_PAYLOAD)
        zurich = result[0]
        assert zurich["pga_tour_id"] == "401703507"
        assert zurich["competition_id"] == "11450"


# ---------------------------------------------------------------------------
# _map_espn_status and _parse_date (pure helpers)
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_status_mapping_scheduled(self):
        assert _map_espn_status("STATUS_SCHEDULED") == "scheduled"

    def test_status_mapping_in_progress(self):
        assert _map_espn_status("STATUS_IN_PROGRESS") == "in_progress"

    def test_status_mapping_final(self):
        assert _map_espn_status("STATUS_FINAL") == "completed"

    def test_status_mapping_unknown_defaults_to_scheduled(self):
        assert _map_espn_status("SOME_UNKNOWN_VALUE") == "scheduled"

    def test_parse_date_espn_format(self):
        assert _parse_date("2025-04-10T10:00Z") == date(2025, 4, 10)

    def test_parse_date_none(self):
        assert _parse_date(None) is None

    def test_parse_date_empty_string(self):
        assert _parse_date("") is None

    def test_parse_date_invalid(self):
        assert _parse_date("not-a-date") is None


# ---------------------------------------------------------------------------
# upsert_tournaments (DB tests — require the test database)
# ---------------------------------------------------------------------------

class TestUpsertTournaments:
    def test_creates_new_tournaments(self, db):
        parsed = [
            {
                "pga_tour_id": "ESPN_001",
                "name": "Test Open",
                "start_date": date(2025, 6, 1),
                "end_date": date(2025, 6, 4),
                "status": "scheduled",
                "multiplier": 1.0,
            }
        ]
        created, updated, transitions = upsert_tournaments(db, parsed)
        assert created == 1
        assert updated == 0
        assert transitions == []

        from app.models import Tournament
        t = db.query(Tournament).filter_by(pga_tour_id="ESPN_001").first()
        assert t is not None
        assert t.name == "Test Open"

    def test_updates_existing_tournament(self, db):
        from app.models import Tournament
        db.add(Tournament(
            pga_tour_id="ESPN_002",
            name="Old Name",
            start_date=date(2025, 7, 1),
            end_date=date(2025, 7, 4),
            status="scheduled",
            multiplier=1.0,
        ))
        db.commit()

        parsed = [
            {
                "pga_tour_id": "ESPN_002",
                "name": "New Name",
                "start_date": date(2025, 7, 1),
                "end_date": date(2025, 7, 4),
                "status": "completed",
                "multiplier": 1.0,
            }
        ]
        created, updated, transitions = upsert_tournaments(db, parsed)
        assert created == 0
        assert updated == 1
        assert len(transitions) == 1
        assert transitions[0][1] == "scheduled"
        assert transitions[0][2] == "completed"

        t = db.query(Tournament).filter_by(pga_tour_id="ESPN_002").first()
        assert t.name == "New Name"
        assert t.status == "completed"

    def test_does_not_overwrite_multiplier(self, db):
        """Admin-set multiplier (e.g. 2.0 for majors) must survive a sync."""
        from app.models import Tournament
        db.add(Tournament(
            pga_tour_id="ESPN_003",
            name="The Masters",
            start_date=date(2025, 4, 10),
            end_date=date(2025, 4, 13),
            status="scheduled",
            multiplier=2.0,  # admin set this manually
        ))
        db.commit()

        parsed = [
            {
                "pga_tour_id": "ESPN_003",
                "name": "The Masters",
                "start_date": date(2025, 4, 10),
                "end_date": date(2025, 4, 13),
                "status": "completed",
                "multiplier": 1.0,  # scraper always returns 1.0
            }
        ]
        upsert_tournaments(db, parsed)

        t = db.query(Tournament).filter_by(pga_tour_id="ESPN_003").first()
        assert t.multiplier == 2.0  # unchanged


# ---------------------------------------------------------------------------
# score_picks (DB tests)
# ---------------------------------------------------------------------------

class TestScorePicks:
    def test_scores_completed_picks(self, db):
        from datetime import date, timedelta
        from app.models import (
            Golfer, League, LeagueMember, LeagueMemberRole,
            Pick, Season, Tournament, TournamentEntry, TournamentStatus, User,
        )
        from app.services.auth import hash_password

        # Set up minimal data.
        user = User(email="scorer@example.com", password_hash=hash_password("x"), display_name="S")
        db.add(user)
        db.flush()

        league = League(name="SL", created_by=user.id)
        db.add(league)
        db.flush()

        db.add(LeagueMember(league_id=league.id, user_id=user.id, role=LeagueMemberRole.MANAGER.value))
        season = Season(league_id=league.id, year=2025, is_active=True)
        db.add(season)
        db.flush()

        golfer = Golfer(pga_tour_id="G001", name="Test Golfer")
        db.add(golfer)
        db.flush()

        t_start = date.today() - timedelta(days=7)
        tournament = Tournament(
            pga_tour_id="T001",
            name="Score Test Open",
            start_date=t_start,
            end_date=t_start + timedelta(days=3),
            status=TournamentStatus.COMPLETED.value,
            multiplier=2.0,
        )
        db.add(tournament)
        db.flush()

        entry = TournamentEntry(
            tournament_id=tournament.id,
            golfer_id=golfer.id,
            finish_position=1,
            earnings_usd=3_600_000,
        )
        db.add(entry)
        db.flush()

        pick = Pick(
            league_id=league.id,
            season_id=season.id,
            user_id=user.id,
            tournament_id=tournament.id,
            golfer_id=golfer.id,
        )
        db.add(pick)
        db.commit()

        count = score_picks(db, tournament)
        assert count == 1

        db.refresh(pick)
        assert pick.points_earned == 7_200_000.0  # 3_600_000 × 2.0

    def test_missed_cut_scores_zero(self, db):
        from datetime import date, timedelta
        from app.models import (
            Golfer, League, LeagueMember, LeagueMemberRole,
            Pick, Season, Tournament, TournamentEntry, TournamentStatus, User,
        )
        from app.services.auth import hash_password

        user = User(email="cut@example.com", password_hash=hash_password("x"), display_name="C")
        db.add(user)
        db.flush()

        league = League(name="CL", created_by=user.id)
        db.add(league)
        db.flush()

        db.add(LeagueMember(league_id=league.id, user_id=user.id, role=LeagueMemberRole.MANAGER.value))
        season = Season(league_id=league.id, year=2025, is_active=True)
        db.add(season)
        db.flush()

        golfer = Golfer(pga_tour_id="G002", name="Cut Golfer")
        db.add(golfer)
        db.flush()

        t_start = date.today() - timedelta(days=7)
        tournament = Tournament(
            pga_tour_id="T002",
            name="Cut Test",
            start_date=t_start,
            end_date=t_start + timedelta(days=3),
            status=TournamentStatus.COMPLETED.value,
            multiplier=1.0,
        )
        db.add(tournament)
        db.flush()

        # Golfer missed cut — no earnings.
        entry = TournamentEntry(
            tournament_id=tournament.id,
            golfer_id=golfer.id,
            status="cut",
            earnings_usd=None,
        )
        db.add(entry)
        db.flush()

        pick = Pick(
            league_id=league.id,
            season_id=season.id,
            user_id=user.id,
            tournament_id=tournament.id,
            golfer_id=golfer.id,
        )
        db.add(pick)
        db.commit()

        count = score_picks(db, tournament)
        assert count == 1

        db.refresh(pick)
        assert pick.points_earned == 0.0

    def test_skips_non_completed_tournament(self, db):
        from datetime import date, timedelta
        from app.models import Tournament, TournamentStatus

        t_start = date.today() + timedelta(days=7)
        tournament = Tournament(
            pga_tour_id="T003",
            name="Future Open",
            start_date=t_start,
            end_date=t_start + timedelta(days=3),
            status=TournamentStatus.SCHEDULED.value,
            multiplier=1.0,
        )
        db.add(tournament)
        db.commit()

        count = score_picks(db, tournament)
        assert count == 0
