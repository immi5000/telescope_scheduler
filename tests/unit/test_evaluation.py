"""The three-arm comparison.

The tests here are mostly about FAIRNESS, because the comparison is the
project's central claim and the easiest way to get a flattering result is to
handicap the baseline by accident.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import numpy as np

from tscheduler.core.clock import AsOf
from tscheduler.core.timegrid import TimeGrid
from tscheduler.domain.plan import Assignment, Plan, SlotKind
from tscheduler.evaluation.arms import stitch
from tscheduler.providers.base import EvidenceLedger
from tscheduler.scheduling.cpsat import SchedulerInput
from tscheduler.scheduling.naive import solve_naive

START = datetime(2026, 9, 12, 23, 0, tzinfo=UTC)
AS_OF = AsOf.at(START)


def make_input(n_targets=3, n_slots=40, eta=1.0, need=1500.0, visible=None, **kw) -> SchedulerInput:
    grid = TimeGrid(START, n_slots)
    return SchedulerInput(
        grid=grid,
        target_ids=tuple(f"t{i}" for i in range(n_targets)),
        eta=np.full((n_targets, n_slots), eta),
        preference=np.ones((n_targets, n_slots)),
        visible=np.ones((n_targets, n_slots), dtype=bool) if visible is None else visible,
        required_ref_seconds=np.full(n_targets, need),
        weight=np.ones(n_targets),
        subs_per_slot=np.full((n_targets, n_slots), 3, dtype=np.int64),
        t_sub_s=np.full(n_targets, 90.0),
        switch_slots=1,
        min_block_slots=4,
        **kw,
    )


# --- fairness of the baseline ------------------------------------------------
def test_naive_obeys_the_minimum_block_length() -> None:
    """A baseline that ignored the operational rules would make the comparison
    measure our code structure rather than our scheduling policy."""
    plan = solve_naive(make_input(), AS_OF)
    for blk in plan.blocks:
        data = blk.n_slots - blk.switch_slots
        assert data >= 4 or blk.slot_end == plan.grid.n_slots


def test_naive_pays_the_same_switch_cost() -> None:
    plan = solve_naive(make_input(), AS_OF)
    assert plan.blocks
    for blk in plan.blocks:
        assert blk.switch_slots == 1


def test_naive_respects_visibility() -> None:
    vis = np.ones((3, 40), dtype=bool)
    vis[0, :20] = False
    plan = solve_naive(make_input(visible=vis), AS_OF)
    obs = [a.slot for a in plan.assignments if a.target_id == "t0" and a.kind is SlotKind.OBSERVE]
    assert all(s >= 20 for s in obs)


def test_naive_follows_list_order_not_quality() -> None:
    """That is the whole point of the naive arm: it is what an observer with a
    printed list does, so it must not accidentally be smart."""
    inp = make_input(n_targets=3, n_slots=60)
    plan = solve_naive(inp, AS_OF, order="input")
    first = next(b.target_id for b in plan.blocks)
    assert first == "t0"


def test_naive_priority_variant_leads_with_the_heaviest() -> None:
    inp = dataclasses.replace(make_input(n_targets=3, n_slots=60), weight=np.array([1.0, 5.0, 1.0]))
    plan = solve_naive(inp, AS_OF, order="priority")
    assert next(b.target_id for b in plan.blocks) == "t1"


def test_naive_stops_a_target_once_it_is_done() -> None:
    """Otherwise it would grind one target all night and the comparison would be
    against a straw man."""
    plan = solve_naive(make_input(n_targets=3, n_slots=60, eta=1.0, need=1500.0), AS_OF)
    assert len({b.target_id for b in plan.blocks}) > 1


# --- stitching ---------------------------------------------------------------
def _plan(grid, ids: list[str | None]) -> Plan:
    return Plan(
        grid=grid,
        as_of=AS_OF,
        assignments=tuple(
            Assignment(
                slot=i,
                kind=SlotKind.OBSERVE if t else SlotKind.IDLE,
                target_id=t,
            )
            for i, t in enumerate(ids)
        ),
        blocks=(),
        included=frozenset(x for x in ids if x),
        dropped=(),
        objective=0.0,
        status="TEST",
        solve_ms=0.0,
        ledger=EvidenceLedger(),
    )


def test_stitch_reconstructs_what_actually_ran() -> None:
    """Each accepted plan governs from when it took effect until the next
    supersedes it -- so a re-plan changes the future, never the past."""
    grid = TimeGrid(START, 6)
    first = _plan(grid, ["a", "a", "a", "a", "a", "a"])
    second = _plan(grid, ["a", "a", "b", "b", "b", "b"])
    assert stitch([(0, first), (2, second)], 6) == ("a", "a", "b", "b", "b", "b")


def test_stitch_with_one_snapshot_is_that_plan() -> None:
    grid = TimeGrid(START, 4)
    p = _plan(grid, ["x", "x", None, "y"])
    assert stitch([(0, p)], 4) == ("x", "x", None, "y")


def test_stitch_ignores_switch_slots_as_non_observing() -> None:
    """Slew slots collect no photons, so they must not count as time on target."""
    grid = TimeGrid(START, 3)
    p = Plan(
        grid=grid,
        as_of=AS_OF,
        assignments=(
            Assignment(0, SlotKind.SWITCH, "a"),
            Assignment(1, SlotKind.OBSERVE, "a"),
            Assignment(2, SlotKind.IDLE, None),
        ),
        blocks=(),
        included=frozenset({"a"}),
        dropped=(),
        objective=0.0,
        status="TEST",
        solve_ms=0.0,
        ledger=EvidenceLedger(),
    )
    assert stitch([(0, p)], 3) == (None, "a", None)


def test_completion_margin_gives_slack_against_forecast_error() -> None:
    """Targeting E_t exactly leaves zero slack, so any forecast error flips
    completion to failure. Measured on six real nights: the optimizer repeatedly
    landed at 0.98x requirement under realized conditions and got no credit."""
    from tscheduler.scheduling.cpsat import SolveOptions, build_and_solve

    inp = make_input(n_targets=1, n_slots=40, eta=1.0, need=1500.0)
    assert inp.completion_margin > 0.0
    plan = build_and_solve(inp, AS_OF, SolveOptions(max_seconds=10.0))
    observed = [a.slot for a in plan.assignments if a.kind is SlotKind.OBSERVE]
    accumulated = len(observed) * inp.grid.slot_seconds
    assert accumulated >= 1500.0 * (1.0 + inp.completion_margin) - 1e-6
