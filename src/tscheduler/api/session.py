"""A night, folded once.

The naive reading of a replay slider is "re-solve at T on every drag". That is
both slow and *wrong*: the plan at T depends on the history of accepted
decisions -- which slots are already history, what the observer was mid-way
through -- not on T alone. So replay is a fold over the event prefix, and the
slider indexes into the result.

The set of instants at which visible data changes is finite, known in advance
and small: the session start plus every forecast publication inside the window.
Between two consecutive instants the plan is identical by construction, which
is what ``valid_from``/``valid_until`` express on the wire.

Each step locks the slots already past. Without that the "replay" would rewrite
history every time a forecast arrived -- which is not only wrong but the
easiest way to accidentally manufacture a flattering result, since a scheduler
allowed to re-do the first half of the night with second-half information will
always look clairvoyant.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np
from numpy.typing import NDArray

from tscheduler.api.presets import EquipmentPreset, SitePreset
from tscheduler.api.weather import synthetic_forecast
from tscheduler.core.clock import AsOf
from tscheduler.domain.plan import Plan, SlotKind
from tscheduler.physics.geometry import NightGeometry
from tscheduler.pipeline.builder import SessionSpec, build_geometry, build_scheduler_input
from tscheduler.providers.weather.base import WeatherForecastProvider
from tscheduler.providers.weather.model import WeatherQuery
from tscheduler.providers.weather.open_meteo import OpenMeteoReplayForecast
from tscheduler.scheduling.cpsat import SchedulerInput, SolveOptions, build_and_solve

#: The fold MUST be deterministic. CP-SAT with a wall-clock limit and more than
#: one worker returns different equal-value optima run to run, which shows up as
#: phantom churn on the timeline and makes every stability number noise.
DETERMINISTIC = SolveOptions(deterministic=True, max_deterministic_time=4.0, random_seed=1)


@dataclass(frozen=True, slots=True)
class DecisionPoint:
    """One instant at which the observer could be handed a new plan."""

    index: int
    at: datetime
    reason: str
    plan: Plan
    inp: SchedulerInput
    cloud_fraction: NDArray[np.float64]
    seeing_fwhm_arcsec: NDArray[np.float64]
    locked_through_slot: int
    changed_slots: int
    added_targets: tuple[str, ...]
    dropped_targets: tuple[str, ...]
    past_slots_rewritten: int

    @property
    def plan_id(self) -> str:
        return self.plan.fingerprint()[:16]


class SessionStatus:
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"


@dataclass
class NightSession:
    """One folded night. Immutable once ``status`` reaches ready."""

    id: str
    name: str
    created_at: datetime
    spec: SessionSpec
    site_preset: SitePreset
    equipment_preset: EquipmentPreset
    weather_source: str
    solve_seconds: float

    geometry: NightGeometry | None = None
    decision_points: tuple[DecisionPoint, ...] = ()
    status: str = SessionStatus.BUILDING
    progress: float = 0.0
    message: str = "queued"
    error: str | None = None
    fold_seconds: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- lookup ------------------------------------------------------------

    def decision_index_for(self, as_of: datetime) -> int:
        """The last decision point at or before ``as_of`` -- a bisect, not a solve.

        Before the first point the answer is still index 0: a session start is
        the plan you were handed at dusk, and there is no earlier one to show.
        """
        t = as_of.astimezone(UTC)
        idx = 0
        for dp in self.decision_points:
            if dp.at <= t:
                idx = dp.index
            else:
                break
        return idx

    def validity(self, index: int) -> tuple[datetime, datetime]:
        """The interval over which this plan is the current one."""
        dps = self.decision_points
        start = dps[index].at if index > 0 else self.spec.grid.start
        end = dps[index + 1].at if index + 1 < len(dps) else self.spec.grid.end
        return start, end

    @property
    def distinct_plans(self) -> int:
        return len({dp.plan_id for dp in self.decision_points})


def build_provider(spec: SessionSpec, weather_source: str) -> WeatherForecastProvider:
    if weather_source == "open_meteo":
        return OpenMeteoReplayForecast()
    return synthetic_forecast(spec.grid)


def fold_night(
    session: NightSession,
    *,
    on_progress: Callable[[float, str], None] | None = None,
) -> None:
    """Walk the night's publication timeline, re-planning at each instant.

    Mutates ``session`` in place and is the only writer; the HTTP layer reads
    ``decision_points`` and never appends to it.
    """
    t_fold = time.perf_counter()

    def report(frac: float, msg: str) -> None:
        session.progress = frac
        session.message = msg
        if on_progress is not None:
            on_progress(frac, msg)

    try:
        spec = session.spec
        grid = spec.grid
        report(0.02, "computing night geometry")
        geo = build_geometry(spec)
        session.geometry = geo

        report(0.10, f"fetching {session.weather_source} forecast runs")
        provider = build_provider(spec, session.weather_source)
        query = WeatherQuery(
            lat=spec.site.latitude_deg,
            lon=spec.site.longitude_deg,
            valid_from=grid.start,
            valid_to=grid.end,
        )

        # Enumerating *when* data arrives is not reading it. Every plan below is
        # still built at its own as_of, which re-applies the publication gate.
        horizon = AsOf.at(grid.end)
        pubs = provider.publication_times(query, horizon)
        during = [p for p in pubs if grid.start < p < grid.end]
        instants = [grid.start, *during]
        report(0.15, f"{len(instants)} decision point(s) in the session")

        opts = SolveOptions(
            deterministic=True,
            max_deterministic_time=session.solve_seconds,
            random_seed=DETERMINISTIC.random_seed,
        )

        points: list[DecisionPoint] = []
        prev: Plan | None = None
        for i, t in enumerate(instants):
            as_of = AsOf.at(t)
            first_free = grid.index_of(t) if t > grid.start else 0

            locked: dict[int, str | None] = {}
            locked_observing: frozenset[int] = frozenset()
            previous: dict[int, str | None] = {}
            if prev is not None:
                locked = {s: prev.assignments[s].target_id for s in range(first_free)}
                # A slew slot is assigned to its target but collects nothing.
                # Counting it as banked progress over-credits every re-plan.
                locked_observing = frozenset(
                    s for s in range(first_free) if prev.assignments[s].kind is SlotKind.OBSERVE
                )
                previous = {
                    s: prev.assignments[s].target_id for s in range(first_free, grid.n_slots)
                }

            inp, ledger = build_scheduler_input(
                spec,
                geo,
                provider,
                as_of,
                locked=locked,
                locked_observing=locked_observing,
                previous_plan=previous,
                first_free_slot=first_free,
            )
            plan = build_and_solve(inp, as_of, opts, ledger)
            cloud, seeing, _ = provider.series(query, as_of, grid)

            changed = rewritten = 0
            added: tuple[str, ...] = ()
            gone: tuple[str, ...] = ()
            if prev is not None and plan.assignments:
                changed = sum(
                    1
                    for s in range(first_free, grid.n_slots)
                    if plan.assignments[s].target_id != prev.assignments[s].target_id
                )
                rewritten = sum(
                    1
                    for s in range(first_free)
                    if plan.assignments[s].target_id != prev.assignments[s].target_id
                )
                added = tuple(sorted(plan.included - prev.included))
                gone = tuple(sorted(prev.included - plan.included))

            newest = ledger.max_published
            reason = (
                "session start"
                if i == 0
                else f"forecast run published {newest:%H:%M} UTC arrived"
                if newest is not None
                else "new data arrived"
            )

            points.append(
                DecisionPoint(
                    index=i,
                    at=t,
                    reason=reason,
                    plan=plan,
                    inp=inp,
                    cloud_fraction=cloud,
                    seeing_fwhm_arcsec=seeing,
                    locked_through_slot=first_free,
                    changed_slots=changed,
                    added_targets=added,
                    dropped_targets=gone,
                    past_slots_rewritten=rewritten,
                )
            )
            prev = plan
            with session._lock:
                session.decision_points = tuple(points)
            report(0.15 + 0.85 * (i + 1) / len(instants), f"solved {i + 1}/{len(instants)}")

        session.fold_seconds = time.perf_counter() - t_fold
        session.status = SessionStatus.READY
        report(
            1.0, f"ready: {len(points)} decision points, {session.distinct_plans} distinct plans"
        )
    except Exception as exc:  # surfaced to the client verbatim, never swallowed
        session.status = SessionStatus.FAILED
        session.error = f"{type(exc).__name__}: {exc}"
        session.fold_seconds = time.perf_counter() - t_fold
        report(1.0, f"failed: {session.error}")


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]


def night_window(date: str, start_hour_utc: float, hours: float) -> tuple[datetime, datetime]:
    day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC)
    start = day + timedelta(hours=start_hour_utc)
    return start, start + timedelta(hours=hours)
