"""CP-SAT model: one test per constraint family, on controlled synthetic input.

Synthetic rather than realistic on purpose -- with hand-built eta and visibility
arrays the correct answer is known exactly, so a violated constraint is
unambiguous instead of arguable.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from tscheduler.core.clock import AsOf
from tscheduler.core.timegrid import TimeGrid
from tscheduler.domain.plan import SlotKind
from tscheduler.scheduling.cpsat import SchedulerInput, SolveOptions, build_and_solve

START = datetime(2026, 9, 12, 23, 0, tzinfo=UTC)
AS_OF = AsOf.at(START)
FAST = SolveOptions(max_seconds=10.0, workers=4)


def make_input(
    n_targets: int = 2,
    n_slots: int = 40,
    *,
    eta: float | np.ndarray = 1.0,
    visible: np.ndarray | None = None,
    need_ref_s: float | np.ndarray = 1500.0,
    switch_slots: int = 1,
    min_block_slots: int = 4,
    min_subs: int = 9,
    weight: np.ndarray | None = None,
    **kw,
) -> SchedulerInput:
    grid = TimeGrid(START, n_slots)
    ids = tuple(f"t{i}" for i in range(n_targets))
    eta_arr = (
        np.full((n_targets, n_slots), eta, dtype=float)
        if np.isscalar(eta)
        else np.asarray(eta, float)
    )
    vis = np.ones((n_targets, n_slots), dtype=bool) if visible is None else visible
    need = (
        np.full(n_targets, need_ref_s, dtype=float)
        if np.isscalar(need_ref_s)
        else np.asarray(need_ref_s, float)
    )
    return SchedulerInput(
        grid=grid,
        target_ids=ids,
        eta=eta_arr,
        preference=np.ones((n_targets, n_slots)),
        visible=vis,
        required_ref_seconds=need,
        weight=np.ones(n_targets) if weight is None else weight,
        subs_per_slot=np.full((n_targets, n_slots), 3, dtype=np.int64),
        t_sub_s=np.full(n_targets, 90.0),
        switch_slots=switch_slots,
        min_block_slots=min_block_slots,
        min_subs=min_subs,
        **kw,
    )


def observed(plan, tid: str) -> list[int]:
    return [a.slot for a in plan.assignments if a.target_id == tid and a.kind is SlotKind.OBSERVE]


def runs(slots: list[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for s in sorted(slots):
        if out and s == out[-1][-1] + 1:
            out[-1].append(s)
        else:
            out.append([s])
    return out


# --- C1 ---------------------------------------------------------------------
def test_at_most_one_target_per_slot() -> None:
    plan = build_and_solve(make_input(n_targets=4), AS_OF, FAST)
    seen: set[int] = set()
    for a in plan.assignments:
        if a.target_id is not None:
            assert a.slot not in seen
            seen.add(a.slot)


# --- C3 ---------------------------------------------------------------------
def test_never_schedules_an_invisible_slot() -> None:
    vis = np.ones((2, 40), dtype=bool)
    vis[0, :20] = False
    plan = build_and_solve(make_input(visible=vis), AS_OF, FAST)
    assert all(s >= 20 for s in observed(plan, "t0"))


# --- C5 ---------------------------------------------------------------------
def test_every_block_meets_the_minimum_length() -> None:
    """Nobody pointing by hand wants to re-aim every ten minutes."""
    plan = build_and_solve(make_input(n_targets=3, min_block_slots=4), AS_OF, FAST)
    for blk in plan.blocks:
        data = blk.n_slots - blk.switch_slots
        assert data >= 4, f"{blk.target_id} block has only {data} data slots"


def test_no_block_starts_too_late_to_finish() -> None:
    plan = build_and_solve(make_input(n_targets=2, n_slots=20, min_block_slots=4), AS_OF, FAST)
    for blk in plan.blocks:
        assert blk.slot_end <= 20


# --- C6: the switch reservation, at every width -----------------------------
@pytest.mark.parametrize("w", [0, 1, 2, 3])
def test_switch_reservation_holds_for_every_width(w: int) -> None:
    """The spec's formulation looks degenerate at switch_time = 1 slot. It is not
    -- that artefact comes from deriving block starts off x instead of z. Here
    the first w slots of every block collect no data, for w = 0, 1, 2 and 3."""
    plan = build_and_solve(
        make_input(n_targets=2, n_slots=48, switch_slots=w, min_block_slots=4), AS_OF, FAST
    )
    assert plan.blocks, "expected a non-empty plan"
    for blk in plan.blocks:
        assert blk.switch_slots == w, f"block {blk.target_id} reserved {blk.switch_slots}, want {w}"


def test_pointing_without_integrating_only_happens_during_the_slew() -> None:
    """Otherwise the solver parks on a target for free whenever the
    over-exposure cap bites -- an early run produced a 31-slot 'switch'."""
    plan = build_and_solve(make_input(n_targets=3, switch_slots=1), AS_OF, FAST)
    for blk in plan.blocks:
        assert blk.switch_slots <= 1


# --- C7: accumulation --------------------------------------------------------
def test_included_targets_accumulate_their_requirement() -> None:
    inp = make_input(n_targets=2, n_slots=40, eta=1.0, need_ref_s=1500.0)
    plan = build_and_solve(inp, AS_OF, FAST)
    for tid in plan.included:
        t = inp.target_ids.index(tid)
        acc = sum(inp.eta[t, s] * inp.grid.slot_seconds for s in observed(plan, tid))
        assert acc >= inp.required_ref_seconds[t] - 1e-6


def test_low_efficiency_slots_require_proportionally_more_of_them() -> None:
    """The whole point of eta: half the efficiency, twice the slots."""
    fast = build_and_solve(make_input(n_targets=1, eta=1.0, need_ref_s=1500.0), AS_OF, FAST)
    slow = build_and_solve(make_input(n_targets=1, eta=0.5, need_ref_s=1500.0), AS_OF, FAST)
    assert len(observed(slow, "t0")) == pytest.approx(2 * len(observed(fast, "t0")), abs=1)


def test_target_that_cannot_possibly_fit_is_dropped_not_half_observed() -> None:
    inp = make_input(n_targets=1, n_slots=20, eta=1.0, need_ref_s=10_000.0)
    plan = build_and_solve(inp, AS_OF, FAST)
    assert plan.included == frozenset()
    assert [d.target_id for d in plan.dropped] == ["t0"]
    assert not observed(plan, "t0"), "must not waste time part-observing an impossible target"


# --- C9 ----------------------------------------------------------------------
def test_minimum_frame_count_is_enforced() -> None:
    """>= 9 frames so sigma-clipped stacking can reject satellite trails and
    cosmic rays. Good practice independent of satellites, which is what makes
    the design robust to a wrong streak-rate model."""
    inp = make_input(n_targets=2, min_subs=9)
    plan = build_and_solve(inp, AS_OF, FAST)
    for tid in plan.included:
        total = sum(b.n_subs for b in plan.blocks if b.target_id == tid)
        assert total >= 9


# --- C10 ---------------------------------------------------------------------
def test_locked_slots_are_honoured_exactly() -> None:
    locked = {0: "t0", 1: "t0", 2: "t0", 3: "t0"}
    plan = build_and_solve(make_input(n_targets=2, locked=locked), AS_OF, FAST)
    for slot, tid in locked.items():
        assert plan.assignments[slot].target_id == tid
        assert plan.assignments[slot].locked


def test_continuing_the_current_target_across_a_replan_costs_no_switch() -> None:
    """Without this the scheduler abandons whatever you are observing on every
    single re-plan, which is the most annoying possible behaviour."""
    locked = dict.fromkeys(range(6), "t0")
    plan = build_and_solve(
        make_input(n_targets=2, n_slots=40, locked=locked, first_free_slot=6), AS_OF, FAST
    )
    first = next(b for b in plan.blocks if b.slot_start == 0)
    assert first.target_id == "t0"


# --- priority ----------------------------------------------------------------
def test_higher_priority_target_wins_a_contested_night() -> None:
    vis = np.ones((2, 20), dtype=bool)
    inp = make_input(
        n_targets=2, n_slots=20, visible=vis, need_ref_s=4500.0, weight=np.array([1.0, 10.0])
    )
    plan = build_and_solve(inp, AS_OF, FAST)
    assert "t1" in plan.included
    assert "t0" not in plan.included


def test_prefers_completing_more_targets_over_one_expensive_one() -> None:
    """Scaling the completion bonus by E_t is the intuitive choice and it is
    backwards: it makes expensive targets worth more, and the solver completes
    one 17-hour target instead of five short ones."""
    inp = make_input(
        n_targets=3, n_slots=60, eta=1.0, need_ref_s=np.array([1500.0, 1500.0, 9000.0])
    )
    plan = build_and_solve(inp, AS_OF, FAST)
    assert {"t0", "t1"} <= plan.included


# --- solver behaviour --------------------------------------------------------
def test_empty_plan_is_always_feasible() -> None:
    """u[t] is the escape hatch: the model can never be INFEASIBLE, so an
    infeasible status is a bug rather than a data condition."""
    vis = np.zeros((2, 20), dtype=bool)
    plan = build_and_solve(make_input(n_targets=2, n_slots=20, visible=vis), AS_OF, FAST)
    assert plan.status in ("OPTIMAL", "FEASIBLE")
    assert plan.included == frozenset()


def test_deterministic_mode_is_reproducible() -> None:
    """Required for the replay truncation-equivalence property test: CP-SAT with
    a wall-clock limit and workers > 1 is NOT reproducible."""
    # A SMALL deterministic budget is fine and is the point: deterministic time
    # is itself reproducible, so truncating at 2.0 gives the same answer twice
    # just as 20.0 would -- and this instance is highly symmetric, so proving
    # optimality would otherwise take ~13 s per solve.
    opts = SolveOptions(deterministic=True, max_deterministic_time=2.0)
    inp = make_input(n_targets=4, n_slots=48)
    a = build_and_solve(inp, AS_OF, opts)
    b = build_and_solve(inp, AS_OF, opts)
    assert a.fingerprint() == b.fingerprint()


def test_blocks_and_assignments_agree() -> None:
    plan = build_and_solve(make_input(n_targets=3, n_slots=48), AS_OF, FAST)
    for blk in plan.blocks:
        for s in range(blk.slot_start, blk.slot_end):
            assert plan.assignments[s].target_id == blk.target_id


# --- regression guards for bugs found by running the solver ------------------
# Each of these is written to FAIL against the original buggy behaviour; a guard
# that passes on the bug it names is worse than no guard, because it reads as
# coverage. tests/unit/test_regressions_have_teeth.py proves they discriminate.


def test_bright_target_needing_almost_no_integration_is_still_scheduled() -> None:
    """Regression: the over-exposure cap contradicted the operational constraints.

    A bright target can need essentially no integration (M31 through an 8" scope
    reaches SNR 20 in ~20 s) while the minimum block length and the >= 9 frame
    rule still force it to occupy L slots. A naive cap of 1.25 * E_t is then
    unsatisfiable, so u[t] was forced to 0 and the target vanished.

    The failure mode was the worst kind: the solver returned status OPTIMAL with
    an empty plan and every target "dropped". It looked like success.
    """
    inp = make_input(n_targets=2, n_slots=40, eta=1.0, need_ref_s=0.0)
    plan = build_and_solve(inp, AS_OF, FAST)
    assert plan.included, "a trivially-completable target must not be dropped"
    assert plan.blocks, "an OPTIMAL but empty plan is the failure this guards"
    for blk in plan.blocks:
        assert blk.n_slots - blk.switch_slots >= inp.min_block_slots


def test_cheap_targets_beat_one_expensive_target_when_capacity_forces_a_choice() -> None:
    """Regression: the completion bonus was scaled by E_t, which is backwards.

    Scaling by cost makes an expensive target worth MORE, so the solver
    completes one long target instead of several short ones. The scenario below
    is sized so only one option fits: two cheap targets (12 slots) or one
    expensive one (16 slots), out of 20.

    Under the old objective the expensive target scored 15 units against 10 for
    the pair, so it won. Under a flat per-target bonus the pair wins 2:1.
    """
    inp = make_input(
        n_targets=3,
        n_slots=20,
        eta=1.0,
        need_ref_s=np.array([1500.0, 1500.0, 4500.0]),
        min_block_slots=5,
    )
    plan = build_and_solve(inp, AS_OF, FAST)
    assert len(plan.included) >= 2, f"expected >=2 targets, got {sorted(plan.included)}"
    assert "t2" not in plan.included or len(plan.included) > 1


def test_model_contains_no_big_m_constants() -> None:
    """Regression: the model claimed "no big-M" and then used (1 - u) * 1e9.

    CP-SAT was chosen precisely because every structural constraint is
    expressible with reified implications instead of big-M. A big-M destroys the
    LP relaxation and is exactly the kind of thing that creeps back in during a
    late fix, so this is a lint-style guard on the source rather than behaviour.
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src" / "tscheduler" / "scheduling" / "cpsat.py"
    code = "\n".join(
        line for line in src.read_text().splitlines() if not line.strip().startswith("#")
    )
    hits = re.findall(r"(?<![\w.])(?:10\s*\*\*\s*[6-9]|[1-9]\s*e\s*[6-9]|\d{7,})(?![\w.])", code)
    assert not hits, f"possible big-M constant(s) reintroduced into the CP-SAT model: {hits}"
