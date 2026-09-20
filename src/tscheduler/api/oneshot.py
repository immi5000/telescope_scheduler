"""A whole night, folded once and handed over in a single response.

The stateful API keeps a folded night in memory and serves slices of it:
``/plan?as_of=``, ``/grid?as_of=``, ``/geometry``. That is the right shape for
a process that outlives the request. It is the wrong one for a platform where
the next request lands in a different process holding an empty dict, which is
what deploying to a serverless host means.

So this module answers the same question while keeping nothing at all. One
POST folds the night and returns every slice at once. For a four-decision
night that is ~900 KB of JSON, ~250 KB gzipped -- the same bytes the stateful
API sends across six requests, minus the six round trips. The client holds it
and scrubs against its own copy, so a slider drag costs nothing.

Three consequences, stated rather than discovered:

* **The response takes as long as the fold** -- 3-7 s, against the
  milliseconds ``POST /api/sessions`` took before returning ``202`` and
  folding in the background. ``on_progress`` is how that wait is made legible:
  ``POST /api/night/stream`` reports it frame by frame down the same
  connection it will deliver the night on, so a percentage needs no session to
  ask about. Plain ``POST /api/night`` passes no callback and is unchanged.

* **A night is not watched.** Nothing re-plans at 02:00, because nothing is
  holding the night at 02:00. The live edge survives as a *pull*: POST the
  same request again and the fold re-runs against whatever the forecast says
  now. ``api/live.py``'s rule is untouched -- every plan is still built at its
  own ``as_of``, and the past is still locked -- it is simply the client, not
  a background task, that decides when to ask.

* **Satellites are included.** They are half the payload (~160 KB gzipped) and
  the sky view is the only thing that wants them, but splitting them back out
  would reintroduce a second request that needs the session to still exist.
  A failure here is not an error: ``available: false`` is the normal answer at
  a dark site with no network, and the fold succeeds regardless.

ADDING AN OBJECT is the same fold with one more step, and it is here rather
than in the client for the reason set out in ``api/amend.py``: an object added
at 02:00 may only be given time *after* 02:00, and the only thing that can
promise that is a solve whose earlier slots are locked to the plan already
handed over. Re-folding the night from dusk with one extra target -- which is
what the client did while this module had no amendment -- cannot promise it,
and routinely scheduled the new object hours before the observer asked for it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException, status

from tscheduler.api import amend, mappers, schemas, skyview
from tscheduler.api.session import NightSession, SessionStatus, fold_night
from tscheduler.core.clock import AsOf
from tscheduler.domain.targets import Target
from tscheduler.providers.satellites.base import SatelliteElementsProvider

#: Reported as the fold runs, as ``(fraction, message)`` over the whole job --
#: geometry, weather, every CP-SAT solve, and the derived payloads after them.
#: Monotone and bounded to [0, 1]; the message is the stage, in the words the
#: UI shows.
Progress = Callable[[float, str], None]

#: What share of the wait the fold itself is, measured rather than guessed: a
#: four-decision night spends 3.2 s folding against 0.75 s deriving the sky,
#: the satellite passes and the per-decision plans and grids. An amendment
#: takes a slice out of the middle, because it is more CP-SAT on top.
FOLD_SHARE = 0.80
AMENDED_FOLD_SHARE = 0.55
AMENDED_SHARE = 0.82


@dataclass(frozen=True, slots=True)
class Addition:
    """One object to add to the night once it has been folded.

    ``at`` is the instant the observer asked from -- the cursor. It is a
    request, not a promise: :func:`fold_full_night` will not let it name an
    instant that has already passed on a night still happening, because a
    re-plan from there would rewrite a stretch of the night the observer was
    already handed.
    """

    target: Target
    at: datetime | None = None


def fold_full_night(
    session: NightSession,
    elements: SatelliteElementsProvider,
    *,
    on_progress: Progress | None = None,
    addition: Addition | None = None,
) -> schemas.FullNightOut:
    """Fold ``session`` and return everything derived from it.

    Blocking and CPU-bound -- CP-SAT holds the interpreter for whole seconds --
    so callers run it in a worker thread rather than on the event loop.

    With an ``addition``, the night is folded WITHOUT the object first and the
    object is then added to the folded result. That order is the whole point:
    it is what gives the amendment a plan to lock the past against, and so
    what confines the new object to the time still ahead.
    """

    def report(fraction: float, message: str) -> None:
        if on_progress is not None:
            on_progress(min(max(fraction, 0.0), 1.0), message)

    fold_share = AMENDED_FOLD_SHARE if addition is not None else FOLD_SHARE

    def folding(fraction: float, message: str) -> None:
        # The fold signs off as "ready", and of the fold it is -- but the sky,
        # the satellite passes and every plan are still to come, so reporting
        # it verbatim would answer "is the night ready" with a yes at 55%.
        report(
            fraction * fold_share,
            "the night is planned" if message.startswith("ready:") else message,
        )

    fold_night(session, on_progress=folding)

    if session.status is SessionStatus.FAILED:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            session.error or "the fold failed",
        )
    if not session.decision_points or session.geometry is None:
        # Not reachable through a successful fold; a guard rather than a
        # promise, so a future change fails loudly instead of serving a
        # half-built night.
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "the fold produced no decision points",
        )

    amended: schemas.AmendOut | None = None
    if addition is not None:
        amended = _add(session, addition, report)

    derived = AMENDED_SHARE if addition is not None else FOLD_SHARE
    span = 1.0 - derived

    report(derived, "placing every object in the sky")
    geometry = mappers.geometry_out(session)
    report(derived + 0.15 * span, "measuring how dark the sky gets")
    sky = skyview.sky_out(session)
    report(derived + 0.30 * span, "propagating satellite passes")
    satellites = skyview.satellites_out(session, elements)

    report(derived + 0.85 * span, "writing up the plans")
    decisions = [
        schemas.DecisionPlanOut(
            index=dp.index,
            at=dp.at,
            plan=mappers.plan_out(session, dp.index),
            grid=mappers.quality_grid_out(session, dp.index),
        )
        for dp in session.decision_points
    ]

    report(1.0, amended.message if amended is not None else _ready(session))
    return schemas.FullNightOut(
        session=mappers.session_out(session),
        geometry=geometry,
        sky=sky,
        satellites=satellites,
        decisions=decisions,
        amend=amended,
    )


def _add(session: NightSession, addition: Addition, report: Progress) -> schemas.AmendOut:
    """Add the object to the folded night, from ``at`` onward. Refuses, or 409s.

    The refusal is the feature, not a failure mode: an object the optimiser
    cannot give time to after ``at`` leaves the night exactly as it was, and
    travels back with the reason, so the caller never has to undo an add it
    was told had not happened.
    """
    grid = session.spec.grid
    # The session's creation instant, which is also the instant its weather was
    # resolved at. ``core/clock.py`` is the one sanctioned wall-clock reader and
    # the API layer has already been through it, so nothing here reads a clock.
    now = session.now or AsOf.at(grid.end)
    at = addition.at or grid.start
    if now.t < grid.end:
        # A night that has not ended is amended from NOW, never from a cursor
        # dragged back into it. `api/amend.py` makes the same rule for a
        # session with a live watch; a stateless fold has no watch to ask, so
        # the comparison is made here instead. Without it, adding an object at
        # 02:00 while reviewing 22:00 would re-plan four hours the observer has
        # already been given -- and could give the new object time at 22:15,
        # which is exactly the answer that cannot be acted on.
        at = max(at, now.t)

    report(AMENDED_FOLD_SHARE, f"re-planning the night around {addition.target.name}")
    span = AMENDED_SHARE - AMENDED_FOLD_SHARE
    try:
        result = amend.add_target(
            session,
            addition.target,
            at=at,
            now=now,
            on_progress=lambda f, m: report(AMENDED_FOLD_SHARE + f * span, m),
        )
    except amend.UnplannableError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except amend.AmendError as exc:
        # Infeasible, already planned, or asked of a night that cannot take it.
        # 409 either way: the request was fine, the night could not take it.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    return schemas.AmendOut(
        session_id=session.id,
        target_id=result.target.id,
        target_name=result.target.name,
        at=result.decision.at,
        decision_index=result.decision.index,
        message=result.message,
    )


def _ready(session: NightSession) -> str:
    points = len(session.decision_points)
    return f"ready: {points} decision points, {session.distinct_plans} distinct plans"
