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
  folding in the background. There is no progress to report because there is
  nowhere to report it from; the client shows a spinner, not a percentage.

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
"""

from __future__ import annotations

from fastapi import HTTPException, status

from tscheduler.api import mappers, schemas, skyview
from tscheduler.api.session import NightSession, SessionStatus, fold_night
from tscheduler.providers.satellites.base import SatelliteElementsProvider


def fold_full_night(
    session: NightSession,
    elements: SatelliteElementsProvider,
) -> schemas.FullNightOut:
    """Fold ``session`` and return everything derived from it.

    Blocking and CPU-bound -- CP-SAT holds the interpreter for whole seconds --
    so callers run it in a worker thread rather than on the event loop.
    """
    fold_night(session)

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

    decisions = [
        schemas.DecisionPlanOut(
            index=dp.index,
            at=dp.at,
            plan=mappers.plan_out(session, dp.index),
            grid=mappers.quality_grid_out(session, dp.index),
        )
        for dp in session.decision_points
    ]

    return schemas.FullNightOut(
        session=mappers.session_out(session),
        geometry=mappers.geometry_out(session),
        sky=skyview.sky_out(session),
        satellites=skyview.satellites_out(session, elements),
        decisions=decisions,
    )
