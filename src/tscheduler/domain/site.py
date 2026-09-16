"""Observing site."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Site:
    latitude_deg: float
    longitude_deg: float
    """Positive EAST. The single most common user error is entering a western
    longitude as positive, so callers should sanity-check against the resolved
    timezone."""
    elevation_m: float
    name: str = "site"
    min_altitude_deg: float = 30.0
    min_moon_separation_deg: float = 15.0
    """Hard veto. A softer preference operates above this in the quality score."""
    natural_zenith_mag_arcsec2: float = 22.0
    artificial_zenith_nl: float = 0.0
    """Artificial sky brightness at the zenith, nanoLamberts, from a Falchi
    lookup or a Bortle class. Zero means a pristine site."""
    extinction_k: float = 0.20
    """mag/airmass in V. 0.12-0.15 at a good mountain site, 0.25-0.35 hazy."""

    def __post_init__(self) -> None:
        if not -90.0 <= self.latitude_deg <= 90.0:
            raise ValueError(f"latitude out of range: {self.latitude_deg}")
        if not -180.0 <= self.longitude_deg <= 360.0:
            raise ValueError(f"longitude out of range: {self.longitude_deg}")
