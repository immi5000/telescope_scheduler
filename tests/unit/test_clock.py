"""The as-of clock. These tests encode the invariant the whole system rests on."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from tscheduler.core.clock import EPOCH_ZERO, AsOf, LookaheadError, Vantage

T0 = datetime(2026, 9, 12, 22, 0, tzinfo=UTC)


def test_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        AsOf(datetime(2026, 9, 12, 22, 0))  # noqa: DTZ001


def test_rejects_non_utc_offset() -> None:
    with pytest.raises(ValueError, match="must be UTC"):
        AsOf(datetime(2026, 9, 12, 22, 0, tzinfo=timezone(timedelta(hours=-5))))


def test_at_normalises_other_zones_to_utc() -> None:
    eastern = timezone(timedelta(hours=-5))
    a = AsOf.at(datetime(2026, 9, 12, 17, 0, tzinfo=eastern))
    assert a.t == T0
    assert a.vantage is Vantage.REPLAY


def test_live_is_tagged_live() -> None:
    assert AsOf.live().vantage is Vantage.LIVE


def test_oracle_is_tagged_oracle() -> None:
    assert AsOf.oracle(T0).vantage is Vantage.ORACLE


def test_permits_is_inclusive_at_the_boundary() -> None:
    a = AsOf.at(T0)
    assert a.permits(T0), "a record published exactly at as_of WAS knowable"
    assert a.permits(T0 - timedelta(seconds=1))
    assert not a.permits(T0 + timedelta(microseconds=1))


def test_epoch_zero_is_always_permitted() -> None:
    """Static reference data must never be gated out."""
    assert AsOf.at(T0).permits(EPOCH_ZERO)


def test_advanced_to_moves_forward_and_keeps_vantage() -> None:
    a = AsOf.at(T0)
    b = a.advanced_to(T0 + timedelta(hours=1))
    assert b.t == T0 + timedelta(hours=1)
    assert b.vantage is Vantage.REPLAY


def test_advanced_to_refuses_to_go_backwards() -> None:
    """A fold over an event timeline only ever advances; going back would mean
    re-deriving history from data that already moved on."""
    with pytest.raises(ValueError, match="only move forward"):
        AsOf.at(T0).advanced_to(T0 - timedelta(seconds=1))


def test_ordering_is_by_time() -> None:
    assert AsOf.at(T0) < AsOf.at(T0 + timedelta(minutes=5))


def test_is_hashable_and_frozen() -> None:
    a = AsOf.at(T0)
    assert {a: 1}[a] == 1
    with pytest.raises(AttributeError):
        a.t = T0  # type: ignore[misc]


def test_lookahead_error_is_an_assertion_error() -> None:
    """It is a bug, not a data condition -- so it must not be caught by
    `except Exception` handlers that mean to swallow bad input."""
    assert issubclass(LookaheadError, AssertionError)
