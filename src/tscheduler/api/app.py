"""The HTTP surface.

Shape of the API, and why:

``GET  /api/presets``                     sites, rigs, catalog -- one POST to a session
``POST /api/sessions``                    create; the fold starts in the background
``GET  /api/sessions``                    list
``GET  /api/sessions/{id}``               the night: twilight, moon, targets, decision points
``GET  /api/sessions/{id}/plan?as_of=``   the plan in force at an instant
``GET  /api/sessions/{id}/grid?as_of=``   the efficiency/preference heatmap at an instant
``GET  /api/events``                      SSE notifications (identifiers only, never data)
``DELETE /api/sessions/{id}``             drop it

The plan endpoint takes an *instant*, not a plan index, and answers with the
interval that plan is valid over. That is what turns a slider drag from one
request per pointer-move into one request per decision point: the client caches
on ``[validFrom, validUntil)`` and only asks again when the cursor leaves it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from tscheduler import __version__
from tscheduler.api import mappers, presets, schemas
from tscheduler.api.session import (
    NightSession,
    SessionStatus,
    new_session_id,
    night_window,
)
from tscheduler.api.store import EventBus, SessionStore
from tscheduler.config import Settings, load_settings
from tscheduler.core.clock import AsOf
from tscheduler.core.timegrid import TimeGrid
from tscheduler.domain.site import Site
from tscheduler.domain.targets import Target
from tscheduler.physics.convert import bortle_to_artificial_nl
from tscheduler.pipeline.builder import SessionSpec

UNPROCESSABLE = 422
"""Spelled as a literal: Starlette renamed its constant for this code and
importing either name pins us to a Starlette version for no benefit."""

HEARTBEAT_SECONDS = 15.0
"""Proxies and load balancers cut an idle stream. A comment frame is the
cheapest thing that counts as traffic and EventSource ignores it."""


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or load_settings()
    bus = EventBus()
    store = SessionStore(bus)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        bus.bind(asyncio.get_running_loop())
        yield

    app = FastAPI(
        title="Telescope Scheduler",
        version=__version__,
        summary="Real-time visible-light night scheduler with a no-lookahead replay.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cfg.cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -- helpers -----------------------------------------------------------

    def require(session_id: str) -> NightSession:
        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no session {session_id!r}")
        return sess

    def require_ready(session_id: str) -> NightSession:
        sess = require(session_id)
        if sess.status == SessionStatus.FAILED:
            raise HTTPException(status.HTTP_409_CONFLICT, sess.error or "fold failed")
        if not sess.decision_points:
            raise HTTPException(
                status.HTTP_425_TOO_EARLY,
                f"session is still building ({sess.progress:.0%}: {sess.message})",
            )
        return sess

    def resolve_index(sess: NightSession, as_of: str | None) -> int:
        """An instant, or nothing at all, becomes a decision index.

        Anything outside the night clamps to an end rather than erroring: the
        replay cursor is allowed to sit on either boundary, and a slider that
        throws at its own extremes is a slider nobody can use.
        """
        if as_of is None:
            return 0
        try:
            t = datetime.fromisoformat(as_of)
        except ValueError as exc:
            raise HTTPException(
                UNPROCESSABLE,
                f"as_of must be ISO-8601, got {as_of!r}",
            ) from exc
        if t.tzinfo is None:
            t = t.replace(tzinfo=UTC)
        return sess.decision_index_for(t)

    # -- routes ------------------------------------------------------------

    @app.get("/api/health", response_model=schemas.HealthOut, tags=["meta"])
    def health() -> schemas.HealthOut:
        return schemas.HealthOut(
            status="ok",
            version=__version__,
            sessions=len(store),
            settings=cfg.describe(),
        )

    @app.get("/api/presets", response_model=schemas.PresetsOut, tags=["meta"])
    def get_presets() -> schemas.PresetsOut:
        return schemas.PresetsOut(
            sites=[mappers.site_out(s) for s in presets.SITES],
            equipment=[mappers.equipment_out(e) for e in presets.EQUIPMENT],
            catalog=mappers.catalog_out(),
            default_target_ids=list(presets.DEFAULT_TARGET_IDS),
        )

    @app.post(
        "/api/sessions",
        response_model=schemas.SessionOut,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["sessions"],
    )
    async def create_session(
        req: schemas.SessionRequest, tasks: BackgroundTasks
    ) -> schemas.SessionOut:
        sess = _build_session(req, cfg)
        store.add(sess)
        tasks.add_task(store.start_fold, sess)
        return mappers.session_out(sess)

    @app.get("/api/sessions", response_model=list[schemas.SessionListItemOut], tags=["sessions"])
    def list_sessions() -> list[schemas.SessionListItemOut]:
        return [mappers.session_list_item(s) for s in store.list()]

    @app.get("/api/sessions/{session_id}", response_model=schemas.SessionOut, tags=["sessions"])
    def get_session(session_id: str) -> schemas.SessionOut:
        return mappers.session_out(require(session_id))

    @app.delete(
        "/api/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["sessions"]
    )
    def delete_session(session_id: str) -> None:
        if not store.delete(session_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no session {session_id!r}")

    @app.get("/api/sessions/{session_id}/plan", response_model=schemas.PlanOut, tags=["plan"])
    def get_plan(
        session_id: str,
        as_of: str | None = Query(
            default=None,
            description="ISO-8601 instant. The plan in force then; omitted means dusk.",
        ),
    ) -> schemas.PlanOut:
        sess = require_ready(session_id)
        return mappers.plan_out(sess, resolve_index(sess, as_of))

    @app.get(
        "/api/sessions/{session_id}/grid", response_model=schemas.QualityGridOut, tags=["plan"]
    )
    def get_grid(
        session_id: str, as_of: str | None = Query(default=None)
    ) -> schemas.QualityGridOut:
        sess = require_ready(session_id)
        return mappers.quality_grid_out(sess, resolve_index(sess, as_of))

    @app.get("/api/events", tags=["stream"])
    async def events(request: Request) -> StreamingResponse:
        """Server-sent notifications. Handlers should do one thing: refetch.

        Nothing here carries plan data, so live and replay share a single
        fetching path and the whole stream can be swapped for a poll.
        """

        async def gen() -> AsyncIterator[str]:
            async with bus.subscribe() as q:
                yield 'event: hello\ndata: {"ok":true}\n\n'
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_SECONDS)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    kind = event.get("type", "message")
                    yield f"event: {kind}\ndata: {json.dumps(event)}\n\n"

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _build_session(req: schemas.SessionRequest, cfg: Settings) -> NightSession:
    start, end = _window(req)
    grid = TimeGrid.from_window(start, end, req.slot_minutes)

    eq = presets.equipment_by_id(req.equipment_id)
    if eq is None:
        raise HTTPException(
            UNPROCESSABLE,
            f"unknown equipmentId {req.equipment_id!r}; try {[e.id for e in presets.EQUIPMENT]}",
        )

    site_preset, site = _resolve_site(req)
    targets = _resolve_targets(req)
    if not targets:
        raise HTTPException(UNPROCESSABLE, "no targets resolved")
    if req.weather not in ("synthetic", "open_meteo"):
        raise HTTPException(
            UNPROCESSABLE,
            f"weather must be 'synthetic' or 'open_meteo', got {req.weather!r}",
        )

    spec = SessionSpec(
        site=site,
        grid=grid,
        optics=eq.optics,
        camera=eq.camera,
        mount=eq.mount,
        targets=targets,
        snr_goal=req.snr_goal,
        t_sub_s=req.t_sub_s,
    )
    return NightSession(
        id=new_session_id(),
        name=req.name or f"{site.name} {req.date}",
        # AsOf.live() rather than datetime.now(): core/clock.py is the one
        # sanctioned wall-clock reader in the package, and the AST guardrail
        # enforces it here too.
        created_at=AsOf.live().t,
        spec=spec,
        site_preset=site_preset,
        equipment_preset=eq,
        weather_source=req.weather,
        solve_seconds=min(req.solve_seconds, cfg.solve_seconds * 4),
    )


def _window(req: schemas.SessionRequest) -> tuple[datetime, datetime]:
    try:
        return night_window(req.date, req.start_hour_utc, req.hours)
    except ValueError as exc:
        raise HTTPException(UNPROCESSABLE, f"bad date {req.date!r}: {exc}") from exc


def _resolve_site(req: schemas.SessionRequest) -> tuple[presets.SitePreset, Site]:
    if req.site is not None:
        r = req.site
        preset = presets.SitePreset(
            id="custom",
            name=r.name,
            latitude_deg=r.latitude_deg,
            longitude_deg=r.longitude_deg,
            elevation_m=r.elevation_m,
            bortle=r.bortle,
            extinction_k=r.extinction_k,
        )
        site = Site(
            latitude_deg=r.latitude_deg,
            longitude_deg=r.longitude_deg,
            elevation_m=r.elevation_m,
            name=r.name,
            min_altitude_deg=r.min_altitude_deg,
            min_moon_separation_deg=r.min_moon_separation_deg,
            artificial_zenith_nl=bortle_to_artificial_nl(r.bortle),
            extinction_k=r.extinction_k,
        )
        return preset, site

    found = presets.site_by_id(req.site_id or "urbana")
    if found is None:
        raise HTTPException(
            UNPROCESSABLE,
            f"unknown siteId {req.site_id!r}; try {[s.id for s in presets.SITES]}",
        )
    return found, found.to_site()


def _resolve_targets(req: schemas.SessionRequest) -> tuple[Target, ...]:
    if req.targets is None:
        resolved = [presets.catalog_target(t) for t in presets.DEFAULT_TARGET_IDS]
        return tuple(t for t in resolved if t is not None)

    out: list[Target] = []
    for r in req.targets:
        if r.ra_deg is not None and r.dec_deg is not None and r.magnitude is not None:
            out.append(
                Target(
                    id=r.id,
                    name=r.name or r.id,
                    ra_deg=r.ra_deg,
                    dec_deg=r.dec_deg,
                    magnitude=r.magnitude,
                    priority=r.priority,
                    urgency=r.urgency,
                    snr_goal=r.snr_goal,
                )
            )
            continue
        found = presets.catalog_target(r.id, priority=r.priority, snr_goal=r.snr_goal)
        if found is None:
            raise HTTPException(
                UNPROCESSABLE,
                f"target {r.id!r} is not in the catalog; supply raDeg, decDeg and magnitude",
            )
        out.append(found)
    return tuple(out)


app = create_app()
