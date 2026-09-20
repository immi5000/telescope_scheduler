"""The sky around the plan: planets, sky darkness and satellites.

Everything here is DISPLAY ONLY. None of it reaches a grid, a ledger or a plan,
so -- like the thumbnails -- it sits outside the as-of machinery. That is a
statement about where the data goes, not a relaxation of the rule: the moment
any of this starts influencing a schedule it has to move behind a provider.

The satellite elements are the one input with a clock in it. They come from a
``SatelliteElementsProvider`` asked at ``AsOf.live()``, because a display of
"where the ISS is" can only ever use the elements we hold now; the payload
carries their publication time and epoch spread so a replay days in the past
is visibly an estimate rather than silently one.
"""

from __future__ import annotations

import numpy as np

from tscheduler.api import schemas
from tscheduler.api.mappers import _f, _fl, dec_sexagesimal, ra_sexagesimal
from tscheduler.api.session import NightSession
from tscheduler.core.clock import AsOf
from tscheduler.physics.convert import naked_eye_limit_mag
from tscheduler.physics.planets import build_planet_tracks
from tscheduler.physics.satellite_tracks import build_satellite_passes
from tscheduler.physics.sky import sky_brightness
from tscheduler.providers.satellites.base import SatelliteElementsProvider, TleQuery

#: CelesTrak's "brightest" group: ~160 objects including the ISS, Tiangong and
#: Hubble. These are the satellites a person actually sees cross the sky.
SATELLITE_GROUP = "visual"


def sky_out(sess: NightSession) -> schemas.SkyOut:
    geo = sess.geometry
    assert geo is not None
    site = sess.spec.site
    n = geo.n_slots

    # The zenith, through the scheduler's own sky model. The Moon's distance
    # from the zenith is its zenith angle, which is all the scattering term
    # needs; cloud is left out on purpose, because this says how dark the
    # night CAN be and the weather is drawn separately.
    zenith = sky_brightness(
        target_altitude_deg=np.full(n, 90.0),
        moon_altitude_deg=geo.moon_altitude_deg,
        moon_separation_deg=90.0 - geo.moon_altitude_deg,
        moon_phase_angle_deg=geo.moon_phase_angle_deg,
        sun_altitude_deg=geo.sun_altitude_deg,
        natural_zenith_mag_arcsec2=site.natural_zenith_mag_arcsec2,
        artificial_zenith_nl=site.artificial_zenith_nl,
        extinction_k=site.extinction_k,
    )
    mu = np.minimum(zenith.total_mag_arcsec2, 23.0)

    planets = [
        schemas.PlanetOut(
            id=p.body,
            name=p.name,
            altitude_deg=_fl(p.altitude_deg, 3),
            azimuth_deg=_fl(p.azimuth_deg, 3),
            ra_deg=_f(p.ra_deg, 4),
            dec_deg=_f(p.dec_deg, 4),
            ra=ra_sexagesimal(p.ra_deg),
            dec=dec_sexagesimal(p.dec_deg),
            magnitude=_f(p.magnitude, 2),
            distance_au=_f(p.distance_au, 4),
            phase_angle_deg=_f(p.phase_angle_deg, 2),
        )
        for p in build_planet_tracks(site, sess.spec.grid)
    ]
    return schemas.SkyOut(
        session_id=sess.id,
        n_slots=n,
        planets=planets,
        zenith_sky_mag_arcsec2=_fl(mu, 3),
        naked_eye_limit_mag=_fl(naked_eye_limit_mag(mu), 2),
    )


def satellites_out(
    sess: NightSession, provider: SatelliteElementsProvider
) -> schemas.SatellitesOut:
    """Propagate the current elements across this night.

    Never raises for an upstream failure: an offline field laptop is the
    normal case, and "no satellites, because CelesTrak is unreachable" is an
    answer the sky can show, where a 502 would only be an error it has to
    explain.
    """

    def unavailable(reason: str) -> schemas.SatellitesOut:
        return schemas.SatellitesOut(
            session_id=sess.id,
            available=False,
            reason=reason,
            source=provider.source_id,
            elements_published_at=None,
            epoch_spread_days=0.0,
            n_objects=0,
            step_seconds=0.0,
            passes=[],
        )

    try:
        records = provider.fetch(TleQuery(SATELLITE_GROUP), AsOf.live())
    except Exception as exc:
        return unavailable(f"orbital elements unavailable: {type(exc).__name__}: {exc}")
    if not len(records):
        return unavailable("the element source returned nothing")

    site = sess.spec.site
    grid = sess.spec.grid
    tracks = build_satellite_passes(
        [(r.norad_id, r.name, r.line1, r.line2) for r in records.values()],
        site.latitude_deg,
        site.longitude_deg,
        site.elevation_m,
        grid.start,
        grid.end,
    )
    if tracks.n_propagated == 0:
        return unavailable(
            "the orbital elements we hold are more than two weeks from this night, "
            "too far for SGP4 to place a satellite honestly"
        )

    def mags(m: np.ndarray) -> list[float | None]:
        return [None if not np.isfinite(v) else round(float(v), 1) for v in m]

    return schemas.SatellitesOut(
        session_id=sess.id,
        available=True,
        source=f"{provider.source_id} ({SATELLITE_GROUP})",
        elements_published_at=max(r.published_at for r in records.records),
        epoch_spread_days=_f(tracks.epoch_spread_days, 2),
        n_objects=tracks.n_propagated,
        step_seconds=tracks.step_seconds,
        passes=[
            schemas.SatellitePassOut(
                norad_id=p.norad_id,
                name=p.name,
                starts_at=p.start,
                step_seconds=p.step_seconds,
                altitude_deg=_fl(p.altitude_deg, 2),
                azimuth_deg=_fl(p.azimuth_deg, 2),
                magnitude=mags(p.magnitude),
                range_km=_fl(p.range_km, 0),
            )
            for p in tracks.passes
        ],
    )
