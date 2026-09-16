"""The runtime gate: fetch() ASSERTS rather than filters.

Filtering silently would let a subtly wrong provider ship and only surface as
inexplicably good replay results months later.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from tscheduler.core.clock import EPOCH_ZERO, AsOf, LookaheadError
from tscheduler.providers.base import (
    Confidence,
    Provider,
    ProviderKind,
    Record,
    Temporality,
)

T0 = datetime(2026, 9, 12, 22, 0, tzinfo=UTC)


@dataclass
class _Query:
    n: int = 0


def _rec(
    offset_min: int, *, oracle: bool = False, conf: Confidence = Confidence.EXACT
) -> Record[int]:
    return Record(
        value=offset_min,
        published_at=T0 + timedelta(minutes=offset_min),
        source_id="test",
        record_id=f"r{offset_min}",
        valid_from=T0 + timedelta(hours=3),
        confidence=conf,
        oracle=oracle,
    )


class _Leaky(Provider[_Query, int]):
    """Deliberately ignores as_of -- stands in for a provider whose upstream
    filter is wrong."""

    kind = ProviderKind.WEATHER_FORECAST
    source_id = "test.leaky"

    def __init__(self, records: Sequence[Record[int]]) -> None:
        self._records = list(records)

    def _fetch(self, query: _Query, as_of: AsOf) -> Sequence[Record[int]]:
        return self._records

    def coverage_key(self, query: _Query) -> str:
        return "k"


class _Static(_Leaky):
    temporality = Temporality.STATIC
    source_id = "test.static"


def test_compliant_provider_passes() -> None:
    p = _Leaky([_rec(-60), _rec(-10)])
    rs = p.fetch(_Query(), AsOf.at(T0))
    assert len(rs) == 2


def test_record_published_after_as_of_raises() -> None:
    p = _Leaky([_rec(-10), _rec(+30)])
    with pytest.raises(LookaheadError, match=r"published after"):
        p.fetch(_Query(), AsOf.at(T0))


def test_error_names_the_worst_offender() -> None:
    p = _Leaky([_rec(+5), _rec(+90)])
    with pytest.raises(LookaheadError, match=r"'r90'"):
        p.fetch(_Query(), AsOf.at(T0))


def test_boundary_record_is_allowed() -> None:
    p = _Leaky([_rec(0)])
    assert len(p.fetch(_Query(), AsOf.at(T0))) == 1


def test_static_provider_is_stamped_epoch_zero_and_never_gated() -> None:
    """A star catalogue published 'in the future' is a nonsense condition; static
    data is timeless and must not be filterable."""
    p = _Static([_rec(+9999)])
    rs = p.fetch(_Query(), AsOf.at(T0))
    assert [r.published_at for r in rs.records] == [EPOCH_ZERO]


def test_oracle_record_refused_for_replay_vantage() -> None:
    p = _Leaky([_rec(-10, oracle=True)])
    with pytest.raises(LookaheadError, match="oracle-tainted"):
        p.fetch(_Query(), AsOf.at(T0))


def test_oracle_record_allowed_for_oracle_vantage() -> None:
    p = _Leaky([_rec(-10, oracle=True)])
    assert len(p.fetch(_Query(), AsOf.oracle(T0))) == 1


def test_latest_per_keeps_most_recently_published() -> None:
    """The canonical forecast operation: same valid hour, three model runs; the
    newest run available at as_of wins."""
    valid = T0 + timedelta(hours=3)
    runs = [
        Record(1, T0 - timedelta(hours=6), "s", "run00", valid_from=valid),
        Record(2, T0 - timedelta(hours=1), "s", "run12", valid_from=valid),
        Record(3, T0 - timedelta(hours=3), "s", "run06", valid_from=valid),
    ]
    p = _Leaky(runs)
    latest = p.fetch(_Query(), AsOf.at(T0)).latest_per(lambda r: r.valid_from)
    assert latest.values() == (2,), "should keep run12, the newest published"


def test_evidence_ledger_tracks_max_published_and_estimated_fraction() -> None:
    p = _Leaky([_rec(-60), _rec(-10, conf=Confidence.ESTIMATED)])
    led = p.fetch(_Query(), AsOf.at(T0)).evidence()
    assert led.max_published == T0 - timedelta(minutes=10)
    assert led.estimated_fraction() == 0.5
    assert not led.has_oracle


def test_ledger_ignores_epoch_zero_in_max_published() -> None:
    """Static data must not drag max_published down to 1970 and mask a real leak."""
    p = _Static([_rec(-60)])
    assert p.fetch(_Query(), AsOf.at(T0)).evidence().max_published is None


def test_fetch_cannot_be_overridden() -> None:
    """@final is advisory to type checkers; assert the contract holds at runtime
    by checking the method identity is the base one."""
    assert _Leaky.fetch is Provider.fetch
