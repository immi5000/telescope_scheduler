"""From (site, equipment, targets, providers, as_of) to a solvable problem.

The layer split that makes re-planning fast lives here:

  GEOMETRY   as_of-INDEPENDENT. Site + date + coordinates only. Computed once
             per session (~250 ms) and reused by every re-plan and all three
             comparison arms.
  CONDITIONS as_of-DEPENDENT. Pure numpy over the geometry, ~5 ms. Built in
             ``pipeline/conditions.py``, which keeps the sky decomposition and
             the named preference factors this module used to discard.
  SATELLITES as_of-DEPENDENT but SLOW, so it is cached separately rather than
             rebuilt per decision point. Not yet wired; see the plan's Stage 3.

A re-plan therefore never touches astropy.

``build_scheduler_input`` is now a thin adapter: it calls ``build_conditions``
and then ``scheduler_input_from``. The signature is unchanged on purpose --
seven callers and two test modules depend on it, and a refactor that also
moves a call site is a refactor you cannot bisect.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from tscheduler.core.clock import AsOf
from tscheduler.core.timegrid import TimeGrid
from tscheduler.domain.equipment import Camera, Mount, Optics
from tscheduler.domain.site import Site
from tscheduler.domain.targets import Target
from tscheduler.physics.geometry import NightGeometry, build_night_geometry
from tscheduler.physics.satellites import SatelliteRiskModel
from tscheduler.pipeline.conditions import ConditionsLayer, build_conditions
from tscheduler.providers.base import EvidenceLedger
from tscheduler.providers.weather.base import WeatherForecastProvider
from tscheduler.providers.weather.model import WeatherQuery
from tscheduler.scheduling.cpsat import SchedulerInput


@dataclass(frozen=True, slots=True)
class SessionSpec:
    site: Site
    grid: TimeGrid
    optics: Optics
    camera: Camera
    mount: Mount
    targets: tuple[Target, ...]
    snr_goal: float = 15.0
    t_sub_s: float = 90.0

    @property
    def fov_deg2(self) -> float:
        w, h = self.camera.fov_deg(self.optics)
        return w * h


def min_block_slots(min_block_minutes: float, slot_minutes: float) -> int:
    """The minimum block in whole slots, rounded UP.

    It is the shortest visit the observer asked for, so it may not come out
    shorter: 20 minutes on 15-minute slots is two slots, not one, and 15
    minutes on 2-minute slots is eight, not seven. The epsilon keeps an exact
    multiple (20 on 5) from rounding up on float noise.
    """
    return max(math.ceil(min_block_minutes / slot_minutes - 1e-9), 1)


def build_geometry(spec: SessionSpec) -> NightGeometry:
    """The expensive, as_of-independent half. Cache this per session."""
    return build_night_geometry(spec.site, spec.grid, spec.targets)


def build_scheduler_input(
    spec: SessionSpec,
    geo: NightGeometry,
    weather: WeatherForecastProvider,
    as_of: AsOf,
    *,
    previous_plan: dict[int, str | None] | None = None,
    locked: dict[int, str | None] | None = None,
    locked_observing: frozenset[int] | None = None,
    first_free_slot: int = 0,
    satellite_risk: SatelliteRiskModel | None = None,
) -> tuple[SchedulerInput, EvidenceLedger]:
    """The as_of-dependent half. Pure numpy once the forecast is in hand."""
    cond = build_conditions(spec, geo, weather, as_of, satellite_risk=satellite_risk)
    inp = scheduler_input_from(
        spec,
        geo,
        cond,
        previous_plan=previous_plan,
        locked=locked,
        locked_observing=locked_observing,
        first_free_slot=first_free_slot,
    )
    return inp, cond.ledger


def scheduler_input_from(
    spec: SessionSpec,
    geo: NightGeometry,
    cond: ConditionsLayer,
    *,
    previous_plan: dict[int, str | None] | None = None,
    locked: dict[int, str | None] | None = None,
    locked_observing: frozenset[int] | None = None,
    first_free_slot: int = 0,
) -> SchedulerInput:
    """Project a conditions layer onto exactly what the optimizer reads.

    Kept separate from ``build_conditions`` because re-scoring an existing plan
    under new conditions needs the layer but not the model, and re-solving with
    different locks needs the model but not a rebuilt layer.
    """
    grid = spec.grid
    n_t = len(spec.targets)
    return SchedulerInput(
        grid=grid,
        target_ids=tuple(t.id for t in spec.targets),
        eta=cond.eta,
        preference=cond.preference,
        visible=geo.visible,
        required_ref_seconds=cond.required_ref_seconds,
        weight=np.array([t.weight for t in spec.targets], dtype=float),
        subs_per_slot=cond.subs_per_slot,
        t_sub_s=np.full(n_t, spec.t_sub_s),
        snr_goal=np.array(
            [t.snr_goal if t.snr_goal is not None else spec.snr_goal for t in spec.targets],
            dtype=float,
        ),
        readout_s=np.full(n_t, spec.camera.readout_s),
        switch_slots=grid.minutes_to_slots(spec.mount.switch_minutes),
        switch_remainder=grid.fractional_remainder(spec.mount.switch_minutes),
        min_block_slots=min_block_slots(spec.mount.min_block_minutes, grid.slot_minutes),
        locked=locked or {},
        locked_observing=locked_observing,
        previous_plan=previous_plan or {},
        first_free_slot=first_free_slot,
    )


def conditions_arrays(
    spec: SessionSpec, weather: WeatherForecastProvider, as_of: AsOf
) -> tuple[NDArray[np.float64], NDArray[np.float64], EvidenceLedger]:
    q = WeatherQuery(
        lat=spec.site.latitude_deg,
        lon=spec.site.longitude_deg,
        valid_from=spec.grid.start,
        valid_to=spec.grid.end,
    )
    return weather.series(q, as_of, spec.grid)
