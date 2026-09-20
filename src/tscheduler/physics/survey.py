"""Where a whole catalogue is, all night, in one matrix multiply.

``build_night_geometry`` transforms each target through astropy separately,
which is right for the handful a session schedules and hopeless for ranking a
catalogue of thousands: at ~30 ms a target, a two-thousand-object survey is a
minute. This module instead asks astropy for the ONE thing that is expensive
-- the rotation from the observer's horizon into ICRS at each sample, the same
rotation the globe is drawn with -- and then places every object by applying
it, which is a few milliseconds of numpy however long the catalogue.

The price is aberration. ICRS to horizontal is not exactly rigid (annual
aberration is direction-dependent, up to ~20 arcsec), so positions here agree
with astropy's per-object transform to about half an arcminute -- see
``tests/unit/test_survey.py``. That is ample for "is it up, and when is it
highest", and it is not what the scheduler plans from: a chosen target is
recomputed exactly when its session is built.

Like the geometry, this depends only on the site, the instants and the
coordinates. No weather, no clock, nothing published -- so nothing here can
leak, and a result may be cached for as long as anyone likes.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from astropy import units as u
from astropy.coordinates import AltAz, EarthLocation, get_body
from astropy.time import Time
from numpy.typing import NDArray

from tscheduler.domain.site import Site
from tscheduler.physics.geometry import _frame_rotation, configure_astropy_offline

__all__ = ["NightSurvey", "survey_night"]


def _matrices_from_quats(q: NDArray[np.float64]) -> NDArray[np.float64]:
    """(S,4) quaternions ``(x, y, z, w)`` to (S,3,3) rotation matrices."""
    x, y, z, w = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    m = np.empty((q.shape[0], 3, 3), dtype=np.float64)
    m[:, 0, 0] = 1 - 2 * (y * y + z * z)
    m[:, 0, 1] = 2 * (x * y - z * w)
    m[:, 0, 2] = 2 * (x * z + y * w)
    m[:, 1, 0] = 2 * (x * y + z * w)
    m[:, 1, 1] = 1 - 2 * (x * x + z * z)
    m[:, 1, 2] = 2 * (y * z - x * w)
    m[:, 2, 0] = 2 * (x * z - y * w)
    m[:, 2, 1] = 2 * (y * z + x * w)
    m[:, 2, 2] = 1 - 2 * (x * x + y * y)
    return m


def _local_unit(alt_deg: NDArray[np.float64], az_deg: NDArray[np.float64]) -> NDArray[np.float64]:
    """Altitude/azimuth to a unit vector in the ``(East, North, Up)`` basis."""
    alt, az = np.radians(alt_deg), np.radians(az_deg)
    return np.stack([np.cos(alt) * np.sin(az), np.cos(alt) * np.cos(az), np.sin(alt)], axis=-1)


@dataclass(frozen=True, slots=True)
class NightSurvey:
    """The per-instant half of a survey. Place any number of objects with
    :meth:`place`; everything else here is shape ``(S,)`` or ``(S, 3, 3)``."""

    times_unix: NDArray[np.float64]
    horizon_to_icrs: NDArray[np.float64]
    """(S, 3, 3). Columns are East, North and Up expressed in ICRS."""
    sun_altitude_deg: NDArray[np.float64]
    moon_altitude_deg: NDArray[np.float64]
    moon_local: NDArray[np.float64]
    """(S, 3). The Moon's topocentric direction in ``(East, North, Up)``."""
    moon_illumination: NDArray[np.float64]

    @property
    def n_samples(self) -> int:
        return int(self.times_unix.shape[0])

    def place(
        self, ra_deg: NDArray[np.float64], dec_deg: NDArray[np.float64]
    ) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
        """``(altitude, azimuth, moon separation)``, each ``(N, S)`` degrees.

        Separation is measured in the observer's frame, against the
        topocentric Moon, for the same reason ``build_night_geometry`` does:
        that is the separation the lunar veto is about.
        """
        ra, dec = (
            np.radians(np.asarray(ra_deg, dtype=np.float64)),
            np.radians(np.asarray(dec_deg, dtype=np.float64)),
        )
        icrs = np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], axis=-1)
        # local = R^T v for every (object, instant): (N,3) x (S,3,3) -> (N,S,3).
        local = np.einsum("sji,nj->nsi", self.horizon_to_icrs, icrs)
        up = np.clip(local[..., 2], -1.0, 1.0)
        alt = np.degrees(np.arcsin(up))
        az = np.degrees(np.arctan2(local[..., 0], local[..., 1])) % 360.0
        cos_sep = np.clip(np.einsum("nsi,si->ns", local, self.moon_local), -1.0, 1.0)
        return alt, az, np.degrees(np.arccos(cos_sep))


def survey_night(site: Site, times_unix: NDArray[np.float64]) -> NightSurvey:
    """The astropy half: one frame rotation, the Sun and the Moon per instant.

    Cost is independent of how many objects are later placed -- about as much
    as the fixed part of a session's geometry.
    """
    configure_astropy_offline()
    t = np.asarray(times_unix, dtype=np.float64)
    location = EarthLocation(
        lat=site.latitude_deg * u.deg,
        lon=site.longitude_deg * u.deg,
        height=site.elevation_m * u.m,
    )
    times = Time(t, format="unix", scale="utc")
    frame = AltAz(obstime=times, location=location)

    sun_icrs = get_body("sun", times, location)
    moon_icrs = get_body("moon", times, location)
    sun = sun_icrs.transform_to(frame)
    moon = moon_icrs.transform_to(frame)
    elongation = np.asarray(sun_icrs.separation(moon_icrs).deg, dtype=np.float64)
    illumination = (1.0 + np.cos(np.radians(180.0 - elongation))) / 2.0

    moon_alt = np.asarray(moon.alt.deg, dtype=np.float64)
    moon_az = np.asarray(moon.az.deg, dtype=np.float64)

    return NightSurvey(
        times_unix=t,
        horizon_to_icrs=_matrices_from_quats(_frame_rotation(frame, t.shape[0])),
        sun_altitude_deg=np.asarray(sun.alt.deg, dtype=np.float64),
        moon_altitude_deg=moon_alt,
        moon_local=_local_unit(moon_alt, moon_az),
        moon_illumination=illumination,
    )
