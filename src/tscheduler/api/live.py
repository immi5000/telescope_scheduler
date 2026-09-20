"""Keeping tonight current: the server half of the live edge.

A night that is over is folded once, because everything that will ever be
published about it already has been. A night that has not ended cannot be:
the forecast runs that will decide its second half do not exist yet. Folding
it once at creation and then letting the UI scrub to 04:00 presents the plan
made at 21:00 as though it were what 04:00 looked like -- a snapshot passing
itself off as a live feed.

So after its first fold, a live night is WATCHED. Every few minutes the server
asks the weather source one cheap question -- is there a model run newer than
the one this session holds? -- and only when there is does it fetch, re-plan
and append a decision point. Two rules keep that honest:

* **A live decision is stamped when we learned the data, not when it was
  published.** In a replay those are assumed to coincide; live they do not,
  and "the plan changed at 00:40" is only true if at 00:40 we had it.
* **The past stays locked.** Each re-plan locks every slot before its own
  instant, exactly as the replay fold does, so the watch can never rewrite
  what the observer was already told.

Before dusk nothing has been handed to the observer, so a new run re-makes the
OPENING plan instead of appending one. After dawn the watch stops; the session
is then a complete record and the UI unlocks replay.

The wall clock enters only through ``AsOf.live()``, taken once per check.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from tscheduler.api import schemas
from tscheduler.api.session import (
    NightSession,
    SessionStatus,
    build_provider,
    solve_options,
    solve_step,
    weather_query,
)
from tscheduler.core.clock import AsOf, Vantage
from tscheduler.providers.weather import auto
from tscheduler.providers.weather import open_meteo as om
from tscheduler.providers.weather.auto import OpenMeteoAuto, WeatherMode, resolve_weather

log = logging.getLogger(__name__)

#: A manual "check now" closer than this to the previous check is refused. The
#: weather service is free and rate-limited; a button is an easy way to 429.
MIN_MANUAL_GAP: Final = timedelta(seconds=60)


class CheckOutcome(StrEnum):
    UPDATED = "updated"
    """New data arrived and the night was re-planned."""
    NOTHING_NEW = "nothing-new"
    FAILED = "failed"
    """The source could not be asked (429, offline). Tried again next time."""
    OVER = "over"
    """The night has ended; the watch stops."""
    BUSY = "busy"
    """Another check of this session is already running."""
    TOO_SOON = "too-soon"


@dataclass
class LiveWatch:
    """What the server knows about keeping one live night current."""

    every: timedelta | None
    """Scheduled interval; None means manual checks only."""
    following: bool = True
    checks: int = 0
    updates: int = 0
    revision: int = 0
    last_checked_at: datetime | None = None
    next_check_at: datetime | None = None
    last_result: str = "watching for new forecast data"
    last_error: str | None = None
    held_run: datetime | None = None
    """Initialisation of the newest model run the session's plans were built
    on. A check re-plans only when the source has a newer one."""
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def is_live_night(sess: NightSession) -> bool:
    """True when the night had not ended at the session's creation instant.

    Decided from the injected ``now``, never from a fresh clock read: a session
    created during tonight stays a live session after dawn -- its watch simply
    stops -- rather than being re-classified as a replay it never was.
    """
    now = sess.now
    return now is not None and now.vantage is Vantage.LIVE and now.t < sess.spec.grid.end


def attach_watch(sess: NightSession, every: timedelta | None) -> LiveWatch | None:
    """Give a live night its watch. Idempotent; returns None for a replay."""
    if not is_live_night(sess):
        return None
    if sess.live is None:
        sess.live = LiveWatch(every=every, held_run=_seed_run(sess))
    return sess.live


def _seed_run(sess: NightSession) -> datetime | None:
    """The model run the fold's live fetch served, when that is certain.

    ``FetchReport.live_run`` comes from a meta.json read made AFTER the live
    fetch. When the stamp moved past ``now`` a run landed around the fetch,
    and the data may be from the one before it; trusting the label would
    suppress refetching until the NEXT run, hours later. Unknown instead, so
    the first check refetches once.
    """
    rep = sess.weather_report
    now = sess.now
    if rep is None or rep.live_fetched_at is None or now is None:
        return None
    return rep.live_run if rep.live_fetched_at <= now.t else None


def live_out(sess: NightSession) -> schemas.LiveOut | None:
    w = sess.live
    if w is None:
        return None
    return schemas.LiveOut(
        following=w.following,
        every_minutes=round(w.every.total_seconds() / 60.0, 2) if w.every else None,
        checks=w.checks,
        updates=w.updates,
        revision=w.revision,
        last_checked_at=w.last_checked_at,
        next_check_at=w.next_check_at,
        last_result=w.last_result,
        last_error=w.last_error,
    )


def check_now(sess: NightSession, *, now: AsOf | None = None, manual: bool = False) -> CheckOutcome:
    """Ask the source for anything new, and re-plan if there is.

    Blocking: it may fetch and solve. Callers on the event loop run it in an
    executor. Never raises for an upstream failure -- that is recorded on the
    watch and retried at the next check.
    """
    watch = sess.live
    if watch is None:
        raise ValueError(f"session {sess.id} is not a live night")
    if not watch.lock.acquire(blocking=False):
        return CheckOutcome.BUSY
    try:
        return _check(sess, watch, now or AsOf.live(), manual=manual)
    except Exception as exc:
        # A malformed payload or a solver failure must not end the watch: the
        # scheduled loop would die silently while the payload still promised
        # a next check. Recorded, and tried again next time. `_check` touches
        # the session only at its very end, so nothing is left half-written.
        log.exception("live check of session %s failed", sess.id)
        watch.last_error = f"{type(exc).__name__}: {exc}"
        watch.last_result = "the check failed unexpectedly"
        return CheckOutcome.FAILED
    finally:
        watch.lock.release()


def _check(sess: NightSession, watch: LiveWatch, now: AsOf, *, manual: bool) -> CheckOutcome:
    grid = sess.spec.grid
    if (
        manual
        and watch.last_checked_at is not None
        and now.t - watch.last_checked_at < MIN_MANUAL_GAP
    ):
        return CheckOutcome.TOO_SOON

    watch.checks += 1
    watch.last_checked_at = now.t
    if now.t >= grid.end:
        watch.following = False
        watch.next_check_at = None
        watch.last_error = None
        watch.last_result = "the night is over; its record is complete"
        return CheckOutcome.OVER
    watch.next_check_at = now.t + watch.every if watch.every else None

    if sess.status != SessionStatus.READY or not sess.decision_points:
        watch.last_result = "the first plan is still being built"
        return CheckOutcome.NOTHING_NEW

    # Re-resolved at every check: a night beyond the forecast's reach at
    # creation comes within it as the days pass.
    res = resolve_weather(sess.weather.requested, grid.start, grid.end, now.t)
    if res.mode is WeatherMode.UNAVAILABLE:
        watch.last_error = None
        watch.last_result = f"no forecast covers this night yet: {res.reason}"
        return CheckOutcome.NOTHING_NEW

    meta_init: datetime | None = None
    if res.mode is WeatherMode.FORECAST:
        model = res.model or auto.DEFAULT_MODEL
        name = om.model_name(model)
        try:
            with auto.client_factory() as client:
                meta = om.fetch_meta(model, client)
        except om.OpenMeteoError as exc:
            watch.last_error = exc.detail
            watch.last_result = f"could not ask Open-Meteo for new {name} runs"
            return CheckOutcome.FAILED
        except (ValueError, KeyError, TypeError) as exc:
            watch.last_error = f"unreadable meta.json: {exc}"
            watch.last_result = f"could not ask Open-Meteo for new {name} runs"
            return CheckOutcome.FAILED
        if watch.held_run is not None and meta.init <= watch.held_run:
            watch.last_error = None
            watch.last_result = f"no new {name} run since the {watch.held_run:%d %b %HZ} run"
            return CheckOutcome.NOTHING_NEW
        # Read BEFORE the fetch, so the live data is at least this run.
        meta_init = meta.init

    provider = build_provider(sess.spec, sess.weather_source, resolution=res, now=now)
    query = weather_query(sess.spec)
    # Enumerating when data arrived, as the fold does. Loads the records.
    provider.publication_times(query, AsOf.at(grid.end))

    # When we learned it: now, or -- if meta.json showed the newest run landed
    # between taking `now` and reading it -- that later instant.
    learned = now.t
    live_failed: str | None = None
    if isinstance(provider, OpenMeteoAuto) and provider.mode is WeatherMode.FORECAST:
        rep = provider.report
        if rep.live_fetched_at is not None:
            learned = max(learned, rep.live_fetched_at)
        else:
            live_failed = rep.live_error or rep.halt_sentence() or "no data"

    # New means a newer MODEL RUN than the last plan used -- compared by run,
    # not by publication instant, because a re-measured delivery lag re-stamps
    # the same run -- or, for a source without runs, a later publication.
    last = sess.decision_points[-1]
    visible = provider.fetch(query, AsOf.at(learned)).records
    have = _newest_run(((r.source_id, r.record_id, r.published_at) for r in visible), meta_init)
    had = _newest_run(
        ((r.provider, r.record_id, r.published_at) for r in last.plan.ledger.refs),
        watch.held_run,
    )
    if have is None or (had is not None and have <= had):
        if live_failed is not None:
            watch.last_error = live_failed
            watch.last_result = "the live forecast could not be fetched"
            return CheckOutcome.FAILED
        watch.last_error = None
        watch.last_result = "nothing new has been published"
        return CheckOutcome.NOTHING_NEW

    opts = solve_options(sess)
    dps = sess.decision_points
    stamp = f"{learned:%H:%M} UTC"
    if learned < grid.start:
        # Nobody has been handed a plan yet: re-make the opening one, still
        # handed over at dusk, on what exists now.
        dp = solve_step(
            sess,
            provider,
            index=0,
            at=grid.start,
            prev=None,
            opts=opts,
            reason=(
                f"opening plan, re-made on the forecast fetched {stamp}"
                if res.mode is WeatherMode.FORECAST
                else f"opening plan, re-made on data published by {stamp}"
            ),
            data_as_of=learned,
        )
        new_points = (dp, *dps[1:])
        moved = dp.plan_id != dps[0].plan_id
    else:
        if learned <= last.at:
            watch.last_result = "nothing new since the last re-plan"
            return CheckOutcome.NOTHING_NEW
        dp = solve_step(sess, provider, index=len(dps), at=learned, prev=last.plan, opts=opts)
        new_points = (*dps, dp)
        moved = dp.changed_slots > 0 or bool(dp.added_targets or dp.dropped_targets)

    with sess._lock:
        sess.decision_points = new_points
        sess.weather = res
        if isinstance(provider, OpenMeteoAuto):
            sess.weather_report = provider.report
        if res.mode is WeatherMode.FORECAST:
            watch.held_run = meta_init if live_failed is None else have
    watch.updates += 1
    watch.revision += 1
    watch.last_error = live_failed
    if live_failed is not None:
        what = "live forecast failed; re-planned on the newer archived run"
    elif res.mode is WeatherMode.FORECAST:
        what = f"new forecast fetched {stamp}"
    else:
        what = f"new data {stamp}"
    watch.last_result = f"{what}; " + ("the plan changed" if moved else "the plan held")
    return CheckOutcome.UPDATED


def _newest_run(
    records: Iterable[tuple[str, str, datetime]], live_run: datetime | None
) -> datetime | None:
    """The newest model run among ``(source, record_id, published_at)``.

    An archived single run is identified by its initialisation; the live
    forecast by the run it was served from (``live_run``, None if unknown); a
    source with no runs (the synthetic demo) by its publication instant.
    """
    best: datetime | None = None
    for source, record_id, published_at in records:
        key = live_run if source == om.LIVE_SOURCE_ID else om.run_of(record_id) or published_at
        if key is not None and (best is None or key > best):
            best = key
    return best
