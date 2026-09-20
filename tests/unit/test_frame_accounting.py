"""Frames, progress and expected SNR on one time base.

eta is progress per wall-clock second of back-to-back frames -- the camera's
download already taken out -- and a block lists the whole frames that fit in
it, counted over the WHOLE block. These pin that the optimizer, the block card
("N x t_sub, expected SNR") and the progress bar all count the same frames.

The failures they guard against, from review:
  * frames counted per slot, floor(slot / period) each, so a 20-minute block
    of 90 s subs listed 12 frames where 13 fit;
  * a floor of one frame per slot, so 300 s subs in 5-minute slots listed a
    frame in every slot, and the >= 9 frame rule passed on 3 real frames;
  * eta crediting every wall-clock second as open shutter, so the download
    time changed the frame count and never the SNR.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from tscheduler.core.clock import AsOf
from tscheduler.core.timegrid import TimeGrid
from tscheduler.domain.equipment import Camera, Mount, Optics
from tscheduler.domain.plan import SlotKind
from tscheduler.domain.site import Site
from tscheduler.domain.targets import Target
from tscheduler.pipeline.builder import (
    SessionSpec,
    build_geometry,
    min_block_slots,
    scheduler_input_from,
)
from tscheduler.pipeline.conditions import build_conditions, download_duty_cycle
from tscheduler.providers.weather.fixture import FixtureForecast
from tscheduler.scheduling.cpsat import (
    SchedulerInput,
    SolveOptions,
    block_expected_snr,
    block_frames,
    block_ref_seconds,
    build_and_solve,
    frame_period_s,
    listed_ref_seconds,
    min_data_slots,
)

START = datetime(2026, 9, 12, 23, 0, tzinfo=UTC)
AS_OF = AsOf.at(START)
FAST = SolveOptions(max_seconds=10.0, workers=4)


def make_input(
    n_targets: int = 1,
    n_slots: int = 40,
    *,
    eta: float | np.ndarray = 1.0,
    t_sub: float = 90.0,
    readout: float = 0.0,
    slot_minutes: int = 5,
    need: float | np.ndarray = 1500.0,
    visible: np.ndarray | None = None,
    **kw,
) -> SchedulerInput:
    grid = TimeGrid(START, n_slots, slot_minutes)
    eta_arr = (
        np.full((n_targets, n_slots), eta, dtype=float)
        if np.isscalar(eta)
        else np.asarray(eta, float)
    )
    slot_s = slot_minutes * 60.0
    # What conditions.py used to hand over, floor of one and all. The model no
    # longer reads it, which is part of what these tests pin.
    per_slot = max(int(slot_s // (t_sub + readout)), 1)
    kw.setdefault("switch_slots", 1)
    kw.setdefault("min_block_slots", 4)
    return SchedulerInput(
        grid=grid,
        target_ids=tuple(f"t{i}" for i in range(n_targets)),
        eta=eta_arr,
        preference=np.ones((n_targets, n_slots)),
        visible=np.ones((n_targets, n_slots), dtype=bool) if visible is None else visible,
        required_ref_seconds=(
            np.full(n_targets, need, dtype=float) if np.isscalar(need) else np.asarray(need)
        ),
        weight=np.ones(n_targets),
        subs_per_slot=np.full((n_targets, n_slots), per_slot, dtype=np.int64),
        t_sub_s=np.full(n_targets, t_sub),
        snr_goal=np.full(n_targets, 20.0),
        readout_s=np.full(n_targets, readout),
        **kw,
    )


def data_slots(blk) -> range:
    return range(blk.slot_start + blk.switch_slots, blk.slot_end)


# --- counting -----------------------------------------------------------------
def test_frames_are_counted_over_the_block_not_per_slot() -> None:
    """Frames run back to back across slot boundaries."""
    inp = make_input(t_sub=90.0, readout=0.164)
    assert block_frames(inp, 0, 4) == 13, "20 min / 90.164 s is 13.3 frames; per slot says 12"
    assert block_frames(inp, 0, 12) == 39


def test_a_frame_that_would_run_past_the_block_is_not_listed() -> None:
    """300 s subs plus a 0.164 s download do not fit a 300 s slot. Ten slots
    hold nine frames; one slot holds none. The old floor of one frame per slot
    claimed ten and one."""
    inp = make_input(t_sub=300.0, readout=0.164)
    assert block_frames(inp, 0, 10) == 9
    assert block_frames(inp, 0, 1) == 0


def test_an_exact_fit_is_not_lost_to_rounding() -> None:
    inp = make_input(t_sub=60.0, readout=15.0)
    assert block_frames(inp, 0, 4) == 16  # 1200 s / 75 s exactly


def test_the_slew_remainder_comes_out_of_the_frames() -> None:
    """A 5-minute slew on 10-minute slots eats half the first data slot -- the
    same half the accumulation constraint debits."""
    inp = make_input(t_sub=300.0, slot_minutes=10, switch_slots=0, switch_remainder=0.5)
    assert block_frames(inp, 0, 6) == 11  # (6 - 0.5) * 600 / 300


# --- the >= 9 frame rule, per block -------------------------------------------
def test_long_subs_lengthen_the_minimum_block() -> None:
    inp = make_input(n_targets=2, n_slots=60, t_sub=300.0, readout=0.164, need=100.0)
    assert inp.min_block_slots == 4
    assert min_data_slots(inp, 0) == 10, "nine 300.164 s frames need ten 5-minute slots"

    plan = build_and_solve(inp, AS_OF, FAST)
    assert plan.status in {"OPTIMAL", "FEASIBLE"}
    assert plan.blocks
    for blk in plan.blocks:
        n_data = blk.n_slots - blk.switch_slots
        assert n_data >= 10
        assert blk.n_subs >= 9, f"{blk.target_id}: {blk.n_subs} frames"
        assert blk.n_subs * frame_period_s(inp, 0) <= n_data * inp.grid.slot_seconds


def test_a_target_whose_windows_are_all_too_short_says_why() -> None:
    """900 s subs need 27 five-minute slots for nine frames. A target up for
    only 20 is not "outranked" -- it has no window -- and the reason says so."""
    vis = np.zeros((1, 60), dtype=bool)
    vis[0, 10:30] = True
    plan = build_and_solve(
        make_input(n_slots=60, t_sub=900.0, need=100.0, visible=vis), AS_OF, FAST
    )
    assert plan.included == frozenset()
    (reason,) = plan.dropped
    assert reason.code == "no_window"
    assert "9 frames of 900 s" in reason.message
    assert "need 135 min of integrating" in reason.message


def test_a_night_shorter_than_nine_frames_quotes_the_real_figure() -> None:
    """A one-hour night cannot hold nine 900 s frames. The reason must still
    say they need 135 minutes, not wherever the count stopped at dawn."""
    plan = build_and_solve(make_input(n_slots=12, t_sub=900.0, need=100.0), AS_OF, FAST)
    (reason,) = plan.dropped
    assert reason.code == "no_window"
    assert "need 135 min of integrating" in reason.message
    assert "no usable 140-minute window" in reason.message


def test_history_frames_that_never_fit_do_not_count() -> None:
    """400 s subs in 5-minute slots: nine past data slots hold six whole frames.
    The old per-slot count (floor of one) called that nine and marked the
    target complete on history alone."""
    vis = np.ones((1, 40), dtype=bool)
    vis[0, 10:] = False
    plan = build_and_solve(
        make_input(
            n_slots=40,
            t_sub=400.0,
            need=1.0,
            visible=vis,
            locked=dict.fromkeys(range(10), "t0"),
            locked_observing=frozenset(range(1, 10)),
            first_free_slot=10,
        ),
        AS_OF,
        FAST,
    )
    assert plan.status in {"OPTIMAL", "FEASIBLE"}
    (blk,) = plan.blocks
    assert blk.n_subs == 6
    assert "t0" not in plan.included


def test_the_block_running_at_the_boundary_is_counted_as_one_block() -> None:
    """Two past data slots hold six 100 s frames; continuing one more slot
    makes nine. That continuation is the only way to reach nine -- the target
    is up for one slot, too short to start a block -- and taking it must
    complete the target."""
    vis = np.zeros((1, 40), dtype=bool)
    vis[0, :4] = True
    plan = build_and_solve(
        make_input(
            n_slots=40,
            t_sub=100.0,
            need=1.0,
            visible=vis,
            locked={0: "t0", 1: "t0", 2: "t0"},
            locked_observing=frozenset({1, 2}),
            first_free_slot=3,
        ),
        AS_OF,
        FAST,
    )
    assert "t0" in plan.included
    assert plan.assignments[3].target_id == "t0"
    assert plan.assignments[3].kind is SlotKind.OBSERVE
    (blk,) = plan.blocks
    assert blk.n_subs == 9


# --- expected SNR and progress --------------------------------------------------
def test_the_expected_snr_is_that_of_the_listed_frames() -> None:
    """At steady conditions a block delivers exactly n_subs * t_sub seconds of
    open shutter at the open-shutter efficiency -- not its wall-clock length."""
    eta_open, t_sub, readout = 0.8, 300.0, 0.164
    eta = eta_open * download_duty_cycle(t_sub, readout)
    inp = make_input(eta=eta, t_sub=t_sub, readout=readout)
    n = block_frames(inp, 0, 10)
    assert n == 9
    assert block_ref_seconds(inp, 0, range(10)) == pytest.approx(n * t_sub * eta_open, rel=1e-12)
    want = 20.0 * np.sqrt(n * t_sub * eta_open / 1500.0)
    assert block_expected_snr(inp, 0, range(10)) == pytest.approx(want, rel=1e-12)


def test_progress_is_the_blocks_added_in_quadrature() -> None:
    eta = np.linspace(0.6, 1.1, 60)[None, :].repeat(2, axis=0)
    inp = make_input(n_targets=2, n_slots=60, eta=eta, t_sub=90.0, readout=0.164)
    plan = build_and_solve(inp, AS_OF, FAST)
    assert plan.blocks
    listed = listed_ref_seconds(inp, plan.assignments)
    for t, tid in enumerate(inp.target_ids):
        from_blocks = sum(
            block_ref_seconds(inp, t, data_slots(b)) for b in plan.blocks if b.target_id == tid
        )
        assert listed[t] == pytest.approx(from_blocks, rel=1e-12)
        snr2 = sum(b.expected_snr**2 for b in plan.blocks if b.target_id == tid)
        assert snr2 == pytest.approx(20.0**2 * listed[t] / 1500.0, rel=1e-9)


# --- the builder ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("minutes", "slot", "want"),
    [(20, 5, 4), (20, 15, 2), (15, 2, 8), (20, 3, 7), (1, 30, 1), (30, 10, 3)],
)
def test_the_minimum_block_rounds_up_to_whole_slots(minutes: float, slot: int, want: int) -> None:
    """It is the shortest visit the observer asked for; it may not come out shorter."""
    assert min_block_slots(minutes, slot) == want


# --- end to end, through the real pipeline ---------------------------------------
LAT, LON, ELEV = 40.1106, -88.2073, 222.0
NIGHT = datetime(2026, 9, 18, 2, 0, tzinfo=UTC)
OPTICS = Optics(aperture_mm=203.0, focal_length_mm=2032.0, central_obstruction_mm=68.0)


def _spec(readout_s: float, t_sub_s: float) -> SessionSpec:
    return SessionSpec(
        site=Site(latitude_deg=LAT, longitude_deg=LON, elevation_m=ELEV),
        grid=TimeGrid.from_window(NIGHT, NIGHT + timedelta(hours=6)),
        optics=OPTICS,
        camera=Camera(
            pixel_size_um=3.76, sensor_width_px=6248, sensor_height_px=4176, readout_s=readout_s
        ),
        mount=Mount(switch_minutes=5.0, min_block_minutes=20.0),
        targets=(
            Target("m31", "M31", 10.6847, 41.2690, 22.5),
            Target("ngc7000", "NGC 7000", 314.75, 44.33, 23.0),
        ),
        t_sub_s=t_sub_s,
    )


@pytest.fixture(scope="module")
def night():
    spec = _spec(0.0, 300.0)
    forecast = FixtureForecast.from_runs(
        [(NIGHT - timedelta(hours=6), [(NIGHT + timedelta(hours=h), 0.0) for h in range(8)])]
    )
    return spec, build_geometry(spec), forecast


def test_the_pipeline_takes_the_download_out_of_eta(night) -> None:
    """Same night, same rig, only the download differs: eta scales by exactly
    the duty cycle, and E_t (open-shutter reference-seconds) does not move."""
    spec0, geo, forecast = night
    spec30 = _spec(30.0, spec0.t_sub_s)
    c0 = build_conditions(spec0, geo, forecast, AsOf.at(NIGHT))
    c30 = build_conditions(spec30, geo, forecast, AsOf.at(NIGHT))
    up = c0.eta > 0
    assert up.any()
    assert np.allclose(c30.eta[up] / c0.eta[up], 300.0 / 330.0, rtol=1e-12)
    assert np.array_equal(c30.rho_reference, c0.rho_reference)
    assert np.array_equal(c30.required_ref_seconds, c0.required_ref_seconds)


def test_every_planned_block_lists_only_frames_that_fit(night) -> None:
    _, geo, forecast = night
    spec = _spec(2.0, 300.0)
    cond = build_conditions(spec, geo, forecast, AsOf.at(NIGHT))
    inp = scheduler_input_from(spec, geo, cond)
    plan = build_and_solve(inp, AsOf.at(NIGHT), FAST, cond.ledger)
    assert plan.blocks, "expected something to be scheduled"
    slot_s = inp.grid.slot_seconds
    for blk in plan.blocks:
        t = inp.target_ids.index(blk.target_id)
        n_data = blk.n_slots - blk.switch_slots
        exposable = (n_data - inp.switch_remainder) * slot_s
        assert blk.n_subs * (spec.t_sub_s + spec.camera.readout_s) <= exposable
        assert blk.n_subs >= inp.min_subs
        assert blk.expected_snr == pytest.approx(
            block_expected_snr(inp, t, data_slots(blk)), rel=1e-12
        )
