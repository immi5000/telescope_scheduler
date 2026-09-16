"""Exposure-time calculator: magnitudes in, electrons and seconds out.

Three things here are easy to get wrong and silent when you do:

1. **The encircled-energy fraction must be applied to the source.** A 0.67*FWHM
   photometric aperture collects ~71% of a Gaussian PSF's light, not 100%. An
   ETC that uses the optimal aperture but counts every photon overstates SNR by
   ~19%. This is the most common error in amateur exposure calculators.
2. **Extinction must NOT be applied to the sky.** ``sky_brightness()`` returns
   the brightness measured AT THE GROUND -- it has already propagated through
   the atmosphere. Its airmass dependence lives inside that function. Applying
   10^(-0.4 k X) here dims the sky a second time.
3. **Both inversions are closed form.** SNR^2 is a quadratic in t, so solving for
   "time to reach SNR" or "faintest magnitude at SNR" needs no root-finding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np
from numpy.typing import NDArray

from tscheduler.domain.equipment import Camera, Optics

#: Photon flux of a V=0 star above the atmosphere.
#:   f_lambda(V=0) = 3.63e-9 erg/s/cm^2/A at 5500 A  (Bessell, Castelli & Plez 1998)
#:   N = f_lambda * lambda / (h c) = 1004 photons/s/cm^2/A   -- the "~1000 photons" rule
#: times the Johnson V effective width of 880 A (Bessell 1990).
V_BAND_PHOTON_ZP: Final = 1004.0 * 880.0  # 8.84e5 photons/s/cm^2 for V=0

#: SNR-optimal photometric aperture radius for a Gaussian PSF in the
#: background-limited regime: ~1.58 sigma = 0.67 FWHM (Naylor 1998, MNRAS 296, 339).
OPTIMAL_APERTURE_FWHM_FACTOR: Final = 0.67

FWHM_PER_SIGMA: Final = 2.3548200450309493


@dataclass(frozen=True, slots=True)
class Aperture:
    n_pix: float
    encircled_energy: float
    undersampled: bool


def photometric_aperture(
    fwhm_arcsec: float,
    pixel_scale_arcsec: float,
    radius_factor: float = OPTIMAL_APERTURE_FWHM_FACTOR,
) -> Aperture:
    """Pixels in the aperture, and the fraction of the PSF they contain.

    Clamps ``n_pix >= 4`` when undersampled: below that the Gaussian aperture
    model breaks down, and an unclamped n_pix of 1-2 makes the noise term
    optimistic in exactly the regime where it should not be trusted.
    """
    sigma = fwhm_arcsec / FWHM_PER_SIGMA
    r_arcsec = radius_factor * fwhm_arcsec
    n_pix = np.pi * (r_arcsec / pixel_scale_arcsec) ** 2
    ee = 1.0 - np.exp(-(r_arcsec**2) / (2.0 * sigma**2))
    under = bool(pixel_scale_arcsec > fwhm_arcsec)
    return Aperture(
        n_pix=float(max(n_pix, 4.0 if under else 1.0)),
        encircled_energy=float(ee),
        undersampled=under,
    )


def source_e_per_s(
    magnitude: NDArray[np.float64] | float,
    airmass: NDArray[np.float64] | float,
    optics: Optics,
    camera: Camera,
    encircled_energy: float,
    extinction_k: float = 0.20,
    thin_cloud_mag: float = 0.0,
) -> NDArray[np.float64]:
    """Electrons per second from the source, inside the photometric aperture."""
    mag = np.asarray(magnitude, dtype=np.float64)
    x = np.asarray(airmass, dtype=np.float64)
    x = np.where(np.isfinite(x), x, np.inf)
    eff_mag = mag + extinction_k * x + thin_cloud_mag
    rate = (
        V_BAND_PHOTON_ZP
        * 10.0 ** (-0.4 * eff_mag)
        * optics.collecting_area_cm2
        * optics.throughput
        * camera.quantum_efficiency
        * encircled_energy
    )
    return np.asarray(np.where(np.isfinite(rate), rate, 0.0), dtype=np.float64)


def sky_e_per_s_per_px(
    sky_mag_arcsec2: NDArray[np.float64] | float,
    optics: Optics,
    camera: Camera,
) -> NDArray[np.float64]:
    """Sky electrons per pixel per second.

    NOTE the absence of an extinction term -- see the module docstring.
    """
    mu = np.asarray(sky_mag_arcsec2, dtype=np.float64)
    pix = camera.pixel_scale_arcsec(optics)
    return np.asarray(
        V_BAND_PHOTON_ZP
        * 10.0 ** (-0.4 * mu)
        * optics.collecting_area_cm2
        * optics.throughput
        * camera.quantum_efficiency
        * pix**2,
        dtype=np.float64,
    )


def ccd_snr(
    source_rate: NDArray[np.float64] | float,
    sky_rate_px: NDArray[np.float64] | float,
    dark_rate_px: float,
    read_noise_e: float,
    n_pix: float,
    exposure_s: NDArray[np.float64] | float,
    n_sub: int = 1,
) -> NDArray[np.float64]:
    """Howell, *Handbook of CCD Astronomy* eq. 4.9, for a stack of ``n_sub`` subs.

        SNR = S t / sqrt( S t + n_pix (B t + D t + R^2) )

    Read noise enters once PER SUB, which is the whole reason sub-exposure
    length matters.
    """
    s = np.asarray(source_rate, dtype=np.float64)
    b = np.asarray(sky_rate_px, dtype=np.float64)
    t = np.asarray(exposure_s, dtype=np.float64)

    total = t * n_sub
    signal = s * total
    noise_var = signal + n_pix * ((b + dark_rate_px) * total + n_sub * read_noise_e**2)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(noise_var > 0.0, signal / np.sqrt(noise_var), 0.0)
    return np.asarray(out, dtype=np.float64)


def snr2_rate(
    source_rate: NDArray[np.float64] | float,
    sky_rate_px: NDArray[np.float64] | float,
    dark_rate_px: float,
    read_noise_e: float,
    n_pix: float,
    t_sub_s: float,
) -> NDArray[np.float64]:
    """**The key quantity in the whole scheduler**: SNR-squared accumulated per
    second of open shutter.

        rho = S^2 / ( S + n_pix (B + D) + n_pix R^2 / t_sub )

    SNR^2 is exactly additive across independent sub-exposures, so integrating
    rho over the chosen slots is an EXACT statement of progress, not an
    approximation. That additivity is what lets the scheduler's completion
    constraint be linear.

    The n_pix R^2 / t_sub term is read noise expressed as an effective extra
    background rate -- which is also where the optimal sub-exposure rule comes
    from, so the two fall out of one expression rather than being bolted together.
    """
    s = np.asarray(source_rate, dtype=np.float64)
    b = np.asarray(sky_rate_px, dtype=np.float64)
    denom = s + n_pix * (b + dark_rate_px) + n_pix * read_noise_e**2 / max(t_sub_s, 1e-9)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where((denom > 0.0) & (s > 0.0), s * s / denom, 0.0)
    return np.asarray(out, dtype=np.float64)


def required_exposure_s(
    source_rate: NDArray[np.float64] | float,
    sky_rate_px: NDArray[np.float64] | float,
    dark_rate_px: float,
    read_noise_e: float,
    n_pix: float,
    snr_goal: float,
    t_sub_s: float,
) -> NDArray[np.float64]:
    """Total open-shutter seconds to reach ``snr_goal``. Closed form.

    Simply ``snr_goal^2 / rho``. Returns ``inf`` where the source rate is zero
    (below the horizon, vetoed) rather than raising, so the whole grid stays
    branch-free and ``np.isfinite`` means "reachable".
    """
    rho = snr2_rate(source_rate, sky_rate_px, dark_rate_px, read_noise_e, n_pix, t_sub_s)
    with np.errstate(divide="ignore"):
        out = np.where(rho > 0.0, snr_goal**2 / rho, np.inf)
    return np.asarray(out, dtype=np.float64)


def limiting_magnitude(
    sky_rate_px: NDArray[np.float64] | float,
    dark_rate_px: float,
    read_noise_e: float,
    n_pix: float,
    exposure_s: float,
    optics: Optics,
    camera: Camera,
    encircled_energy: float,
    airmass: NDArray[np.float64] | float = 1.0,
    snr_threshold: float = 5.0,
    extinction_k: float = 0.20,
) -> NDArray[np.float64]:
    """Faintest magnitude reaching ``snr_threshold`` in ``exposure_s``.

    **Meaningless without a stated exposure time** -- quote a long enough
    exposure and anything is detectable. The signature forces the caller to say.

    Inverts SNR^2 (S t + N_bg) = S^2 t^2, a quadratic in S.
    """
    b = np.asarray(sky_rate_px, dtype=np.float64)
    x = np.asarray(airmass, dtype=np.float64)
    t = float(exposure_s)

    n_bg = n_pix * ((b + dark_rate_px) * t + read_noise_e**2)
    k2 = snr_threshold**2
    s = (k2 / (2.0 * t)) * (1.0 + np.sqrt(1.0 + 4.0 * n_bg / k2))

    per_mag0 = (
        V_BAND_PHOTON_ZP
        * optics.collecting_area_cm2
        * optics.throughput
        * camera.quantum_efficiency
        * encircled_energy
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        mag = -2.5 * np.log10(s / per_mag0) - extinction_k * x
    return np.asarray(mag, dtype=np.float64)
