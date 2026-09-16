"""Twilight sky brightness as a function of solar altitude.

**This is necessarily an approximation.** There is no closed-form twilight
model; every usable one is an empirical fit to one site's sky under one aerosol
regime. So it lives behind a single documented, swappable function rather than
being smeared through the codebase.

Default is Patat, Ugolnikov & Postylyakov (2006), A&A 455, 385, who fit >2000
FORS1 twilight frames at Paranal over solar zenith distance 94-112 deg.
"""

from __future__ import annotations

from typing import Final, Protocol

import numpy as np
from numpy.typing import NDArray

from tscheduler.physics.convert import mag_arcsec2_to_nl

#: Patat+2006 Table 1, V band: mu = a0 + a1*(zeta-95) + a2*(zeta-95)^2
PATAT_V: Final[tuple[float, float, float]] = (11.84, 1.518, -0.057)
PATAT_V_SIGMA: Final = 0.18
PATAT_VALID_FROM_ZETA: Final = 95.0

#: Patat+2006 document the fit as valid over 95 <= zeta <= 105.
PATAT_VALID_TO_ZETA: Final = 105.0

#: The parabola turns over at zeta = 95 + a1/(2|a2|) = 108.32 and gets BRIGHTER
#: again past it, which is unphysical.
PATAT_TURNOVER_ZETA: Final = 95.0 + PATAT_V[1] / (2.0 * abs(PATAT_V[2]))

#: Past the validity limit we extrapolate LOG-LINEARLY rather than clamping to
#: zero. Clamping looks tidy but is wrong: at the turnover the polynomial still
#: gives 21.95 mag/arcsec^2, comparable to the dark sky itself, so dropping to
#: zero there is an ~0.8 mag cliff -- precisely the kind of discontinuity that
#: makes the schedule flip between re-solves for no physical reason.
#:
#: The slope is the secant across the fitted range, (21.320-11.840)/(105-95).
#: It is an EXTRAPOLATION, not a fit, and it is calibrated against the
#: conventional meaning of astronomical twilight: it leaves twilight adding
#: ~0.14 mag at a solar altitude of -18 deg, i.e. negligible but not absent.
TWILIGHT_DECAY_MAG_PER_DEG: Final = (21.320 - 11.840) / (PATAT_VALID_TO_ZETA - 95.0)


class TwilightModel(Protocol):
    def zenith_nl(self, sun_altitude_deg: NDArray[np.float64] | float) -> NDArray[np.float64]: ...


class PatatZenithTwilight:
    """Patat et al. (2006) zenith V twilight.

    Two site caveats, both real:

    * The fit is for PARANAL. Its sigma of 0.18 mag is the night-to-night scatter
      *there*; at a low-altitude hazier site the true value can be ~0.3 mag
      brighter. ``offset_mag`` exists to calibrate that per site.
    * It is a ZENITH fit. Off-zenith scaling is applied separately and is
      explicitly tuned by eye, not fitted.
    """

    def __init__(self, offset_mag: float = 0.0) -> None:
        self.offset_mag = offset_mag

    def zenith_nl(self, sun_altitude_deg: NDArray[np.float64] | float) -> NDArray[np.float64]:
        alt = np.asarray(sun_altitude_deg, dtype=np.float64)
        zeta = 90.0 - alt
        a0, a1, a2 = PATAT_V

        # Inside the fitted range, use the polynomial.
        d_in = np.clip(zeta, PATAT_VALID_FROM_ZETA, PATAT_VALID_TO_ZETA) - 95.0
        mu = a0 + a1 * d_in + a2 * d_in * d_in + self.offset_mag

        # Past the validity limit, continue log-linearly. Continuous in value by
        # construction, and monotonic all the way down -- no turnover, no cliff.
        beyond = np.maximum(zeta - PATAT_VALID_TO_ZETA, 0.0)
        mu = mu + TWILIGHT_DECAY_MAG_PER_DEG * beyond

        return np.asarray(mag_arcsec2_to_nl(mu), dtype=np.float64)


def twilight_sky_nl(
    sun_altitude_deg: NDArray[np.float64] | float,
    target_altitude_deg: NDArray[np.float64] | float,
    sun_azimuth_deg: NDArray[np.float64] | float | None = None,
    target_azimuth_deg: NDArray[np.float64] | float | None = None,
    model: TwilightModel | None = None,
    airmass_growth: float = 0.3,
    sunward_excess: float = 0.3,
) -> NDArray[np.float64]:
    """Twilight brightness at a pointing, in nanoLamberts.

    Off-zenith scaling follows the Rubin ``twilight_func`` shape: growth with
    airmass, plus an extra excess on the sunward side of the sky.

    ``airmass_growth`` and ``sunward_excess`` are **tuned by eye, not fitted**.
    They are named parameters precisely so that is visible at the call site.
    """
    from tscheduler.physics.airmass import airmass_kasten_young

    m = model or PatatZenithTwilight()
    base = m.zenith_nl(sun_altitude_deg)

    x = airmass_kasten_young(target_altitude_deg)
    x = np.where(np.isfinite(x), x, 1.0)
    scale = 10.0 ** (airmass_growth * (x - 1.0))

    if sun_azimuth_deg is not None and target_azimuth_deg is not None:
        d_az = np.radians(np.asarray(target_azimuth_deg) - np.asarray(sun_azimuth_deg))
        scale = scale * 10.0 ** (sunward_excess * np.cos(d_az) * (x - 1.0))

    return np.asarray(base * scale, dtype=np.float64)
