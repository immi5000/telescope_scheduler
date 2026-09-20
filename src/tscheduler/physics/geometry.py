"""Night geometry: where everything is, all night.

This layer is deliberately as_of-INDEPENDENT. It depends only on the site, the
date and the target coordinates -- never on the weather -- so it is computed
once per session and reused across every re-plan and all three comparison arms.
That split is what makes a re-plan ~5 ms of numpy instead of a rebuild.

Everything is vectorised: ONE SkyCoord[T] x Time[S] transform, not T*S scalar
calls. At 15 targets x 120 slots the scalar version takes tens of seconds and
the vectorised version takes a few hundred milliseconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from astropy import units as u
from astropy.coordinates import (
    AltAz,
    EarthLocation,
    SkyCoord,
    get_body,
    solar_system_ephemeris,
)
from astropy.time import Time
from astropy.utils import iers
from numpy.typing import NDArray

from tscheduler.core.timegrid import TimeGrid
from tscheduler.domain.site import Site
from tscheduler.domain.targets import Target
from tscheduler.physics.airmass import airmass_kasten_young

__all__ = ["NightGeometry", "build_night_geometry", "configure_astropy_offline"]


def configure_astropy_offline() -> None:
    """Stop astropy reaching for the network mid-solve.

    A blocking IERS download at 2 a.m. would blow the interactive time budget,
    and on an observatory laptop with no connectivity it hangs outright. The
    accuracy cost of stale IERS tables is milliarcseconds -- utterly irrelevant
    when our moon-separation limit is 15 DEGREES.
    """
    iers.conf.auto_download = False
    iers.conf.iers_degraded_accuracy = "ignore"
    solar_system_ephemeris.set("builtin")


@dataclass(frozen=True, slots=True)
class NightGeometry:
    """Per-target and per-slot geometry. Shapes are (T, S) and (S,)."""

    grid: TimeGrid
    target_ids: tuple[str, ...]

    # (T, S)
    altitude_deg: NDArray[np.float64]
    azimuth_deg: NDArray[np.float64]
    airmass: NDArray[np.float64]
    """inf below the horizon -- never NaN, which would poison argmax silently."""
    moon_separation_deg: NDArray[np.float64]

    # (S,)
    sun_altitude_deg: NDArray[np.float64]
    sun_azimuth_deg: NDArray[np.float64]
    lst_hours: NDArray[np.float64]
    """Local apparent sidereal time, hours in [0, 24). **Display only.**

    Do NOT build the sky frame from this. ``RA = LST, Dec = latitude`` is the
    zenith only in APPARENT coordinates of date, and our targets and every star
    catalogue are ICRS/J2000 -- the difference is precession, which in 2026 is
    0.32 degrees. That is nineteen arcminutes of silently rotated sky, and it
    renders perfectly plausibly. Use ``frame_quat``.
    """
    moon_altitude_deg: NDArray[np.float64]
    moon_azimuth_deg: NDArray[np.float64]
    moon_phase_angle_deg: NDArray[np.float64]
    """Lunar phase angle alpha: 0 = full, 180 = new. This is what Krisciunas &
    Schaefer's model takes -- NOT the illuminated fraction."""
    moon_illumination: NDArray[np.float64]

    # (T, S)
    visible: NDArray[np.bool_]
    """Altitude floor AND the hard lunar exclusion, combined once here so no
    downstream consumer can forget one of them."""

    # (S, 4)
    frame_quat: NDArray[np.float64]
    """Per slot, the rotation taking the observer's horizon basis into ICRS,
    as a quaternion ``(x, y, z, w)``.

    The horizon basis is right-handed ``(East, North, Up)``; ICRS is the usual
    right-handed ``(cos d cos a, cos d sin a, sin d)``. So a target's ICRS unit
    vector ``v`` has local components ``R^T v``, whose third element is
    ``sin(altitude)``.

    This exists so the browser performs NO astronomy. It is astropy's own
    transform, sampled at the three basis directions and orthonormalised --
    not a re-derivation the client could get subtly wrong. ICRS to horizontal
    is not *exactly* rigid (annual aberration is direction-dependent, up to
    ~20 arcsec), so this closes on ``altitude_deg`` to ~30 arcsec rather than
    to zero. That floor is physical, and
    ``tests/unit/test_geometry.py::test_frame_quaternion_reproduces_altaz``
    pins it.
    """

    @property
    def n_targets(self) -> int:
        return len(self.target_ids)

    @property
    def n_slots(self) -> int:
        return self.grid.n_slots

    def arrays(self) -> dict[str, NDArray[Any]]:
        """Every array, for fingerprinting. Mixed dtype: `visible` is bool."""
        return {
            "altitude_deg": self.altitude_deg,
            "azimuth_deg": self.azimuth_deg,
            "airmass": self.airmass,
            "moon_separation_deg": self.moon_separation_deg,
            "sun_altitude_deg": self.sun_altitude_deg,
            "sun_azimuth_deg": self.sun_azimuth_deg,
            "lst_hours": self.lst_hours,
            "moon_altitude_deg": self.moon_altitude_deg,
            "moon_phase_angle_deg": self.moon_phase_angle_deg,
            "moon_illumination": self.moon_illumination,
            "visible": self.visible,
            "frame_quat": self.frame_quat,
        }


def _icrs_unit(ra_deg: NDArray[np.float64], dec_deg: NDArray[np.float64]) -> NDArray[np.float64]:
    ra, dec = np.radians(ra_deg), np.radians(dec_deg)
    return np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], axis=-1)


def _quat_from_matrix(r: NDArray[np.float64]) -> NDArray[np.float64]:
    """(S,3,3) rotations to (S,4) quaternions ``(x, y, z, w)``.

    Shepperd's branch, not the short trace-only formula: the latter divides by
    ``sqrt(1 + trace)``, which goes to zero for a 180-degree rotation. Over a
    whole night the horizon rig passes through every orientation, so that case
    is reached routinely rather than never.
    """
    q = np.empty((r.shape[0], 4), dtype=np.float64)
    trace = r[:, 0, 0] + r[:, 1, 1] + r[:, 2, 2]
    diag = np.stack([r[:, 0, 0], r[:, 1, 1], r[:, 2, 2]], axis=-1)
    use_trace = trace > diag.max(axis=-1)

    if np.any(use_trace):
        m = use_trace
        s = np.sqrt(trace[m] + 1.0) * 2.0
        q[m, 3] = 0.25 * s
        q[m, 0] = (r[m, 2, 1] - r[m, 1, 2]) / s
        q[m, 1] = (r[m, 0, 2] - r[m, 2, 0]) / s
        q[m, 2] = (r[m, 1, 0] - r[m, 0, 1]) / s

    rest = ~use_trace
    if np.any(rest):
        i = np.argmax(diag[rest], axis=-1)
        idx = np.flatnonzero(rest)
        for k in (0, 1, 2):
            sel = idx[i == k]
            if sel.size == 0:
                continue
            a, b = (k + 1) % 3, (k + 2) % 3
            s = np.sqrt(1.0 + r[sel, k, k] - r[sel, a, a] - r[sel, b, b]) * 2.0
            q[sel, 3] = (r[sel, b, a] - r[sel, a, b]) / s
            q[sel, k] = 0.25 * s
            q[sel, a] = (r[sel, a, k] + r[sel, k, a]) / s
            q[sel, b] = (r[sel, b, k] + r[sel, k, b]) / s

    # Sign is free; pick w >= 0 so consecutive slots do not flip and make a
    # slerp take the long way round the sphere mid-night.
    q[q[:, 3] < 0.0] *= -1.0
    return q / np.linalg.norm(q, axis=-1, keepdims=True)


def _frame_rotation(frame: AltAz, n_slots: int) -> NDArray[np.float64]:
    """Sample astropy's own transform at the three basis directions.

    Deriving this in the client instead would mean re-implementing precession,
    nutation and aberration in TypeScript and getting the same answer as
    astropy to arcseconds, which is not a thing anyone should attempt.
    """
    cols = []
    for alt_d, az_d in ((0.0, 90.0), (0.0, 0.0), (90.0, 0.0)):  # East, North, Up
        aa = SkyCoord(
            alt=np.full(n_slots, alt_d) * u.deg,
            az=np.full(n_slots, az_d) * u.deg,
            frame=frame,
        ).transform_to("icrs")
        cols.append(_icrs_unit(np.asarray(aa.ra.deg), np.asarray(aa.dec.deg)))

    m = np.stack(cols, axis=-1)
    # Aberration is direction-dependent, so the sampled triad is only
    # orthonormal to ~30 arcsec. Take the nearest true rotation (Procrustes)
    # rather than shipping a slightly-shearing matrix the client would
    # renormalise differently every frame.
    uu, _, vt = np.linalg.svd(m)
    rot = uu @ vt
    flip = np.linalg.det(rot) < 0.0
    if np.any(flip):
        fix = np.diag([1.0, 1.0, -1.0])
        rot[flip] = uu[flip] @ fix @ vt[flip]
    return _quat_from_matrix(rot)


def build_night_geometry(site: Site, grid: TimeGrid, targets: tuple[Target, ...]) -> NightGeometry:
    """Compute the full geometry tensor for a night.

    Cost is dominated by the two frame transforms; it is independent of the
    number of targets to first order, because they all ride one vectorised call.
    """
    configure_astropy_offline()

    location = EarthLocation(
        lat=site.latitude_deg * u.deg,
        lon=site.longitude_deg * u.deg,
        height=site.elevation_m * u.m,
    )
    times = Time(grid.mid_unix(), format="unix", scale="utc")
    frame = AltAz(obstime=times, location=location)

    sun = get_body("sun", times, location).transform_to(frame)
    moon_icrs = get_body("moon", times, location)
    moon = moon_icrs.transform_to(frame)

    sun_alt = np.asarray(sun.alt.deg, dtype=np.float64)
    sun_az = np.asarray(sun.az.deg, dtype=np.float64)
    moon_alt = np.asarray(moon.alt.deg, dtype=np.float64)
    moon_az = np.asarray(moon.az.deg, dtype=np.float64)

    # Phase angle = Sun-Moon-Earth elongation complement. astropy gives the
    # Sun-Moon separation as seen from Earth (the elongation); the phase angle
    # is 180 - elongation, so full moon (elongation 180) -> alpha 0.
    sun_icrs = get_body("sun", times, location)
    elongation_deg = np.asarray(sun_icrs.separation(moon_icrs).deg, dtype=np.float64)
    phase_angle_deg = 180.0 - elongation_deg
    illumination = (1.0 + np.cos(np.radians(phase_angle_deg))) / 2.0

    n_t, n_s = len(targets), grid.n_slots
    alt = np.empty((n_t, n_s), dtype=np.float64)
    az = np.empty((n_t, n_s), dtype=np.float64)
    sep = np.empty((n_t, n_s), dtype=np.float64)

    for i, tgt in enumerate(targets):
        coord = SkyCoord(ra=tgt.ra_deg * u.deg, dec=tgt.dec_deg * u.deg, frame="icrs")
        aa = coord.transform_to(frame)
        alt[i] = aa.alt.deg
        az[i] = aa.az.deg
        # Separate in the OBSERVER's frame, not ICRS. Krisciunas & Schaefer want
        # the apparent (topocentric) moon-target separation as seen from the
        # ground, and comparing an ICRS target against a GCRS Moon is both
        # slightly wrong and something astropy rightly warns about.
        sep[i] = aa.separation(moon).deg

    airmass = airmass_kasten_young(alt)

    # Apparent, not mean: the equation of the equinoxes is ~1 second of time,
    # which is 15 arcsec of sky. Irrelevant to a 15-degree moon veto and
    # entirely visible as a 15-arcsec offset when a star is overlaid on an
    # image, which is precisely what this array is for.
    lst = np.asarray(times.sidereal_time("apparent", longitude=location.lon).hour, dtype=np.float64)
    quat = _frame_rotation(frame, grid.n_slots)

    # The lunar veto applies only while the Moon is actually up. A target 5 deg
    # from a Moon that is below the horizon is perfectly observable, and vetoing
    # it would silently discard good sky.
    moon_up = moon_alt > 0.0
    too_close = (sep < site.min_moon_separation_deg) & moon_up[None, :]
    visible = (alt >= site.min_altitude_deg) & ~too_close

    return NightGeometry(
        grid=grid,
        target_ids=tuple(t.id for t in targets),
        altitude_deg=alt,
        azimuth_deg=az,
        airmass=airmass,
        moon_separation_deg=sep,
        sun_altitude_deg=sun_alt,
        sun_azimuth_deg=sun_az,
        lst_hours=lst,
        moon_altitude_deg=moon_alt,
        moon_azimuth_deg=moon_az,
        moon_phase_angle_deg=phase_angle_deg,
        moon_illumination=illumination,
        visible=visible,
        frame_quat=quat,
    )
