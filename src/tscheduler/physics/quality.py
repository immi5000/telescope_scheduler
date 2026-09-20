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

import math
from dataclasses import dataclass
from typing import Final

import numpy as np
from numpy.typing import NDArray

from tscheduler.domain.equipment import Camera, Optics
from tscheduler.physics.cloud import TwoStateCloud
from tscheduler.physics.detector import (
    Aperture,
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


@dataclass(frozen=True, slots=True)
class ResolutionElement:
    """The patch of sky an extended target's SNR is quoted over.

    A target here is a SURFACE -- a galaxy disk, a nebula -- described by its
    surface brightness in mag/arcsec^2, and a surface has no total signal to
    speak of until you say over what area. The area used is the one a star
    occupies on this rig: the SNR-optimal photometric aperture for the
    reference star size (seeing and diffraction in quadrature). So "SNR 20"
    means SNR 20 in each star-sized patch of the object, the scale at which an
    image's detail is actually resolved.

    The star size is the one RECORDED, ``hypot(psf, pixel)``: a pixel blurs
    what it samples, so on an undersampled rig the patch grows smoothly with
    the pixel instead of jumping. (Reusing the point-source aperture's
    ``n_pix >= 4`` clamp here would be wrong twice over: that floor exists to
    keep a star's NOISE conservative, and in a surface's SIGNAL it inflates the
    patch -- up to ~3x the moment the pixel scale crosses the FWHM.)
    """

    n_pix: float
    solid_angle_arcsec2: float


def resolution_element(optics: Optics, camera: Camera) -> ResolutionElement:
    pix = camera.pixel_scale_arcsec(optics)
    recorded = math.hypot(optics.psf_fwhm_arcsec(REFERENCE_SEEING_ARCSEC), pix)
    # recorded > pix always, so photometric_aperture's undersampling clamp
    # never fires and n_pix >= pi * 0.67^2 ~ 1.4 without it.
    ap = photometric_aperture(recorded, pix)
    return ResolutionElement(n_pix=ap.n_pix, solid_angle_arcsec2=ap.n_pix * pix * pix)


def extended_snr2_rate(
    *,
    surface_brightness: NDArray[np.float64] | float,
    airmass: NDArray[np.float64] | float,
    sky_mag_arcsec2: NDArray[np.float64] | float,
    optics: Optics,
    camera: Camera,
    t_sub_s: float,
    extinction_k: float = 0.20,
    element: ResolutionElement | None = None,
) -> NDArray[np.float64]:
    """SNR^2 per second of open shutter, for a uniform extended source.

    The source term is surface brightness TIMES the element's solid angle. The
    earlier model passed a surface brightness to the point-source calculator
    as though it were a star's magnitude -- counting the light of ONE square
    arcsecond, concentrated into a star-sized aperture of ~10 -- and was only
    ever right because the hand-entered brightnesses were 3-4 mag too bright
    to compensate. With real catalogue values that error is a factor of ~25
    in exposure time.

    No encircled-energy factor: a uniform surface spills as much light into
    the aperture from outside as it loses to the PSF's wings. For the same
    reason seeing does not appear -- blurring a uniform surface leaves its
    brightness per arcsec^2 unchanged. Seeing still matters for DETAIL, which
    is what the preference term's seeing factor is for.
    """
    el = element or resolution_element(optics, camera)
    s = source_e_per_s(surface_brightness, airmass, optics, camera, 1.0, extinction_k)
    s = s * el.solid_angle_arcsec2
    b = sky_e_per_s_per_px(sky_mag_arcsec2, optics, camera)
    return snr2_rate(s, b, camera.dark_current_e_per_s, camera.read_noise_e, el.n_pix, t_sub_s)


def point_snr2_rate(
    *,
    magnitude: NDArray[np.float64] | float,
    airmass: NDArray[np.float64] | float,
    sky_mag_arcsec2: NDArray[np.float64] | float,
    optics: Optics,
    camera: Camera,
    t_sub_s: float,
    extinction_k: float = 0.20,
    element: ResolutionElement | None = None,
    aperture: Aperture | None = None,
) -> NDArray[np.float64]:
    """SNR^2 per second of open shutter, for a POINT source: a star.

    The counterpart of :func:`extended_snr2_rate`, and the difference between
    them is the whole reason ``Target`` has to say which kind of source it is.
    A star's V magnitude is its TOTAL light, so:

    * the encircled-energy fraction applies -- the aperture catches most of the
      PSF but not all of it, where a uniform surface loses nothing because it
      gains as much from outside the aperture as it spills out;
    * there is NO multiplication by the element's solid angle. That factor is
      what turns "light from one square arcsecond" into "light from the whole
      aperture", and a star's magnitude already counts all of it.

    Getting this backwards is the error ``extended_snr2_rate`` documents: a
    factor of about 25 in exposure time, in the direction of finishing early.
    Hence the two functions rather than one with a flag buried in it.

    Seeing DOES matter here, unlike the extended case: it sets the recorded
    PSF, which sets both the encircled energy and the pixel count the noise is
    summed over. It is taken at the reference value through
    :func:`resolution_element`, the same place the extended path takes it, so
    the two stay comparable.
    """
    el = element or resolution_element(optics, camera)
    ap = aperture or _reference_aperture(optics, camera)
    s = source_e_per_s(magnitude, airmass, optics, camera, ap.encircled_energy, extinction_k)
    b = sky_e_per_s_per_px(sky_mag_arcsec2, optics, camera)
    return snr2_rate(s, b, camera.dark_current_e_per_s, camera.read_noise_e, el.n_pix, t_sub_s)


def _reference_aperture(optics: Optics, camera: Camera) -> Aperture:
    """The photometric aperture :func:`resolution_element` is measured on.

    Factored out so the point-source path takes its encircled energy from the
    SAME aperture whose ``n_pix`` it uses for the noise. Reading the two from
    different apertures is silently wrong and impossible to see in the output.
    """
    pix = camera.pixel_scale_arcsec(optics)
    recorded = math.hypot(optics.psf_fwhm_arcsec(REFERENCE_SEEING_ARCSEC), pix)
    return photometric_aperture(recorded, pix)


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
    point_source: bool = False,
) -> tuple[NDArray[np.float64], float]:
    """Return ``(eta[S], rho_reference)``.

    ``target_magnitude`` is a V SURFACE brightness in mag/arcsec^2 for an
    extended object; see :func:`extended_snr2_rate` for how it becomes signal.
    ``seeing_fwhm_arcsec`` is accepted for the callers' sake and deliberately
    unused: it cannot change the signal per arcsec^2 of a uniform surface.

    WITH ``point_source=True`` it is instead a TOTAL V magnitude -- a star --
    and :func:`point_snr2_rate` prices it. The two readings of the same number
    differ by the element's solid angle, so the flag is not a refinement: get
    it wrong and the answer is off by a factor of ~25. It defaults to False so
    every catalogue object keeps the meaning it already had.

    ``rho_reference`` is the SNR^2 accumulation rate this target would enjoy at
    the named reference condition, so ``E_t = snr_goal^2 / rho_ref`` comes out in
    *reference-seconds* and can be quoted in the UI as "M51 needs 92
    reference-minutes".

    eta may legitimately EXCEED 1 when conditions beat the reference; it is a
    ratio, not a probability, and clamping it at 1 would quietly discard the
    advantage of a superb slot.
    """
    del seeing_fwhm_arcsec  # see the docstring: not an input to a surface's signal
    cloud = cloud_model or TwoStateCloud()
    element = resolution_element(optics, camera)

    def rate(
        airmass_: NDArray[np.float64] | float, sky_: NDArray[np.float64] | float
    ) -> NDArray[np.float64]:
        if point_source:
            return point_snr2_rate(
                magnitude=target_magnitude,
                airmass=airmass_,
                sky_mag_arcsec2=sky_,
                optics=optics,
                camera=camera,
                t_sub_s=t_sub_s,
                extinction_k=extinction_k,
                element=element,
            )
        return extended_snr2_rate(
            surface_brightness=target_magnitude,
            airmass=airmass_,
            sky_mag_arcsec2=sky_,
            optics=optics,
            camera=camera,
            t_sub_s=t_sub_s,
            extinction_k=extinction_k,
            element=element,
        )

    rho_ref = float(rate(REFERENCE_AIRMASS, reference_sky_mag_arcsec2))
    if rho_ref <= 0.0:
        return np.zeros_like(airmass), 0.0

    rho = rate(np.asarray(airmass, dtype=np.float64), np.asarray(sky_mag_arcsec2, dtype=np.float64))

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
