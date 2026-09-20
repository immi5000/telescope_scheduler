"""Session registry and the notification bus behind SSE.

Two deliberate choices.

**Sessions live in memory.** Nothing here is durable: restart the process and
every session is gone. That is stated rather than implied, because a store that
looks like a database and is not is worse than one that is obviously
ephemeral. SQLite persistence is a separate, additive step -- the fold is a
pure function of the request, so a session can always be rebuilt from it.

**The stream carries notifications, not data.** Every event is an identifier
and a status; the client responds by re-fetching through the same REST
endpoints a replay scrub uses. One fetching path, one cache, one set of types --
and the entire push layer can be replaced by a 15-second poll without touching
a single component.

**A night that is still happening is watched.** After its fold, the store runs
one task per live session that checks for new data every few minutes until
dawn (``api/live.py``). The tasks are the store's to cancel: on delete, and
when the app shuts down.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from tscheduler.api import live
from tscheduler.api.session import NightSession, SessionStatus, fold_night

MAX_QUEUE = 256


@dataclass
class EventBus:
    """Fan-out to SSE subscribers, safe to publish into from a worker thread."""

    _loop: asyncio.AbstractEventLoop | None = None
    _queues: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, event: dict[str, Any]) -> None:
        """Callable from any thread. Drops events for a subscriber that has
        stopped reading rather than blocking the fold behind a slow client."""
        loop = self._loop
        if loop is None:
            return
        with self._lock:
            targets = list(self._queues)
        for q in targets:
            loop.call_soon_threadsafe(self._offer, q, event)

    @staticmethod
    def _offer(q: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait(event)

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=MAX_QUEUE)
        with self._lock:
            self._queues.add(q)
        try:
            yield q
        finally:
            with self._lock:
                self._queues.discard(q)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._queues)


class SessionStore:
    def __init__(self, bus: EventBus, *, live_every: timedelta | None = None) -> None:
        self._sessions: dict[str, NightSession] = {}
        self._order: list[str] = []
        self._bus = bus
        self._lock = threading.Lock()
        self._live_every = live_every if live_every and live_every > timedelta(0) else None
        self._watches: dict[str, asyncio.Task[None]] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def get(self, session_id: str) -> NightSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def list(self) -> list[NightSession]:
        with self._lock:
            return [self._sessions[i] for i in reversed(self._order)]

    def delete(self, session_id: str) -> bool:
        with self._lock:
            if session_id not in self._sessions:
                return False
            del self._sessions[session_id]
            self._order.remove(session_id)
            task = self._watches.pop(session_id, None)
        if task is not None:
            task.cancel()
        return True

    def add(self, session: NightSession) -> None:
        with self._lock:
            self._sessions[session.id] = session
            self._order.append(session.id)

    async def start_fold(self, session: NightSession) -> None:
        """Run the fold off the event loop.

        CP-SAT is CPU-bound and holds the interpreter for whole seconds at a
        time; folding inline would stall every other request including the SSE
        heartbeat, which is exactly when a client decides the server is dead.
        """
        loop = asyncio.get_running_loop()
        self._bus.bind(loop)
        seen = 0

        def on_progress(frac: float, msg: str) -> None:
            nonlocal seen
            self._bus.publish(
                {
                    "type": "session.progress",
                    "sessionId": session.id,
                    "progress": round(frac, 3),
                    "message": msg,
                }
            )
            # One notification per newly available plan -- this is the
            # invalidate-and-refetch signal, and it carries no plan data.
            while seen < len(session.decision_points):
                dp = session.decision_points[seen]
                self._bus.publish(
                    {
                        "type": "plan.available",
                        "sessionId": session.id,
                        "decisionIndex": dp.index,
                        "at": dp.at.isoformat(),
                        "planId": dp.plan_id,
                    }
                )
                seen += 1

        await loop.run_in_executor(None, lambda: fold_night(session, on_progress=on_progress))

        if session.status == SessionStatus.READY:
            # Before the ready event, so the payload a client fetches in
            # response already says it is being watched.
            self.watch(session)
            self._bus.publish(
                {
                    "type": "session.ready",
                    "sessionId": session.id,
                    "decisionPoints": len(session.decision_points),
                    "distinctPlans": session.distinct_plans,
                    "foldSeconds": round(session.fold_seconds, 2),
                }
            )
        else:
            self._bus.publish(
                {"type": "session.failed", "sessionId": session.id, "error": session.error}
            )

    # -- the live watch --------------------------------------------------------

    def watch(self, session: NightSession) -> None:
        """Start keeping a live night current. A no-op for a replay.

        The watch is attached even with scheduled checks off, so a manual
        refresh still works and the payload still says the night is live.
        """
        if live.attach_watch(session, self._live_every) is None or self._live_every is None:
            return
        with self._lock:
            if session.id in self._watches or session.id not in self._sessions:
                return
            task = asyncio.get_running_loop().create_task(
                self._follow(session), name=f"live-watch-{session.id}"
            )
            self._watches[session.id] = task

        def forget(t: asyncio.Task[None]) -> None:
            with self._lock:
                if self._watches.get(session.id) is t:
                    del self._watches[session.id]

        task.add_done_callback(forget)

    async def _follow(self, session: NightSession) -> None:
        every = self._live_every
        watch = session.live
        if every is None or watch is None:
            return
        while watch.following:
            await asyncio.sleep(every.total_seconds())
            if self.get(session.id) is not session:
                return
            await self.check(session)

    async def check(self, session: NightSession, *, manual: bool = False) -> live.CheckOutcome:
        """One check, off the event loop -- it may fetch and run CP-SAT."""
        loop = asyncio.get_running_loop()
        before = len(session.decision_points)
        outcome = await loop.run_in_executor(None, lambda: live.check_now(session, manual=manual))
        if outcome is live.CheckOutcome.UPDATED:
            dps = session.decision_points
            # Appended points, or -- before dusk, where the opening plan is
            # re-made in place -- that one.
            for dp in dps[before:] or dps[:1]:
                self._bus.publish(
                    {
                        "type": "plan.available",
                        "sessionId": session.id,
                        "decisionIndex": dp.index,
                        "at": dp.at.isoformat(),
                        "planId": dp.plan_id,
                    }
                )
        if outcome not in (live.CheckOutcome.BUSY, live.CheckOutcome.TOO_SOON):
            self._bus.publish(
                {"type": "session.live", "sessionId": session.id, "outcome": outcome.value}
            )
        return outcome

    async def close(self) -> None:
        """Cancel every watch. Called once, when the app shuts down."""
        with self._lock:
            tasks = list(self._watches.values())
            self._watches.clear()
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
