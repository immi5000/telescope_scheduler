"""Telescope, camera and mount."""

from __future__ import annotations

import math
from dataclasses import dataclass

ARCSEC_PER_RADIAN = 206264.806247


@dataclass(frozen=True, slots=True)
class Optics:
    aperture_mm: float
    focal_length_mm: float
    central_obstruction_mm: float = 0.0
    throughput: float = 0.80
    """Total optical transmission. Two aluminised mirrors ~0.81, SCT with
    corrector ~0.75, refractor ~0.95. 0.80 is a PLACEHOLDER -- users should set
    it, because it scales the signal linearly."""

    @property
    def collecting_area_cm2(self) -> float:
        d = self.aperture_mm / 10.0
        o = self.central_obstruction_mm / 10.0
        return math.pi / 4.0 * (d * d - o * o)

    @property
    def focal_ratio(self) -> float:
        return self.focal_length_mm / self.aperture_mm


@dataclass(frozen=True, slots=True)
class Camera:
    pixel_size_um: float
    sensor_width_px: int
    sensor_height_px: int
    quantum_efficiency: float = 0.70
    read_noise_e: float = 1.5
    dark_current_e_per_s: float = 0.01
    full_well_e: float = 50_000.0
    gain_e_per_adu: float = 1.0
    readout_s: float = 2.0

    def pixel_scale_arcsec(self, optics: Optics) -> float:
        """206.265 * pixel_size_um / focal_length_mm."""
        return ARCSEC_PER_RADIAN * (self.pixel_size_um * 1e-6) / (optics.focal_length_mm * 1e-3)

    def fov_deg(self, optics: Optics) -> tuple[float, float]:
        """Field of view (width, height) in degrees.

        Uses a proper arctangent rather than the small-angle shortcut, so wide
        short-focal-length rigs do not quietly report too small a field.
        """
        p = self.pixel_scale_arcsec(optics) / 3600.0
        return (self.sensor_width_px * p, self.sensor_height_px * p)


@dataclass(frozen=True, slots=True)
class Mount:
    switch_minutes: float = 5.0
    """Time to move to and set up a new target manually: slew, centre, focus."""
    min_block_minutes: float = 20.0
    """Nobody pointing by hand wants to re-aim every ten minutes."""
