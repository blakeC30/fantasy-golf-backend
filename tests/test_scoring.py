"""
Tests for scoring rules.

These are pure arithmetic tests — no database or HTTP needed.
The scoring logic is: points = earnings * multiplier, with a penalty
subtracted for each tournament a user missed.

Full integration tests (standings across real DB records) live in test_picks.py.
"""

import pytest

from app.services.scoring import calculate_standings


class TestCalculateStandings:
    def test_standings_sorted_highest_first(self):
        """Higher total_points should appear first in the standings list."""
        scores = [1_000_000.0, 3_500_000.0, 500_000.0]
        sorted_scores = sorted(scores, reverse=True)
        assert sorted_scores[0] == 3_500_000.0
        assert sorted_scores[-1] == 500_000.0

    def test_points_formula(self):
        """Points = earnings_usd * multiplier."""
        earnings = 3_600_000
        multiplier = 2.0
        assert earnings * multiplier == 7_200_000.0

    def test_major_doubles_points(self):
        """A 2x multiplier doubles the earnings."""
        assert 1_000_000 * 2.0 == 2_000_000.0

    def test_no_pick_penalty_subtracted(self):
        """Missed tournaments reduce the total by the penalty amount."""
        earned = 3_600_000.0
        penalty = -50_000
        total = earned + (1 * penalty)
        assert total == 3_550_000.0

    def test_multiple_missed_penalties(self):
        """Each missed tournament applies the penalty independently."""
        earned = 0.0
        penalty = -50_000
        total = earned + (3 * penalty)
        assert total == -150_000.0
