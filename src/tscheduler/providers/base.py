"""The provider layer: the one place where "what was knowable at T" is enforced.

Enforcement is four independent layers, each sufficient on its own:

1. STRUCTURAL   Providers cannot read a wall clock. Enforced by an AST-walking
                guardrail test (tests/guardrails/test_no_wall_clock.py).
2. RUNTIME      ``Provider.fetch()`` is final and ASSERTS rather than filters.
                A silent filter would let a subtly wrong provider ship forever.
3. PROVENANCE   Every RecordSet yields an EvidenceLedger; ledgers merge up into
                the Plan, which asserts ledger.max_published <= plan.as_of. This
                is the ONLY layer that catches memoization leaks -- a grid layer
                computed at a later as_of being reused at an earlier one -- which
                is the leak most likely to actually happen and is invisible to
                layers 1 and 2.
4. ORACLE       "What actually happened" data is typed oracle=True and may only
                be held by evaluation code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, final

from tscheduler.core.clock import EPOCH_ZERO, AsOf, LookaheadError, Vantage


class Confidence(StrEnum):
    """How well we know a record's publication time.

    This distinction is reported in the evaluation, because it bounds how much
    the headline numbers can be trusted.
    """

    EXACT = "exact"
    """The source told us. Space-Track CREATION_DATE, Open-Meteo run availability,
    ANTARES processed_at, METAR receiptTime."""

    ESTIMATED = "estimated"
    """We inferred it, typically ``observation time + a documented latency``.
    ALeRCE and Fink expose no ingestion timestamp, so their alerts are estimated."""


class Temporality(StrEnum):
    PUBLISHED = "published"
    """Normal: the record has a real publication time and is subject to the gate."""

    STATIC = "static"
    """Timeless reference data (catalogues, light-pollution raster). Cannot leak;
    stamped EPOCH_ZERO in one place rather than special-cased at every call site."""


class ProviderKind(StrEnum):
    WEATHER_FORECAST = "weather_forecast"
    WEATHER_ACTUALS = "weather_actuals"  # ORACLE
    SATELLITE_ELEMENTS = "satellite_elements"
    TRANSIENT_ALERTS = "transient_alerts"
    LIGHT_POLLUTION = "light_pollution"  # STATIC
    CATALOG = "catalog"  # STATIC


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    provider: str
    record_id: str
    published_at: datetime
    confidence: Confidence
    oracle: bool


@dataclass(frozen=True, slots=True)
class EvidenceLedger:
    """Provenance that travels with data all the way into the Plan."""

    refs: tuple[EvidenceRef, ...] = ()

    def merge(self, other: EvidenceLedger) -> EvidenceLedger:
        return EvidenceLedger(self.refs + other.refs)

    @property
    def max_published(self) -> datetime | None:
        real = [r.published_at for r in self.refs if r.published_at != EPOCH_ZERO]
        return max(real) if real else None

    @property
    def has_oracle(self) -> bool:
        return any(r.oracle for r in self.refs)

    def estimated_fraction(self) -> float:
        """Share of evidence whose publication time we guessed. Goes in the report
        so every headline number carries its own caveat."""
        if not self.refs:
            return 0.0
        n = sum(1 for r in self.refs if r.confidence is Confidence.ESTIMATED)
        return n / len(self.refs)


@dataclass(frozen=True, slots=True)
class Record[T]:
    value: T
    published_at: datetime
    source_id: str
    record_id: str
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    confidence: Confidence = Confidence.EXACT
    oracle: bool = False

    def ref(self) -> EvidenceRef:
        return EvidenceRef(
            self.source_id, self.record_id, self.published_at, self.confidence, self.oracle
        )


@dataclass(frozen=True, slots=True)
class RecordSet[T]:
    records: tuple[Record[T], ...]
    as_of: AsOf
    provider: str

    def latest_per(self, key: Callable[[Record[T]], Hashable]) -> RecordSet[T]:
        """For each key, keep the record with the greatest ``published_at``.

        THE canonical forecast operation -- "the most recent run that had been
        issued by as_of, for each valid hour". It lives here rather than in each
        provider because it is the step everyone gets wrong.
        """
        best: dict[Hashable, Record[T]] = {}
        for r in self.records:
            k = key(r)
            cur = best.get(k)
            if cur is None or r.published_at > cur.published_at:
                best[k] = r
        ordered = tuple(sorted(best.values(), key=lambda r: r.valid_from or r.published_at))
        return RecordSet(ordered, self.as_of, self.provider)

    def values(self) -> tuple[T, ...]:
        return tuple(r.value for r in self.records)

    def evidence(self) -> EvidenceLedger:
        return EvidenceLedger(tuple(r.ref() for r in self.records))

    def __len__(self) -> int:
        return len(self.records)


class Provider[Q, T](ABC):
    """Base for every external data source.

    Subclasses implement ``_fetch``; ``fetch`` is final and verifies them.
    """

    kind: ClassVar[ProviderKind]
    source_id: ClassVar[str]
    temporality: ClassVar[Temporality] = Temporality.PUBLISHED
    produces_oracle: ClassVar[bool] = False

    @final
    def fetch(self, query: Q, as_of: AsOf) -> RecordSet[T]:
        raw = list(self._fetch(query, as_of))

        if self.temporality is Temporality.STATIC:
            raw = [replace(r, published_at=EPOCH_ZERO) for r in raw]

        late = [r for r in raw if not as_of.permits(r.published_at)]
        if late:
            worst = max(late, key=lambda r: r.published_at)
            raise LookaheadError(
                f"{self.source_id} returned {len(late)} record(s) published after "
                f"as_of={as_of.t.isoformat()}; worst offender {worst.record_id!r} @ "
                f"{worst.published_at.isoformat()} (+{worst.published_at - as_of.t})"
            )

        if as_of.vantage is not Vantage.ORACLE and any(r.oracle for r in raw):
            raise LookaheadError(
                f"{self.source_id} returned oracle-tainted records to a "
                f"{as_of.vantage.value} vantage; only evaluation may read actuals"
            )

        return RecordSet(tuple(raw), as_of, self.source_id)

    @abstractmethod
    def _fetch(self, query: Q, as_of: AsOf) -> Sequence[Record[T]]:
        """Return records. MUST bound the upstream query by ``as_of``; ``fetch``
        then verifies you did. Returning a superset is a loud failure, by design."""

    @abstractmethod
    def coverage_key(self, query: Q) -> str:
        """Stable cache key for the query (NOT including as_of -- see cache.py)."""

    def publication_times(self, query: Q, horizon: AsOf) -> tuple[datetime, ...]:
        """Distinct instants at or before ``horizon`` at which this source published.

        These are the replay fold's decision points: between two consecutive
        publications nothing visible changes, so the plan is identical by
        construction and the slider can index rather than re-solve.

        Asking with ``horizon = night_end`` is not a lookahead -- it enumerates
        *when* information will arrive, and every plan is still built at its own
        as_of, which re-applies the gate. The records themselves go no further
        than this method.
        """
        return tuple(sorted({r.published_at for r in self.fetch(query, horizon).records}))
