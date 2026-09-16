"""The Plan, and enforcement layer 3 of the no-lookahead guarantee.

``Plan.__post_init__`` asserts that the evidence ledger contains nothing
published after the plan's own as_of. This is the ONLY layer that catches a
*memoization* leak -- a grid layer computed at a later as_of being reused at an
earlier one -- which is the leak most likely to actually occur in practice and
is completely invisible to the provider gate and the wall-clock ban.

Do not disable it as an optimisation. It costs microseconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from tscheduler.core.clock import AsOf, LookaheadError, Vantage
from tscheduler.core.hashing import hash_json
from tscheduler.core.timegrid import TimeGrid
from tscheduler.providers.base import EvidenceLedger


class SlotKind(StrEnum):
    OBSERVE = "observe"
    SWITCH = "switch"
    IDLE = "idle"


@dataclass(frozen=True, slots=True)
class Assignment:
    slot: int
    kind: SlotKind
    target_id: str | None = None
    locked: bool = False


@dataclass(frozen=True, slots=True)
class Block:
    """A contiguous run of slots on one target, as the observer experiences it."""

    target_id: str
    slot_start: int
    slot_end: int  # exclusive
    switch_slots: int
    n_subs: int
    t_sub_s: float
    expected_snr: float

    @property
    def n_slots(self) -> int:
        return self.slot_end - self.slot_start


@dataclass(frozen=True, slots=True)
class DropReason:
    target_id: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class Plan:
    grid: TimeGrid
    as_of: AsOf
    assignments: tuple[Assignment, ...]
    blocks: tuple[Block, ...]
    included: frozenset[str]
    dropped: tuple[DropReason, ...]
    objective: float
    status: str
    solve_ms: float
    best_bound: float = 0.0
    ledger: EvidenceLedger = field(default_factory=EvidenceLedger)

    def __post_init__(self) -> None:
        mp = self.ledger.max_published
        if mp is not None and mp > self.as_of.t:
            raise LookaheadError(
                f"plan at as_of={self.as_of.t.isoformat()} carries evidence published at "
                f"{mp.isoformat()} (+{mp - self.as_of.t}). A cached layer was almost "
                "certainly reused across the no-lookahead boundary."
            )
        if self.ledger.has_oracle and self.as_of.vantage is not Vantage.ORACLE:
            raise LookaheadError(
                f"plan at {self.as_of.vantage.value} vantage carries oracle-tainted evidence"
            )

    @property
    def gap(self) -> float:
        """Relative optimality gap. Surfaced in the UI rather than hidden: a
        3%-gap schedule is operationally indistinguishable from optimal, but the
        observer deserves to know which they got."""
        if self.best_bound == 0.0:
            return 0.0
        return abs(self.best_bound - self.objective) / abs(self.best_bound)

    def target_slots(self, target_id: str) -> tuple[int, ...]:
        return tuple(
            a.slot
            for a in self.assignments
            if a.target_id == target_id and a.kind is SlotKind.OBSERVE
        )

    def fingerprint(self) -> str:
        """Content hash. The assertion in the no-lookahead property test compares
        these, so it must cover everything a leak could change."""
        return hash_json(
            {
                "assignments": [(a.slot, a.kind.value, a.target_id) for a in self.assignments],
                "included": sorted(self.included),
                "dropped": sorted((d.target_id, d.code) for d in self.dropped),
                "objective": round(self.objective, 6),
            }
        )

    def summary(self) -> str:
        obs = sum(1 for a in self.assignments if a.kind is SlotKind.OBSERVE)
        sw = sum(1 for a in self.assignments if a.kind is SlotKind.SWITCH)
        idle = len(self.assignments) - obs - sw
        return (
            f"{len(self.blocks)} blocks, {len(self.included)} targets, "
            f"{obs} observing / {sw} switching / {idle} idle slots, "
            f"status={self.status}, gap={self.gap:.1%}"
        )
