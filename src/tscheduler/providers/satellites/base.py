"""Orbital-element providers.

The publication-time question is sharper here than anywhere else in the system,
and it is the one place where the naive implementation is wrong in BOTH
directions:

    A TLE's EPOCH is the reference time of the orbital state.
    A TLE's CREATION_DATE is when it was published.

They are different, and filtering replay on EPOCH leaks both ways: an element
set published at 09:00 can carry an epoch of 14:00 the same day (so EPOCH-based
filtering would hide data we legitimately had), and one published tomorrow can
carry today's epoch (so it would admit data we could not have had). Space-Track
exposes CREATION_DATE precisely to make this distinction available.

CelesTrak's GP feed carries only current elements and no creation timestamp, so
it is LIVE-ONLY: published_at is the retrieval time, which cannot cause a
lookahead in a live vantage because the future has not happened.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from datetime import datetime

from tscheduler.providers.base import Provider, ProviderKind


@dataclass(frozen=True, slots=True)
class TleQuery:
    group: str = "visual"
    """CelesTrak GROUP: 'visual' (bright objects, the ones that streak),
    'starlink', 'oneweb', 'active'."""


@dataclass(frozen=True, slots=True)
class TleRecord:
    name: str
    line1: str
    line2: str
    epoch: datetime
    """Reference time of the orbital state. NOT the publication time."""
    norad_id: int

    def as_lines(self) -> tuple[str, str, str]:
        return (self.name, self.line1, self.line2)


class SatelliteElementsProvider(Provider[TleQuery, TleRecord], ABC):
    kind = ProviderKind.SATELLITE_ELEMENTS
