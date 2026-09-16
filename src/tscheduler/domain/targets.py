"""Observing targets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class TargetKind(StrEnum):
    STATIC = "static"
    TRANSIENT = "transient"


@dataclass(frozen=True, slots=True)
class Target:
    id: str
    name: str
    ra_deg: float
    dec_deg: float
    magnitude: float
    priority: float = 1.0
    urgency: float = 1.0
    """Transient urgency. MUST encode only what the optimizer cannot already
    see -- science that decays in wall-clock time. Never 'it sets soon': the
    optimizer discovers window scarcity from the visibility mask, and putting it
    here double-counts and over-prioritises setting targets."""
    kind: TargetKind = TargetKind.STATIC
    snr_goal: float | None = None
    discovered_at: datetime | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.ra_deg < 360.0:
            raise ValueError(f"{self.id}: ra_deg out of range: {self.ra_deg}")
        if not -90.0 <= self.dec_deg <= 90.0:
            raise ValueError(f"{self.id}: dec_deg out of range: {self.dec_deg}")

    @property
    def weight(self) -> float:
        return self.priority * self.urgency
