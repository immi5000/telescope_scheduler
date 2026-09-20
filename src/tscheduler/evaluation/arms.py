"""The three comparison arms, and the metrics that decide between them.

The arms differ on exactly TWO axes -- which scheduler, and whether it re-plans.
Nothing else may differ, or the comparison measures our code structure instead of
our scheduling policy.

Two anti-bias measures are built in rather than bolted on, because this
comparison is the project's central claim and it is easy to rig by accident:

* Every arm is scored against the SAME realized conditions, never against the
  forecast it planned with. A plan that looked complete under an optimistic
  forecast and delivered SNR 68 instead of 100 gets no credit.
* The naive arm obeys every operational rule the optimizer does -- minimum block
  length, slew reservation, frame counts. Only optimisation is withheld.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
from numpy.typing import NDArray

from tscheduler.core.clock import AsOf
from tscheduler.domain.plan import Plan, SlotKind
from tscheduler.physics.geometry import NightGeometry
from tscheduler.pipeline.builder import SessionSpec, build_scheduler_input
from tscheduler.providers.weather.base import WeatherForecastProvider
from tscheduler.providers.weather.model import WeatherQuery
from tscheduler.scheduling.cpsat import SolveOptions, build_and_solve
from tscheduler.scheduling.naive import solve_naive

#: Replay comparisons MUST be deterministic, or plan churn is pure noise and the
#: headline numbers are irreproducible.
DET = SolveOptions(deterministic=True, max_deterministic_time=5.0, random_seed=1)


@dataclass(frozen=True, slots=True)
class ArmResult:
    name: str
    label: str
    executed: tuple[str | None, ...]
    n_replans: int
    plan_churn: int
    completed: frozenset[str]
    achieved_ref_seconds: dict[str, float]
    metrics: dict[str, float]


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    spec: SessionSpec
    arms: tuple[ArmResult, ...]
    decision_points: tuple[datetime, ...]
    truth_cloud: NDArray[np.float64]
    required_ref_seconds: NDArray[np.float64]

    def by_name(self, name: str) -> ArmResult:
        return next(a for a in self.arms if a.name == name)


def stitch(snapshots: list[tuple[int, Plan]], n_slots: int) -> tuple[str | None, ...]:
    """Reconstruct the night as ACTUALLY EXECUTED under any re-planning policy.

    Each accepted plan governs from the slot it took effect until the next
    supersedes it. Arm-agnostic, which is what lets all three arms share one
    scoring path instead of each growing its own.
    """
    out: list[str | None] = [None] * n_slots
    for k, (effective_from, plan) in enumerate(snapshots):
        until = snapshots[k + 1][0] if k + 1 < len(snapshots) else n_slots
        for s in range(effective_from, min(until, n_slots)):
            if s >= len(plan.assignments):
                break
            a = plan.assignments[s]
            out[s] = a.target_id if a.kind is SlotKind.OBSERVE else None
    return tuple(out)


def score(
    spec: SessionSpec,
    executed: tuple[str | None, ...],
    truth_eta: NDArray[np.float64],
    required: NDArray[np.float64],
    geo: NightGeometry,
) -> tuple[dict[str, float], frozenset[str], dict[str, float]]:
    """Score an executed night against the TRUTH efficiency grid."""
    ids = [t.id for t in spec.targets]
    slot_s = spec.grid.slot_seconds
    achieved: dict[str, float] = dict.fromkeys(ids, 0.0)
    alts: list[float] = []
    skies: list[float] = []

    for s, tid in enumerate(executed):
        if tid is None:
            continue
        i = ids.index(tid)
        achieved[tid] += float(truth_eta[i, s]) * slot_s
        alts.append(float(geo.altitude_deg[i, s]))
        skies.append(float(truth_eta[i, s]))

    completed = frozenset(
        tid for i, tid in enumerate(ids) if achieved[tid] >= float(required[i]) > 0.0
    )
    science = sum(
        spec.targets[i].weight * min(1.0, achieved[tid] / float(required[i]))
        for i, tid in enumerate(ids)
        if float(required[i]) > 0.0
    )
    observing = sum(1 for t in executed if t is not None)

    metrics = {
        "targets_completed": float(len(completed)),
        "science_value": float(science),
        "open_shutter_fraction": observing / max(len(executed), 1),
        "mean_altitude_deg": float(np.mean(alts)) if alts else 0.0,
        "mean_efficiency": float(np.mean(skies)) if skies else 0.0,
        "useful_ref_minutes": sum(achieved.values()) / 60.0,
    }
    return metrics, completed, achieved


def run_comparison(
    spec: SessionSpec,
    geo: NightGeometry,
    weather: WeatherForecastProvider,
    decision_points: list[datetime],
) -> ComparisonReport:
    """Run all three arms over ONE shared event timeline.

    The timeline, the geometry, the targets, the equipment and the truth grid are
    all shared; only the scheduler and the re-planning policy vary.
    """
    grid = spec.grid
    start = grid.start
    truth_at = AsOf.at(grid.end)  # best estimate of what actually happened

    truth_inp, _ = build_scheduler_input(spec, geo, weather, truth_at)
    truth_eta = truth_inp.eta
    required = truth_inp.required_ref_seconds
    truth_cloud, _, _ = weather.series(
        WeatherQuery(
            lat=spec.site.latitude_deg,
            lon=spec.site.longitude_deg,
            valid_from=grid.start,
            valid_to=grid.end,
        ),
        truth_at,
        grid,
    )

    arms: list[ArmResult] = []

    # --- arm 1: naive, list order, never re-planned ---------------------------
    inp0, led0 = build_scheduler_input(spec, geo, weather, AsOf.at(start))
    naive_plan = solve_naive(inp0, AsOf.at(start), order="input", ledger=led0)
    ex = stitch([(0, naive_plan)], grid.n_slots)
    m, c, a = score(spec, ex, truth_eta, required, geo)
    arms.append(ArmResult("naive", "Naive (list order)", ex, 0, 0, c, a, m))

    # --- arm 2: optimized once at dusk, never re-planned ----------------------
    static_plan = build_and_solve(inp0, AsOf.at(start), DET, led0)
    ex = stitch([(0, static_plan)], grid.n_slots)
    m, c, a = score(spec, ex, truth_eta, required, geo)
    arms.append(ArmResult("static", "Static (optimized at dusk)", ex, 1, 0, c, a, m))

    # --- arm 3: adaptive, re-planned at every decision point ------------------
    snapshots: list[tuple[int, Plan]] = [(0, static_plan)]
    prev = static_plan
    churn = 0
    for t in decision_points:
        if not (start < t < grid.end):
            continue
        first_free = grid.index_of(t)
        locked = {s: prev.assignments[s].target_id for s in range(first_free)}
        locked_observing = frozenset(
            s for s in range(first_free) if prev.assignments[s].kind is SlotKind.OBSERVE
        )
        # A plan the solver failed to find is idle after its history; offering
        # it as the plan to stay close to would make the change penalty argue
        # for an idle night.
        found = prev.status in ("OPTIMAL", "FEASIBLE")
        previous = (
            {s: prev.assignments[s].target_id for s in range(first_free, grid.n_slots)}
            if found
            else {}
        )
        inp, led = build_scheduler_input(
            spec,
            geo,
            weather,
            AsOf.at(t),
            locked=locked,
            locked_observing=locked_observing,
            previous_plan=previous,
            first_free_slot=first_free,
        )
        plan = build_and_solve(inp, AsOf.at(t), DET, led)
        if not plan.assignments:
            continue
        churn += sum(
            1
            for s in range(first_free, grid.n_slots)
            if plan.assignments[s].target_id != prev.assignments[s].target_id
        )
        snapshots.append((first_free, plan))
        prev = plan

    ex = stitch(snapshots, grid.n_slots)
    m, c, a = score(spec, ex, truth_eta, required, geo)
    arms.append(ArmResult("adaptive", "Adaptive (re-plans)", ex, len(snapshots), churn, c, a, m))

    return ComparisonReport(
        spec=spec,
        arms=tuple(arms),
        decision_points=tuple(decision_points),
        truth_cloud=truth_cloud,
        required_ref_seconds=required,
    )
