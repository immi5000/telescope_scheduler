"""The naked-eye planets: the sky view, and targets a night can be given.

Positions never reach a ledger, so there is still no as-of question to answer
and this module sits outside the provider gate the way the thumbnails do. What
changed is that a planet can now be SCHEDULED, which needs two things a dot on
a screen never did: an apparent diameter, and a surface brightness derived
from it. See :func:`disc_surface_brightness` for what that figure is and what
it is not.

A planet is resolved per NIGHT rather than from the catalogue, because where it
is and how bright it is both depend on the date; ``api.presets.planet_target``
is where a name becomes a target.

Positions come from astropy's own ``get_body`` in the observer's AltAz frame --
the same transform the targets and the Moon go through -- so a planet lands on
the sky exactly where the rest of the night's geometry says it should, and the
browser does no astronomy to put it there.

Magnitudes are the Astronomical Almanac phase laws (Explanatory Supplement,
1992): ``V = V(1,0) + 5 log10(r * delta) + phase terms``. They are good to
about 0.1 mag, which is far finer than a dot on a screen can show. Saturn's
rings are left out; in 2026 they are within a few degrees of edge-on, where
they add under 0.2 mag.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

import numpy as np
from astropy import units as u
from astropy.coordinates import AltAz, EarthLocation, get_body, get_body_barycentric
from astropy.time import Time
from numpy.typing import NDArray

from tscheduler.core.timegrid import TimeGrid
from tscheduler.domain.site import Site
from tscheduler.physics.geometry import configure_astropy_offline

__all__ = [
    "PLANETS",
    "PlanetTrack",
    "apparent_diameter_arcsec",
    "build_planet_tracks",
    "disc_surface_brightness",
    "planet_body",
    "planet_magnitude",
]

#: astropy body name, display name. Earth order, not brightness order: the
#: list is short enough that the order only matters for reading it.
PLANETS: Final = (
    ("mercury", "Mercury"),
    ("venus", "Venus"),
    ("mars", "Mars"),
    ("jupiter", "Jupiter"),
    ("saturn", "Saturn"),
    ("uranus", "Uranus"),
    ("neptune", "Neptune"),
)

#: V(1,0) and the phase-angle polynomial in degrees: V10 + c1*a + c2*a^2 + c3*a^3.
_PHASE_LAW: Final[dict[str, tuple[float, float, float, float]]] = {
    "mercury": (-0.42, 3.80e-2, -2.73e-4, 2.0e-6),
    "venus": (-4.40, 9.0e-4, 2.39e-4, -6.5e-7),
    "mars": (-1.52, 1.6e-2, 0.0, 0.0),
    "jupiter": (-9.40, 5.0e-3, 0.0, 0.0),
    "saturn": (-8.88, 4.4e-2, 0.0, 0.0),
    "uranus": (-7.19, 2.8e-3, 0.0, 0.0),
    "neptune": (-6.87, 0.0, 0.0, 0.0),
}


#: IAU 2015 equatorial radii, km. Equatorial rather than mean: the apparent
#: disc of an oblate planet seen near its equatorial plane is the equatorial
#: one, and for Jupiter and Saturn mean-vs-equatorial differs by 7% and 10% --
#: a fifth of a magnitude in a surface brightness, which is more than the phase
#: law's own error.
_RADIUS_KM: Final[dict[str, float]] = {
    "mercury": 2439.7,
    "venus": 6051.8,
    "mars": 3396.2,
    "jupiter": 71492.0,
    "saturn": 60268.0,
    "uranus": 25559.0,
    "neptune": 24764.0,
}

_AU_KM: Final = 149_597_870.7
_ARCSEC_PER_RAD: Final = 206_264.8062471


def planet_body(query: str) -> str | None:
    """The astropy body a name means, or None. Case and spacing are ignored."""
    want = query.strip().casefold().replace(" ", "")
    return next((b for b, _ in PLANETS if b == want), None)


def apparent_diameter_arcsec(body: str, distance_au: float) -> float:
    """Apparent equatorial diameter at ``distance_au``, in arcseconds."""
    radius = _RADIUS_KM[body]
    km = max(distance_au, 1e-9) * _AU_KM
    return float(2.0 * np.arctan(radius / km) * _ARCSEC_PER_RAD)


def disc_surface_brightness(v_mag: float, diameter_arcsec: float, phase_angle_deg: float) -> float:
    """Mean V surface brightness of the ILLUMINATED disc, in mag/arcsec^2.

    The scheduler plans in surface brightness -- signal per square arcsecond --
    so a planet's integrated magnitude is not the number the exposure model
    wants. The illuminated fraction is ``(1 + cos a) / 2``; dividing the flux
    over the whole disc instead would make a crescent Venus about two
    magnitudes per arcsec^2 fainter than it really is, at exactly the phase
    where it is most obviously bright.

    TWO KNOWN APPROXIMATIONS. This is a MEAN over the disc, so limb darkening
    and belt structure are averaged away. And Saturn's rings contribute to its
    V magnitude while only its disc is counted here, which makes Saturn come
    out brighter per arcsec^2 than an eyepiece shows.

    Neither is worth correcting at the precision this is used for: every planet
    lands fifteen to twenty magnitudes per arcsec^2 brighter than the faintest
    thing in the catalogue, and nothing downstream resolves a tenth of a
    magnitude at that end of the scale.
    """
    lit = (1.0 + math.cos(math.radians(phase_angle_deg))) / 2.0
    area = math.pi / 4.0 * diameter_arcsec**2 * max(lit, 1e-6)
    return float(v_mag + 2.5 * math.log10(max(area, 1e-12)))


@dataclass(frozen=True, slots=True)
class PlanetTrack:
    body: str
    name: str
    altitude_deg: NDArray[np.float64]
    """Per slot midpoint, like every other per-slot array on the wire."""
    azimuth_deg: NDArray[np.float64]
    ra_deg: float
    """Apparent place at the night's middle slot. For the info card only."""
    dec_deg: float
    magnitude: float
    """At the night's middle slot. A planet's brightness changes by hundredths
    of a magnitude over one night, so one value is the honest precision."""
    distance_au: float
    phase_angle_deg: float
    apparent_diameter_arcsec: float
    """Equatorial, at the night's middle slot."""
    surface_brightness: float
    """Mean V mag/arcsec^2 over the illuminated disc -- what a plan exposes
    for, and the one figure here the scheduler actually reads."""


def planet_magnitude(body: str, r_au: float, delta_au: float, phase_angle_deg: float) -> float:
    """Apparent V magnitude from heliocentric and geocentric distance."""
    v10, c1, c2, c3 = _PHASE_LAW[body]
    a = phase_angle_deg
    return float(v10 + 5.0 * np.log10(r_au * delta_au) + c1 * a + c2 * a * a + c3 * a * a * a)


def _distance_au(a: object, b: object) -> float:
    d = (a - b).norm()  # type: ignore[operator]
    return float(d.to_value(u.au))


def build_planet_tracks(site: Site, grid: TimeGrid) -> tuple[PlanetTrack, ...]:
    """Every naked-eye planet's altitude and azimuth at each slot midpoint."""
    configure_astropy_offline()
    location = EarthLocation(
        lat=site.latitude_deg * u.deg,
        lon=site.longitude_deg * u.deg,
        height=site.elevation_m * u.m,
    )
    times = Time(grid.mid_unix(), format="unix", scale="utc")
    frame = AltAz(obstime=times, location=location)
    mid = times[len(times) // 2]

    sun_b = get_body_barycentric("sun", mid)
    earth_b = get_body_barycentric("earth", mid)
    earth_sun = _distance_au(earth_b, sun_b)

    out: list[PlanetTrack] = []
    for body, name in PLANETS:
        apparent = get_body(body, times, location)
        aa = apparent.transform_to(frame)

        body_b = get_body_barycentric(body, mid)
        r = _distance_au(body_b, sun_b)
        delta = _distance_au(body_b, earth_b)
        # Law of cosines in the Sun-planet-Earth triangle. Clipped because at
        # opposition the argument can land a rounding error outside [-1, 1].
        cos_a = (r * r + delta * delta - earth_sun * earth_sun) / (2.0 * r * delta)
        phase = float(np.degrees(np.arccos(np.clip(cos_a, -1.0, 1.0))))

        centre = apparent[len(times) // 2]
        v = planet_magnitude(body, r, delta, phase)
        diameter = apparent_diameter_arcsec(body, delta)
        out.append(
            PlanetTrack(
                body=body,
                name=name,
                altitude_deg=np.asarray(aa.alt.deg, dtype=np.float64),
                azimuth_deg=np.asarray(aa.az.deg, dtype=np.float64) % 360.0,
                ra_deg=float(centre.ra.deg),
                dec_deg=float(centre.dec.deg),
                magnitude=v,
                distance_au=delta,
                phase_angle_deg=phase,
                apparent_diameter_arcsec=diameter,
                surface_brightness=disc_surface_brightness(v, diameter, phase),
            )
        )
    return tuple(out)
