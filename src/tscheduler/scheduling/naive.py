"""The naive baseline.

Deliberately NOT a straw man. It consumes the same eta, visibility, E_t, minimum
block length, switch reservation and frame-count rules as the optimizer -- the
only thing withheld is optimisation. A baseline that ignored the operational
constraints would make the comparison measure our code structure rather than our
scheduling policy, and any resulting improvement would be meaningless.
"""

from __future__ import annotations

import time

import numpy as np

from tscheduler.core.clock import AsOf
from tscheduler.domain.plan import Assignment, Block, DropReason, Plan, SlotKind
from tscheduler.providers.base import EvidenceLedger
from tscheduler.scheduling.cpsat import SchedulerInput, block_expected_snr, block_frames


def solve_naive(
    inp: SchedulerInput,
    as_of: AsOf,
    *,
    order: str = "input",
    ledger: EvidenceLedger | None = None,
) -> Plan:
    """Observe targets in order, taking the first that is visible for long enough.

    ``order='input'``   user list order -- what an observer with a printed list does.
    ``order='priority'`` highest weight first -- what a careful observer does.
    """
    t0 = time.perf_counter()
    n_t, n_s = len(inp.target_ids), inp.grid.n_slots
    w, min_block = inp.switch_slots, inp.min_block_slots
    need_run = min_block + w

    idx = list(range(n_t))
    if order == "priority":
        idx.sort(key=lambda t: -float(inp.weight[t]))

    feasible = inp.visible & (inp.eta > 0.0)
    remaining = {t: float(inp.required_ref_seconds[t]) for t in range(n_t)}
    slot_target: list[str | None] = [None] * n_s
    slot_kind: list[SlotKind] = [SlotKind.IDLE] * n_s
    completed: set[str] = set()

    s = 0
    while s < n_s:
        pick = None
        for t in idx:
            if remaining[t] <= 0.0:
                continue
            if s + need_run > n_s:
                continue
            if not np.all(feasible[t, s : s + need_run]):
                continue
            pick = t
            break
        if pick is None:
            s += 1
            continue

        # Reserve the slew, then integrate until done or no longer feasible,
        # honouring the same minimum block length the optimizer must obey.
        for j in range(w):
            slot_target[s + j] = inp.target_ids[pick]
            slot_kind[s + j] = SlotKind.SWITCH
        s += w

        run = 0
        while s < n_s and feasible[pick, s] and (remaining[pick] > 0.0 or run < min_block):
            slot_target[s] = inp.target_ids[pick]
            slot_kind[s] = SlotKind.OBSERVE
            remaining[pick] -= float(inp.eta[pick, s]) * inp.grid.slot_seconds
            run += 1
            s += 1
        if remaining[pick] <= 0.0:
            completed.add(inp.target_ids[pick])

    assignments = tuple(
        Assignment(slot=i, kind=slot_kind[i], target_id=slot_target[i], locked=i in inp.locked)
        for i in range(n_s)
    )

    blocks: list[Block] = []
    i = 0
    while i < n_s:
        tid = slot_target[i]
        if tid is None:
            i += 1
            continue
        end = i
        while end < n_s and slot_target[end] == tid:
            end += 1
        t = inp.target_ids.index(tid)
        data = [k for k in range(i, end) if slot_kind[k] is SlotKind.OBSERVE]
        if not data:
            # See cpsat._extract: a run with no integrating slot is an
            # abandoned slew, not a block.
            i = end
            continue
        blocks.append(
            Block(
                target_id=tid,
                slot_start=i,
                slot_end=end,
                switch_slots=end - i - len(data),
                # Per block, as the optimizer's blocks count them.
                n_subs=block_frames(inp, t, len(data)),
                t_sub_s=float(inp.t_sub_s[t]),
                expected_snr=block_expected_snr(inp, t, data),
            )
        )
        i = end

    dropped = tuple(
        DropReason(tid, "naive_not_reached", "list order never reached it before dawn")
        for tid in inp.target_ids
        if tid not in completed
    )
    return Plan(
        grid=inp.grid,
        as_of=as_of,
        assignments=assignments,
        blocks=tuple(blocks),
        included=frozenset(completed),
        dropped=dropped,
        objective=0.0,
        status="NAIVE",
        solve_ms=(time.perf_counter() - t0) * 1000.0,
        ledger=ledger or EvidenceLedger(),
    )
