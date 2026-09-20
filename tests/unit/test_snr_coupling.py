"""The coupling test: does integrating eta actually equal the real SNR?

This is the most important test in the physics suite. The scheduler's completion
constraint is a plain linear sum:

    sum over chosen slots of ( slot_seconds * eta[t,s] )  >=  E_t

That is only legitimate if SNR^2 is exactly additive across sub-exposures, AND
if eta counts the time a slot really spends with the shutter open. These tests
check both end to end: the optimizer's side is computed exactly as cpsat does
(``slot_seconds * eta``, eta as ``pipeline.conditions`` hands it over, download
dead time taken out), and the ground truth runs the FULL CCD equation over the
frames the block would actually list (``cpsat.block_frames``), quadrature-summed.

If this passes, the linear constraint is physics. If it fails, the scheduler is
solving the wrong problem -- and notably it is exactly the test that fails under
the original spec's conflated single-q design, where the objective weight and
the exposure scaling are the same number. It also fails if eta credits every
wall-clock second as open shutter, or if a block lists frames per slot.

Brightnesses are catalogue-like surface brightnesses (22.5 mag/arcsec^2). At
the 14 used earlier a single slot beat the goal ~2000-fold, which made the
completion test pass whatever the time base was.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from numpy.typing import NDArray

from tscheduler.core.clock import AsOf
from tscheduler.core.timegrid import TimeGrid
from tscheduler.domain.equipment import Camera, Mount, Optics
from tscheduler.domain.site import Site
from tscheduler.domain.targets import Target
from tscheduler.physics.detector import (
    ccd_snr,
    sky_e_per_s_per_px,
    source_e_per_s,
)
from tscheduler.physics.quality import efficiency, resolution_element
from tscheduler.pipeline.builder import SessionSpec, build_geometry, scheduler_input_from
from tscheduler.pipeline.conditions import build_conditions, download_duty_cycle
from tscheduler.providers.weather.fixture import FixtureForecast
from tscheduler.scheduling.cpsat import SchedulerInput, block_expected_snr, block_frames

OPTICS = Optics(aperture_mm=203.0, focal_length_mm=2032.0, central_obstruction_mm=68.0)
CAMERA = Camera(
    pixel_size_um=3.76,
    sensor_width_px=6248,
    sensor_height_px=4176,
    quantum_efficiency=0.75,
    read_noise_e=1.5,
    dark_current_e_per_s=0.002,
)
"""Downloads in the Camera default 2 s, so a 92 s frame period that does not
divide the 300 s slot."""
TILING = dataclasses.replace(CAMERA, readout_s=10.0)
"""A 100 s frame period: exactly three frames per slot, so the per-slot ground
truth is exact and agreement can be demanded to machine precision."""
T_SUB = 90.0
SLOT_S = 300.0
SB = 22.5
GOAL = 20.0
START = datetime(2026, 9, 18, 2, 0, tzinfo=UTC)


def _one_block(eta: NDArray[np.float64], camera: Camera, need: float) -> SchedulerInput:
    """One target integrating through every slot of ``eta``, as cpsat sees it."""
    n = len(eta)
    return SchedulerInput(
        grid=TimeGrid(START, n, int(SLOT_S // 60)),
        target_ids=("t",),
        eta=np.asarray(eta, float)[None, :],
        preference=np.ones((1, n)),
        visible=np.ones((1, n), dtype=bool),
        required_ref_seconds=np.array([need]),
        weight=np.ones(1),
        subs_per_slot=np.zeros((1, n), dtype=np.int64),
        t_sub_s=np.array([T_SUB]),
        snr_goal=np.array([GOAL]),
        readout_s=np.array([camera.readout_s]),
    )


def _frames_snr(airmass: float, sky: float, camera: Camera, n_frames: int) -> float:
    """The FULL CCD equation over ``n_frames`` frames of T_SUB, for a surface of
    SB mag/arcsec^2 measured over the resolution element."""
    el = resolution_element(OPTICS, camera)
    s = float(source_e_per_s(SB, airmass, OPTICS, camera, 1.0)) * el.solid_angle_arcsec2
    b = float(sky_e_per_s_per_px(sky, OPTICS, camera))
    return float(
        ccd_snr(s, b, camera.dark_current_e_per_s, camera.read_noise_e, el.n_pix, T_SUB, n_frames)
    )


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

    Exact needs frames that tile the slots, hence the 100 s frame period. The
    optimizer's side credits the whole slot at the scheduling eta; the truth
    counts only the listed frames' open shutter. Crediting every wall-clock
    second as open shutter is 11% out here.
    """
    airmass, sky, seeing, cloud = _varied_night()

    eta_open, rho_ref = efficiency(
        target_magnitude=SB,
        airmass=airmass,
        sky_mag_arcsec2=sky,
        seeing_fwhm_arcsec=seeing,
        cloud_fraction=cloud,
        optics=OPTICS,
        camera=TILING,
        t_sub_s=T_SUB,
    )
    eta = eta_open * download_duty_cycle(T_SUB, TILING.readout_s)

    # What the optimizer believes: cpsat credits slot_seconds * eta per slot.
    optimizer_snr2 = float(np.sum(eta * SLOT_S)) * rho_ref

    # Ground truth over the frames the block lists: one block through every
    # slot, its frames three to a slot, each slot's frames under that slot's sky.
    n = len(airmass)
    inp = _one_block(eta, TILING, need=GOAL**2 / rho_ref)
    n_subs = block_frames(inp, 0, n)
    assert n_subs == 3 * n
    truth_snr2 = sum(_frames_snr(airmass[i], sky[i], TILING, 3) ** 2 for i in range(n))

    rel = abs(optimizer_snr2 - truth_snr2) / truth_snr2
    assert rel < 1e-10, f"eta integral disagrees with real SNR^2 by {rel:.3%}"
    # ...and the block card quotes the SNR of exactly those frames.
    card = block_expected_snr(inp, 0, range(n))
    assert card**2 == pytest.approx(truth_snr2, rel=1e-10)


def test_eta_integral_matches_through_the_real_pipeline() -> None:
    """The same identity with eta straight out of ``build_conditions``: real
    geometry, the real sky model, M31 as a 22.5 mag/arcsec^2 surface. This is
    the guard that the pipeline itself takes the download out of eta."""
    spec = SessionSpec(
        site=Site(latitude_deg=40.1106, longitude_deg=-88.2073, elevation_m=222.0),
        grid=TimeGrid.from_window(START, START + timedelta(hours=6)),
        optics=OPTICS,
        camera=TILING,
        mount=Mount(switch_minutes=5.0, min_block_minutes=20.0),
        targets=(Target("m31", "M31", 10.6847, 41.2690, SB),),
        snr_goal=GOAL,
        t_sub_s=T_SUB,
    )
    forecast = FixtureForecast.from_runs(
        [(START - timedelta(hours=6), [(START + timedelta(hours=h), 0.0) for h in range(8)])]
    )
    geo = build_geometry(spec)
    cond = build_conditions(spec, geo, forecast, AsOf.at(START))
    inp = scheduler_input_from(spec, geo, cond)
    assert inp.switch_remainder == 0.0

    data = np.flatnonzero(cond.eta[0] > 0)
    assert data.size >= 24 and np.all(np.diff(data) == 1), "want one long contiguous window"
    assert block_frames(inp, 0, data.size) == 3 * data.size

    optimizer_snr2 = float(np.sum(cond.eta[0, data] * SLOT_S)) * float(cond.rho_reference[0])
    truth_snr2 = sum(
        _frames_snr(float(geo.airmass[0, s]), float(cond.sky_mag_arcsec2[0, s]), TILING, 3) ** 2
        for s in data
    )
    rel = abs(optimizer_snr2 - truth_snr2) / truth_snr2
    assert rel < 1e-10, f"pipeline eta disagrees with real SNR^2 by {rel:.3%}"
    assert block_expected_snr(inp, 0, data) ** 2 == pytest.approx(truth_snr2, rel=1e-10)


def test_completion_target_is_reached_when_eta_says_so() -> None:
    """End-to-end statement of the constraint: accumulate exactly E_t
    reference-seconds of eta and the real stacked SNR of the frames the block
    lists must hit the goal -- and be the SNR the block card prints.

    The Camera-default 2 s download gives a 92 s frame period, which does not
    divide the slot: the block lists the whole frames that fit, and the
    fraction of a frame left over at the end is the only slack.
    """
    n = 400
    airmass = np.full(n, 1.3)
    sky = np.full(n, 20.5)

    eta_open, rho_ref = efficiency(
        target_magnitude=SB,
        airmass=airmass,
        sky_mag_arcsec2=sky,
        seeing_fwhm_arcsec=np.full(n, 2.5),
        cloud_fraction=np.zeros(n),
        optics=OPTICS,
        camera=CAMERA,
        t_sub_s=T_SUB,
    )
    eta = eta_open * download_duty_cycle(T_SUB, CAMERA.readout_s)
    e_required = GOAL**2 / rho_ref  # reference-seconds

    # Take slots until the eta integral clears the requirement, exactly as the
    # optimizer credits them.
    cum = np.cumsum(eta * SLOT_S)
    k = int(np.searchsorted(cum, e_required) + 1)
    assert k <= n, "test scenario should be completable"

    inp = _one_block(eta[:k], CAMERA, need=e_required)
    n_subs = block_frames(inp, 0, k)
    achieved = _frames_snr(1.3, 20.5, CAMERA, n_subs)
    assert achieved >= GOAL * 0.98, f"achieved SNR {achieved:.2f} short of goal {GOAL}"
    card = block_expected_snr(inp, 0, range(k))
    assert card == pytest.approx(achieved, rel=1e-10), "the card must quote its frames' SNR"


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


def test_a_surface_is_not_a_star() -> None:
    """The regression this model exists to prevent.

    A surface brightness is light PER square arcsecond. Treating it as a star's
    magnitude counts one square arcsecond of it and throws the rest of the
    resolution element away. Pin the ratio: a surface of mu mag/arcsec^2 over
    the element must deliver exactly as much signal as a star of
    mu - 2.5 log10(omega) whose light all lands inside it.
    """
    patch = resolution_element(OPTICS, CAMERA)
    omega = patch.solid_angle_arcsec2
    assert 5.0 < omega < 20.0, "a C8 resolution element is ~10 arcsec^2"

    mu = 22.0
    _, rho_surface = efficiency(
        target_magnitude=mu,
        airmass=np.full(1, 1.0),
        sky_mag_arcsec2=np.full(1, 21.5),
        seeing_fwhm_arcsec=np.full(1, 2.5),
        cloud_fraction=np.zeros(1),
        optics=OPTICS,
        camera=CAMERA,
        t_sub_s=T_SUB,
    )
    star = float(source_e_per_s(mu - 2.5 * np.log10(omega), 1.0, OPTICS, CAMERA, 1.0))
    b = float(sky_e_per_s_per_px(21.5, OPTICS, CAMERA))
    rho_star = star**2 / (
        star
        + patch.n_pix * (b + CAMERA.dark_current_e_per_s)
        + patch.n_pix * CAMERA.read_noise_e**2 / T_SUB
    )
    assert rho_surface == pytest.approx(rho_star, rel=1e-12)


def test_seeing_does_not_change_the_signal_of_a_surface() -> None:
    """Blurring a uniform surface leaves its brightness per arcsec^2 alone, so
    eta must not move with seeing. Seeing is a preference, not a yield."""
    n = 4
    eta, _ = efficiency(
        target_magnitude=21.0,
        airmass=np.full(n, 1.2),
        sky_mag_arcsec2=np.full(n, 20.8),
        seeing_fwhm_arcsec=np.array([1.2, 2.5, 4.0, 6.0]),
        cloud_fraction=np.zeros(n),
        optics=OPTICS,
        camera=CAMERA,
        t_sub_s=T_SUB,
    )
    assert np.ptp(eta) == pytest.approx(0.0, abs=1e-12)
