"""The as-of clock: the system's only notion of "now".

Every datum in this system carries THREE timestamps, and conflating any two of
them is the bug that destroys the evaluation:

    published_at  when the datum first became knowable to anyone   <-- THE GATE
    valid_time    the instant the datum describes                  <-- unconstrained
    fetched_at    when our process pulled it                       <-- bookkeeping only

The invariant this module exists to protect:

    For a plan computed at as_of = T, every record that influenced it satisfies
    published_at <= T.  valid_time is unconstrained.

This is not pedantry. Two traps it prevents:

* A TLE's EPOCH is NOT its publication time. Space-Track's gp_history exposes
  CREATION_DATE precisely to separate the two. Filtering historical elements on
  ``EPOCH <= as_of`` leaks in BOTH directions: a TLE published at 09:00 can
  carry a 14:00 epoch, and one published tomorrow can carry today's epoch.
* A weather forecast's entire purpose is ``valid_time > published_at``. Naively
  dropping rows after as_of on the time axis deletes exactly the data you need.

``AsOf`` is a distinct nominal type, never a bare ``datetime``, so a stray
``datetime.now()`` cannot be passed where an as-of instant is expected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

#: Sentinel publication time for genuinely timeless data (star catalogues, the
#: light-pollution raster). Such records can never leak, so they are stamped
#: with a time that precedes every possible as_of.
EPOCH_ZERO: Final = datetime(1970, 1, 1, tzinfo=UTC)


class Vantage(StrEnum):
    """Where an ``AsOf`` came from, which decides how much we must distrust it."""

    LIVE = "live"
    """as_of == wall clock. Cannot leak by construction: the future has not happened."""

    REPLAY = "replay"
    """as_of is historical. The whole night is already on disk, so leaks ARE possible
    and every layer must actively prevent them."""

    ORACLE = "oracle"
    """Post-hoc truth ("what the weather actually did"). Only evaluation code may
    hold one of these, and oracle-tainted records may never reach a Plan."""


class LookaheadError(AssertionError):
    """A record published after as_of reached a consumer. Always a bug, never data."""


@dataclass(frozen=True, slots=True, order=True)
class AsOf:
    """An instant, plus the vantage it was observed from.

    Construct only through :meth:`live`, :meth:`at`, or :meth:`oracle` so that the
    vantage is always a deliberate choice rather than a default.
    """

    t: datetime
    vantage: Vantage = Vantage.REPLAY

    def __post_init__(self) -> None:
        if self.t.tzinfo is None:
            raise ValueError("AsOf requires a timezone-aware datetime; got a naive one")
        if self.t.utcoffset() != UTC.utcoffset(None):
            raise ValueError(f"AsOf must be UTC; got offset {self.t.utcoffset()}")

    @classmethod
    def live(cls) -> AsOf:
        """Wall clock. The ONLY place in the package that reads the system time."""
        return cls(datetime.now(UTC), Vantage.LIVE)

    @classmethod
    def at(cls, t: datetime) -> AsOf:
        """A historical instant, for replay."""
        return cls(t.astimezone(UTC), Vantage.REPLAY)

    @classmethod
    def oracle(cls, t: datetime) -> AsOf:
        """Post-hoc truth. Evaluation only; taints every record it touches."""
        return cls(t.astimezone(UTC), Vantage.ORACLE)

    def permits(self, published_at: datetime) -> bool:
        """True if a record published at this time was knowable by now."""
        return published_at <= self.t

    def advanced_to(self, t: datetime) -> AsOf:
        """Move forward within the same vantage. Moving backwards is a bug: a fold
        over an event timeline only ever advances."""
        new = t.astimezone(UTC)
        if new < self.t:
            raise ValueError(
                f"as_of may only move forward within a fold: {new.isoformat()} < {self.t.isoformat()}"
            )
        return AsOf(new, self.vantage)

    def __str__(self) -> str:
        return f"{self.t.isoformat()}[{self.vantage.value}]"
