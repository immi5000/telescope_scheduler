"""Telescope, camera and mount."""

from __future__ import annotations

import math
from dataclasses import dataclass

ARCSEC_PER_RADIAN = 206264.806247

#: Effective wavelength of Johnson V, the band the whole calculator works in.
V_BAND_WAVELENGTH_M = 550e-9

#: Airy core FWHM in units of lambda/D, for an UNOBSTRUCTED circular aperture.
#: A central obstruction narrows the core by a few percent and moves that
#: light into the rings; neither is modelled, and at amateur obstructions the
#: net effect on a seeing-broadened star is well under the seeing's own scatter.
AIRY_FWHM_LAMBDA_OVER_D = 1.029


@dataclass(frozen=True, slots=True)
class Optics:
    aperture_mm: float
    focal_length_mm: float
    """EFFECTIVE focal length -- after any reducer or Barlow -- because that is
    what sets the pixel scale and the field."""
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

    @property
    def diffraction_fwhm_arcsec(self) -> float:
        """Width of the Airy core at 550 nm. Added in quadrature to the seeing.

        Negligible behind an 11-inch SCT (0.4") and not at all negligible behind
        a 51 mm refractor (2.3"), which is exactly the case where ignoring it
        would overstate how concentrated a star is.
        """
        return (
            AIRY_FWHM_LAMBDA_OVER_D
            * V_BAND_WAVELENGTH_M
            / (self.aperture_mm * 1e-3)
            * ARCSEC_PER_RADIAN
        )

    @property
    def rayleigh_limit_arcsec(self) -> float:
        """1.22 lambda/D at 550 nm -- the first dark ring of the Airy pattern."""
        return 1.22 * V_BAND_WAVELENGTH_M / (self.aperture_mm * 1e-3) * ARCSEC_PER_RADIAN

    @property
    def dawes_limit_arcsec(self) -> float:
        """Dawes' empirical 116/D -- the separation an observer can just split by eye."""
        return 116.0 / self.aperture_mm

    def psf_fwhm_arcsec(self, seeing_fwhm_arcsec: float) -> float:
        """Star size on the sensor: seeing and diffraction in quadrature.

        Quadrature is the standard approximation for convolving two roughly
        Gaussian profiles; the Airy pattern is not Gaussian, but its core is
        close enough that the error is a few percent of the smaller term.
        """
        return math.hypot(seeing_fwhm_arcsec, self.diffraction_fwhm_arcsec)


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

        Uses a proper arctangent rather than the small-angle shortcut, so a
        camera lens does not quietly report too large a field (4% at 50 mm).
        """
        f_mm = optics.focal_length_mm

        def across(n_px: int) -> float:
            half_mm = n_px * self.pixel_size_um * 1e-3 / 2.0
            return 2.0 * math.degrees(math.atan(half_mm / f_mm))

        return (across(self.sensor_width_px), across(self.sensor_height_px))


@dataclass(frozen=True, slots=True)
class Mount:
    switch_minutes: float = 5.0
    """Time to move to and set up a new target manually: slew, centre, focus."""
    min_block_minutes: float = 20.0
    """Nobody pointing by hand wants to re-aim every ten minutes."""
