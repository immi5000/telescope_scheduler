"""The CP-SAT slot-assignment model.

Chosen over MILP on two independent grounds:

* EXPRESSIVENESS. Every structural constraint below is expressible with NO
  big-M, via add_bool_or / add_implication / only_enforce_if. Big-M relaxations
  are weak and their M values are a source of silently wrong answers.
* INSTALL. ortools ships macosx_11_0_arm64 wheels through cp314. PuLP 3.3.2
  bundles linux/arm64 but only osx/i64, so on Apple Silicon its CBC runs under
  Rosetta or not at all. (Verified by inspecting the wheel.)

THE KEY MODELLING MOVE is separating pointing from integrating:

    z[t,s]  telescope is ASSIGNED to t (pointing, slewing, or tracking)
    x[t,s]  shutter is OPEN on t (data lands on disk)
    b[t,s]  a z-block for t STARTS at s

z carries structure (contiguity, exclusivity, minimum length, slew); x carries
data (accumulation, objective). Deriving block starts from x instead -- the
literal reading of the spec -- double-fires whenever a slew slot sits inside a
block, and makes the reservation collide with the block start. That collision is
what makes "switch_time = 1 slot" look degenerate. With z carrying the
structure, the first slot of the z-block IS the slew slot and x's run simply
starts one slot later. Nothing collapses, and the formulation stays correct for
switch times of 0, 1, 2 and 3+ slots, and for switch times that are not a whole
multiple of the slot length.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray
from ortools.sat.python import cp_model

from tscheduler.core.clock import AsOf
from tscheduler.core.timegrid import TimeGrid
from tscheduler.domain.plan import Assignment, Block, DropReason, Plan, SlotKind
from tscheduler.providers.base import EvidenceLedger

#: Objective coefficients are integers; this is the single documented place
#: floats become integers, so scaling bugs cannot hide.
SCALE = 1000


@dataclass(frozen=True, slots=True)
class SchedulerInput:
    grid: TimeGrid
    target_ids: tuple[str, ...]
    eta: NDArray[np.float64]
    """(T, S) efficiency: reference-seconds of progress per real second."""
    preference: NDArray[np.float64]
    """(T, S) soft policy score in [0, 1]. Never affects feasibility."""
    visible: NDArray[np.bool_]
    required_ref_seconds: NDArray[np.float64]
    """(T,) E_t. Comes from snr_goal^2 / rho_reference."""
    weight: NDArray[np.float64]
    """(T,) priority * urgency."""
    subs_per_slot: NDArray[np.int64]
    """(T, S) whole sub-exposures obtainable in a slot, after readout overhead."""
    t_sub_s: NDArray[np.float64]
    switch_slots: int = 1
    switch_remainder: float = 0.0
    min_block_slots: int = 4
    min_subs: int = 9
    """Sigma-clipped stacking needs >= 9 frames to reject satellite trails and
    cosmic rays. This is good practice independent of satellites, which is what
    makes the design robust to a wrong streak-rate model."""
    max_blocks_per_target: int = 3
    overexposure_allowance: float = 0.25
    switch_penalty: float = 2.0
    change_penalty: float = 0.5
    preference_weight: float = 0.3
    """Must stay well below 1 so preference shades the PLACEMENT of a block
    without ever out-voting completion."""
    locked: dict[int, str | None] = field(default_factory=dict)
    """slot -> target_id (or None for idle/switch). Immovable history."""
    previous_plan: dict[int, str | None] = field(default_factory=dict)
    first_free_slot: int = 0


@dataclass(frozen=True, slots=True)
class SolveOptions:
    max_seconds: float = 2.0
    workers: int = 8
    relative_gap: float = 0.01
    deterministic: bool = False
    """REPLAY MUST SET THIS. CP-SAT with a wall-clock limit and workers > 1 is
    nondeterministic, which breaks the truncation-equivalence property test and
    makes plan-churn metrics pure noise."""
    max_deterministic_time: float = 8.0
    random_seed: int = 1


def build_and_solve(
    inp: SchedulerInput,
    as_of: AsOf,
    opts: SolveOptions | None = None,
    ledger: EvidenceLedger | None = None,
) -> Plan:
    o = opts or SolveOptions()
    t0 = time.perf_counter()
    n_t, n_s = len(inp.target_ids), inp.grid.n_slots
    m = cp_model.CpModel()

    W, L = inp.switch_slots, inp.min_block_slots  # noqa: N806 - paper's symbols

    # --- presolve: never create variables for impossible (t, s) pairs ---------
    # Typically removes 40-60% of the model before the solver sees it.
    feasible = inp.visible & (inp.eta > 0.0)

    z: dict[tuple[int, int], cp_model.IntVar] = {}
    x: dict[tuple[int, int], cp_model.IntVar] = {}
    b: dict[tuple[int, int], cp_model.IntVar] = {}
    for t in range(n_t):
        for s in range(n_s):
            if feasible[t, s]:
                z[t, s] = m.new_bool_var(f"z{t}_{s}")
                x[t, s] = m.new_bool_var(f"x{t}_{s}")
                b[t, s] = m.new_bool_var(f"b{t}_{s}")

    u = [m.new_bool_var(f"u{t}") for t in range(n_t)]

    def zv(t: int, s: int) -> cp_model.IntVar | None:
        return z.get((t, s))

    # --- C1: at most one target per slot -------------------------------------
    for s in range(n_s):
        here = [z[t, s] for t in range(n_t) if (t, s) in z]
        if here:
            m.add_at_most_one(here)

    for t in range(n_t):
        for s in range(n_s):
            if (t, s) not in z:
                continue
            # --- C2: data implies pointing ----------------------------------
            m.add(x[t, s] <= z[t, s])
            # --- C8: inclusion gating ---------------------------------------
            m.add(z[t, s] <= u[t])

            # --- C4: block-start definition, exact, no big-M ----------------
            # b = z[s] AND NOT z[s-1]. All three are needed: the first alone is
            # only valid under minimisation pressure, and b appears in the
            # accumulation constraint where an over-estimate would be unsound.
            prev = zv(t, s - 1) if s > 0 else None
            if prev is None:
                m.add(b[t, s] == z[t, s])
            else:
                m.add(b[t, s] >= z[t, s] - prev)
                m.add(b[t, s] <= z[t, s])
                m.add(b[t, s] + prev <= 1)

            # --- C5: minimum contiguous block length ------------------------
            # L + W because L counts DATA slots and the first W are eaten by the
            # slew. A block that cannot fit is forbidden from starting at all.
            need = L + W
            if s + need > n_s or any((t, s + j) not in z for j in range(need)):
                m.add(b[t, s] == 0)
            else:
                for j in range(1, need):
                    m.add(z[t, s + j] >= b[t, s])

            # --- C6: slew reservation ---------------------------------------
            # The first W slots of a block collect no data. Only the same target
            # needs checking: a different target starting there is already
            # excluded by C1 + C5.
            for j in range(W):
                if (t, s - j) in b:
                    m.add(x[t, s] + b[t, s - j] <= 1)

            # The first data slot must actually carry data.
            if (t, s - W) in b:
                m.add(x[t, s] >= b[t, s - W])

            # ...and conversely, pointing at a target while collecting NOTHING is
            # only allowed during the slew reservation. Without this the solver
            # parks on a target for free whenever the over-exposure cap bites:
            # the first end-to-end run produced an M31 "block" with 31 switch
            # slots. It also tightens the model considerably, because z and x are
            # now equal everywhere except the W reservation slots.
            recent_starts = [b[t, s - j] for j in range(W) if (t, s - j) in b]
            if recent_starts:
                m.add(z[t, s] - x[t, s] <= sum(recent_starts))
            else:
                m.add(x[t, s] == z[t, s])

    # --- C11: symmetry breaking / search reduction ---------------------------
    for t in range(n_t):
        starts = [b[t, s] for s in range(n_s) if (t, s) in b]
        if starts:
            m.add(sum(starts) <= inp.max_blocks_per_target)

    # --- C7 / C7b: effective-exposure accumulation ---------------------------
    slot_s = inp.grid.slot_seconds
    for t in range(n_t):
        gained = []
        for s in range(n_s):
            if (t, s) in x:
                gained.append(round(SCALE * slot_s * float(inp.eta[t, s])) * x[t, s])
        # Debit the fractional slew remainder from the first data slot.
        if inp.switch_remainder > 0.0:
            for s in range(n_s):
                if (t, s) in b and (t, s + W) in x:
                    debit = round(SCALE * slot_s * inp.switch_remainder * float(inp.eta[t, s + W]))
                    if debit:
                        gained.append(-debit * b[t, s])
        if not gained:
            m.add(u[t] == 0)
            continue
        need = round(SCALE * float(inp.required_ref_seconds[t]))
        m.add(sum(gained) >= need * u[t])

        # Over-exposure cap: without it the optimizer can dump the whole night on
        # one high-weight target rather than completing several.
        #
        # But the cap must never contradict the operational constraints. A bright
        # target can need almost no integration (M31 through an 8" scope reaches
        # SNR 20 in seconds), while the minimum block length and the >= 9 frame
        # rule still force it to occupy L slots. A naive 1.25*E_t cap is then
        # unsatisfiable and the target is silently dropped -- which is exactly
        # what happened on the first end-to-end run: all 12 targets dropped with
        # an OPTIMAL empty plan. Floor the cap at what one minimum-length block
        # necessarily delivers.
        best_eta = float(np.max(inp.eta[t])) if inp.eta[t].size else 0.0
        min_block_delivery = round(SCALE * slot_s * best_eta * (L + W))
        cap = max(round(need * (1.0 + inp.overexposure_allowance)), min_block_delivery)

        # History cannot violate a cap. Slots already locked to this target
        # collected real photons, and no re-plan can un-collect them -- so the
        # cap has to be raised by whatever the past already contributed, or a
        # mid-night re-plan on a nearly-finished target makes the whole model
        # INFEASIBLE. That is not hypothetical: replaying a real night hit it at
        # the third decision point and returned an empty plan.
        locked_gain = round(
            SCALE
            * slot_s
            * sum(
                float(inp.eta[t, sl])
                for sl, tid in inp.locked.items()
                if tid == inp.target_ids[t] and sl < n_s
            )
        )
        cap += locked_gain
        # only_enforce_if, NOT a big-M. An earlier version wrote
        #     sum(gained) <= cap + (1 - u[t]) * 1e9
        # which is the exact thing this model was chosen to avoid: the 1e9
        # destroys the LP relaxation and the optimality gap sat at 84% after 5
        # seconds. The reified form is both correct and tight.
        m.add(sum(gained) <= cap).only_enforce_if(u[t])

        # --- C9: frame count, linear ------------------------------------------
        frames = [int(inp.subs_per_slot[t, s]) * x[t, s] for s in range(n_s) if (t, s) in x]
        if frames:
            m.add(sum(frames) >= inp.min_subs * u[t])

    # --- C10: locked past ----------------------------------------------------
    for s, tid in inp.locked.items():
        for t in range(n_t):
            if (t, s) not in z:
                continue
            want = 1 if tid == inp.target_ids[t] else 0
            m.add(z[t, s] == want)

    # --- objective -----------------------------------------------------------
    terms = []
    # Completion bonus is FLAT per target (scaled only by priority x urgency),
    # deliberately NOT scaled by how much integration the target needs.
    #
    # Scaling it by E_t is the intuitive choice and it is wrong: it makes an
    # expensive target worth more than a cheap one, so the solver prefers
    # completing one 17-hour target over five 90-minute ones. On the first
    # contended test run that produced a night with exactly 2 targets completed
    # and 10 dropped.
    #
    # n_slots is the smallest bonus that still dominates: the most preference
    # any single target could ever accumulate is n_slots * preference_weight *
    # weight, so a flat n_slots * weight beats it by 1/preference_weight (3.3x
    # at the default). Completing one more target therefore always wins.
    for t in range(n_t):
        terms.append(round(SCALE * float(inp.weight[t]) * n_s) * u[t])
    for t in range(n_t):
        for s in range(n_s):
            if (t, s) in x:
                c = (
                    SCALE
                    * inp.preference_weight
                    * float(inp.weight[t])
                    * float(inp.preference[t, s])
                )
                if round(c):
                    terms.append(round(c) * x[t, s])
    for var in b.values():
        terms.append(-round(SCALE * inp.switch_penalty) * var)

    # Plan-stability penalty, linearised with NO extra variables: for a slot the
    # previous plan assigned to t0, "changed" is just (1 - z[t0,s]).
    if inp.previous_plan:
        for s, tid in inp.previous_plan.items():
            if s < inp.first_free_slot:
                continue
            pen = round(SCALE * inp.change_penalty)
            if not pen:
                continue
            if tid is None:
                for t in range(n_t):
                    if (t, s) in z:
                        terms.append(-pen * z[t, s])
            else:
                ti = inp.target_ids.index(tid) if tid in inp.target_ids else None
                if ti is not None and (ti, s) in z:
                    terms.append(pen * z[ti, s])

    # NOTE: no lexicographic tie-break term. The obvious one -- a small
    # earliest-first bonus -- is not small: at (n_slots - s) it reaches ~120 per
    # slot against preference terms of ~300, so summed over a night it would
    # visibly distort the schedule rather than merely break ties. Determinism
    # comes from SolveOptions(deterministic=True) instead, which is what replay
    # must use anyway.

    m.maximize(sum(terms))

    # --- solve ---------------------------------------------------------------
    solver = cp_model.CpSolver()
    p = solver.parameters
    p.random_seed = o.random_seed
    p.relative_gap_limit = o.relative_gap
    if o.deterministic:
        p.num_workers = 1
        p.interleave_search = True
        p.max_deterministic_time = o.max_deterministic_time
    else:
        p.num_workers = o.workers
        p.max_time_in_seconds = o.max_seconds

    status = solver.solve(m)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    status_name = solver.status_name(status)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return Plan(
            grid=inp.grid,
            as_of=as_of,
            assignments=(),
            blocks=(),
            included=frozenset(),
            dropped=tuple(
                DropReason(tid, "no_solution", f"solver returned {status_name}")
                for tid in inp.target_ids
            ),
            objective=0.0,
            status=status_name,
            solve_ms=elapsed_ms,
            ledger=ledger or EvidenceLedger(),
        )

    return _extract(
        inp, as_of, solver, z, x, b, u, status_name, elapsed_ms, ledger or EvidenceLedger()
    )


def _extract(
    inp: SchedulerInput,
    as_of: AsOf,
    solver: cp_model.CpSolver,
    z: dict[tuple[int, int], cp_model.IntVar],
    x: dict[tuple[int, int], cp_model.IntVar],
    b: dict[tuple[int, int], cp_model.IntVar],
    u: list[cp_model.IntVar],
    status_name: str,
    elapsed_ms: float,
    ledger: EvidenceLedger,
) -> Plan:
    n_t, n_s = len(inp.target_ids), inp.grid.n_slots
    assignments, blocks = [], []
    included = {inp.target_ids[t] for t in range(n_t) if solver.value(u[t])}

    slot_target: list[str | None] = [None] * n_s
    slot_kind: list[SlotKind] = [SlotKind.IDLE] * n_s
    for s in range(n_s):
        for t in range(n_t):
            if (t, s) in z and solver.value(z[t, s]):
                slot_target[s] = inp.target_ids[t]
                slot_kind[s] = SlotKind.OBSERVE if solver.value(x[t, s]) else SlotKind.SWITCH
                break

    for s in range(n_s):
        assignments.append(
            Assignment(slot=s, kind=slot_kind[s], target_id=slot_target[s], locked=s in inp.locked)
        )

    s = 0
    while s < n_s:
        tid = slot_target[s]
        if tid is None:
            s += 1
            continue
        end = s
        while end < n_s and slot_target[end] == tid:
            end += 1
        t = inp.target_ids.index(tid)
        data_slots = [k for k in range(s, end) if slot_kind[k] is SlotKind.OBSERVE]
        n_subs = int(sum(int(inp.subs_per_slot[t, k]) for k in data_slots))
        acc = float(sum(inp.eta[t, k] * inp.grid.slot_seconds for k in data_slots))
        need = float(inp.required_ref_seconds[t])
        snr = 0.0 if need <= 0 else float(np.sqrt(max(acc, 0.0) / need)) * 0.0
        blocks.append(
            Block(
                target_id=tid,
                slot_start=s,
                slot_end=end,
                switch_slots=end - s - len(data_slots),
                n_subs=n_subs,
                t_sub_s=float(inp.t_sub_s[t]),
                expected_snr=snr,
            )
        )
        s = end

    dropped = tuple(
        DropReason(tid, "outranked", "did not fit; see explain() for the counterfactual")
        for i, tid in enumerate(inp.target_ids)
        if tid not in included
    )

    return Plan(
        grid=inp.grid,
        as_of=as_of,
        assignments=tuple(assignments),
        blocks=tuple(blocks),
        included=frozenset(included),
        dropped=dropped,
        objective=float(solver.objective_value),
        status=status_name,
        solve_ms=elapsed_ms,
        best_bound=float(solver.best_objective_bound),
        ledger=ledger,
    )
