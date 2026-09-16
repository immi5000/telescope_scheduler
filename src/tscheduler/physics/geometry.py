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
            "moon_altitude_deg": self.moon_altitude_deg,
            "moon_phase_angle_deg": self.moon_phase_angle_deg,
            "moon_illumination": self.moon_illumination,
            "visible": self.visible,
        }


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
        moon_altitude_deg=moon_alt,
        moon_azimuth_deg=moon_az,
        moon_phase_angle_deg=phase_angle_deg,
        moon_illumination=illumination,
        visible=visible,
    )
