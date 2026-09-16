"""The night's slot grid -- the shared index for everything.

`slot` is the universal join key: the timeline, instruction cards, quality grid,
plan diffs and comparison strips all index the same grid. Nothing in the system
renders off a timestamp it computed for itself, and two plans must never exist
on different grids (that is what keeps diffing an elementwise comparison rather
than a sequence-alignment problem).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True, slots=True)
class TimeGrid:
    """A uniform grid of slots covering the session.

    Slot ``i`` spans ``[start + i*slot, start + (i+1)*slot)``; physics is
    evaluated at the slot MIDPOINT, which halves the discretisation error versus
    using the leading edge.
    """

    start: datetime
    n_slots: int
    slot_minutes: int = 5

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.start.utcoffset() != UTC.utcoffset(None):
            raise ValueError("TimeGrid.start must be timezone-aware UTC")
        if self.n_slots <= 0:
            raise ValueError(f"n_slots must be positive, got {self.n_slots}")
        if self.slot_minutes <= 0:
            raise ValueError(f"slot_minutes must be positive, got {self.slot_minutes}")

    @classmethod
    def from_window(cls, start: datetime, end: datetime, slot_minutes: int = 5) -> TimeGrid:
        """Cover [start, end). A partial trailing slot is DROPPED rather than
        rounded up: a half-slot at dawn cannot hold a real exposure, and
        pretending otherwise would let the solver schedule photons that the
        session has no time to collect."""
        if end <= start:
            raise ValueError(f"end {end.isoformat()} must be after start {start.isoformat()}")
        total_min = (end - start).total_seconds() / 60.0
        return cls(start, int(total_min // slot_minutes), slot_minutes)

    @property
    def slot_seconds(self) -> float:
        return self.slot_minutes * 60.0

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.slot_minutes * self.n_slots)

    def slot_start(self, i: int) -> datetime:
        return self.start + timedelta(minutes=self.slot_minutes * i)

    def slot_mid(self, i: int) -> datetime:
        return self.start + timedelta(minutes=self.slot_minutes * (i + 0.5))

    def starts(self) -> list[datetime]:
        return [self.slot_start(i) for i in range(self.n_slots)]

    def mids(self) -> list[datetime]:
        return [self.slot_mid(i) for i in range(self.n_slots)]

    def mid_unix(self) -> NDArray[np.float64]:
        """Slot midpoints as Unix seconds -- the form astropy and numpy want."""
        base = self.start.timestamp()
        step = self.slot_seconds
        return base + step * (np.arange(self.n_slots, dtype=np.float64) + 0.5)

    def index_of(self, t: datetime) -> int:
        """Slot containing ``t``, clamped into range. Clamping (rather than
        raising) is what lets a replay cursor sit at either boundary."""
        off_min = (t.astimezone(UTC) - self.start).total_seconds() / 60.0
        return int(np.clip(off_min // self.slot_minutes, 0, self.n_slots - 1))

    def minutes_to_slots(self, minutes: float) -> int:
        """Whole slots fully consumed by a duration (floor).

        Used for the switch-time reservation. Floor rather than ceil because the
        fractional remainder is debited separately from the first data slot --
        rounding a 3-minute switch up to a whole 5-minute slot would waste two
        minutes on every switch and systematically over-penalise switching.
        """
        return int(minutes // self.slot_minutes)

    def fractional_remainder(self, minutes: float) -> float:
        """The leftover fraction of a slot after ``minutes_to_slots``, in [0, 1)."""
        return (minutes % self.slot_minutes) / self.slot_minutes

    def __len__(self) -> int:
        return self.n_slots
