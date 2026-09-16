from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tscheduler.core.timegrid import TimeGrid

START = datetime(2026, 9, 12, 23, 0, tzinfo=UTC)


def test_from_window_counts_whole_slots() -> None:
    g = TimeGrid.from_window(START, START + timedelta(hours=10))
    assert len(g) == 120
    assert g.end == START + timedelta(hours=10)


def test_partial_trailing_slot_is_dropped_not_rounded_up() -> None:
    """A half-slot at dawn cannot hold a real exposure. Rounding up would let the
    solver schedule photons the session has no time to collect."""
    g = TimeGrid.from_window(START, START + timedelta(minutes=47))
    assert len(g) == 9
    assert g.end == START + timedelta(minutes=45)


def test_rejects_naive_start() -> None:
    with pytest.raises(ValueError, match="UTC"):
        TimeGrid(datetime(2026, 9, 12, 23, 0), 10)  # noqa: DTZ001


def test_rejects_inverted_window() -> None:
    with pytest.raises(ValueError, match="must be after"):
        TimeGrid.from_window(START, START - timedelta(hours=1))


def test_midpoints_are_half_a_slot_in() -> None:
    """Physics is evaluated at the midpoint, which halves discretisation error
    versus the leading edge."""
    g = TimeGrid(START, 3)
    assert g.slot_mid(0) == START + timedelta(minutes=2.5)
    assert g.slot_start(1) == START + timedelta(minutes=5)


def test_index_of_clamps_rather_than_raising() -> None:
    """Clamping is what lets a replay cursor sit at either boundary."""
    g = TimeGrid(START, 10)
    assert g.index_of(START) == 0
    assert g.index_of(START + timedelta(minutes=7)) == 1
    assert g.index_of(START - timedelta(days=1)) == 0
    assert g.index_of(START + timedelta(days=1)) == 9


def test_switch_time_splits_into_whole_slots_plus_remainder() -> None:
    """Floor + remainder, not ceil: rounding a 3-minute switch up to a whole
    5-minute slot would waste 2 minutes on every switch and systematically
    over-penalise switching."""
    g = TimeGrid(START, 10)
    assert (g.minutes_to_slots(5), g.fractional_remainder(5)) == (1, 0.0)
    assert (g.minutes_to_slots(3), g.fractional_remainder(3)) == (0, pytest.approx(0.6))
    assert (g.minutes_to_slots(7), g.fractional_remainder(7)) == (1, pytest.approx(0.4))
    assert (g.minutes_to_slots(12), g.fractional_remainder(12)) == (2, pytest.approx(0.4))


def test_mid_unix_matches_datetime_midpoints() -> None:
    g = TimeGrid(START, 5)
    assert list(g.mid_unix()) == [m.timestamp() for m in g.mids()]
