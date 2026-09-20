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

import math
import time
from collections.abc import Iterator, Sequence
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
    """(T, S) efficiency: reference-seconds of progress per wall-clock second of
    continuous imaging. The camera's download dead time is already taken out
    (``pipeline.conditions`` scales by the duty cycle), so ``slot_seconds *
    eta`` is what a slot of back-to-back frames delivers."""
    preference: NDArray[np.float64]
    """(T, S) soft policy score in [0, 1]. Never affects feasibility."""
    visible: NDArray[np.bool_]
    required_ref_seconds: NDArray[np.float64]
    """(T,) E_t. Comes from snr_goal^2 / rho_reference."""
    weight: NDArray[np.float64]
    """(T,) priority * urgency."""
    subs_per_slot: NDArray[np.int64]
    """(T, S) whole sub-exposures one slot holds ON ITS OWN, after readout.

    Not what a block lists, and no longer read by this model: frames run back
    to back across slot boundaries, so a block's frames are counted over the
    whole block (:func:`block_frames`). A 20-minute block of 90 s subs holds
    13 frames, not 4 x 3."""
    t_sub_s: NDArray[np.float64]
    snr_goal: NDArray[np.float64] | None = None
    """(T,) the SNR each target is aiming for. Carried only so the extractor can
    turn accumulated reference-seconds back into an SNR for the instruction
    card; the model itself works entirely in required_ref_seconds."""
    readout_s: NDArray[np.float64] | None = None
    """(T,) the camera's download time between frames. ``None`` means none.
    With ``t_sub_s`` it sets the frame period, which is what decides how many
    whole frames a block holds."""
    switch_slots: int = 1
    switch_remainder: float = 0.0
    min_block_slots: int = 4
    min_subs: int = 9
    """Sigma-clipped stacking needs >= 9 frames to reject satellite trails and
    cosmic rays. This is good practice independent of satellites, which is what
    makes the design robust to a wrong streak-rate model.

    Enforced per BLOCK, in whole frames: a new block is never shorter than
    :func:`min_data_slots`, which lengthens ``min_block_slots`` until the block
    holds this many. That is also the rule the instruction card warns on."""
    max_blocks_per_target: int = 3
    completion_margin: float = 0.15
    """Accumulate (1 + margin) x E_t before declaring a target done.

    Targeting E_t exactly leaves zero slack, so any forecast error flips
    completion to failure. Measured on six real nights: the optimizer repeatedly
    landed targets at 0.98x requirement under realized conditions and got no
    credit, while the naive baseline's greedy blocks overshot by accident and
    survived. A real observer does not stop at exactly SNR 20 either.
    """
    overexposure_allowance: float = 0.25
    switch_penalty: float = 2.0
    change_penalty: float = 0.5
    preference_weight: float = 0.3
    """Must stay well below 1 so preference shades the PLACEMENT of a block
    without ever out-voting completion."""
    locked: dict[int, str | None] = field(default_factory=dict)
    """slot -> target_id (or None for idle/switch). Immovable history."""
    locked_observing: frozenset[int] | None = None
    """Which locked slots actually INTEGRATED, as opposed to slewing or idling.

    A switch slot is assigned to its target but collects nothing, so counting it
    as progress over-credits every re-plan by the slew reservation. ``None``
    means "assume every locked slot with a target integrated", which is right
    for callers that never modelled a slew."""
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

    W = inp.switch_slots  # noqa: N806 - paper's symbol
    slot_s = inp.grid.slot_seconds
    # L, per target: the minimum block in DATA slots, lengthened where needed so
    # that one block holds min_subs whole frames. With long subs or a slow
    # download the configured minimum can be too short for that, and counting
    # frames it cannot hold is exactly the error this replaces.
    L = [min_data_slots(inp, t) for t in range(n_t)]  # noqa: N806 - paper's symbol

    # --- history is DATA, not a decision --------------------------------------
    # The past gets no variables at all. Modelling it as forced assignments and
    # letting the structural constraints run over it reads as the same thing and
    # is not: it makes the model claim things about history it cannot know.
    #
    # Two ways that bites, both observed on a real replayed night. C8
    # (z <= u) turns "the observer pointed here" into "this target WILL be
    # completed" -- but eta is recomputed at each as_of, so a worsening forecast
    # can put completion out of reach while the assignment stays forced, and the
    # model goes INFEASIBLE. And the presolve mask itself depends on the
    # forecast, so a locked slot can simply vanish from the model, splitting a
    # block in two and tripping the minimum-length rule.
    #
    # INFEASIBLE is a bug in this model by construction -- u[t] makes the
    # all-idle plan always available -- so either failure returns an empty plan
    # in the middle of a night. History enters as constants instead.
    s0 = inp.first_free_slot
    if inp.locked:
        s0 = max(s0, max(inp.locked) + 1)
    s0 = min(max(s0, 0), n_s)

    observing = (
        inp.locked_observing
        if inp.locked_observing is not None
        else frozenset(s for s, tid in inp.locked.items() if tid is not None)
    )
    index_of = {tid: t for t, tid in enumerate(inp.target_ids)}

    #: The target the telescope is sitting on at the boundary, if any. Continuing
    #: it must cost no switch and reserve no slew -- otherwise every re-plan
    #: charges the observer to keep doing what they are already doing, and the
    #: scheduler abandons whatever is half-finished.
    boundary = inp.locked.get(s0 - 1) if s0 > 0 else None

    # Banked progress, valued under the CURRENT forecast. That is deliberate:
    # for hours already past, the newest run is the best estimate of what the
    # sky actually did, so a night that clouded over early correctly reports
    # less progress than was projected at dusk -- which is precisely the signal
    # that should make the scheduler give the target more time or give up on it.
    #
    # Walked block by block, like the future: each past block pays the same
    # fractional-slew debit a new one does, and its frames are counted whole
    # over the block. The block still running at the boundary is held apart,
    # since how many frames it ends up with depends on how long the plan keeps
    # it going (see C9).
    hist_gain = [0] * n_t
    hist_frames = [0] * n_t
    boundary_data = 0
    past_target, past_kind = _history(inp, s0, observing)
    for run_id, _start, end, data in _runs(past_target, past_kind):
        t = index_of.get(run_id)
        if t is None or not data:
            continue
        hist_gain[t] += sum(round(SCALE * slot_s * float(inp.eta[t, k])) for k in data)
        hist_gain[t] -= round(SCALE * slot_s * inp.switch_remainder * float(inp.eta[t, data[0]]))
        if end == s0 and run_id == boundary:
            boundary_data = len(data)
        else:
            hist_frames[t] += block_frames(inp, t, len(data))

    # --- presolve: never create variables for impossible (t, s) pairs ---------
    # Typically removes 40-60% of the model before the solver sees it.
    feasible = inp.visible & (inp.eta > 0.0)

    z: dict[tuple[int, int], cp_model.IntVar] = {}
    x: dict[tuple[int, int], cp_model.IntVar] = {}
    b: dict[tuple[int, int], cp_model.IntVar] = {}
    for t in range(n_t):
        for s in range(s0, n_s):
            if feasible[t, s]:
                z[t, s] = m.new_bool_var(f"z{t}_{s}")
                x[t, s] = m.new_bool_var(f"x{t}_{s}")
                b[t, s] = m.new_bool_var(f"b{t}_{s}")

    u = [m.new_bool_var(f"u{t}") for t in range(n_t)]

    def zv(t: int, s: int) -> cp_model.IntVar | None:
        return z.get((t, s))

    #: Whether each target has any window long enough to START a block in. One
    #: that has none is reported as such, not as "outranked".
    startable = [False] * n_t

    # --- C1: at most one target per slot -------------------------------------
    for s in range(s0, n_s):
        here = [z[t, s] for t in range(n_t) if (t, s) in z]
        if here:
            m.add_at_most_one(here)

    for t in range(n_t):
        for s in range(s0, n_s):
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
            # At the boundary, z[t, s0-1] is a CONSTANT from history, not a
            # variable: 1 for the target already under the telescope, 0 for
            # everything else. The same b = z[s] AND NOT z[s-1] definition then
            # collapses to "continuing costs nothing, starting is a new block".
            prev = zv(t, s - 1) if s > s0 else None
            if s == s0:
                if boundary is not None and boundary == inp.target_ids[t]:
                    m.add(b[t, s] == 0)
                else:
                    m.add(b[t, s] == z[t, s])
            elif prev is None:
                m.add(b[t, s] == z[t, s])
            else:
                m.add(b[t, s] >= z[t, s] - prev)
                m.add(b[t, s] <= z[t, s])
                m.add(b[t, s] + prev <= 1)

            # --- C5: minimum contiguous block length ------------------------
            # L + W because L counts DATA slots and the first W are eaten by the
            # slew. A block that cannot fit is forbidden from starting at all.
            need = L[t] + W
            if s + need > n_s or any((t, s + j) not in z for j in range(need)):
                m.add(b[t, s] == 0)
            else:
                startable[t] = True
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
        starts = [b[t, s] for s in range(s0, n_s) if (t, s) in b]
        if starts:
            m.add(sum(starts) <= inp.max_blocks_per_target)

    # --- C7 / C7b: effective-exposure accumulation ---------------------------
    for t in range(n_t):
        gained = []
        for s in range(s0, n_s):
            if (t, s) in x:
                gained.append(round(SCALE * slot_s * float(inp.eta[t, s])) * x[t, s])
        # Debit the fractional slew remainder from the first data slot.
        if inp.switch_remainder > 0.0:
            for s in range(s0, n_s):
                if (t, s) in b and (t, s + W) in x:
                    debit = round(SCALE * slot_s * inp.switch_remainder * float(inp.eta[t, s + W]))
                    if debit:
                        gained.append(-debit * b[t, s])
            # The block running at the boundary was still slewing when history
            # ended, so its first data slot -- and its debit -- is s0.
            if (
                boundary is not None
                and boundary == inp.target_ids[t]
                and boundary_data == 0
                and (t, s0) in x
            ):
                debit = round(SCALE * slot_s * inp.switch_remainder * float(inp.eta[t, s0]))
                if debit:
                    gained.append(-debit * x[t, s0])
        # Banked progress is a constant on the left-hand side. Note this stays
        # correct when there is no future at all: a target already finished
        # before the boundary may still be marked complete, and one that cannot
        # finish is simply not included rather than making the model unsolvable.
        future = cp_model.LinearExpr.sum(gained)
        need = round(SCALE * float(inp.required_ref_seconds[t]) * (1.0 + inp.completion_margin))
        m.add(hist_gain[t] + future >= need * u[t])

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
        future_eta = inp.eta[t, s0:]
        best_eta = float(np.max(future_eta)) if future_eta.size else 0.0
        min_block_delivery = round(SCALE * slot_s * best_eta * (L[t] + W))
        cap = max(round(need * (1.0 + inp.overexposure_allowance)), min_block_delivery)

        # History cannot violate a cap. Slots already locked to this target
        # collected real photons, and no re-plan can un-collect them -- so the
        # cap has to be raised by whatever the past already contributed, or a
        # mid-night re-plan on a nearly-finished target makes the whole model
        # INFEASIBLE. That is not hypothetical: replaying a real night hit it at
        # the third decision point and returned an empty plan.
        cap = max(cap, hist_gain[t] + min_block_delivery)
        # only_enforce_if, NOT a big-M. An earlier version wrote
        #     sum(gained) <= cap + (1 - u[t]) * 1e9
        # which is the exact thing this model was chosen to avoid: the 1e9
        # destroys the LP relaxation and the optimality gap sat at 84% after 5
        # seconds. The reified form is both correct and tight.
        m.add(hist_gain[t] + future <= cap).only_enforce_if(u[t])

        # --- C9: frame count, in whole frames, linear -------------------------
        # A block's frames are floor(exposable time / frame period), and the
        # floor of a SUM of blocks is not linear. Each block on its own is,
        # though: a block holds >= k whole frames iff its exposable time is >=
        # k frame periods. C5 already makes every NEW block hold min_subs, so
        # any block start satisfies this outright. What is left is history,
        # counted exactly, and the block still running at the boundary, whose
        # frames depend on how long the plan keeps it going. Its other future
        # slots can only belong to new blocks, which satisfy this anyway.
        period = frame_period_s(inp, t)
        have = [SCALE * inp.min_subs * b[t, s] for s in range(s0, n_s) if (t, s) in b]
        banked = SCALE * hist_frames[t]
        if boundary is not None and boundary == inp.target_ids[t]:
            per_slot = math.floor(SCALE * slot_s / period)
            have += [per_slot * x[t, s] for s in range(s0, n_s) if (t, s) in x]
            banked += math.floor(SCALE * slot_s * (boundary_data - inp.switch_remainder) / period)
        m.add(banked + cp_model.LinearExpr.sum(have) >= SCALE * inp.min_subs * u[t])

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
        for s in range(s0, n_s):
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
            if s < s0:
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
        # Still a WHOLE night: history replayed from the record, idle after.
        # Callers index assignments by slot -- the next re-plan locks
        # prev.assignments[s] for every slot before its boundary -- so the
        # empty tuple this used to return turned one slow solve (2-minute
        # slots, a long target list, a 4 s budget) into an IndexError that
        # failed the whole session. The status and the "no_solution" drops
        # still say plainly that the solver found nothing.
        past_target, past_kind = _history(inp, s0, observing)
        assignments, blocks = _assemble(inp, past_target, past_kind, s0)
        return Plan(
            grid=inp.grid,
            as_of=as_of,
            assignments=assignments,
            blocks=blocks,
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
        inp,
        as_of,
        solver,
        z,
        x,
        b,
        u,
        status_name,
        elapsed_ms,
        ledger or EvidenceLedger(),
        s0=s0,
        observing=observing,
        no_window=frozenset(
            t
            for t in range(n_t)
            if not startable[t]
            and not (boundary is not None and boundary == inp.target_ids[t] and (t, s0) in z)
        ),
        min_data=L,
    )


# --- frames: one time base for the optimizer, the card and the progress bar --
#
# eta is progress per wall-clock second of back-to-back frames, download
# included, so the optimizer's slot_seconds * eta and everything reported below
# count the same time. Frames are counted over a whole BLOCK, never per slot:
# they run across slot boundaries, and a frame that would run past the end of
# the block is not listed.


def frame_period_s(inp: SchedulerInput, t: int) -> float:
    """Wall-clock seconds per frame: the exposure plus the camera's download."""
    readout = 0.0 if inp.readout_s is None else max(float(inp.readout_s[t]), 0.0)
    return max(float(inp.t_sub_s[t]) + readout, 1e-9)


def _exposable_s(inp: SchedulerInput, n_data_slots: int) -> float:
    """Seconds of a block's data slots left for frames once the slew is paid.

    The fractional part of the slew eats into the first data slot -- the same
    remainder the accumulation constraint debits at every block start.
    """
    if n_data_slots <= 0:
        return 0.0
    return max((n_data_slots - inp.switch_remainder) * inp.grid.slot_seconds, 0.0)


def block_frames(inp: SchedulerInput, t: int, n_data_slots: int) -> int:
    """Whole frames a block with ``n_data_slots`` integrating slots holds.

    A 20-minute block of 90 s subs with a 0.2 s download holds 13, where
    counting per slot says 4 x 3 = 12; 300 s subs in 5-minute slots hold one
    frame fewer than the block has slots, where counting per slot with a floor
    of one claimed a frame in every slot.
    """
    usable = _exposable_s(inp, n_data_slots)
    # The epsilon keeps an exact fit (20 min of 60 s + 15 s frames = 16) from
    # losing a frame to float rounding; it is far below one frame.
    return math.floor(usable / frame_period_s(inp, t) + 1e-9)


def min_data_slots(inp: SchedulerInput, t: int) -> int:
    """The shortest block ``t`` may be given, in data slots.

    ``min_block_slots``, lengthened until one block holds ``min_subs`` whole
    frames. More than ``n_slots`` means no block can ever hold them.
    """
    n = max(inp.min_block_slots, 1)
    while n <= inp.grid.n_slots and block_frames(inp, t, n) < inp.min_subs:
        n += 1
    return n


def block_ref_seconds(inp: SchedulerInput, t: int, data_slots: Sequence[int]) -> float:
    """Reference-seconds delivered by the frames a block lists.

    The optimizer credits ``slot_seconds * eta`` per data slot, less the slew
    remainder: back-to-back frames for the whole exposable time. The block
    lists only the whole frames that fit, so that credit is scaled by the
    fraction of the exposable time those frames cover. At steady conditions
    the result is exactly ``n_subs * t_sub`` seconds of open shutter, which is
    what makes the SNR quoted beside "N x t_sub" the SNR those N frames give.
    """
    data = list(data_slots)
    usable = _exposable_s(inp, len(data))
    if usable <= 0.0:
        return 0.0
    slot_s = inp.grid.slot_seconds
    credit = slot_s * (
        float(np.sum(inp.eta[t, data])) - inp.switch_remainder * float(inp.eta[t, data[0]])
    )
    listed = block_frames(inp, t, len(data)) * frame_period_s(inp, t)
    return max(credit, 0.0) * listed / usable


def block_expected_snr(inp: SchedulerInput, t: int, data_slots: Sequence[int]) -> float:
    """SNR delivered by one block's listed frames, on its own.

    Exact rather than approximate: SNR^2 is additive across sub-exposures, so a
    block that accumulates ``acc`` reference-seconds against a requirement of
    ``E_t = goal^2 / rho_ref`` delivers ``goal * sqrt(acc / E_t)``. Summing
    several blocks' SNRs therefore has to happen in quadrature, not linearly.
    """
    need = float(inp.required_ref_seconds[t])
    if need <= 0 or inp.snr_goal is None:
        return 0.0
    acc = block_ref_seconds(inp, t, data_slots)
    return float(inp.snr_goal[t]) * float(np.sqrt(max(acc, 0.0) / need))


def listed_ref_seconds(
    inp: SchedulerInput, assignments: Sequence[Assignment]
) -> NDArray[np.float64]:
    """(T,) reference-seconds each target's listed frames deliver over a plan.

    History and future alike, block by block, exactly as the blocks are cut:
    the figure a progress bar should show, and the one whose quadrature split
    the block cards' expected SNRs are.
    """
    out = np.zeros(len(inp.target_ids))
    index_of = {tid: t for t, tid in enumerate(inp.target_ids)}
    targets = [a.target_id for a in assignments]
    kinds = [a.kind for a in assignments]
    for tid, _start, _end, data in _runs(targets, kinds):
        t = index_of.get(tid)
        if t is not None:
            out[t] += block_ref_seconds(inp, t, data)
    return out


def _history(
    inp: SchedulerInput, s0: int, observing: frozenset[int]
) -> tuple[list[str | None], list[SlotKind]]:
    """The whole night's slots with history replayed from the record and every
    slot from ``s0`` on left idle. History never enters the model, so it is
    never read back out of the solver either."""
    n_s = inp.grid.n_slots
    slot_target: list[str | None] = [None] * n_s
    slot_kind: list[SlotKind] = [SlotKind.IDLE] * n_s
    for s in range(min(s0, n_s)):
        tid = inp.locked.get(s)
        slot_target[s] = tid
        if tid is not None:
            slot_kind[s] = SlotKind.OBSERVE if s in observing else SlotKind.SWITCH
    return slot_target, slot_kind


def _runs(
    slot_target: Sequence[str | None], slot_kind: Sequence[SlotKind]
) -> Iterator[tuple[str, int, int, list[int]]]:
    """Maximal runs of one target: ``(target_id, start, end, data_slots)``,
    ``end`` exclusive. A run is a block as the observer experiences it."""
    n = len(slot_target)
    s = 0
    while s < n:
        tid = slot_target[s]
        if tid is None:
            s += 1
            continue
        end = s
        while end < n and slot_target[end] == tid:
            end += 1
        yield tid, s, end, [k for k in range(s, end) if slot_kind[k] is SlotKind.OBSERVE]
        s = end


def _assemble(
    inp: SchedulerInput,
    slot_target: Sequence[str | None],
    slot_kind: Sequence[SlotKind],
    s0: int,
) -> tuple[tuple[Assignment, ...], tuple[Block, ...]]:
    """Every slot of the night as an Assignment, and the runs as Blocks."""
    n_s = inp.grid.n_slots
    assignments = tuple(
        Assignment(slot=s, kind=slot_kind[s], target_id=slot_target[s], locked=s < s0)
        for s in range(n_s)
    )
    index_of = {tid: t for t, tid in enumerate(inp.target_ids)}
    blocks: list[Block] = []
    for tid, start, end, data in _runs(slot_target, slot_kind):
        t = index_of.get(tid)
        if t is None or not data:
            # A run that never integrates is an abandoned slew, not a block.
            # It happens at a re-plan boundary: the observer began slewing to a
            # target, and by the next decision point the plan wants that target
            # later (or not at all). Emitting it as a Block produced instruction
            # cards reading "0 x 90 s, expected SNR 0", which is noise.
            # The slot assignments keep the record -- those minutes really were
            # spent -- so nothing is hidden by leaving it out of the blocks.
            continue
        blocks.append(
            Block(
                target_id=tid,
                slot_start=start,
                slot_end=end,
                switch_slots=end - start - len(data),
                n_subs=block_frames(inp, t, len(data)),
                t_sub_s=float(inp.t_sub_s[t]),
                expected_snr=block_expected_snr(inp, t, data),
            )
        )
    return assignments, tuple(blocks)


def _hm(minutes: float) -> str:
    """A duration written the way the rest of the interface writes one."""
    if not math.isfinite(minutes):
        return "more time than any night holds"
    m = round(minutes)
    if m < 60:
        return f"{m} min"
    h, rem = divmod(m, 60)
    return f"{h} h" if rem == 0 else f"{h} h {rem} min"


def _usable_span(inp: SchedulerInput, t: int, s0: int) -> tuple[int, int]:
    """``(usable slots, longest contiguous usable run)`` from ``s0`` on.

    Usable is the model's own presolve mask: above the altitude floor, clear of
    the Moon, and with a positive efficiency. The longest RUN is what decides
    whether a block can start, so a target with plenty of scattered minutes and
    no single stretch is told about the stretch rather than the total, which
    would read as though the time were there and the solver had wasted it.
    """
    ok = inp.visible[t, s0:] & (inp.eta[t, s0:] > 0.0)
    best = run = 0
    for v in ok.tolist():
        run = run + 1 if v else 0
        best = max(best, run)
    return int(np.count_nonzero(ok)), best


def _best_case_minutes(inp: SchedulerInput, t: int, s0: int) -> float:
    """Minutes of integrating to complete ``t`` at its best efficiency tonight.

    A floor, never an estimate: it prices every minute at the single best slot
    the target has all night, which no real block achieves, and it ignores the
    slew. That is why the message says "at least" -- quoting it as the cost
    would understate every target, and an observer who trimmed the list by
    exactly this much would still not fit.
    """
    eta = inp.eta[t, s0:]
    best = float(np.max(eta)) if eta.size else 0.0
    if best <= 0.0:
        return math.inf
    need = float(inp.required_ref_seconds[t]) * (1.0 + inp.completion_margin)
    return need / best / 60.0


def _drop_reason(
    inp: SchedulerInput,
    t: int,
    *,
    s0: int,
    no_window: bool,
    min_data: int,
    n_included: int,
) -> DropReason:
    """Why a target got no time, stated about the NIGHT rather than the solve.

    Three genuinely different failures used to share one sentence, and two of
    them shared it wrongly. A target that never rises was told "no usable
    25-minute window left tonight", which reads as a near miss -- it sent the
    observer hunting for a gap that does not exist at this latitude -- and a
    target the night simply could not afford was told "did not fit; see
    explain() for the counterfactual", which is a note to a developer.

    They are separated because the remedy differs: come back in six months,
    image it on a longer night, or drop something else. Every figure quoted
    comes from the inputs the model actually solved, so none of it can drift
    away from the plan it explains.
    """
    tid = inp.target_ids[t]
    minute = inp.grid.slot_minutes
    total, longest = _usable_span(inp, t, s0)

    if total == 0:
        return DropReason(
            tid,
            "never_up",
            "cannot be scheduled: it is never both above the altitude floor and clear "
            "of the Moon while the sky is dark tonight",
        )
    if no_window:
        return _no_window_reason(inp, t, min_data, longest)

    at_least = _hm(_best_case_minutes(inp, t, s0))
    left = _hm((inp.grid.n_slots - s0) * minute)
    goal = "" if inp.snr_goal is None else f" to reach SNR {float(inp.snr_goal[t]):.0f}"
    if n_included == 0:
        tail = f"and only {left} of tonight is left"
    elif n_included == 1:
        tail = f"and the one object already in the plan uses the {left} left tonight"
    else:
        tail = f"and the {n_included} objects already in the plan use the {left} left tonight"
    return DropReason(
        tid,
        "outranked",
        f"cannot be scheduled: it needs at least {at_least} of integrating{goal}, {tail}",
    )


def _no_window_reason(inp: SchedulerInput, t: int, min_data: int, longest: int) -> DropReason:
    """Why a target that IS up, but never for long enough, was left out.

    The minimum block is lengthened to hold ``min_subs`` whole frames rather
    than listing frames that cannot fit, so with long subs or a slow download
    the block a target needs can outgrow every window it has. Say so, with the
    numbers, rather than calling it outranked.
    """
    minute = inp.grid.slot_minutes
    if min_data > inp.grid.n_slots:
        # min_data_slots stops counting at the end of the night, so on a night
        # shorter than the frames it would under-state them. Say the real figure.
        slots = inp.min_subs * frame_period_s(inp, t) / inp.grid.slot_seconds
        if math.isfinite(slots):
            min_data = max(min_data, math.ceil(slots + inp.switch_remainder - 1e-9))
    total = (min_data + inp.switch_slots) * minute
    if min_data > max(inp.min_block_slots, 1):
        why = (
            f"{inp.min_subs} frames of {float(inp.t_sub_s[t]):.0f} s plus download need "
            f"{min_data * minute:.0f} min of integrating"
        )
    else:
        why = f"the minimum block is {min_data * minute:.0f} min of integrating"
    return DropReason(
        inp.target_ids[t],
        "no_window",
        f"cannot be scheduled: no usable {total:.0f}-minute window left tonight -- its "
        f"longest is {longest * minute:.0f} min: {why}, plus the slew",
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
    *,
    s0: int,
    observing: frozenset[int],
    no_window: frozenset[int] = frozenset(),
    min_data: Sequence[int] = (),
) -> Plan:
    n_t, n_s = len(inp.target_ids), inp.grid.n_slots
    included = {inp.target_ids[t] for t in range(n_t) if solver.value(u[t])}

    # History is replayed from the record, not read back out of the solver --
    # it never entered the model.
    slot_target, slot_kind = _history(inp, s0, observing)
    for s in range(s0, n_s):
        for t in range(n_t):
            if (t, s) in z and solver.value(z[t, s]):
                slot_target[s] = inp.target_ids[t]
                slot_kind[s] = SlotKind.OBSERVE if solver.value(x[t, s]) else SlotKind.SWITCH
                break

    assignments, blocks = _assemble(inp, slot_target, slot_kind, s0)

    dropped = tuple(
        _drop_reason(
            inp,
            t,
            s0=s0,
            no_window=t in no_window and t < len(min_data),
            min_data=min_data[t] if t < len(min_data) else max(inp.min_block_slots, 1),
            n_included=len(included),
        )
        for t, tid in enumerate(inp.target_ids)
        if tid not in included
    )

    return Plan(
        grid=inp.grid,
        as_of=as_of,
        assignments=assignments,
        blocks=blocks,
        included=frozenset(included),
        dropped=dropped,
        objective=float(solver.objective_value),
        status=status_name,
        solve_ms=elapsed_ms,
        best_bound=float(solver.best_objective_bound),
        ledger=ledger,
    )
