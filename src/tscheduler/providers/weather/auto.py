"""Real weather, chosen by when the night is.

The UI no longer asks where the weather should come from. It sends ``auto``,
and the answer depends on one thing only -- where the night sits relative to
the wall clock at the moment the session is created:

    archive      the night is over. The model runs that had been published by
                 each moment of it, replayed in publication order.
    forecast     tonight, a night in progress, or a coming night inside the
                 forecast horizon. The runs published so far, plus the live
                 forecast fetched once, now.
    unavailable  nothing can cover the night: too far ahead, older than the
                 archive, or the service could not be reached. The plan then
                 assumes a clear sky, and every surface says so.

"Now" is INJECTED. Nothing in this module reads a clock: the API layer takes
one ``AsOf.live()`` when the session is created and hands the same instant to
``resolve_weather`` and to ``OpenMeteoAuto``.

How the live forecast keeps the publication gate
------------------------------------------------
Every record the live fetch returns is stamped ``published_at = now`` (the
session's creation instant) -- or later, never earlier, if Open-Meteo's own
metadata, read AFTER the fetch, shows its newest run only became available
after that instant. It is then an ordinary record: ``_fetch`` returns it only
to an ``as_of`` at or after the stamp, and ``Provider.fetch`` asserts that it
did. For a night in progress the stamp falls inside the window, so it becomes
one of the fold's decision points: plans before it see only the archived runs,
plans from it on see the live forecast. For a coming night the stamp precedes
dusk and every plan sees it. At no ``as_of`` before ``now`` can a live byte
reach a plan.

Frugality
---------
The service is free and rate-limited, so this provider asks only for what can
reach a plan: the newest run published by dusk (with up to two older runs as
fallbacks if it is missing), and the runs published during the night up to
``now``. When the night has not started and the live forecast has hours for
it, no archived run is requested at all -- the live one supersedes every one
of them at every as_of the fold will ask about, and where it stops short of
the night's end no older run reaches further. Nor is a run older than the
Single Runs archive: it can only fail. Runs are fetched a few at a time,
archived runs come from a process-wide cache when any session already holds
them, and the first HTTP 429 or connection failure stops every further request
for the session: degraded data, never a crashed fold and never a pile of
serial timeouts.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

import httpx

from tscheduler.core.clock import AsOf, Vantage
from tscheduler.providers.base import Record
from tscheduler.providers.weather import open_meteo as om
from tscheduler.providers.weather.base import COVERAGE_SLACK_S, WeatherForecastProvider
from tscheduler.providers.weather.model import WeatherQuery, WeatherSample

DEFAULT_MODEL: Final = "ecmwf_ifs025"

#: How far ahead ECMWF IFS 0.25 open data reaches. A night starting later than
#: this is resolved to ``unavailable`` without a single request. Nights just
#: inside it are still checked against ``meta.json``'s ``data_end_time``.
FORECAST_HORIZON: Final = timedelta(days=15)

#: Earliest initialisation held by the Single Runs archive. A past night older
#: than this is ``unavailable`` without a request. Open-Meteo's Single Runs page
#: (open-meteo.com/en/docs/single-runs-api, read 2026-09-18): "ECMWF IFS from
#: March 2024, all other models from 2nd of April 2026" -- where "ECMWF IFS" is
#: the 9 km HRES; ``ecmwf_ifs025`` is one of the "other models".
SINGLE_RUNS_ARCHIVE_START: Final[datetime | None] = datetime(2026, 4, 2, tzinfo=UTC)


class WeatherMode(StrEnum):
    ARCHIVE = "archive"
    FORECAST = "forecast"
    SYNTHETIC = "synthetic"
    UNAVAILABLE = "unavailable"


class NightTiming(StrEnum):
    PAST = "past"
    IN_PROGRESS = "in-progress"
    UPCOMING = "upcoming"


def night_timing(start: datetime, end: datetime, now: datetime) -> NightTiming:
    if end <= now:
        return NightTiming.PAST
    if start <= now:
        return NightTiming.IN_PROGRESS
    return NightTiming.UPCOMING


def fmt_utc(t: datetime) -> str:
    """Like "17 Sep 06:00 UTC": the day matters once a night spans two dates."""
    t = t.astimezone(UTC)
    return f"{t.day} {t:%b %H:%M} UTC"


def _fmt_day(t: datetime) -> str:
    """Like "2 Apr 2026"."""
    t = t.astimezone(UTC)
    return f"{t.day} {t:%b %Y}"


@dataclass(frozen=True, slots=True)
class WeatherResolution:
    """Where a session's weather will come from, decided once at creation."""

    requested: str
    mode: WeatherMode
    now: datetime | None
    """The wall-clock instant it was decided at (the session's creation)."""
    timing: NightTiming | None
    reason: str | None = None
    """Why ``mode`` is unavailable, when that is known before any request."""
    model: str | None = None


def resolve_weather(
    requested: str,
    start: datetime,
    end: datetime,
    now: datetime | None,
    *,
    model: str = DEFAULT_MODEL,
    horizon: timedelta = FORECAST_HORIZON,
    archive_start: datetime | None = SINGLE_RUNS_ARCHIVE_START,
) -> WeatherResolution:
    """Pure: the same (request, night, now) always resolves the same way."""
    timing = night_timing(start, end, now) if now is not None else None

    if requested == "open_meteo":
        # Archived runs only, as before -- bounded by ``now`` so a run that
        # does not exist yet is never requested.
        return WeatherResolution(requested, WeatherMode.ARCHIVE, now, timing, model=model)
    if requested != "auto":
        # "synthetic", and anything unrecognised, is the offline demo source:
        # the behaviour the session API has always had for those values.
        return WeatherResolution(requested, WeatherMode.SYNTHETIC, now, timing)

    if now is None or timing is None:
        return WeatherResolution(
            requested,
            WeatherMode.UNAVAILABLE,
            None,
            None,
            reason="the session has no creation time to decide between archive and forecast",
            model=model,
        )

    name = om.model_name(model)
    if timing is NightTiming.PAST:
        if archive_start is not None and start < archive_start:
            return WeatherResolution(
                requested,
                WeatherMode.UNAVAILABLE,
                now,
                timing,
                reason=(
                    f"Open-Meteo's archive of {name} runs begins {archive_start:%Y-%m-%d}, "
                    "after this night"
                ),
                model=model,
            )
        return WeatherResolution(requested, WeatherMode.ARCHIVE, now, timing, model=model)

    lead = start - now
    if lead > horizon:
        days = f"{lead.total_seconds() / 86400.0:.1f}"
        if float(days) * 86400.0 <= horizon.total_seconds():
            # Just past the horizon, one decimal still reads "15.0 days".
            days = f"more than {horizon.days}"
        wait = (lead - horizon).total_seconds() / 86400.0
        return WeatherResolution(
            requested,
            WeatherMode.UNAVAILABLE,
            now,
            timing,
            reason=(
                f"the night starts {days} days from now, beyond the "
                f"{horizon.days}-day reach of the {name} forecast; plan it again "
                f"in {max(wait, 1.0):.0f} day{'s' if wait >= 1.5 else ''}"
            ),
            model=model,
        )
    return WeatherResolution(requested, WeatherMode.FORECAST, now, timing, model=model)


# --------------------------------------------------------------------------
# what the fetch actually did
# --------------------------------------------------------------------------


@dataclass
class FetchReport:
    """Everything the session needs to say about where its weather came from.

    Written only by ``OpenMeteoAuto`` on the fold's thread; read by the API
    once the fold is done.
    """

    model: str
    mode: WeatherMode
    now: datetime | None = None
    """The wall-clock instant these requests were made at (the session's, or
    a live check's). A failure is news only to plans made from then on."""
    lag: timedelta | None = None
    lag_measured: bool = False
    live_fetched_at: datetime | None = None
    """The live forecast's publication stamp: ``now``, or later (never earlier)."""
    live_run: datetime | None = None
    """Initialisation of the newest run Open-Meteo served when we fetched."""
    live_records: int = 0
    live_error: str | None = None
    data_end: datetime | None = None
    """The newest run's last valid time, from meta.json: the forecast horizon."""
    runs_requested: list[datetime] = field(default_factory=list)
    runs_loaded: list[datetime] = field(default_factory=list)
    run_errors: list[tuple[datetime, str]] = field(default_factory=list)
    archive_start: datetime | None = None
    before_archive: list[datetime] = field(default_factory=list)
    """Runs that would have reached a plan (the dusk run, or one published
    during the night) but were not requested: they are older than
    ``archive_start``, so the Single Runs archive does not hold them."""
    halted: str | None = None
    """``rate_limited`` or ``unreachable`` once a request stopped the rest."""
    halt_detail: str | None = None
    requests: int = 0
    """HTTP requests actually sent. Cache hits are free and not counted."""

    @property
    def name(self) -> str:
        return om.model_name(self.model)

    @property
    def live_failed(self) -> bool:
        return self.mode is WeatherMode.FORECAST and self.live_fetched_at is None

    def halt_sentence(self) -> str | None:
        if self.halted == "rate_limited":
            return "Open-Meteo is rate-limiting requests right now (HTTP 429)"
        if self.halted == "unreachable":
            return f"Open-Meteo could not be reached ({self.halt_detail or 'network error'})"
        return None

    def empty_reason(self, night_start: datetime) -> str:
        """Why no record reached any plan -- the specific cause, not a shrug."""
        halt = self.halt_sentence()
        if self.halted == "rate_limited":
            return f"{halt}; create the session again in a few minutes"
        if halt is not None:
            return f"{halt}; check the server's internet connection"
        if self.data_end is not None and night_start >= self.data_end:
            return (
                f"the newest {self.name} run reaches only {fmt_utc(self.data_end)}, "
                "before this night starts; plan it again nearer the night"
            )
        if self.live_error is not None:
            return f"the live {self.name} forecast has nothing for this night ({self.live_error})"
        old = self._archive_sentence(self.before_archive)
        if self.run_errors:
            run, why = self.run_errors[0]
            return (
                f"no archived {self.name} run for this night could be fetched "
                f"(run of {fmt_utc(run)}: {why})" + (f"; {old}" if old else "")
            )
        if old is not None and self.archive_start is not None:
            return (
                f"Open-Meteo's archive of {self.name} runs begins "
                f"{_fmt_day(self.archive_start)}, and every run that could reach this night "
                "is older"
            )
        return f"Open-Meteo returned no {self.name} data for this night"

    def _published_by(self, run: datetime, at: datetime | None) -> bool:
        return at is None or self.lag is None or run + self.lag <= at

    def _archive_sentence(self, runs: Sequence[datetime]) -> str | None:
        if not runs or self.archive_start is None:
            return None
        n = len(runs)
        return (
            f"{n} older run{'s' if n != 1 else ''} predate{'s' if n == 1 else ''} "
            f"Open-Meteo's archive, which begins {_fmt_day(self.archive_start)}"
        )

    def degradation(self, at: datetime | None = None, *, live: bool = True) -> str | None:
        """What went wrong even though some data arrived, or None.

        With ``at``, only what a plan made at ``at`` could have known: a run
        that failed (or predates the archive) counts only if it would have been
        published by then. ``live=False`` leaves out a failed live fetch -- for
        a plan made before that fetch was attempted.
        """
        halt = self.halt_sentence()
        bits: list[str] = []
        live_failed = live and self.live_failed
        if live_failed:
            caused_by_halt = halt is not None and self.live_error in (None, self.halt_detail)
            why = halt if caused_by_halt else (self.live_error or "no data")
            bits.append(f"the live forecast could not be fetched ({why})")
        failed = [
            r for r, _ in self.run_errors if r not in self.runs_loaded and self._published_by(r, at)
        ]
        if failed:
            n = len(failed)
            text = f"{n} archived run{'s' if n != 1 else ''} could not be fetched"
            if halt is not None and not live_failed:
                text += f" ({halt})"
            bits.append(text)
        old = self._archive_sentence([r for r in self.before_archive if self._published_by(r, at)])
        if old is not None:
            bits.append(old)
        return "; ".join(bits) if bits else None


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------


class NoForecast(WeatherForecastProvider):
    """Returns nothing, honestly. For a night no forecast can cover."""

    source_id = "none"

    def _fetch(self, query: WeatherQuery, as_of: AsOf) -> Sequence[Record[WeatherSample]]:
        return []

    def coverage_key(self, query: WeatherQuery) -> str:
        return "none"


@dataclass(frozen=True, slots=True)
class RunPlan:
    """Which archived runs can reach a plan, newest fallback first."""

    dusk: datetime | None
    """The newest run published by dusk (or by now, if the night is ahead)."""
    inside: tuple[datetime, ...]
    """Runs published during the night, up to the cutoff."""
    fallbacks: tuple[datetime, ...]
    """Older runs to try, in order, if the dusk run yields nothing."""

    @property
    def primary(self) -> tuple[datetime, ...]:
        return (*self.inside, *((self.dusk,) if self.dusk is not None else ()))


#: meta.json, per model and hour of "now". Keyed by the INJECTED now, so no
#: clock is read to expire it; the forecast mode always refetches it anyway.
_META_CACHE: dict[tuple[str, datetime], om.ModelMeta] = {}
_META_LOCK = threading.Lock()

#: Clients for sessions that were not handed one. Tests swap this out.
client_factory = om.default_client


def clear_caches() -> None:
    """Forget every cached run and every meta.json. For tests."""
    om.clear_run_cache()
    with _META_LOCK:
        _META_CACHE.clear()


class OpenMeteoAuto(WeatherForecastProvider):
    """Archived Single Runs published by ``now``, plus (in forecast mode) the
    live forecast fetched once and stamped ``now``.

    Every record carries its own ``source_id`` (``open_meteo:single_runs`` or
    ``open_meteo:live``), so a plan's evidence names exactly what it used.
    """

    source_id = "open_meteo:auto"

    def __init__(
        self,
        now: AsOf,
        mode: WeatherMode,
        *,
        client: httpx.Client | None = None,
        lag: timedelta | None = None,
        max_workers: int = 3,
        fallback_runs: int = 2,
        archive_start: datetime | None = SINGLE_RUNS_ARCHIVE_START,
    ) -> None:
        if mode not in (WeatherMode.ARCHIVE, WeatherMode.FORECAST):
            raise ValueError(f"OpenMeteoAuto serves archive or forecast, not {mode}")
        if mode is WeatherMode.FORECAST and now.vantage is not Vantage.LIVE:
            raise ValueError(
                "forecast mode needs a LIVE now: a replay instant has no live forecast, "
                "and stamping one with it would be a lookahead"
            )
        self._now = now
        self._mode = mode
        self._client = client
        self._lag = lag
        self._max_workers = max(1, max_workers)
        self._fallback_runs = max(0, fallback_runs)
        self._archive_start = archive_start
        self._loaded: dict[str, list[Record[WeatherSample]]] = {}
        self._halt = threading.Event()
        self.report = FetchReport(
            model=DEFAULT_MODEL, mode=mode, now=now.t, archive_start=archive_start
        )

    # -- Provider -----------------------------------------------------------

    def _fetch(self, query: WeatherQuery, as_of: AsOf) -> Sequence[Record[WeatherSample]]:
        # Bound by as_of here; Provider.fetch then VERIFIES we did. This one
        # line is the whole gate for the live forecast: its records carry
        # published_at >= now, so no as_of before now ever receives one.
        return [r for r in self._load(query) if r.published_at <= as_of.t]

    def coverage_key(self, query: WeatherQuery) -> str:
        return (
            f"{query.lat:.4f},{query.lon:.4f},{query.model},"
            f"{query.valid_from:%Y%m%dT%H%M},{query.valid_to:%Y%m%dT%H%M},{self._mode.value}"
        )

    @property
    def now(self) -> datetime:
        return self._now.t

    @property
    def mode(self) -> WeatherMode:
        return self._mode

    # -- loading ------------------------------------------------------------

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = client_factory()
        return self._client

    def _note_halt(self, exc: om.OpenMeteoError) -> None:
        self._halt.set()
        if self.report.halted is None:
            self.report.halted = exc.kind
            self.report.halt_detail = exc.detail

    def _load(self, query: WeatherQuery) -> list[Record[WeatherSample]]:
        key = self.coverage_key(query)
        hit = self._loaded.get(key)
        if hit is not None:
            return hit

        rep = self.report
        rep.model = query.model

        live_samples: list[WeatherSample] = []
        if self._mode is WeatherMode.FORECAST:
            live_samples = self._fetch_live(query)

        # AFTER the live fetch, deliberately: if the newest run became
        # available after ``now``, this is how we find out, and the live
        # records are then stamped with that later instant instead.
        meta = self._meta(query.model, fresh=self._mode is WeatherMode.FORECAST)
        if meta is not None:
            rep.data_end = meta.data_end
        # meta.json times only the newest run; the floor keeps a fast 06z/18z
        # delivery from being applied to a slower 00z/12z run.
        lag = self._lag or om.conservative_lag(query.model, meta.lag if meta is not None else None)
        rep.lag = lag
        rep.lag_measured = self._lag is None and meta is not None

        live: list[Record[WeatherSample]] = []
        stamp = self.now
        if meta is not None and meta.available > stamp:
            stamp = meta.available
        if live_samples:
            live = [om.live_record(s, query.model, stamp) for s in live_samples]
            rep.live_fetched_at = stamp
            rep.live_run = meta.init if meta is not None else None
            rep.live_records = len(live)

        if meta is not None and meta.data_end is not None and meta.data_end < query.valid_from:
            # The newest run ends before the night starts, and no archived
            # run reaches further than the newest: asking would be a waste.
            plan = RunPlan(dusk=None, inside=(), fallbacks=())
        else:
            # Published by dusk, the live forecast replaces the archive for
            # every plan. Even when it stops short of the night's end: every
            # archived run is older, and none reaches further than the newest,
            # so asking for them would only fill the same gap with nothing.
            at_dusk = stamp <= query.valid_from and bool(live_samples)
            plan = self.plan_runs(query, lag, live_covers=at_dusk)
        records = self._load_runs(query, plan, lag) + live
        self._loaded[key] = records
        return records

    def _fetch_live(self, query: WeatherQuery) -> list[WeatherSample]:
        if self._halt.is_set():
            return []
        self.report.requests += 1
        try:
            samples = [s for s in om.fetch_live(self._http(), query) if om.in_window(s, query)]
        except om.OpenMeteoError as exc:
            self.report.live_error = exc.detail
            if exc.halts:
                self._note_halt(exc)
            return []
        # Hours only in the interpolation pad before dusk reach no slot (see
        # COVERAGE_SLACK_S): they must not make the session read as forecast.
        reach = timedelta(seconds=COVERAGE_SLACK_S)
        lo, hi = query.valid_from - reach, query.valid_to + reach
        if not any(lo < s.valid_time < hi for s in samples):
            self.report.live_error = "no forecast hours for this night"
            return []
        return samples

    def _meta(self, model: str, *, fresh: bool) -> om.ModelMeta | None:
        key = (model, self.now.replace(minute=0, second=0, microsecond=0))
        if not fresh:
            with _META_LOCK:
                cached = _META_CACHE.get(key)
            if cached is not None:
                return cached
        if self._halt.is_set():
            return None
        self.report.requests += 1
        try:
            meta = om.fetch_meta(model, self._http())
        except om.OpenMeteoError as exc:
            if exc.halts:
                self._note_halt(exc)
            return None
        except (ValueError, KeyError, TypeError):
            return None
        with _META_LOCK:
            _META_CACHE[key] = meta
        return meta

    def plan_runs(self, query: WeatherQuery, lag: timedelta, *, live_covers: bool) -> RunPlan:
        """The archived runs that can reach a plan, and nothing else.

        Only initialisations with ``init + lag <= cutoff`` are candidates, where
        the cutoff is ``now`` or the end of the night, whichever is first: a
        run published later either does not exist yet or reaches no plan.
        ``live_covers`` means the live forecast has hours for the night and is
        visible at dusk, so no archived run can win any hour of any plan.

        A run initialised before the Single Runs archive begins is not a
        candidate either: asking for it can only return HTTP 400. The ones
        that would have reached a plan are noted in the report, so the plans
        they would have fed can say why they have no forecast.
        """
        start, end = query.valid_from, query.valid_to
        cutoff = min(end, self.now)
        if self._mode is WeatherMode.FORECAST and live_covers and self.now <= start:
            # Every plan's as_of is >= dusk >= now, so the live forecast is
            # visible to all of them and supersedes every archived run.
            return RunPlan(dusk=None, inside=(), fallbacks=())

        hours = om.RUN_HOURS.get(query.model, (0, 6, 12, 18))
        anchor = min(start, cutoff)
        earliest = anchor - lag - timedelta(hours=6 * (self._fallback_runs + 2))
        t = (cutoff - lag).replace(minute=0, second=0, microsecond=0)
        newest_first: list[datetime] = []
        while t >= earliest:
            if t.hour in hours and t + lag <= cutoff:
                newest_first.append(t)
            t -= timedelta(hours=1)

        archive = self._archive_start
        too_old = [r for r in newest_first if archive is not None and r < archive]
        newest_first = [r for r in newest_first if r not in too_old]

        inside = tuple(sorted(r for r in newest_first if start < r + lag < end))
        before = [r for r in newest_first if r + lag <= start]
        # Of the runs the archive lacks, the ones that would have been asked
        # for: any published during the night, and the dusk run -- which is
        # only ever too old when no run the archive holds was out by dusk.
        dropped_before = [r for r in too_old if r + lag <= start]
        self.report.before_archive = sorted(
            [r for r in too_old if start < r + lag < end]
            + ([dropped_before[0]] if dropped_before and not before else [])
        )
        return RunPlan(
            dusk=before[0] if before else None,
            inside=inside,
            fallbacks=tuple(before[1 : 1 + self._fallback_runs]),
        )

    def _load_runs(
        self, query: WeatherQuery, plan: RunPlan, lag: timedelta
    ) -> list[Record[WeatherSample]]:
        got = self._fetch_runs(query, plan.primary)
        if plan.dusk is not None and not _any_in_window(got.get(plan.dusk), query):
            for fb in plan.fallbacks:
                if self._halt.is_set():
                    break
                got.update(self._fetch_runs(query, (fb,)))
                if _any_in_window(got.get(fb), query):
                    break

        records: list[Record[WeatherSample]] = []
        for run in sorted(got):
            published = run + lag
            recs = [
                om.run_record(s, query.model, run, published)
                for s in got[run]
                if om.in_window(s, query)
            ]
            if recs:
                self.report.runs_loaded.append(run)
            records.extend(recs)
        return records

    def _fetch_runs(
        self, query: WeatherQuery, runs: Sequence[datetime]
    ) -> dict[datetime, tuple[WeatherSample, ...]]:
        """A few at a time; report bookkeeping stays on this thread."""
        rep = self.report
        out: dict[datetime, tuple[WeatherSample, ...]] = {}
        need: list[datetime] = []
        for r in runs:
            rep.runs_requested.append(r)
            hit = om.cached_run(om.run_cache_key(query, r))
            if hit is not None:
                out[r] = hit
            else:
                need.append(r)
        if not need:
            return out
        if self._halt.is_set():
            rep.run_errors.extend((r, "not requested after an earlier failure") for r in need)
            return out

        client = self._http()

        def one(run: datetime) -> tuple[datetime, tuple[WeatherSample, ...] | om.OpenMeteoError]:
            if self._halt.is_set():
                return run, om.OpenMeteoError("skipped", "not requested after an earlier failure")
            try:
                return run, om.fetch_single_run(client, query, run)
            except om.OpenMeteoError as exc:
                if exc.halts:
                    self._halt.set()
                return run, exc

        with ThreadPoolExecutor(max_workers=min(self._max_workers, len(need))) as pool:
            results = list(pool.map(one, need))

        for run, res in results:
            if isinstance(res, om.OpenMeteoError):
                if res.kind != "skipped":
                    rep.requests += 1
                rep.run_errors.append((run, res.detail))
                if res.halts:
                    self._note_halt(res)
            else:
                rep.requests += 1
                out[run] = res
        return out


def _any_in_window(samples: Sequence[WeatherSample] | None, query: WeatherQuery) -> bool:
    return bool(samples) and any(om.in_window(s, query) for s in samples or ())
