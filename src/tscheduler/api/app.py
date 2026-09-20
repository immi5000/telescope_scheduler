"""The HTTP surface.

Shape of the API, and why:

``GET  /api/presets``                     sites, rigs, catalog -- one POST to a session
``GET  /api/site/locate?lat=&lon=``       what the browser's coordinates are called
``POST /api/night``                       fold a whole night, keep nothing
``POST /api/night/stream``                the same, reported as it happens (NDJSON)
``POST /api/sessions``                    create; the fold starts in the background
``GET  /api/sessions``                    list
``GET  /api/sessions/{id}``               the night: twilight, moon, targets, decision points
``GET  /api/sessions/{id}/geometry``      where everything is, all night (no as_of)
``GET  /api/sessions/{id}/plan?as_of=``   the plan in force at an instant
``GET  /api/sessions/{id}/grid?as_of=``   the efficiency/preference heatmap at an instant
``GET  /api/sessions/{id}/sky``           planets and sky darkness, for the sky view only
``GET  /api/sessions/{id}/satellites``    satellite passes, for the sky view only
``POST /api/sessions/{id}/targets``       add an object to a planned night, and re-plan
``GET  /api/catalog``                     every catalogue object, positioned, for the sky
``GET  /api/events``                      SSE notifications (identifiers only, never data)
``DELETE /api/sessions/{id}``             drop it

The plan endpoint takes an *instant*, not a plan index, and answers with the
interval that plan is valid over. That is what turns a slider drag from one
request per pointer-move into one request per decision point: the client caches
on ``[validFrom, validUntil)`` and only asks again when the cursor leaves it.

``/geometry`` takes no ``as_of`` **by construction**, and that is the same idea
one level down. Altitude, azimuth, airmass, lunar separation and visibility do
not depend on the weather, so they are identical at every decision point;
serving them from ``/grid`` meant resending a quarter-megabyte of unchanged
numbers every time a forecast run crossed the cursor. The absence of the
parameter is the guarantee -- there is no way to ask this endpoint a question
whose answer could leak, because there is no clock in the question.
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES

from tscheduler import __version__, catalog
from tscheduler.api import (
    amend,
    equipment,
    geolocate,
    live,
    mappers,
    nightwindow,
    oneshot,
    presets,
    schemas,
    skyview,
    thumbnails,
    tonight,
)
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
from tscheduler.providers.satellites.base import SatelliteElementsProvider
from tscheduler.providers.satellites.celestrak import CelesTrakProvider

UNPROCESSABLE = 422
"""Spelled as a literal: Starlette renamed its constant for this code and
importing either name pins us to a Starlette version for no benefit."""

WEATHER_SOURCES: tuple[str, ...] = ("auto", "synthetic", "open_meteo")
"""``auto`` is what the UI sends: real data, chosen by when the night is. The
other two exist for tests, demos and reproducing a published result."""

HEARTBEAT_SECONDS = 15.0
"""Proxies and load balancers cut an idle stream. A comment frame is the
cheapest thing that counts as traffic and EventSource ignores it."""

GZIP_EXCLUDED_CONTENT_TYPES: tuple[str, ...] = tuple(
    dict.fromkeys((*DEFAULT_EXCLUDED_CONTENT_TYPES, "text/event-stream"))
)
"""Starlette's defaults, with SSE pinned explicitly.

``text/event-stream`` is already in the default tuple, and naming it again
costs nothing while making the requirement legible: an event stream is a
sequence of independent notifications, and anything between here and the
browser that buffers one delivers a live alert minutes late -- a failure far
too quiet to leave resting on someone else's default.

It is NOT a statement about Starlette, and the difference matters because
``/api/night/stream`` depends on the other half of it. Starlette flushes a
streaming body per chunk (``Z_SYNC_FLUSH``), so ``application/x-ndjson`` is
deliberately absent from this tuple: its progress frames still arrive as they
are produced, and its final frame -- the whole night, three quarters of a
megabyte -- compresses better than three to one. Excluding it would buy
nothing and cost that.

Built by extending the defaults rather than replacing them, because passing
only the one entry would silently re-enable compression of already-compressed
PNG, WebP and video responses. ``dict.fromkeys`` dedupes and keeps the order
stable, so the value does not shuffle between runs.
"""


def create_app(
    settings: Settings | None = None,
    *,
    satellite_elements: SatelliteElementsProvider | None = None,
) -> FastAPI:
    """Build the app. ``satellite_elements`` exists so tests can run offline."""
    cfg = settings or load_settings()
    bus = EventBus()
    store = SessionStore(bus, live_every=timedelta(minutes=cfg.live_refresh_minutes))
    elements = satellite_elements or CelesTrakProvider(cache_dir=Path(cfg.cache_dir) / "tle")
    # Display-only derived data, one entry per session. Both are pure
    # functions of the session (plus, for satellites, the elements we hold),
    # so they are computed on first request and kept for the session's life.
    sky_cache: dict[str, schemas.SkyOut] = {}
    satellite_cache: dict[str, schemas.SatellitesOut] = {}
    satellite_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        bus.bind(asyncio.get_running_loop())
        yield
        # Live watches sleep for minutes at a time; left alone they would
        # outlive the app and wake to a closed loop.
        await store.close()

    app = FastAPI(
        title="Traveling Telescope",
        version=__version__,
        summary="Real-time visible-light night scheduler with a no-lookahead replay.",
        lifespan=lifespan,
    )
    # Order matters: middleware added later sits further out, so CORS must be
    # added AFTER GZip to stay outermost. A CORS rejection that came back
    # gzipped without its headers would fail in the browser as an opaque
    # network error with nothing in the console to explain it.
    app.add_middleware(
        GZipMiddleware,
        minimum_size=1024,
        compresslevel=6,
        exclude_content_types=GZIP_EXCLUDED_CONTENT_TYPES,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cfg.cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's own 422 echoes the offending input, and Starlette's JSON
        # parser accepts NaN and Infinity -- which the response encoder then
        # refuses, turning a validation error into a 500. Same body, finite.
        return JSONResponse(
            status_code=UNPROCESSABLE, content={"detail": _finite(jsonable_encoder(exc.errors()))}
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
            telescopes=[equipment.telescope_out(t) for t in equipment.TELESCOPES],
            cameras=[equipment.camera_out(c) for c in equipment.CAMERAS],
            default_telescope_id=equipment.DEFAULT_TELESCOPE_ID,
            default_camera_id=equipment.DEFAULT_CAMERA_ID,
            default_mount=equipment.mount_out(equipment.DEFAULT_MOUNT),
            default_target_ids=list(presets.DEFAULT_TARGET_IDS),
            catalog_size=presets.catalog_size(),
            catalog_attribution=presets.CATALOG_ATTRIBUTION,
        )

    @app.post("/api/equipment/derive", response_model=schemas.EquipmentOut, tags=["meta"])
    def derive_equipment(req: schemas.EquipmentRequest) -> schemas.EquipmentOut:
        """A rig's derived figures, for a rig that is not a session yet.

        Focal ratio, resolving power, star size and sampling are one line of
        arithmetic each, and exactly the kind of number a browser copy would
        round differently from the one the scheduler uses. No caller in the
        frontend is left: the planning form stopped showing these. Kept
        because the arithmetic belongs on this side of the wire whenever
        something asks for it again.
        """
        return equipment.equipment_out(equipment.from_request(req))

    thumbs = thumbnails.ThumbnailCache(Path(cfg.cache_dir))

    @app.get("/api/thumbnail", tags=["meta"], response_class=Response)
    def get_thumbnail(
        ra: float = Query(ge=0, lt=360),
        dec: float = Query(ge=-90, le=90),
        fov: float = Query(default=0.5, gt=0, le=thumbnails.MAX_FOV_DEG),
        size: int = Query(default=384, ge=16, le=thumbnails.MAX_SIZE_PX),
        survey: str = Query(default=thumbnails.DEFAULT_SURVEY),
    ) -> Response:
        """A deep-sky cutout, from disk after the first request.

        The survey is chosen from an allowlist by short key and never taken
        from the request, so this cannot be pointed at an arbitrary upstream.

        These are pictures and nothing else: no thumbnail reaches a ledger, a
        grid or a plan, so the as-of machinery does not apply to this route.
        """
        try:
            req = thumbnails.ThumbnailRequest(
                ra_deg=ra, dec_deg=dec, fov_deg=fov, size_px=size, survey=survey
            )
        except ValueError as exc:
            raise HTTPException(UNPROCESSABLE, str(exc)) from exc

        try:
            body = thumbs.fetch(req)
        except Exception as exc:
            # A missing picture must never take a panel down with it, so this
            # is reported plainly and the UI simply draws no thumbnail.
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"sky survey cutout unavailable: {type(exc).__name__}: {exc}",
            ) from exc

        return Response(
            content=body,
            media_type="image/jpeg",
            headers={
                "ETag": f'W/"{req.key}"',
                # The sky does not change. This is a 1990s photographic survey.
                "Cache-Control": "public, max-age=31536000, immutable",
            },
        )

    @app.get("/api/night-window", response_model=schemas.NightWindowOut, tags=["meta"])
    def get_night_window(
        date: str = Query(description="night start date, YYYY-MM-DD, local to the site"),
        lat: float = Query(ge=-90, le=90),
        lon: float = Query(ge=-180, le=360, description="positive EAST"),
        elevation_m: float = Query(default=0.0),
    ) -> schemas.NightWindowOut:
        """Astronomical dusk and dawn, so the client asks rather than derives.

        The night that BEGINS on ``date``, anchored on local solar noon -- at
        longitude -155 that night is mostly the following day in UTC, and
        anchoring on UTC midnight shifts every default by a day for anyone
        west of Greenwich.
        """
        try:
            w = nightwindow.night_window(lat, lon, elevation_m, date)
        except ValueError as exc:
            raise HTTPException(UNPROCESSABLE, f"bad date {date!r}: {exc}") from exc

        # Dusk-to-dawn when the sky genuinely gets dark; otherwise sunset to
        # sunrise, so a high-latitude summer still yields a usable window
        # instead of a zero-hour one.
        start = w.dusk or w.sunset
        if start is None:
            start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(
                hours=12 - w.utc_offset_hours + 12
            )
        return schemas.NightWindowOut(
            date=date,
            sunset=w.sunset,
            dusk=w.dusk,
            dawn=w.dawn,
            sunrise=w.sunrise,
            dark_hours=w.dark_hours,
            utc_offset_hours=w.utc_offset_hours,
            always_up=w.always_up,
            never_rises=w.never_rises,
            suggested_start=start,
            suggested_hours=round(max(0.5, min(w.dark_hours or 8.0, 16.0)), 2),
        )

    @app.get("/api/site/locate", response_model=schemas.LocationOut, tags=["meta"])
    def locate_site(
        lat: float = Query(ge=-90, le=90),
        lon: float = Query(ge=-180, le=360, description="positive EAST"),
    ) -> schemas.LocationOut:
        """What the browser's coordinates are called, and how high they are.

        Decoration, not data. The coordinates ARE the site; this only says what
        the place is named and reads an elevation off a terrain model, so both
        fields are nullable and a lookup that fails answers 200 with nulls
        rather than an error. The form shows the coordinates either way.
        """
        lon = ((lon + 180.0) % 360.0) - 180.0
        place = geolocate.locate(lat, lon)
        return schemas.LocationOut(
            latitude_deg=lat,
            longitude_deg=lon,
            name=place.name,
            elevation_m=place.elevation_m,
            attribution=geolocate.ATTRIBUTION,
        )

    @app.get("/api/catalog/notice", tags=["meta"], response_class=PlainTextResponse)
    def catalog_notice() -> str:
        """The catalogue's licence notice: the CC BY-SA 4.0 attribution for
        OpenNGC, what this project changed, and the Sharpless acknowledgement.
        Linked from wherever the catalogue is shown."""
        return (resources.files("tscheduler.catalog") / "NOTICE").read_text(encoding="utf-8")

    @app.post("/api/targets/tonight", response_model=schemas.TonightOut, tags=["meta"])
    def get_tonight(req: schemas.TonightRequest) -> schemas.TonightOut:
        """The whole catalogue, ranked for one site, night and rig.

        Takes no ``as_of`` and needs none: where an object is and how bright it
        is are not published data. The weather is deliberately absent -- this
        answers "what is up and worth it", and the plan answers "and will it
        be clear".
        """
        site_preset, site = _resolve_site(req)
        try:
            rig = equipment.resolve(req.equipment_id, req.equipment)
        except LookupError as exc:
            raise HTTPException(UNPROCESSABLE, str(exc)) from exc
        try:
            return tonight.tonight_out(req, site_preset, site, rig)
        except ValueError as exc:
            raise HTTPException(UNPROCESSABLE, str(exc)) from exc
        except NotImplementedError as exc:
            raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(exc)) from exc

    @app.post("/api/night", response_model=schemas.FullNightOut, tags=["night"])
    async def create_night(req: schemas.SessionRequest) -> schemas.FullNightOut:
        """Fold a night and return all of it, keeping nothing.

        The stateless twin of ``POST /api/sessions`` and the five GETs that
        follow it -- and the only one of the two that can work where the next
        request reaches a different process with an empty store. What it gives
        up to do that is set out in ``api/oneshot.py``.

        Blocks for the length of the fold (3-7 s), in a worker thread. CP-SAT
        holds the interpreter for whole seconds, and folding on the event loop
        would stall every concurrent request behind this one.
        """
        sess = _build_session(req, cfg)
        return await run_in_threadpool(oneshot.fold_full_night, sess, elements)

    @app.post("/api/night/stream", response_model=schemas.NightFrameOut, tags=["night"])
    async def create_night_stream(req: schemas.NightStreamRequest) -> StreamingResponse:
        """``/api/night``, reported while it happens -- and where an add goes.

        NDJSON: one ``NightFrameOut`` per line. Progress frames while the fold
        runs, then one ``night`` frame carrying exactly what ``/api/night``
        would have returned, or one ``error`` frame instead. A percentage needs
        somewhere to be reported FROM, and a response that is already open is
        the only such place a stateless server has.

        Everything that can be refused before the first byte still is, with a
        real status code: an unknown site, an unparseable date, an object the
        catalogue will not plan. After that the status is committed, so a fold
        that fails four seconds in arrives as an ``error`` frame carrying the
        code it would have been.

        With ``add``, the night is folded WITHOUT the object and the object is
        then added to the folded result from ``add.at`` onward -- locking every
        slot before it, so nothing lands in a part of the night that has
        already happened. On a night still under way ``at`` is held at the wall
        clock however far back the cursor has been dragged. An object the
        optimiser cannot fit into the time that is LEFT is refused with 409 and
        the reason, and the night comes back unamended.
        """
        sess = _build_session(req, cfg)
        addition = None
        if req.add is not None:
            addition = oneshot.Addition(
                target=_resolve_addition(sess, req.add), at=_utc(req.add.at)
            )

        loop = asyncio.get_running_loop()
        # `None` closes the stream. The queue belongs to the event loop, so the
        # fold -- which runs in a worker thread -- reaches it through
        # `call_soon_threadsafe` and never touches it directly.
        frames: asyncio.Queue[str | None] = asyncio.Queue()
        opening = _ndjson({"type": "progress", "fraction": 0.0, "message": "queued"})

        def emit(frame: dict[str, Any]) -> None:
            frames.put_nowait(_ndjson(frame))

        def on_progress(fraction: float, message: str) -> None:
            loop.call_soon_threadsafe(
                emit, {"type": "progress", "fraction": fraction, "message": message}
            )

        async def fold() -> None:
            try:
                night = await run_in_threadpool(
                    oneshot.fold_full_night,
                    sess,
                    elements,
                    on_progress=on_progress,
                    addition=addition,
                )
                emit({"type": "night", "night": jsonable_encoder(night)})
            except HTTPException as exc:
                emit({"type": "error", "status": exc.status_code, "detail": str(exc.detail)})
            except Exception as exc:  # never a truncated stream; always a reason
                emit({"type": "error", "status": 500, "detail": f"{type(exc).__name__}: {exc}"})
            finally:
                frames.put_nowait(None)

        async def body() -> AsyncIterator[str]:
            # Sent before the fold is even scheduled, so the headers and a
            # first chunk leave immediately: a proxy that waits for the first
            # byte stops being able to sit on the whole response.
            yield opening
            last = opening
            task = asyncio.create_task(fold())
            while True:
                try:
                    line = await asyncio.wait_for(frames.get(), HEARTBEAT_SECONDS)
                except TimeoutError:
                    # A stage that runs long -- a cold forecast fetch, one hard
                    # solve -- is still a live stream. Repeating the last frame
                    # says so; it is idempotent for the client.
                    yield last
                    continue
                if line is None:
                    break
                last = line
                yield line
            await task

        return StreamingResponse(
            body(),
            media_type="application/x-ndjson",
            headers={"cache-control": "no-store", "x-accel-buffering": "no"},
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
        sky_cache.pop(session_id, None)
        satellite_cache.pop(session_id, None)

    @app.post(
        "/api/sessions/{session_id}/refresh",
        response_model=schemas.SessionOut,
        tags=["sessions"],
    )
    async def refresh_session(session_id: str) -> schemas.SessionOut:
        """Check a live night for new data now, instead of at the next tick.

        The same check the watch runs every few minutes: one cheap question to
        the weather source, and a re-plan only if it has something newer. A
        night that was over when it was created has nothing to check (409);
        a check within a minute of the last one is refused (429), because the
        weather service is free and rate-limited.
        """
        sess = require(session_id)
        if sess.live is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "this night was over when the session was created; it is a replay "
                "and has nothing new to check",
            )
        outcome = await store.check(sess, manual=True)
        if outcome is live.CheckOutcome.TOO_SOON:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "checked less than a minute ago; try again shortly",
            )
        return mappers.session_out(sess)

    @app.post(
        "/api/sessions/{session_id}/targets",
        response_model=schemas.AmendOut,
        tags=["sessions"],
    )
    async def add_session_target(
        session_id: str, req: schemas.AddTargetRequest
    ) -> schemas.AmendOut:
        """Add an object to a planned night, and re-plan.

        A night still happening is amended from now; a night that is over,
        from ``at`` (the cursor), with every later forecast arrival re-solved
        on top. The past is locked either way. See ``api/amend.py``.

        200 means it is scheduled. An object the optimiser cannot give time to
        is refused with 409 and the reason, and the night is left exactly as it
        was -- so a caller never has to undo an add it was told had not
        happened. 422 is for an object the catalogue will not plan at all.
        """
        sess = require_ready(session_id)
        target = _resolve_addition(sess, req)
        at = _utc(req.at)
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, lambda: amend.add_target(sess, target, at=at))
        except amend.AmendError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        dp = result.decision
        bus.publish(
            {
                "type": "plan.available",
                "sessionId": sess.id,
                "decisionIndex": dp.index,
                "at": dp.at.isoformat(),
                "planId": dp.plan_id,
            }
        )
        return schemas.AmendOut(
            session_id=sess.id,
            target_id=result.target.id,
            target_name=result.target.name,
            at=dp.at,
            decision_index=dp.index,
            message=result.message,
        )

    catalog_cache: list[schemas.CatalogOut] = []

    @app.get("/api/catalog", response_model=schemas.CatalogOut, tags=["meta"])
    def get_catalog() -> Response:
        """Every catalogue object with its position, for the sky to draw and
        pick from. Fixed for the life of the process, and cached as such."""
        if not catalog_cache:
            catalog_cache.append(mappers.catalog_out())
        return JSONResponse(
            content=catalog_cache[0].model_dump(mode="json", by_alias=True),
            headers={"Cache-Control": "private, max-age=3600"},
        )

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
        "/api/sessions/{session_id}/geometry",
        response_model=schemas.GeometryOut,
        tags=["plan"],
    )
    def get_geometry(session_id: str, request: Request) -> Response:
        """Where everything is, all night. Deliberately has no ``as_of``.

        Independent of the cursor, so the client fetches it once when a session
        opens and never again however far the cursor moves. It is NOT immutable:
        adding a target to the night (``api/amend.py``) adds a row. So it is
        revalidated rather than cached forever -- the content ETag makes an
        unchanged geometry a 304 with no body.
        """
        sess = require(session_id)
        if sess.geometry is None:
            raise HTTPException(
                status.HTTP_425_TOO_EARLY,
                f"geometry is still being computed ({sess.progress:.0%}: {sess.message})",
            )
        etag = f'W/"{mappers.geometry_id(sess.geometry)}"'
        # Weak comparison, and a list: a conforming client may send several,
        # and a proxy is entitled to add its own.
        if etag in {v.strip() for v in request.headers.get("if-none-match", "").split(",")}:
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
        body = mappers.geometry_out(sess)
        return JSONResponse(
            content=body.model_dump(mode="json", by_alias=True),
            headers={
                "ETag": etag,
                # Private, not public: a session id is a capability here.
                "Cache-Control": "private, no-cache",
            },
        )

    @app.get("/api/sessions/{session_id}/sky", response_model=schemas.SkyOut, tags=["sky"])
    def get_sky(session_id: str, request: Request) -> Response:
        """Planets and sky darkness. Display only; no ``as_of``, like ``/geometry``.

        Derived from the geometry and nothing else, so it shares the geometry's
        content hash as its ETag and is cached as hard.
        """
        sess = require(session_id)
        if sess.geometry is None:
            raise HTTPException(
                status.HTTP_425_TOO_EARLY,
                f"geometry is still being computed ({sess.progress:.0%}: {sess.message})",
            )
        etag = f'W/"sky-{mappers.geometry_id(sess.geometry)}"'
        if etag in {v.strip() for v in request.headers.get("if-none-match", "").split(",")}:
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
        body = sky_cache.get(sess.id)
        if body is None:
            body = sky_cache[sess.id] = skyview.sky_out(sess)
        return JSONResponse(
            content=body.model_dump(mode="json", by_alias=True),
            headers={"ETag": etag, "Cache-Control": "private, max-age=31536000, immutable"},
        )

    @app.get(
        "/api/sessions/{session_id}/satellites",
        response_model=schemas.SatellitesOut,
        tags=["sky"],
    )
    def get_satellites(session_id: str) -> schemas.SatellitesOut:
        """Satellite passes across this night, every ten seconds. Display only.

        Answers 200 with ``available: false`` rather than an error when no
        elements can be had -- offline is the normal case at a dark site. A
        failure is not cached, so the next request tries again.
        """
        sess = require(session_id)
        cached = satellite_cache.get(sess.id)
        if cached is not None:
            return cached
        # One propagation at a time: a second request for the same session
        # waits for the first and then reads its result.
        with satellite_lock:
            cached = satellite_cache.get(sess.id)
            if cached is not None:
                return cached
            out = skyview.satellites_out(sess, elements)
            if out.available:
                satellite_cache[sess.id] = out
            return out

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


def _ndjson(frame: dict[str, Any]) -> str:
    """One newline-terminated frame.

    ``allow_nan=False`` deliberately, and it is the same setting Starlette's
    ``JSONResponse`` uses: Python will happily write a bare ``NaN``, and it is
    not JSON -- every browser's ``JSON.parse`` rejects it, which would turn one
    non-finite number deep in a grid into an unreadable response with nothing
    to say why. Raising here instead makes it an ``error`` frame.
    """
    return json.dumps(frame, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"


def _utc(t: datetime | None) -> datetime | None:
    """A naive instant is UTC, as everywhere else in this API."""
    if t is None:
        return None
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def _resolve_addition(sess: NightSession, req: schemas.AddTargetRequest) -> Target:
    """The object an add names. See ``amend.resolve_addition`` for the rules.

    Only the HTTP code is decided here: an object nothing can name is a bad
    request (422), which is a different thing from a night that cannot take
    one (409, raised later by the add itself).
    """
    try:
        return amend.resolve_addition(
            sess,
            target=req.target,
            ra_deg=req.ra_deg,
            dec_deg=req.dec_deg,
            magnitude=req.magnitude,
            name=req.name,
            is_point_source=req.is_point_source,
        )
    except amend.UnplannableError as exc:
        raise HTTPException(UNPROCESSABLE, str(exc)) from exc


def _build_session(req: schemas.SessionRequest, cfg: Settings) -> NightSession:
    start, end = _window(req)
    grid = TimeGrid.from_window(start, end, req.slot_minutes)

    try:
        eq = equipment.resolve(req.equipment_id, req.equipment)
    except LookupError as exc:
        raise HTTPException(UNPROCESSABLE, str(exc)) from exc

    site_preset, site = _resolve_site(req)
    targets = _resolve_targets(req)
    if not targets:
        raise HTTPException(UNPROCESSABLE, "no targets resolved")
    if req.weather not in WEATHER_SOURCES:
        raise HTTPException(
            UNPROCESSABLE,
            f"weather must be one of {list(WEATHER_SOURCES)}, got {req.weather!r}",
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
    # AsOf.live() rather than datetime.now(): core/clock.py is the one
    # sanctioned wall-clock reader in the package, and the AST guardrail
    # enforces it here too. The same instant resolves weather "auto" (a night
    # already over is replayed from archived runs; tonight or a coming night
    # gets the live forecast, stamped with this instant), so the session's
    # creation time and its weather's "now" can never disagree.
    now = AsOf.live()
    return NightSession(
        id=new_session_id(),
        name=req.name or f"{site.name} · night of {_night_of(grid.start, site.longitude_deg)}",
        created_at=now.t,
        spec=spec,
        site_preset=site_preset,
        equipment_preset=eq,
        weather_source=req.weather,
        now=now,
        solve_seconds=min(req.solve_seconds, cfg.solve_seconds * 4),
    )


def _window(req: schemas.SessionRequest) -> tuple[datetime, datetime]:
    try:
        return night_window(req.date, req.start_hour_utc, req.hours)
    except ValueError as exc:
        raise HTTPException(UNPROCESSABLE, f"bad date {req.date!r}: {exc}") from exc


def _resolve_site(
    req: schemas.SessionRequest | schemas.TonightRequest,
) -> tuple[presets.SitePreset, Site]:
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
            min_altitude_deg=r.min_altitude_deg,
            min_moon_separation_deg=r.min_moon_separation_deg,
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
    custom: set[str] = set()
    for r in req.targets:
        if r.ra_deg is not None and r.dec_deg is not None and r.magnitude is not None:
            custom.add(r.id)
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
            why = catalog.refusal(r.id) or f"{r.id!r} is not in the catalogue"
            raise HTTPException(
                UNPROCESSABLE, f"{why}; or supply raDeg, decDeg and magnitude to plan it anyway"
            )
        out.append(found)
    # Catalogue ids arrive in any spelling ("n7000", "NGC 7000") and leave
    # canonical, so two spellings of one object collapse into one target, which
    # keeps the most demanding of what was asked. A CUSTOM target is the
    # caller's own object: one that shares an id with anything else is an
    # ambiguity to report, not a duplicate to drop.
    merged: dict[str, Target] = {}
    for t in out:
        prev = merged.get(t.id)
        if prev is None:
            merged[t.id] = t
            continue
        if t.id in custom:
            raise HTTPException(
                UNPROCESSABLE,
                f"two targets have the id {t.id!r}; give the custom target a unique id",
            )
        goals = [g for g in (prev.snr_goal, t.snr_goal) if g is not None]
        merged[t.id] = replace(
            prev,
            priority=max(prev.priority, t.priority),
            snr_goal=max(goals) if goals else None,
        )
    return tuple(merged.values())


def _night_of(start: datetime, longitude_deg: float) -> str:
    """The local date a night is called by: the date of the local solar noon
    before it began. ``req.date`` is a UTC date, and at an American site the
    night of the 17th starts on the 18th in UTC."""
    return (start + timedelta(hours=longitude_deg / 15.0 - 12.0)).date().isoformat()


def _finite(obj: Any) -> Any:
    """``obj`` with every NaN or infinity replaced by its string, so it can be
    sent as JSON."""
    if isinstance(obj, float) and not math.isfinite(obj):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_finite(v) for v in obj]
    return obj


app = create_app()
