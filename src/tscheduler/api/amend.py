"""Adding an object to a night that is already planned.

Two cases, because "re-plan from when" has two honest answers:

* **A night still happening is amended from NOW.** The cursor may be
  reviewing the past, and re-planning from a past instant would rewrite a plan
  the observer was already handed. So one decision point is appended, exactly
  as the live watch appends one when a forecast arrives. Before dusk nobody has
  been handed anything yet, so the opening plan is re-made instead.
* **A night that is over is amended from the cursor.** A decision point is
  added at that instant and every later forecast arrival is re-solved on top
  of it, so the rest of the night sees the new target too. The past before the
  cursor is locked, as at every decision point.

The session's spec is frozen and its geometry is computed for its targets, so
both are rebuilt with the target APPENDED, never inserted: every existing
decision point addresses targets by their position in the spec it was solved
with, and appending keeps those positions valid.

Adding is a request, not an order. The target joins with the session's highest
priority and then some, so the optimiser strongly prefers it -- but a target
that never clears the altitude floor tonight still cannot be given time.

An add the optimiser cannot place is REFUSED, and the night is left exactly as
it was: no new target in the spec, no new decision point, no new geometry row.
The alternative -- keeping it as an unscheduled target -- means a click that
says "could not be scheduled" quietly changes the night anyway, and leaves the
observer to undo something they were told had not happened. The reason travels
with the refusal instead (``InfeasibleError``), so the caller can say why.

Everything is solved against a working copy; the session changes in one step
under its lock, so a reader never sees a new spec beside an old geometry. That
is also what makes the refusal free: nothing is written until the plan is known
to be worth writing.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import numpy as np

from tscheduler import catalog
from tscheduler.api import presets
from tscheduler.api.session import (
    DecisionPoint,
    NightSession,
    SessionStatus,
    build_provider,
    solve_options,
    solve_step,
    weather_query,
)
from tscheduler.core.clock import AsOf
from tscheduler.core.timegrid import TimeGrid
from tscheduler.domain.plan import Block
from tscheduler.domain.targets import Target
from tscheduler.pipeline.builder import build_geometry
from tscheduler.providers.weather.auto import resolve_weather
from tscheduler.providers.weather.base import WeatherForecastProvider
from tscheduler.scheduling.cpsat import SolveOptions

#: How strongly an added target is preferred, relative to the session's best.
PRIORITY_BOOST = 1.5


class AmendError(ValueError):
    """The request cannot be applied to this session as it stands."""


class InfeasibleError(AmendError):
    """The target was plannable, and the optimiser still gave it no time.

    Distinct from its parent because nothing was wrong with the REQUEST: the
    night simply has no room for it. The session is unchanged either way.
    """


class UnplannableError(AmendError):
    """Nothing the request, the planets or the catalogue can name an object.

    Also distinct from its parent, and in the other direction: every other
    refusal here is a conflict with the night as it stands (409), while this
    one is a request that could not be understood at all (422).
    """


#: Reported by an amendment as it solves, as ``(fraction, message)`` with the
#: fraction local to the amendment. Nothing here knows how long the caller
#: thinks an amendment should take relative to whatever else it is doing.
AmendProgress = Callable[[float, str], None]


def resolve_addition(
    sess: NightSession,
    *,
    target: str,
    ra_deg: float | None = None,
    dec_deg: float | None = None,
    magnitude: float | None = None,
    name: str | None = None,
    is_point_source: bool = False,
) -> Target:
    """The object an add names, however it names it. Resolves; adds nothing.

    Lives beside the add rather than in the HTTP layer because two endpoints
    ask this same question -- the stateful ``POST /sessions/{id}/targets`` and
    the stateless fold-and-amend -- and one wording per refusal is the whole
    point of the module it sits in.
    """
    given = (ra_deg, dec_deg, magnitude)
    if any(v is not None for v in given):
        # Own coordinates win over every lookup: they are how something the
        # server has never heard of is added at all -- a star, which exists
        # only in the browser's own catalogue. Partial coordinates are a client
        # bug, not a reason to fall back to a name search that would resolve to
        # something else entirely.
        if ra_deg is None or dec_deg is None or magnitude is None:
            raise UnplannableError("raDeg, decDeg and magnitude must be given together")
        return Target(
            id=target,
            name=name or target,
            ra_deg=float(ra_deg),
            dec_deg=float(dec_deg),
            magnitude=float(magnitude),
            is_point_source=is_point_source,
        )

    known = next((t for t in sess.spec.targets if t.id == target), None)
    # Planets BEFORE the catalogue, and the order is not arbitrary:
    # `catalog.resolve("saturn")` finds the Saturn Nebula, so looking the
    # catalogue up first swaps the planet for a planetary nebula without saying
    # so. Exact body names only, which leaves "saturn nebula" and "ngc7009"
    # resolving where they should.
    found = (
        known
        or presets.planet_target(sess.spec.site, sess.spec.grid, target)
        or presets.catalog_target(target)
    )
    if found is None:
        raise UnplannableError(catalog.refusal(target) or f"{target!r} is not in the catalogue")
    return found


def is_amendment(reason: str) -> bool:
    """True for a decision point this module made.

    Its reason is the only mark it carries -- ``DecisionPoint`` has no field
    for who asked for a re-plan -- so the wording is owned here, next to the
    three places that write it, and nothing else may parse it.
    """
    return reason.startswith(("added ", "opening plan, with ")) or "; added " in reason


@dataclass(frozen=True, slots=True)
class AmendResult:
    """A successful add. An unsuccessful one raises instead of returning."""

    target: Target
    decision: DecisionPoint
    """The decision point at which the amended plan takes over."""
    message: str
    """Where the plan gives it time, in words."""


def add_target(
    sess: NightSession,
    target: Target,
    *,
    at: datetime | None = None,
    now: AsOf | None = None,
    on_progress: AmendProgress | None = None,
) -> AmendResult:
    """Add ``target`` to the night and re-plan. Blocking: it runs CP-SAT.

    On a live night the watch's lock is held throughout, so a scheduled check
    cannot append a decision point in the middle and be overwritten.

    ``on_progress`` is called as each re-plan lands. An amendment is one solve
    on a live night and one per remaining decision point on a replay, so how
    long it takes is not something a caller can guess from the request.
    """
    if sess.status != SessionStatus.READY or not sess.decision_points or sess.geometry is None:
        raise AmendError("the night is still being planned; try again when it is ready")
    # The amend lock covers the whole of `_amend` on EVERY night: the spec it
    # appends to is read at the top and committed at the bottom, seconds of
    # CP-SAT apart. The live lock is still taken inside it, so a scheduled
    # check cannot append a decision point in the middle either.
    with sess._amend_lock:
        guard = sess.live.lock if sess.live is not None else nullcontext()
        with guard:
            return _amend(sess, target, at=at, now=now or AsOf.live(), on_progress=on_progress)


def _amend(
    sess: NightSession,
    target: Target,
    *,
    at: datetime | None,
    now: AsOf,
    on_progress: AmendProgress | None = None,
) -> AmendResult:
    spec = sess.spec
    grid = spec.grid
    dps = sess.decision_points
    live_now = sess.live is not None and sess.live.following and now.t < grid.end
    start = max(now.t, grid.start) if live_now else _clamp(at or grid.start, grid)

    # "Already in the plan" means it still has time AHEAD. A target whose
    # blocks are all behind the cursor can be asked for again.
    in_force = dps[sess.decision_index_for(start)]
    here = grid.index_of(start) if start > grid.start else 0
    if any(b.target_id == target.id and b.slot_end > here for b in in_force.plan.blocks):
        raise AmendError(f"{target.name} is already in the plan")

    top = max((t.priority for t in spec.targets), default=1.0)
    boost = max(target.priority, top * PRIORITY_BOOST)
    ids = [t.id for t in spec.targets]
    if target.id in ids:
        # Already a target of the night, just not scheduled: ask for it harder.
        i = ids.index(target.id)
        targets = (
            *spec.targets[:i],
            replace(spec.targets[i], priority=boost),
            *spec.targets[i + 1 :],
        )
        name = spec.targets[i].name
    else:
        targets = (*spec.targets, replace(target, priority=boost))
        name = target.name
    new_spec = replace(spec, targets=targets)
    # Geometry depends only on WHICH targets there are, so a re-prioritised
    # target reuses it; a new one needs its rows computed.
    geometry = sess.geometry if target.id in ids else build_geometry(new_spec)

    work = copy.copy(sess)
    work.spec = new_spec
    work.geometry = geometry

    if live_now:
        weather = resolve_weather(sess.weather.requested, grid.start, grid.end, now.t)
        provider = build_provider(new_spec, sess.weather_source, resolution=weather, now=now)
    else:
        weather = sess.weather
        provider = build_provider(new_spec, sess.weather_source, resolution=weather, now=sess.now)
    # Loads the records, as the fold does before its first solve.
    provider.publication_times(weather_query(new_spec), AsOf.at(grid.end))
    opts = solve_options(sess)

    if live_now and now.t >= grid.start:
        last = dps[-1]
        when = max(now.t, last.at + timedelta(seconds=1))
        taking = solve_step(
            work,
            provider,
            index=len(dps),
            at=when,
            prev=last.plan,
            opts=opts,
            reason=f"added {name}",
        )
        points = (*dps, taking)
        if on_progress is not None:
            on_progress(1.0, f"re-planned the rest of the night around {name}")
    else:
        # Before dusk on a live night, or a replay: re-plan from `start` and
        # re-solve every later arrival on the new chain.
        early = now.t if live_now else None
        points, taking = _refold(work, provider, dps, start, name, opts, early, on_progress)

    # Time AHEAD, not merely `included`. A target whose observing is already
    # finished behind the cursor satisfies its SNR goal out of banked history,
    # so the solver can include it for free while giving it not one new slot:
    # re-adding a target completed at 21:00 would otherwise answer 200 with
    # "scheduled 20:00-21:30 UTC", two spans in the past, and append a
    # decision point whose plan is identical to the one before it.
    free = taking.inp.first_free_slot
    gained = tuple(b for b in taking.plan.blocks if b.target_id == target.id and b.slot_end > free)
    if not gained:
        # Nothing above has touched `sess` -- every solve ran against `work`,
        # a copy -- so declining to commit here IS the rollback.
        raise InfeasibleError(_refusal(taking, target.id, name))
    message = _scheduled_message(taking, gained, name)

    with sess._lock:
        sess.spec = new_spec
        sess.geometry = geometry
        sess.decision_points = points
        if live_now:
            sess.weather = weather
    if sess.live is not None:
        sess.live.revision += 1
    return AmendResult(target=replace(target, name=name), decision=taking, message=message)


def _clamp(t: datetime, grid: TimeGrid) -> datetime:
    return min(max(t, grid.start), grid.end - timedelta(seconds=1))


def _refold(
    work: NightSession,
    provider: WeatherForecastProvider,
    dps: tuple[DecisionPoint, ...],
    start: datetime,
    name: str,
    opts: SolveOptions,
    early: datetime | None,
    on_progress: AmendProgress | None = None,
) -> tuple[tuple[DecisionPoint, ...], DecisionPoint]:
    """New decision point at ``start``; every later one re-solved on top of it."""
    k = work.decision_index_for(start)
    if start <= dps[0].at:
        # At dusk: nothing has been handed over, so the opening plan is re-made.
        head: list[DecisionPoint] = []
        first = solve_step(
            work,
            provider,
            index=0,
            at=dps[0].at,
            prev=None,
            opts=opts,
            reason=f"opening plan, with {name} added",
            data_as_of=early,
        )
        rest = dps[1:]
    else:
        # A decision point already AT this instant is replaced rather than
        # shadowed: two points at one instant would leave one never in force.
        same = dps[k].at == start
        head = list(dps[:k] if same else dps[: k + 1])
        reason = f"{dps[k].reason}; added {name}" if same else f"added {name}"
        first = solve_step(
            work,
            provider,
            index=len(head),
            at=start,
            prev=head[-1].plan,
            opts=opts,
            reason=reason,
        )
        rest = dps[k + 1 :]

    chain = [*head, first]
    # The first solve is the amendment; the rest carry it forward through the
    # forecast arrivals that follow it. Both are CP-SAT, so both are counted.
    total = 1 + len(rest)

    def report(done: int) -> None:
        if on_progress is not None:
            on_progress(done / total, f"re-planned {done}/{total} around {name}")

    report(1)
    for dp in rest:
        chain.append(
            solve_step(
                work,
                provider,
                index=len(chain),
                at=dp.at,
                prev=chain[-1].plan,
                opts=opts,
                reason=dp.reason,
            )
        )
        report(len(chain) - len(head))
    return tuple(chain), first


def _scheduled_message(dp: DecisionPoint, blocks: tuple[Block, ...], name: str) -> str:
    """Where the plan gives it time, in the observer's words.

    Only the blocks it GAINED, which is what the caller asked about. A target
    can also hold blocks behind the cursor, from an earlier night's worth of
    replay, and naming those would answer "when will I shoot it" with a time
    that has been and gone.
    """
    grid = dp.plan.grid
    spans = ", ".join(
        f"{grid.slot_start(b.slot_start):%H:%M}–{grid.slot_start(b.slot_end):%H:%M}"
        for b in blocks
    )
    return f"{name} is scheduled {spans} UTC"


def _refusal(dp: DecisionPoint, target_id: str, name: str) -> str:
    """Why the optimiser could not place it, in the observer's words.

    The solver reports every left-out target as "outranked", which is true but
    says nothing. The inputs it solved say more: whether the target is usable
    at all for the rest of the night, and if so how much of that there is.

    Written as a statement about the night rather than about the attempt --
    "it has 35 usable minutes", not "it was added but left out" -- because
    nothing was added: this sentence is what the observer is shown INSTEAD of
    a change to the plan.
    """
    # A solver that returned no plan at all dropped every target with the same
    # code, and none of them was outranked by anything. Saying "the targets
    # already planned gain more from that time" would be a fabricated reason
    # for a refusal that is really about the solve.
    dropped = next((d for d in dp.plan.dropped if d.target_id == target_id), None)
    if dropped is not None and dropped.code == "no_solution":
        return (
            f"{name} could not be added: the solver found no plan for the rest of the night "
            f"at all ({dropped.message}). Nothing has changed -- try again, or give the night "
            "more solve time."
        )
    inp = dp.inp
    i = inp.target_ids.index(target_id)
    free = inp.first_free_slot
    usable = int(np.count_nonzero(inp.visible[i, free:] & (inp.eta[i, free:] > 0)))
    if usable == 0:
        return (
            f"{name} cannot be observed after {dp.at:%H:%M} UTC: it never clears the altitude "
            "floor and the Moon's exclusion again tonight"
        )
    minutes = usable * inp.grid.slot_minutes
    return (
        f"{name} has only {minutes:.0f} usable minutes left tonight, and the targets already "
        "planned gain more from that time"
    )
