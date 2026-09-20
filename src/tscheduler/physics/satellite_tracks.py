"""Satellite passes, sampled finely enough to animate.

Display only, like the planets. The streak-risk factor in ``satellites.py``
bins the whole population into an alt/az density map at slot resolution,
because that is what a scheduling cost needs; this module follows individual
objects every ten seconds, because that is what drawing one moving across the
sky needs. Neither can stand in for the other.

**Why ten seconds.** A low satellite near the zenith crosses about a degree a
second. Between two samples ten seconds apart its apparent path is a great
circle to well under a tenth of a degree -- the orbit barely curves in 75 km --
so the browser can interpolate unit vectors between samples and stay on the
track. At slot resolution (five minutes) the same object would jump a third of
the way across the sky between samples.

**Why not astropy for the frame.** ``TEME -> ITRS -> AltAz`` through astropy
costs seconds for half a million positions. TEME is defined against the IAU-82
Greenwich mean sidereal time, so TEME to Earth-fixed is exactly one rotation
about z by GMST82 plus polar motion; polar motion moves a satellite by metres
and is dropped. That single rotation is vectorised here and checked against
astropy's full transform in ``tests/unit/test_satellite_tracks.py``.

**Brightness is an estimate, and says so.** Two-line elements carry no
magnitude. The standard magnitude (at 1000 km and half phase) is an intrinsic
property of each object and not in the feed, so it defaults to a value typical
of the CelesTrak 'visual' group and is overridden only where it is well known.
The phase law is a diffusely reflecting sphere.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import numpy as np
from numpy.typing import NDArray

from tscheduler.physics.satellites import is_sunlit

__all__ = [
    "DEFAULT_STANDARD_MAGNITUDE",
    "STANDARD_MAGNITUDES",
    "SatellitePass",
    "SatelliteTracks",
    "build_satellite_passes",
    "gmst82_rad",
]

STEP_SECONDS: Final = 10.0

#: Typical of the 'visual' group, which is selected for being bright: mostly
#: large rocket bodies with standard magnitudes between about 2 and 4.5.
DEFAULT_STANDARD_MAGNITUDE: Final = 3.5

#: Standard magnitudes (1000 km, 50% illuminated) where they are well known.
STANDARD_MAGNITUDES: Final[Mapping[int, float]] = {
    25544: -1.3,  # ISS
    20580: 2.2,  # Hubble Space Telescope
}

#: Elements further than this from the night are not propagated at all. SGP4
#: error for a low orbit grows by kilometres a day; beyond two weeks a drawn
#: position is closer to fiction than to an estimate.
MAX_EPOCH_AGE_DAYS: Final = 14.0

_SECONDS_PER_DAY: Final = 86400.0
_UNIX_EPOCH_JD: Final = 2440587.5


@dataclass(frozen=True, slots=True)
class SatellitePass:
    """One continuous stretch above the horizon."""

    norad_id: int
    name: str
    start: datetime
    """Instant of the first sample. Sample ``i`` is at ``start + i * step``."""
    step_seconds: float
    altitude_deg: NDArray[np.float64]
    azimuth_deg: NDArray[np.float64]
    magnitude: NDArray[np.float64]
    """Estimated V magnitude, NaN while the object is in Earth's shadow."""
    range_km: NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class SatelliteTracks:
    passes: tuple[SatellitePass, ...]
    n_objects: int
    """Elements supplied."""
    n_propagated: int
    """Elements close enough to the night to be worth propagating."""
    epoch_spread_days: float
    """Largest gap between an element set's epoch and the night's middle."""
    step_seconds: float


def gmst82_rad(jd_ut1: NDArray[np.float64]) -> NDArray[np.float64]:
    """Greenwich mean sidereal time, IAU-82, in radians.

    This is the angle TEME is defined against, which is why it is the right
    one here and apparent sidereal time is not. UTC is used for UT1: they
    differ by under a second, which turns the sky by 0.004 degrees.
    """
    t = (np.asarray(jd_ut1, dtype=np.float64) - 2451545.0) / 36525.0
    seconds = (
        -6.2e-6 * t**3 + 0.093104 * t**2 + (876600.0 * 3600.0 + 8640184.812866) * t + 67310.54841
    )
    return np.asarray(np.radians(seconds / 240.0) % (2.0 * np.pi), dtype=np.float64)


def _observer_itrs_km(lat_deg: float, lon_deg: float, elevation_m: float) -> NDArray[np.float64]:
    from astropy import units as u
    from astropy.coordinates import EarthLocation

    loc = EarthLocation.from_geodetic(
        lon=lon_deg * u.deg, lat=lat_deg * u.deg, height=elevation_m * u.m
    )
    return np.array([loc.x.to_value(u.km), loc.y.to_value(u.km), loc.z.to_value(u.km)])


def _sun_unit_gcrs(unix: NDArray[np.float64]) -> NDArray[np.float64]:
    """The Sun's direction at every sample, from a coarse astropy series.

    Sampled every ten minutes and interpolated: the Sun moves 0.07 degrees in
    that time, which shifts a shadow crossing by well under a second.
    """
    from astropy import units as u
    from astropy.coordinates import get_body
    from astropy.time import Time

    coarse = np.arange(unix[0], unix[-1] + 600.0, 600.0)
    sun = get_body("sun", Time(coarse, format="unix", scale="utc"))
    xyz = np.asarray(sun.cartesian.xyz.to_value(u.km), dtype=np.float64).T
    xyz /= np.linalg.norm(xyz, axis=-1, keepdims=True)
    out = np.stack([np.interp(unix, coarse, xyz[:, k]) for k in range(3)], axis=-1)
    return np.asarray(out / np.linalg.norm(out, axis=-1, keepdims=True), dtype=np.float64)


def _diffuse_sphere_phase(phase_rad: NDArray[np.float64]) -> NDArray[np.float64]:
    """Brightness of a Lambertian sphere relative to half phase."""
    f = ((np.pi - phase_rad) * np.cos(phase_rad) + np.sin(phase_rad)) / np.pi
    return np.asarray(np.maximum(f * np.pi, 1e-4), dtype=np.float64)


def build_satellite_passes(
    tles: Sequence[tuple[int, str, str, str]],
    lat_deg: float,
    lon_deg: float,
    elevation_m: float,
    start: datetime,
    end: datetime,
    *,
    step_seconds: float = STEP_SECONDS,
    min_altitude_deg: float = -2.0,
    standard_magnitudes: Mapping[int, float] = STANDARD_MAGNITUDES,
) -> SatelliteTracks:
    """Propagate ``(norad_id, name, line1, line2)`` and cut the result into passes.

    A pass is kept only if the object is sunlit and above the horizon for at
    least one sample: an object that crosses the sky entirely inside Earth's
    shadow is invisible, and shipping it would cost bytes to draw nothing.

    ``min_altitude_deg`` is slightly negative so a pass begins just below the
    horizon and the dot rises out of the landscape rather than appearing on it.
    """
    from sgp4.api import Satrec, SatrecArray

    unix = np.arange(start.timestamp(), end.timestamp() + step_seconds * 0.5, step_seconds)
    jd_full = unix / _SECONDS_PER_DAY + _UNIX_EPOCH_JD
    jd = np.floor(jd_full - 0.5) + 0.5
    fr = jd_full - jd
    mid_jd = float(jd_full[len(jd_full) // 2])

    keep: list[tuple[int, str, Satrec]] = []
    spread = 0.0
    for norad, name, l1, l2 in tles:
        sat = Satrec.twoline2rv(l1, l2)
        age = abs(mid_jd - (sat.jdsatepoch + sat.jdsatepochF))
        if age > MAX_EPOCH_AGE_DAYS:
            continue
        spread = max(spread, age)
        keep.append((norad, name, sat))

    if not keep:
        return SatelliteTracks((), len(tles), 0, 0.0, step_seconds)

    err, r_teme, _v = SatrecArray([s for _, _, s in keep]).sgp4(jd, fr)
    ok = err == 0

    # TEME -> pseudo Earth-fixed: one rotation about z by GMST82.
    g = gmst82_rad(jd_full)
    cg, sg = np.cos(g), np.sin(g)
    x, y, z = r_teme[..., 0], r_teme[..., 1], r_teme[..., 2]
    ecef = np.stack([cg * x + sg * y, -sg * x + cg * y, z], axis=-1)

    obs = _observer_itrs_km(lat_deg, lon_deg, elevation_m)
    topo = ecef - obs
    rng = np.linalg.norm(topo, axis=-1)

    # Geodetic East/North/Up at the site.
    phi, lam = np.radians(lat_deg), np.radians(lon_deg)
    east = np.array([-np.sin(lam), np.cos(lam), 0.0])
    north = np.array([-np.sin(phi) * np.cos(lam), -np.sin(phi) * np.sin(lam), np.cos(phi)])
    up = np.array([np.cos(phi) * np.cos(lam), np.cos(phi) * np.sin(lam), np.sin(phi)])
    safe = np.maximum(rng, 1e-6)
    alt = np.degrees(np.arcsin(np.clip((topo @ up) / safe, -1.0, 1.0)))
    az = np.degrees(np.arctan2(topo @ east, topo @ north)) % 360.0

    sun = _sun_unit_gcrs(unix)
    lit = is_sunlit(r_teme, np.broadcast_to(sun, r_teme.shape))

    # Phase angle at the satellite, Sun to observer. The observer's TEME
    # position is the Earth-fixed one turned back by GMST.
    obs_teme = np.stack(
        [cg * obs[0] - sg * obs[1], sg * obs[0] + cg * obs[1], np.full_like(cg, obs[2])],
        axis=-1,
    )
    to_obs = (obs_teme[None, :, :] - r_teme) / safe[..., None]
    phase = np.arccos(np.clip(np.sum(to_obs * sun[None, :, :], axis=-1), -1.0, 1.0))

    std = np.array(
        [standard_magnitudes.get(norad, DEFAULT_STANDARD_MAGNITUDE) for norad, _, _ in keep]
    )
    mag = (
        std[:, None]
        + 5.0 * np.log10(np.maximum(rng, 1.0) / 1000.0)
        - 2.5 * np.log10(_diffuse_sphere_phase(phase))
    )
    mag = np.where(lit, mag, np.nan)

    t0 = datetime.fromtimestamp(float(unix[0]), UTC)
    passes: list[SatellitePass] = []
    for k, (norad, name, _) in enumerate(keep):
        above = ok[k] & (alt[k] > min_altitude_deg)
        if not np.any(above):
            continue
        # Runs of consecutive True, as [start, stop) pairs.
        edges = np.flatnonzero(np.diff(np.concatenate(([0], above.astype(np.int8), [0]))))
        for a, b in zip(edges[::2], edges[1::2], strict=True):
            seen = lit[k, a:b] & (alt[k, a:b] > 0.0)
            if not np.any(seen) or b - a < 2:
                continue
            passes.append(
                SatellitePass(
                    norad_id=norad,
                    name=name,
                    start=t0 + timedelta(seconds=float(a) * step_seconds),
                    step_seconds=step_seconds,
                    altitude_deg=alt[k, a:b].copy(),
                    azimuth_deg=az[k, a:b].copy(),
                    magnitude=mag[k, a:b].copy(),
                    range_km=rng[k, a:b].copy(),
                )
            )

    passes.sort(key=lambda p: (p.start, p.norad_id))
    return SatelliteTracks(tuple(passes), len(tles), len(keep), spread, step_seconds)
