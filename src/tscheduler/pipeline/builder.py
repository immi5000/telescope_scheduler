"""From (site, equipment, targets, providers, as_of) to a solvable problem.

The layer split that makes re-planning fast lives here:

  GEOMETRY   as_of-INDEPENDENT. Site + date + coordinates only. Computed once
             per session (~250 ms) and reused by every re-plan and all three
             comparison arms.
  CONDITIONS as_of-DEPENDENT. Pure numpy over the geometry, ~5 ms.

A re-plan therefore never touches astropy.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from tscheduler.core.clock import AsOf
from tscheduler.core.timegrid import TimeGrid
from tscheduler.domain.equipment import Camera, Mount, Optics
from tscheduler.domain.site import Site
from tscheduler.domain.targets import Target
from tscheduler.physics.geometry import NightGeometry, build_night_geometry
from tscheduler.physics.quality import efficiency, preference
from tscheduler.physics.satellites import NullSatelliteRisk, SatelliteRiskModel
from tscheduler.physics.sky import sky_brightness
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
    snr_goal: float = 20.0
    t_sub_s: float = 90.0

    @property
    def fov_deg2(self) -> float:
        w, h = self.camera.fov_deg(self.optics)
        return w * h


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
    first_free_slot: int = 0,
    satellite_risk: SatelliteRiskModel | None = None,
) -> tuple[SchedulerInput, EvidenceLedger]:
    """The as_of-dependent half. Pure numpy once the forecast is in hand."""
    grid = spec.grid
    n_t, n_s = len(spec.targets), grid.n_slots

    q = WeatherQuery(
        lat=spec.site.latitude_deg,
        lon=spec.site.longitude_deg,
        valid_from=grid.start,
        valid_to=grid.end,
    )
    cloud, seeing, ledger = weather.series(q, as_of, grid)

    # Streak risk is expected illuminated trails per exposure. It enters ONLY
    # the preference term, never efficiency: a streak is mitigated in
    # post-processing by sigma-clipping, so it is a cost to be weighed rather
    # than signal that was never collected.
    risk = satellite_risk or NullSatelliteRisk()
    slots = np.arange(n_s, dtype=np.int64)
    fov = spec.fov_deg2

    eta = np.zeros((n_t, n_s))
    pref = np.zeros((n_t, n_s))
    need = np.zeros(n_t)
    subs = np.zeros((n_t, n_s), dtype=np.int64)
    per_slot_subs = max(int(grid.slot_seconds // (spec.t_sub_s + spec.camera.readout_s)), 1)

    for i, tgt in enumerate(spec.targets):
        sky = sky_brightness(
            target_altitude_deg=geo.altitude_deg[i],
            target_azimuth_deg=geo.azimuth_deg[i],
            moon_altitude_deg=geo.moon_altitude_deg,
            moon_separation_deg=geo.moon_separation_deg[i],
            moon_phase_angle_deg=geo.moon_phase_angle_deg,
            sun_altitude_deg=geo.sun_altitude_deg,
            cloud_fraction=cloud,
            natural_zenith_mag_arcsec2=spec.site.natural_zenith_mag_arcsec2,
            artificial_zenith_nl=spec.site.artificial_zenith_nl,
            extinction_k=spec.site.extinction_k,
        )
        e, rho_ref = efficiency(
            target_magnitude=tgt.magnitude,
            airmass=geo.airmass[i],
            sky_mag_arcsec2=sky.total_mag_arcsec2,
            seeing_fwhm_arcsec=seeing,
            cloud_fraction=cloud,
            optics=spec.optics,
            camera=spec.camera,
            t_sub_s=spec.t_sub_s,
            extinction_k=spec.site.extinction_k,
        )
        eta[i] = np.where(geo.visible[i], e, 0.0)
        streak = risk.streak_rate_per_min(geo.altitude_deg[i], geo.azimuth_deg[i], slots, fov) * (
            spec.t_sub_s / 60.0
        )
        pref[i] = preference(
            altitude_deg=geo.altitude_deg[i],
            moon_separation_deg=geo.moon_separation_deg[i],
            cloud_fraction=cloud,
            seeing_fwhm_arcsec=seeing,
            satellite_risk=streak,
            min_altitude_deg=spec.site.min_altitude_deg,
            min_moon_separation_deg=spec.site.min_moon_separation_deg,
        )["total"]
        goal = tgt.snr_goal if tgt.snr_goal is not None else spec.snr_goal
        need[i] = (goal**2) / rho_ref if rho_ref > 0 else np.inf
        subs[i] = per_slot_subs

    # inf would overflow the integer objective scaling; a large finite value is
    # equivalent (such a target is unschedulable either way) and stays safe.
    need = np.where(np.isfinite(need), need, 1e12)

    inp = SchedulerInput(
        grid=grid,
        target_ids=tuple(t.id for t in spec.targets),
        eta=eta,
        preference=pref,
        visible=geo.visible,
        required_ref_seconds=need,
        weight=np.array([t.weight for t in spec.targets], dtype=float),
        subs_per_slot=subs,
        t_sub_s=np.full(n_t, spec.t_sub_s),
        switch_slots=grid.minutes_to_slots(spec.mount.switch_minutes),
        switch_remainder=grid.fractional_remainder(spec.mount.switch_minutes),
        min_block_slots=max(int(spec.mount.min_block_minutes // grid.slot_minutes), 1),
        locked=locked or {},
        previous_plan=previous_plan or {},
        first_free_slot=first_free_slot,
    )
    return inp, ledger


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
