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

A night that has not ended is the exception to "folded once". Its fold stops at
the wall clock -- a publication that has not happened is not a decision point,
however confidently a source schedules it -- and ``api/live.py`` then extends it
one ``solve_step`` at a time as new data actually arrives.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

from tscheduler.api.presets import EquipmentPreset, SitePreset
from tscheduler.api.weather import synthetic_forecast
from tscheduler.core.clock import AsOf, Vantage
from tscheduler.domain.plan import Plan, SlotKind
from tscheduler.physics.geometry import NightGeometry
from tscheduler.pipeline.builder import SessionSpec, build_geometry, scheduler_input_from
from tscheduler.pipeline.conditions import ConditionsLayer, build_conditions
from tscheduler.providers.weather.auto import (
    FetchReport,
    NoForecast,
    OpenMeteoAuto,
    WeatherMode,
    WeatherResolution,
    resolve_weather,
)
from tscheduler.providers.weather.base import WeatherForecastProvider
from tscheduler.providers.weather.model import WeatherQuery
from tscheduler.providers.weather.open_meteo import LIVE_SOURCE_ID
from tscheduler.scheduling.cpsat import SchedulerInput, SolveOptions, build_and_solve

if TYPE_CHECKING:
    from tscheduler.api.live import LiveWatch

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
    conditions: ConditionsLayer
    """Everything the weather decided here, with its working kept.

    Held rather than discarded because the UI answers "why is this slot bad"
    from the sky decomposition and "why did the plan change" from the named
    preference factors, and both are exact arithmetic over these arrays. The
    optimizer only ever saw their product.
    """
    locked_through_slot: int
    changed_slots: int
    added_targets: tuple[str, ...]
    dropped_targets: tuple[str, ...]
    past_slots_rewritten: int
    report: FetchReport | None = None
    """What the weather fetch behind THIS plan did, when it was Open-Meteo.

    Per decision point, not per session, because a live night's watch fetches
    again and replaces the session's report: an earlier plan must keep naming
    the run it was actually built on, and keep its own caveats."""

    @property
    def plan_id(self) -> str:
        return self.plan.fingerprint()[:16]

    @property
    def cloud_fraction(self) -> NDArray[np.float64]:
        return self.conditions.cloud_fraction

    @property
    def seeing_fwhm_arcsec(self) -> NDArray[np.float64]:
        return self.conditions.seeing_fwhm_arcsec


class SessionStatus:
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"


@dataclass
class NightSession:
    """One folded night.

    Immutable once ``status`` reaches ready -- unless the night had not ended
    when it was created. Then ``live`` holds its watch, and ``decision_points``
    grows (always by whole-tuple replacement under ``_lock``) as new data
    arrives, until dawn.
    """

    id: str
    name: str
    created_at: datetime
    spec: SessionSpec
    site_preset: SitePreset
    equipment_preset: EquipmentPreset
    weather_source: str
    """The requested source, verbatim: auto | synthetic | open_meteo."""
    solve_seconds: float

    now: AsOf | None = None
    """The wall-clock instant the session was created -- the same instant as
    ``created_at``, from the API layer's one ``AsOf.live()``. It is the only
    "now" the weather path ever sees: it decides archive vs forecast, bounds
    which model runs may be requested, and stamps the live forecast."""
    weather: WeatherResolution = field(init=False)
    """Where the weather comes from, resolved from ``weather_source`` and
    ``now`` when the session is built. See ``weather_mode`` for the outcome."""
    weather_report: FetchReport | None = None
    """What the fetch actually did. Set by the fold; None for synthetic data."""

    geometry: NightGeometry | None = None
    decision_points: tuple[DecisionPoint, ...] = ()
    status: str = SessionStatus.BUILDING
    progress: float = 0.0
    message: str = "queued"
    error: str | None = None
    fold_seconds: float = 0.0
    live: LiveWatch | None = None
    """The watch keeping a night that is still happening current. None for a
    night that was over when the session was created. See ``api/live.py``."""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _amend_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    """Held for a whole amendment -- read, solve, commit -- by ``api/amend.py``.

    Separate from ``_lock``, which is taken for the microsecond a tuple is
    swapped and must never be held across a CP-SAT solve: readers take it too.
    Two adds racing without this each append their own target to the SAME base
    spec and the second commit silently discards the first, after both callers
    have been told 200.
    """

    def __post_init__(self) -> None:
        grid = self.spec.grid
        self.weather = resolve_weather(
            self.weather_source,
            grid.start,
            grid.end,
            self.now.t if self.now is not None else None,
        )

    # -- weather -----------------------------------------------------------

    @property
    def weather_mode(self) -> WeatherMode:
        """What actually happened, which is not always what was planned.

        A night resolved to ``forecast`` or ``archive`` whose fetch produced no
        record for ANY plan is ``unavailable``: the plans were built on the
        clear-sky assumption, and nothing may call that a forecast.
        """
        planned = self.weather.mode
        if planned in (WeatherMode.SYNTHETIC, WeatherMode.UNAVAILABLE):
            return planned
        if self.status == SessionStatus.READY and not any(
            dp.conditions.ledger.refs for dp in self.decision_points
        ):
            return WeatherMode.UNAVAILABLE
        return planned

    @property
    def weather_unavailable_reason(self) -> str | None:
        """The specific reason no forecast covers the night, when none does."""
        if self.weather_mode is not WeatherMode.UNAVAILABLE:
            return None
        if self.weather.reason:
            return self.weather.reason
        if self.weather_report is not None:
            return self.weather_report.empty_reason(self.spec.grid.start)
        return "no forecast record reached any plan"

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


def build_provider(
    spec: SessionSpec,
    weather_source: str,
    *,
    resolution: WeatherResolution | None = None,
    now: AsOf | None = None,
) -> WeatherForecastProvider:
    """The provider a resolution calls for. Constructs; fetches nothing.

    ``now`` is the session's creation instant. Without one, an archive replay
    is bounded by the end of the night instead (the old behaviour of the
    explicit ``open_meteo`` source), and a forecast cannot be served.
    """
    grid = spec.grid
    res = resolution or resolve_weather(
        weather_source, grid.start, grid.end, now.t if now is not None else None
    )
    if res.mode is WeatherMode.SYNTHETIC:
        return synthetic_forecast(grid)
    if res.mode is WeatherMode.ARCHIVE:
        bound = now if now is not None else AsOf.at(grid.end)
        return OpenMeteoAuto(bound, WeatherMode.ARCHIVE)
    if res.mode is WeatherMode.FORECAST and now is not None and now.vantage is Vantage.LIVE:
        return OpenMeteoAuto(now, WeatherMode.FORECAST)
    return NoForecast()


_FETCH_MESSAGE: dict[WeatherMode, str] = {
    WeatherMode.ARCHIVE: "fetching archived forecast runs",
    WeatherMode.FORECAST: "fetching the live forecast",
    WeatherMode.SYNTHETIC: "generating synthetic demo weather",
    WeatherMode.UNAVAILABLE: "no forecast covers this night; assuming a clear sky",
}


def _reason(i: int, newest: datetime | None, cond: ConditionsLayer) -> str:
    """Why the fold re-planned here, in the words the timeline shows."""
    if i == 0:
        return "session start"
    if newest is None:
        return "new data arrived"
    live = any(r.provider == LIVE_SOURCE_ID and r.published_at == newest for r in cond.ledger.refs)
    if live:
        return f"live forecast fetched {newest:%H:%M} UTC"
    return f"forecast run published {newest:%H:%M} UTC arrived"


def weather_query(spec: SessionSpec) -> WeatherQuery:
    """The one weather question a session asks: its site, over its night."""
    grid = spec.grid
    return WeatherQuery(
        lat=spec.site.latitude_deg,
        lon=spec.site.longitude_deg,
        valid_from=grid.start,
        valid_to=grid.end,
    )


def solve_options(session: NightSession) -> SolveOptions:
    return SolveOptions(
        deterministic=True,
        max_deterministic_time=session.solve_seconds,
        random_seed=DETERMINISTIC.random_seed,
    )


def solve_step(
    session: NightSession,
    provider: WeatherForecastProvider,
    *,
    index: int,
    at: datetime,
    prev: Plan | None,
    opts: SolveOptions,
    reason: str | None = None,
    data_as_of: datetime | None = None,
) -> DecisionPoint:
    """One decision point: the plan handed over at ``at``, given the last one.

    Every slot before the one containing ``at`` is locked to ``prev``, so a
    step can extend the night but never rewrite it. The fold calls this once
    per publication instant; the live watch calls it once per arrival.

    ``data_as_of`` gates the data more tightly than ``at``. It matters only
    for the opening plan of a night that has not started: that plan is handed
    over at dusk, but it can only be built from what exists at the wall
    clock, however much a scheduled source says will be published by dusk.
    """
    spec = session.spec
    grid = spec.grid
    geo = session.geometry
    if geo is None:
        raise RuntimeError("solve_step needs the night geometry; fold first")
    as_of = AsOf.at(at)
    data_gate = AsOf.at(min(at, data_as_of)) if data_as_of is not None else as_of
    first_free = grid.index_of(at) if at > grid.start else 0

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
        previous = {s: prev.assignments[s].target_id for s in range(first_free, grid.n_slots)}

    cond = build_conditions(spec, geo, provider, data_gate)
    inp = scheduler_input_from(
        spec,
        geo,
        cond,
        locked=locked,
        locked_observing=locked_observing,
        previous_plan=previous,
        first_free_slot=first_free,
    )
    plan = build_and_solve(inp, as_of, opts, cond.ledger)

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

    return DecisionPoint(
        index=index,
        at=at,
        reason=reason if reason is not None else _reason(index, cond.ledger.max_published, cond),
        plan=plan,
        inp=inp,
        conditions=cond,
        locked_through_slot=first_free,
        changed_slots=changed,
        added_targets=added,
        dropped_targets=gone,
        past_slots_rewritten=rewritten,
        report=provider.report if isinstance(provider, OpenMeteoAuto) else None,
    )


def fold_night(
    session: NightSession,
    *,
    on_progress: Callable[[float, str], None] | None = None,
) -> None:
    """Walk the night's publication timeline, re-planning at each instant.

    Mutates ``session`` in place and is the only writer while it runs; the
    HTTP layer reads ``decision_points`` and never appends to it. For a night
    that has not ended, the timeline stops at the wall clock.
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

        report(0.10, _FETCH_MESSAGE[session.weather.mode])
        provider = build_provider(
            spec, session.weather_source, resolution=session.weather, now=session.now
        )
        query = weather_query(spec)

        # Enumerating *when* data arrives is not reading it. Every plan below is
        # still built at its own as_of, which re-applies the publication gate.
        horizon = AsOf.at(grid.end)
        pubs = provider.publication_times(query, horizon)
        if isinstance(provider, OpenMeteoAuto):
            session.weather_report = provider.report
        during = [p for p in pubs if grid.start < p < grid.end]
        cutoff = _live_cutoff(session, provider)
        if cutoff is not None:
            # Tonight: a publication after the wall clock has not happened, so
            # it is not a decision point yet -- the live watch adds it if and
            # when it does. Only a source with a scheduled history (the
            # synthetic demo) can offer one; a live fetch cannot.
            during = [p for p in during if p <= cutoff]
        instants = [grid.start, *during]
        report(0.15, f"{len(instants)} decision point(s) in the session")

        # Before dusk the opening plan is built from what exists NOW, not from
        # what a scheduled source says will exist by dusk.
        early = cutoff if cutoff is not None and cutoff < grid.start else None

        opts = solve_options(session)
        points: list[DecisionPoint] = []
        prev: Plan | None = None
        for i, t in enumerate(instants):
            dp = solve_step(
                session, provider, index=i, at=t, prev=prev, opts=opts, data_as_of=early
            )
            points.append(dp)
            prev = dp.plan
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


def _live_cutoff(session: NightSession, provider: WeatherForecastProvider) -> datetime | None:
    """The latest instant a live night's fold may use, or None for a replay.

    The wall clock, read AFTER the provider loaded -- or the live forecast's
    own stamp if that is later, which it can be by the seconds between taking
    ``now`` and reading meta.json (see ``OpenMeteoAuto``). Both have happened.
    """
    now = session.now
    if now is None or now.vantage is not Vantage.LIVE or now.t >= session.spec.grid.end:
        return None
    cutoff = max(now.t, AsOf.live().t)
    if isinstance(provider, OpenMeteoAuto) and provider.report.live_fetched_at is not None:
        cutoff = max(cutoff, provider.report.live_fetched_at)
    return cutoff


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]


def night_window(date: str, start_hour_utc: float, hours: float) -> tuple[datetime, datetime]:
    day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC)
    start = day + timedelta(hours=start_hour_utc)
    return start, start + timedelta(hours=hours)
