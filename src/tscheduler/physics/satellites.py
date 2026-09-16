"""Satellite streak risk.

Two design decisions worth stating up front, because they change what this
module is for:

**Satellite mitigation is primarily a FRAME-COUNT constraint, not a maximum
exposure length.** The naive framing -- "use shorter subs so a streak ruins only
one frame" -- is wrong for a stacking workflow. Nobody discards whole frames;
sigma-clipped stacking removes the streaked PIXELS in that one frame and keeps
the rest, which needs >= 9 frames for the clip to have statistical power. That
minimum is enforced in the CP-SAT model (C9) and is good practice independently
of satellites, since it is also what makes cosmic-ray rejection work. The design
is therefore robust to this whole module being wrong.

**This layer may not earn its compute.** Expected streak counts are typically a
rounding error next to cloud and moonlight. It is built behind a Protocol with a
null implementation so the `no_satellites` ablation is a config change rather
than a refactor -- and if plan fingerprints never move, it can be demoted to a
display-only annotation on the timeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

import numpy as np
from numpy.typing import NDArray

EARTH_RADIUS_KM: Final = 6378.137

#: Below this solar altitude, satellites are only visible if they are still
#: sunlit at their own altitude -- handled by the shadow test. Above it, the sky
#: is too bright for a streak to matter anyway.
TWILIGHT_GLARE_SUN_ALT_DEG: Final = -6.0


class SatelliteRiskModel(Protocol):
    def streak_rate_per_min(
        self,
        altitude_deg: NDArray[np.float64],
        azimuth_deg: NDArray[np.float64],
        slot_index: NDArray[np.int64] | int,
        fov_deg2: float,
    ) -> NDArray[np.float64]: ...


class NullSatelliteRisk:
    """No satellites. The `no_satellites` ablation, and the default until a TLE
    source is wired in."""

    def streak_rate_per_min(
        self,
        altitude_deg: NDArray[np.float64],
        azimuth_deg: NDArray[np.float64],
        slot_index: NDArray[np.int64] | int,
        fov_deg2: float,
    ) -> NDArray[np.float64]:
        return np.zeros_like(np.asarray(altitude_deg, dtype=np.float64))


def is_sunlit(
    sat_eci_km: NDArray[np.float64], sun_unit_eci: NDArray[np.float64]
) -> NDArray[np.bool_]:
    """Cylindrical Earth-shadow test.

    A satellite is in shadow when it is on the anti-sun side AND its
    perpendicular distance from the Earth-Sun axis is less than one Earth
    radius. The cylindrical model ignores penumbra and the Sun's angular size,
    which shifts terminator crossings by a few seconds -- irrelevant against
    5-minute slots.

    ``sat_eci_km`` is (..., 3); ``sun_unit_eci`` is (..., 3) unit vectors.
    """
    r = np.asarray(sat_eci_km, dtype=np.float64)
    s = np.asarray(sun_unit_eci, dtype=np.float64)
    proj = np.sum(r * s, axis=-1)
    perp = np.linalg.norm(r - proj[..., None] * s, axis=-1)
    in_shadow = (proj < 0.0) & (perp < EARTH_RADIUS_KM)
    return np.asarray(~in_shadow, dtype=np.bool_)


@dataclass(frozen=True, slots=True)
class SatelliteDensityMap:
    """Counts of illuminated, above-horizon satellites per alt/az bin per slot.

    A density map rather than per-target propagation: propagating ~10,000
    objects for every target separately is wasteful and scales badly with the
    target list, whereas the sky only needs binning once per slot.
    """

    alt_edges: NDArray[np.float64]
    az_edges: NDArray[np.float64]
    counts: NDArray[np.float64]  # (n_alt_bin, n_az_bin, n_slots)
    n_objects: int
    epoch_spread_days: float
    """How far the elements were propagated from their own epochs. Large values
    mean the positions are not trustworthy -- SGP4 degrades quickly."""

    @property
    def n_slots(self) -> int:
        return int(self.counts.shape[2])

    def bin_solid_angle_deg2(self) -> NDArray[np.float64]:
        """Solid angle of each altitude band, for converting counts to density.

        Bands near the zenith cover far less sky than bands near the horizon, so
        a raw count per bin would badly overstate zenith density.
        """
        lo = np.radians(self.alt_edges[:-1])
        hi = np.radians(self.alt_edges[1:])
        n_az = len(self.az_edges) - 1
        band = 2.0 * np.pi * (np.sin(hi) - np.sin(lo)) / n_az
        return np.asarray(np.degrees(np.degrees(band)), dtype=np.float64)


class DensityMapRisk:
    """Streak rate looked up from a precomputed density map."""

    def __init__(self, dmap: SatelliteDensityMap, angular_rate_deg_per_min: float = 24.0) -> None:
        self._m = dmap
        # A LEO satellite crosses the sky at roughly 0.4 deg/s near the zenith.
        self._rate = angular_rate_deg_per_min

    def streak_rate_per_min(
        self,
        altitude_deg: NDArray[np.float64],
        azimuth_deg: NDArray[np.float64],
        slot_index: NDArray[np.int64] | int,
        fov_deg2: float,
    ) -> NDArray[np.float64]:
        alt = np.atleast_1d(np.asarray(altitude_deg, dtype=np.float64))
        az = np.atleast_1d(np.asarray(azimuth_deg, dtype=np.float64)) % 360.0
        si = np.atleast_1d(np.asarray(slot_index, dtype=np.int64))
        si = np.broadcast_to(si, alt.shape)

        ai = np.clip(
            np.searchsorted(self._m.alt_edges, alt, side="right") - 1, 0, len(self._m.alt_edges) - 2
        )
        zi = np.clip(
            np.searchsorted(self._m.az_edges, az, side="right") - 1, 0, len(self._m.az_edges) - 2
        )

        counts = self._m.counts[ai, zi, np.clip(si, 0, self._m.n_slots - 1)]
        omega = self._m.bin_solid_angle_deg2()[ai]
        density = np.where(omega > 0, counts / omega, 0.0)  # objects per deg^2

        # Expected crossings per minute: objects sitting in the field, plus
        # those sweeping into it. The sweep term dominates for LEO.
        fov_width = np.sqrt(max(fov_deg2, 1e-9))
        swept = fov_width * self._rate
        return np.asarray(density * (fov_deg2 + swept), dtype=np.float64)


def build_density_map(
    tle_lines: list[tuple[str, str, str]],
    lat_deg: float,
    lon_deg: float,
    elevation_m: float,
    slot_mid_times: list[datetime],
    *,
    n_alt_bins: int = 18,
    n_az_bins: int = 24,
    min_altitude_deg: float = 0.0,
) -> SatelliteDensityMap:
    """Propagate a TLE set and bin illuminated, visible objects by alt/az.

    One vectorised SGP4 call over (n_sat, n_time), then one frame conversion --
    not a loop over targets. Cost is ~1-2 s for ~10,000 objects and 120 slots,
    paid once per TLE update.
    """
    from astropy import units as u
    from astropy.coordinates import (
        ITRS,
        TEME,
        AltAz,
        CartesianRepresentation,
        EarthLocation,
        get_body,
    )
    from astropy.time import Time
    from sgp4.api import Satrec, SatrecArray

    if not tle_lines:
        raise ValueError("no TLEs supplied")

    sats = [Satrec.twoline2rv(l1, l2) for _, l1, l2 in tle_lines]
    arr = SatrecArray(sats)

    times = Time(list(slot_mid_times), scale="utc")
    jd = np.asarray(times.jd1, dtype=np.float64)
    fr = np.asarray(times.jd2, dtype=np.float64)

    err, pos_teme, _vel = arr.sgp4(jd, fr)  # (n_sat, n_time, 3) km, TEME

    ok = err == 0
    epochs = np.array([s.jdsatepoch + s.jdsatepochF for s in sats], dtype=np.float64)
    spread = float(np.max(np.abs(np.mean(jd + fr) - epochs))) if len(epochs) else 0.0

    location = EarthLocation(lat=lat_deg * u.deg, lon=lon_deg * u.deg, height=elevation_m * u.m)
    n_sat, n_time = pos_teme.shape[0], pos_teme.shape[1]

    alt_edges = np.linspace(min_altitude_deg, 90.0, n_alt_bins + 1)
    az_edges = np.linspace(0.0, 360.0, n_az_bins + 1)
    counts = np.zeros((n_alt_bins, n_az_bins, n_time), dtype=np.float64)

    for k in range(n_time):
        t = times[k]
        valid = ok[:, k]
        if not np.any(valid):
            continue
        p = pos_teme[valid, k, :]

        teme = TEME(CartesianRepresentation(p.T * u.km), obstime=t)
        altaz = teme.transform_to(ITRS(obstime=t)).transform_to(AltAz(obstime=t, location=location))
        alt = np.asarray(altaz.alt.deg, dtype=np.float64)
        az = np.asarray(altaz.az.deg, dtype=np.float64) % 360.0

        sun = get_body("sun", t)
        sun_vec = sun.cartesian.xyz.to_value(u.km)
        sun_unit = sun_vec / np.linalg.norm(sun_vec)
        lit = is_sunlit(p, np.broadcast_to(sun_unit, p.shape))

        keep = (alt >= min_altitude_deg) & lit
        if not np.any(keep):
            continue
        h, _, _ = np.histogram2d(alt[keep], az[keep], bins=[alt_edges, az_edges])
        counts[:, :, k] = h

    return SatelliteDensityMap(
        alt_edges=alt_edges,
        az_edges=az_edges,
        counts=counts,
        n_objects=n_sat,
        epoch_spread_days=spread,
    )
