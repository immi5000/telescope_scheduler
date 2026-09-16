"""The coupling test: does integrating eta actually equal the real SNR?

This is the most important test in the physics suite. The scheduler's completion
constraint is a plain linear sum:

    sum over chosen slots of ( slot_seconds * eta[t,s] )  >=  E_t

That is only legitimate if SNR^2 is exactly additive across sub-exposures. This
test checks it end to end: simulate slot-by-slot integration through the FULL
CCD equation under wildly varying conditions, quadrature-sum the per-slot SNR^2,
and compare against the eta integral the optimizer would use.

If this passes, the linear constraint is physics. If it fails, the scheduler is
solving the wrong problem -- and notably it is exactly the test that fails under
the original spec's conflated single-q design, where the objective weight and
the exposure scaling are the same number.
"""

from __future__ import annotations

import numpy as np
import pytest

from tscheduler.domain.equipment import Camera, Optics
from tscheduler.physics.detector import (
    ccd_snr,
    photometric_aperture,
    sky_e_per_s_per_px,
    source_e_per_s,
)
from tscheduler.physics.quality import efficiency

OPTICS = Optics(aperture_mm=203.0, focal_length_mm=2032.0, central_obstruction_mm=68.0)
CAMERA = Camera(
    pixel_size_um=3.76,
    sensor_width_px=6248,
    sensor_height_px=4176,
    quantum_efficiency=0.75,
    read_noise_e=1.5,
    dark_current_e_per_s=0.002,
)
T_SUB = 90.0
SLOT_S = 300.0


def _varied_night(n: int = 40, seed: int = 7):
    rng = np.random.default_rng(seed)
    airmass = np.sort(rng.uniform(1.0, 2.6, n))
    sky = rng.uniform(18.5, 21.6, n)  # moonrise, twilight, city glow
    seeing = rng.uniform(1.8, 4.5, n)
    cloud = np.zeros(n)  # cloud tested separately below
    return airmass, sky, seeing, cloud


def test_eta_integral_matches_quadrature_summed_snr() -> None:
    """Agreement is EXACT to machine precision, not merely close.

    SNR^2 additivity is an algebraic identity rather than an approximation, so
    integrating eta does not approximate the real accumulated SNR^2 -- it *is*
    it. The tolerance is therefore 1e-10, not 1%: a loose bound would let a real
    regression slip through while still looking reassuring.
    """
    airmass, sky, seeing, cloud = _varied_night()
    mag = 13.5

    eta, rho_ref = efficiency(
        target_magnitude=mag,
        airmass=airmass,
        sky_mag_arcsec2=sky,
        seeing_fwhm_arcsec=seeing,
        cloud_fraction=cloud,
        optics=OPTICS,
        camera=CAMERA,
        t_sub_s=T_SUB,
    )

    # Ground truth: run the real CCD equation slot by slot and add SNR^2.
    pix = CAMERA.pixel_scale_arcsec(OPTICS)
    snr2_total = 0.0
    for i in range(len(airmass)):
        ap = photometric_aperture(float(seeing[i]), pix)
        s = float(source_e_per_s(mag, airmass[i], OPTICS, CAMERA, ap.encircled_energy))
        b = float(sky_e_per_s_per_px(sky[i], OPTICS, CAMERA))
        n_sub = int(SLOT_S // T_SUB)
        snr = float(
            ccd_snr(s, b, CAMERA.dark_current_e_per_s, CAMERA.read_noise_e, ap.n_pix, T_SUB, n_sub)
        )
        snr2_total += snr**2

    # What the optimizer believes, in the same units.
    eta_integral_snr2 = float(np.sum(eta * (T_SUB * int(SLOT_S // T_SUB))) * rho_ref)

    rel = abs(eta_integral_snr2 - snr2_total) / snr2_total
    assert rel < 1e-10, f"eta integral disagrees with real SNR^2 by {rel:.3%}"


def test_completion_target_is_reached_when_eta_says_so() -> None:
    """End-to-end statement of the constraint: accumulate exactly E_t
    reference-seconds of eta and the real stacked SNR must hit the goal."""
    n = 60
    airmass = np.full(n, 1.3)
    sky = np.full(n, 20.5)
    seeing = np.full(n, 2.5)
    mag, goal = 14.0, 20.0

    eta, rho_ref = efficiency(
        target_magnitude=mag,
        airmass=airmass,
        sky_mag_arcsec2=sky,
        seeing_fwhm_arcsec=seeing,
        cloud_fraction=np.zeros(n),
        optics=OPTICS,
        camera=CAMERA,
        t_sub_s=T_SUB,
    )
    e_required = goal**2 / rho_ref  # reference-seconds

    # Take slots until the eta integral clears the requirement.
    per_slot = eta * SLOT_S
    cum = np.cumsum(per_slot)
    k = int(np.searchsorted(cum, e_required) + 1)
    assert k <= n, "test scenario should be completable"

    pix = CAMERA.pixel_scale_arcsec(OPTICS)
    ap = photometric_aperture(2.5, pix)
    s = float(source_e_per_s(mag, 1.3, OPTICS, CAMERA, ap.encircled_energy))
    b = float(sky_e_per_s_per_px(20.5, OPTICS, CAMERA))
    n_sub = k * int(SLOT_S // T_SUB)
    achieved = float(
        ccd_snr(s, b, CAMERA.dark_current_e_per_s, CAMERA.read_noise_e, ap.n_pix, T_SUB, n_sub)
    )
    assert achieved >= goal * 0.98, f"achieved SNR {achieved:.1f} short of goal {goal}"


def test_cloud_acts_as_a_duty_cycle_not_grey_extinction() -> None:
    """50% forecast cloud must halve the accumulation rate, NOT dim by 2.5*f mag.

    This is the distinction the spec gets wrong, and it changes the schedule: a
    50%-cloud slot does not mean "expose twice as long", it means "expect half
    your frames to be junk".
    """
    n = 10
    common = {
        "target_magnitude": 14.0,
        "airmass": np.full(n, 1.2),
        "sky_mag_arcsec2": np.full(n, 20.8),
        "seeing_fwhm_arcsec": np.full(n, 2.5),
        "optics": OPTICS,
        "camera": CAMERA,
        "t_sub_s": T_SUB,
    }
    clear, _ = efficiency(cloud_fraction=np.zeros(n), **common)
    half, _ = efficiency(cloud_fraction=np.full(n, 0.5), **common)
    assert float(half[0] / clear[0]) == pytest.approx(0.5, abs=1e-9)


def test_eta_is_absolutely_calibrated_at_the_reference_condition() -> None:
    """eta = 1 must mean exactly the reference slot, or E_t is meaningless."""
    n = 3
    eta, _ = efficiency(
        target_magnitude=14.0,
        airmass=np.full(n, 1.0),
        sky_mag_arcsec2=np.full(n, 21.5),
        seeing_fwhm_arcsec=np.full(n, 2.5),
        cloud_fraction=np.zeros(n),
        optics=OPTICS,
        camera=CAMERA,
        t_sub_s=T_SUB,
    )
    assert float(eta[0]) == pytest.approx(1.0, rel=0.02)


def test_eta_may_exceed_one_in_superb_conditions() -> None:
    """It is a ratio, not a probability. Clamping at 1 would silently discard
    the advantage of a better-than-reference slot."""
    n = 3
    eta, _ = efficiency(
        target_magnitude=14.0,
        airmass=np.full(n, 1.0),
        sky_mag_arcsec2=np.full(n, 22.0),
        seeing_fwhm_arcsec=np.full(n, 1.5),
        cloud_fraction=np.zeros(n),
        optics=OPTICS,
        camera=CAMERA,
        t_sub_s=T_SUB,
    )
    assert float(eta[0]) > 1.0


def test_eta_falls_monotonically_with_worsening_conditions() -> None:
    n = 5
    base = {
        "target_magnitude": 14.0,
        "seeing_fwhm_arcsec": np.full(n, 2.5),
        "cloud_fraction": np.zeros(n),
        "optics": OPTICS,
        "camera": CAMERA,
        "t_sub_s": T_SUB,
    }
    worse_airmass, _ = efficiency(
        airmass=np.array([1.0, 1.3, 1.6, 2.0, 2.5]), sky_mag_arcsec2=np.full(n, 21.0), **base
    )
    assert np.all(np.diff(worse_airmass) < 0)

    brighter_sky, _ = efficiency(
        airmass=np.full(n, 1.2), sky_mag_arcsec2=np.array([22.0, 21.0, 20.0, 19.0, 18.0]), **base
    )
    assert np.all(np.diff(brighter_sky) < 0)
