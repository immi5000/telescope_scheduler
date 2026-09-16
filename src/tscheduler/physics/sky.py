"""Total sky brightness at a pointing: the sum of every component, in flux."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from tscheduler.physics.airmass import airmass_ks91
from tscheduler.physics.cloud import TwoStateCloud
from tscheduler.physics.convert import mag_arcsec2_to_nl, nl_to_mag_arcsec2
from tscheduler.physics.moonlight import moon_sky_brightness_nl
from tscheduler.physics.twilight import PatatZenithTwilight, twilight_sky_nl


@dataclass(frozen=True, slots=True)
class SkyComponents:
    """Every contribution kept separately, because the explanation engine
    attributes plan changes to named factors and cannot do that from a total."""

    natural_nl: NDArray[np.float64]
    artificial_nl: NDArray[np.float64]
    moon_nl: NDArray[np.float64]
    twilight_nl: NDArray[np.float64]
    cloud_delta_nl: NDArray[np.float64]
    total_nl: NDArray[np.float64]
    total_mag_arcsec2: NDArray[np.float64]


def _airmass_scaled(
    zenith_nl: NDArray[np.float64] | float, altitude_deg: NDArray[np.float64], k: float
) -> NDArray[np.float64]:
    """Scale an emitting-layer brightness to a pointing.

    ``B(Z) = B_zenith * X * 10^(-0.4 k (X - 1))``: the ``* X`` is the longer
    column of emitting material, the exponential is extinction of that column.

    Honest limitation: for airglow at ~90 km the true form is the van Rhijn
    function, which SATURATES near the horizon rather than growing as sec(z).
    Within X <= 3 the two differ by < 0.1 mag, which is inside the moonlight
    model's own error, so the simpler KS91 form is used for fidelity.
    """
    x = airmass_ks91(90.0 - altitude_deg)
    return np.asarray(zenith_nl * x * 10.0 ** (-0.4 * k * (x - 1.0)), dtype=np.float64)


def sky_brightness(
    *,
    target_altitude_deg: NDArray[np.float64],
    target_azimuth_deg: NDArray[np.float64] | None = None,
    moon_altitude_deg: NDArray[np.float64],
    moon_separation_deg: NDArray[np.float64],
    moon_phase_angle_deg: NDArray[np.float64],
    sun_altitude_deg: NDArray[np.float64],
    sun_azimuth_deg: NDArray[np.float64] | None = None,
    cloud_fraction: NDArray[np.float64] | float = 0.0,
    natural_zenith_mag_arcsec2: float = 22.0,
    artificial_zenith_nl: float = 0.0,
    extinction_k: float = 0.20,
    cloud_model: TwoStateCloud | None = None,
    twilight_model: PatatZenithTwilight | None = None,
) -> SkyComponents:
    """Total sky brightness, summed **in nanoLamberts**.

    Magnitudes appear exactly once, at the end. Summing components in magnitudes
    is the canonical bug in this domain -- see tests/unit/test_convert.py.
    """
    cloud = cloud_model or TwoStateCloud()
    twi = twilight_model or PatatZenithTwilight()

    natural = _airmass_scaled(
        mag_arcsec2_to_nl(natural_zenith_mag_arcsec2), target_altitude_deg, extinction_k
    )
    artificial = _airmass_scaled(artificial_zenith_nl, target_altitude_deg, extinction_k)
    moon = moon_sky_brightness_nl(
        moon_phase_angle_deg,
        moon_separation_deg,
        moon_altitude_deg,
        target_altitude_deg,
        extinction_k,
    )
    twilight = twilight_sky_nl(
        sun_altitude_deg,
        target_altitude_deg,
        sun_azimuth_deg,
        target_azimuth_deg,
        model=twi,
    )
    delta = cloud.sky_delta_nl(cloud_fraction, artificial, moon, natural)

    total = natural + artificial + moon + twilight + delta
    total = np.maximum(total, 1e-6)  # keep the log finite if cloud darkening dominates

    return SkyComponents(
        natural_nl=natural,
        artificial_nl=np.broadcast_to(artificial, natural.shape).copy(),
        moon_nl=moon,
        twilight_nl=twilight,
        cloud_delta_nl=delta,
        total_nl=total,
        total_mag_arcsec2=nl_to_mag_arcsec2(total),
    )
