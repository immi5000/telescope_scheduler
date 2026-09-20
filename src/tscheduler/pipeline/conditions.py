"""The as_of-dependent layer, with its working kept.

``build_scheduler_input`` used to compute the sky decomposition and the five
named preference factors, hand the optimizer a single product, and drop the
rest on the floor. Both of those docstrings say an explanation engine needs the
parts -- see ``physics/sky.py`` and ``physics/quality.py`` -- so this module
returns them instead of discarding them.

That costs nothing: the arrays were already built. What it buys is a UI that
can say *why* a slot is bad ("the Moon contributes 71% of the sky flux here")
rather than only that it is, and a change panel that attributes a re-plan to a
named factor by exact arithmetic instead of a guess.

The split this module sits on:

  GEOMETRY   as_of-INDEPENDENT, ~250 ms of astropy, once per session.
  CONDITIONS as_of-DEPENDENT, pure numpy over that geometry, ~5 ms.  <- here

Everything here is a pure function of ``(spec, geo, weather-at-as_of)``. No
wall clock, no network beyond the provider call, and the provider applies the
publication gate itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

from tscheduler.core.clock import AsOf
from tscheduler.physics.geometry import NightGeometry
from tscheduler.physics.quality import efficiency, preference
from tscheduler.physics.satellites import NullSatelliteRisk, SatelliteRiskModel
from tscheduler.physics.sky import sky_brightness
from tscheduler.providers.base import EvidenceLedger
from tscheduler.providers.weather.base import WeatherForecastProvider
from tscheduler.providers.weather.model import WeatherQuery

if TYPE_CHECKING:  # builder imports us, so the arrow points one way at runtime
    from tscheduler.pipeline.builder import SessionSpec

__all__ = ["ConditionsLayer", "build_conditions", "download_duty_cycle"]

#: The named sky contributions, in nanoLamberts, in the order they are summed.
#: Kept as a tuple so the wire, the UI legend and the fingerprint cannot drift
#: apart the way three independent literal lists would.
SKY_COMPONENTS: tuple[str, ...] = ("natural", "artificial", "moon", "twilight", "cloudDelta")

#: The preference factors, geometric-mean exponents applied in quality.py.
PREFERENCE_FACTORS: tuple[str, ...] = ("altitude", "moon", "cloud", "seeing", "satellite")


@dataclass(frozen=True, slots=True)
class ConditionsLayer:
    """Everything the weather decides, for one ``as_of``.

    Shapes are ``(T, S)`` for the per-target maps and ``(S,)`` for the shared
    ones. ``ledger`` is the provenance of every record that went into it, and
    it is what the plan's no-lookahead assertion is made against.
    """

    target_ids: tuple[str, ...]

    # (S,)
    cloud_fraction: NDArray[np.float64]
    seeing_fwhm_arcsec: NDArray[np.float64]

    # (T, S)
    eta: NDArray[np.float64]
    """Efficiency. Physically calibrated; drives the completion constraint.
    Per wall-clock second of back-to-back frames, so the camera's download
    dead time is already taken out (see :func:`download_duty_cycle`)."""
    preference: NDArray[np.float64]
    """The product of the factors below. Drives only the objective."""
    preference_factors: dict[str, NDArray[np.float64]]
    """Each named factor separately. Because preference is a PRODUCT, the
    per-factor ``d log`` between two conditions layers is an exact attribution
    of why a slot changed -- which is what explains a re-plan without an LLM."""
    sky_mag_arcsec2: NDArray[np.float64]
    sky_components_nl: dict[str, NDArray[np.float64]]
    """Kept in nanoLamberts, never magnitudes. These are summed to reach the
    total, and summing magnitudes is the canonical bug in this domain. Note
    ``cloudDelta`` is legitimately NEGATIVE at a pristine site, where cloud
    darkens the sky rather than amplifying city glow -- so this is not a set of
    magnitudes that happen to be stored oddly, it is flux."""
    streak_per_sub: NDArray[np.float64]
    """Expected illuminated satellite trails per sub-exposure."""

    # (T,)
    required_ref_seconds: NDArray[np.float64]
    """E_t: reference-seconds needed to reach each target's SNR goal."""
    rho_reference: NDArray[np.float64]

    # (T, S) integer. Whole frames one slot holds ON ITS OWN -- not what a
    # block lists, which the scheduler counts over the whole block.
    subs_per_slot: NDArray[np.int64]

    ledger: EvidenceLedger

    # (S,), optional: what the forecast said beyond cloud and seeing. ``None``
    # when no record carried the field (synthetic weather, or no forecast at
    # all). Read by the weather outlook and the per-slot weather only; the
    # optimizer never sees them.
    humidity: NDArray[np.float64] | None = None
    """Relative humidity at 2 m, 0-1."""
    dew_spread_c: NDArray[np.float64] | None = None
    """Temperature minus dew point at 2 m, in degrees C."""
    wind_ms: NDArray[np.float64] | None = None
    """Mean wind at 10 m, m/s."""
    wind_gust_ms: NDArray[np.float64] | None = None
    """Gusts at 10 m, m/s."""
    temperature_c: NDArray[np.float64] | None = None
    """Air temperature at 2 m, degrees C."""
    dew_point_c: NDArray[np.float64] | None = None
    """Dew point at 2 m, degrees C."""
    precipitation_mm: NDArray[np.float64] | None = None
    """Precipitation, mm per hour."""
    cloud_low: NDArray[np.float64] | None = None
    """Low, mid and high cloud, 0-1 each; the total is ``cloud_fraction``."""
    cloud_mid: NDArray[np.float64] | None = None
    cloud_high: NDArray[np.float64] | None = None
    covered: NDArray[np.bool_] | None = None
    """(S,) True where a forecast reaches the slot. Where it is False,
    ``cloud_fraction`` and ``seeing_fwhm_arcsec`` are the clear-sky assumption
    and the optional fields above are NaN. ``None`` for a layer built without
    it, which reads as covered wherever the ledger has records."""

    @property
    def n_targets(self) -> int:
        return len(self.target_ids)

    @property
    def n_slots(self) -> int:
        return int(self.cloud_fraction.shape[0])


def download_duty_cycle(t_sub_s: float, readout_s: float) -> float:
    """The fraction of imaging time the shutter is open: ``t / (t + readout)``.

    ``quality.efficiency`` returns progress per second of OPEN SHUTTER. A plan
    credits wall-clock slot time, and between frames the camera is
    downloading, not exposing -- so eta is scaled by this once, here, and the
    optimizer, the progress bar and every block's expected SNR then count the
    same time. ``rho_reference`` stays the open-shutter rate, so ``E_t`` is
    still reference-seconds of open shutter.
    """
    return t_sub_s / (t_sub_s + max(readout_s, 0.0))


def build_conditions(
    spec: SessionSpec,
    geo: NightGeometry,
    weather: WeatherForecastProvider,
    as_of: AsOf,
    *,
    satellite_risk: SatelliteRiskModel | None = None,
) -> ConditionsLayer:
    """Build the whole as_of-dependent layer, keeping every intermediate."""
    grid = spec.grid
    n_t, n_s = len(spec.targets), grid.n_slots

    wx = weather.weather_series(
        WeatherQuery(
            lat=spec.site.latitude_deg,
            lon=spec.site.longitude_deg,
            valid_from=grid.start,
            valid_to=grid.end,
        ),
        as_of,
        grid,
    )
    cloud, seeing, ledger = wx.cloud, wx.seeing, wx.ledger

    # Streak risk enters ONLY preference, never efficiency: a trail is removed
    # in post by sigma-clipping, so it is a cost to weigh, not signal never
    # collected.
    risk = satellite_risk or NullSatelliteRisk()
    slots = np.arange(n_s, dtype=np.int64)
    fov = spec.fov_deg2

    eta = np.zeros((n_t, n_s))
    pref = np.zeros((n_t, n_s))
    factors = {k: np.zeros((n_t, n_s)) for k in PREFERENCE_FACTORS}
    sky_mag = np.zeros((n_t, n_s))
    sky_parts = {k: np.zeros((n_t, n_s)) for k in SKY_COMPONENTS}
    streak = np.zeros((n_t, n_s))
    need = np.zeros(n_t)
    rho_ref = np.zeros(n_t)
    subs = np.zeros((n_t, n_s), dtype=np.int64)
    duty = download_duty_cycle(spec.t_sub_s, spec.camera.readout_s)
    # No floor of one: a slot shorter than a frame holds none on its own. The
    # scheduler does not read this -- it counts a block's frames over the whole
    # block, since frames run across slot boundaries.
    per_slot_subs = int(grid.slot_seconds // (spec.t_sub_s + spec.camera.readout_s))

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
        sky_mag[i] = sky.total_mag_arcsec2
        sky_parts["natural"][i] = sky.natural_nl
        sky_parts["artificial"][i] = sky.artificial_nl
        sky_parts["moon"][i] = sky.moon_nl
        sky_parts["twilight"][i] = sky.twilight_nl
        sky_parts["cloudDelta"][i] = sky.cloud_delta_nl

        e, rho = efficiency(
            target_magnitude=tgt.magnitude,
            airmass=geo.airmass[i],
            sky_mag_arcsec2=sky.total_mag_arcsec2,
            seeing_fwhm_arcsec=seeing,
            cloud_fraction=cloud,
            optics=spec.optics,
            camera=spec.camera,
            t_sub_s=spec.t_sub_s,
            extinction_k=spec.site.extinction_k,
            point_source=tgt.is_point_source,
        )
        eta[i] = np.where(geo.visible[i], e * duty, 0.0)
        rho_ref[i] = rho

        streak[i] = risk.streak_rate_per_min(
            geo.altitude_deg[i], geo.azimuth_deg[i], slots, fov
        ) * (spec.t_sub_s / 60.0)

        got = preference(
            altitude_deg=geo.altitude_deg[i],
            moon_separation_deg=geo.moon_separation_deg[i],
            cloud_fraction=cloud,
            seeing_fwhm_arcsec=seeing,
            satellite_risk=streak[i],
            min_altitude_deg=spec.site.min_altitude_deg,
            min_moon_separation_deg=spec.site.min_moon_separation_deg,
        )
        pref[i] = got["total"]
        for k in PREFERENCE_FACTORS:
            factors[k][i] = got[k]

        goal = tgt.snr_goal if tgt.snr_goal is not None else spec.snr_goal
        need[i] = (goal**2) / rho if rho > 0 else np.inf
        subs[i] = per_slot_subs

    # inf would overflow the integer objective scaling; a large finite value is
    # equivalent (such a target is unschedulable either way) and stays safe.
    need = np.where(np.isfinite(need), need, 1e12)

    return ConditionsLayer(
        target_ids=tuple(t.id for t in spec.targets),
        cloud_fraction=cloud,
        seeing_fwhm_arcsec=seeing,
        eta=eta,
        preference=pref,
        preference_factors=factors,
        sky_mag_arcsec2=sky_mag,
        sky_components_nl=sky_parts,
        streak_per_sub=streak,
        required_ref_seconds=need,
        rho_reference=rho_ref,
        subs_per_slot=subs,
        ledger=ledger,
        humidity=wx.humidity,
        dew_spread_c=wx.dew_spread_c,
        wind_ms=wx.wind_ms,
        wind_gust_ms=wx.wind_gust_ms,
        temperature_c=wx.temperature_c,
        dew_point_c=wx.dew_point_c,
        precipitation_mm=wx.precipitation_mm,
        cloud_low=wx.cloud_low,
        cloud_mid=wx.cloud_mid,
        cloud_high=wx.cloud_high,
        covered=wx.covered,
    )
