"""Efficiency and preference: the seam between physics and the optimizer.

**This module corrects a modelling error in the original project spec.** The
spec has a single score q[t,s] in [0,1] that both weights the objective AND
scales effective exposure. Those are different quantities and must be separate:

``efficiency[t,s]``  Physically derived, absolutely calibrated, dimensionless.
                     eta = 0.4 MEANS "one second here buys 0.4 seconds of
                     progress versus a reference slot". Drives the completion
                     constraint. Nothing but physics may touch it.

``preference[t,s]``  Soft, ordinal policy: risk, operational dislike, science
                     needs the CCD equation cannot see. Drives only the
                     objective. Never affects feasibility.

Why the split matters, in order of severity:

1. DOUBLE COUNTING. With one number, a mediocre slot earns less reward AND
   requires more slots -- an effective q^2 penalty that biases the schedule far
   harder than the physics warrants, and invisibly, because nobody ever wrote
   q^2 anywhere.
2. TUNING BECOMES IMPOSSIBLE. Nudging a moon-separation preference slider would
   silently change every target's exposure requirement and could flip
   feasibility, dropping targets for reasons with no physical meaning. With the
   split, E_t is untouchable by UI sliders.
3. UNITS. eta must be calibrated or targets silently finish under-exposed;
   nobody can defend pref = 0.7 as a number.
4. THEY ARE NOT MONOTONE TOGETHER. Forecast cloud f = 0.5 gives the SAME
   expected eta as a clear slot of half the length, but is far worse in
   preference terms because of variance. One number cannot say "same expected
   yield, much worse risk" -- which is exactly the distinction a scheduler
   exists to make.
5. EXPLAINABILITY. "Dropped because its only window averages eta = 0.22, so it
   needed 4.5 h of sky and had 2 h" is checkable. A physics/taste blend can only
   say "the score was low".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np
from numpy.typing import NDArray

from tscheduler.domain.equipment import Camera, Optics
from tscheduler.physics.cloud import TwoStateCloud
from tscheduler.physics.detector import (
    photometric_aperture,
    sky_e_per_s_per_px,
    snr2_rate,
    source_e_per_s,
)

#: Reference conditions defining one "reference second" of progress.
REFERENCE_AIRMASS: Final = 1.0
REFERENCE_SEEING_ARCSEC: Final = 2.5


@dataclass(frozen=True, slots=True)
class PreferenceWeights:
    """Exponents in a weighted GEOMETRIC mean.

    Geometric, not arithmetic, for two reasons: a zero in any factor vetoes the
    slot (the right semantics for "the Moon is 4 degrees away"), and the result
    is scale-free so the weights read as exponents.
    """

    altitude: float = 1.0
    moon: float = 1.0
    cloud: float = 1.5
    seeing: float = 0.5
    satellite: float = 0.5


def efficiency(
    *,
    target_magnitude: float,
    airmass: NDArray[np.float64],
    sky_mag_arcsec2: NDArray[np.float64],
    seeing_fwhm_arcsec: NDArray[np.float64],
    cloud_fraction: NDArray[np.float64],
    optics: Optics,
    camera: Camera,
    t_sub_s: float,
    extinction_k: float = 0.20,
    reference_sky_mag_arcsec2: float = 21.5,
    cloud_model: TwoStateCloud | None = None,
    eta_max: float = 3.0,
) -> tuple[NDArray[np.float64], float]:
    """Return ``(eta[S], rho_reference)``.

    ``rho_reference`` is the SNR^2 accumulation rate this target would enjoy at
    the named reference condition, so ``E_t = snr_goal^2 / rho_ref`` comes out in
    *reference-seconds* and can be quoted in the UI as "M51 needs 92
    reference-minutes".

    eta may legitimately EXCEED 1 when conditions beat the reference; it is a
    ratio, not a probability, and clamping it at 1 would quietly discard the
    advantage of a superb slot.
    """
    cloud = cloud_model or TwoStateCloud()
    pix = camera.pixel_scale_arcsec(optics)

    ap_ref = photometric_aperture(REFERENCE_SEEING_ARCSEC, pix)
    s_ref = source_e_per_s(
        target_magnitude, REFERENCE_AIRMASS, optics, camera, ap_ref.encircled_energy, extinction_k
    )
    b_ref = sky_e_per_s_per_px(reference_sky_mag_arcsec2, optics, camera)
    rho_ref = float(
        snr2_rate(
            s_ref, b_ref, camera.dark_current_e_per_s, camera.read_noise_e, ap_ref.n_pix, t_sub_s
        )
    )
    if rho_ref <= 0.0:
        return np.zeros_like(airmass), 0.0

    # Per-slot. n_pix varies with seeing, so it is computed per slot too.
    fwhm = np.asarray(seeing_fwhm_arcsec, dtype=np.float64)
    n_pix = np.maximum(np.pi * (0.67 * fwhm / pix) ** 2, 1.0)
    sigma = fwhm / 2.3548200450309493
    ee = 1.0 - np.exp(-((0.67 * fwhm) ** 2) / (2.0 * sigma**2))

    s = source_e_per_s(target_magnitude, airmass, optics, camera, 1.0, extinction_k) * ee
    b = sky_e_per_s_per_px(sky_mag_arcsec2, optics, camera)

    rho = np.asarray(
        [
            float(
                snr2_rate(
                    s[i],
                    b[i],
                    camera.dark_current_e_per_s,
                    camera.read_noise_e,
                    float(n_pix[i]),
                    t_sub_s,
                )
            )
            for i in range(len(s))
        ],
        dtype=np.float64,
    )

    # Cloud enters as an unbiased DUTY CYCLE, not as grey extinction.
    rho = rho * cloud.duty_cycle(cloud_fraction)

    eta = np.clip(rho / rho_ref, 0.0, eta_max)
    return np.asarray(eta, dtype=np.float64), rho_ref


def _ramp(x: NDArray[np.float64], lo: float, hi: float) -> NDArray[np.float64]:
    return np.clip((np.asarray(x, dtype=np.float64) - lo) / (hi - lo), 0.0, 1.0)


def preference(
    *,
    altitude_deg: NDArray[np.float64],
    moon_separation_deg: NDArray[np.float64],
    cloud_fraction: NDArray[np.float64],
    seeing_fwhm_arcsec: NDArray[np.float64],
    satellite_risk: NDArray[np.float64] | float = 0.0,
    min_altitude_deg: float = 30.0,
    min_moon_separation_deg: float = 15.0,
    weights: PreferenceWeights | None = None,
    cloud_model: TwoStateCloud | None = None,
) -> dict[str, NDArray[np.float64]]:
    """Return the named factors AND their product under key ``"total"``.

    The factors are returned separately on purpose: because the score is a
    PRODUCT, ``d log(pref)`` per factor is an exact attribution of why a slot
    changed, which is what lets the engine explain a re-plan without an LLM.
    Collapsing to a single number here would throw that away.
    """
    w = weights or PreferenceWeights()
    cloud = cloud_model or TwoStateCloud()

    factors = {
        "altitude": _ramp(altitude_deg, min_altitude_deg, 60.0) ** w.altitude,
        "moon": _ramp(moon_separation_deg, min_moon_separation_deg, 60.0) ** w.moon,
        "cloud": cloud.reliability(cloud_fraction) ** w.cloud,
        "seeing": (1.0 - _ramp(seeing_fwhm_arcsec, 2.0, 6.0)) ** w.seeing,
        "satellite": np.exp(-np.asarray(satellite_risk, dtype=np.float64)) ** w.satellite,
    }

    total = np.ones_like(np.asarray(altitude_deg, dtype=np.float64))
    for v in factors.values():
        total = total * v
    factors["total"] = np.clip(total, 0.0, 1.0)
    return factors
