"""Scattered moonlight: Krisciunas & Schaefer (1991), PASP 103, 1033.

The full published equation set, in nanoLamberts, with the paper's own airmass
function (eq. 3) rather than Kasten-Young -- mixing a different airmass in would
be unfaithful to the fit the constants were derived against.

**Stated accuracy in the paper's abstract is 8%-23%**, i.e. 0.1-0.25 mag. On a
moonlit night this is the dominant physics error in the entire scheduler, so
nothing downstream should be engineered to a tighter tolerance than this.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from tscheduler.physics.airmass import airmass_ks91

#: Below this altitude the Moon contributes nothing.
MOON_SET_ALT_DEG = -0.8
#: Taper to zero across this band instead of stepping. A hard step puts a
#: discontinuity in the efficiency array, which makes the schedule flip between
#: re-solves for no physical reason.
MOON_TAPER_TOP_DEG = 3.0


def moon_magnitude(phase_angle_deg: NDArray[np.float64] | float) -> NDArray[np.float64]:
    """KS91 eq. 20: the Moon's scaled illuminance exponent.

        m = 3.84 + 0.026|alpha| + 4e-9 alpha^4,   I* = 10^(-0.4 m)

    ``alpha`` is the lunar phase angle in degrees: 0 = full, 180 = new. The
    quartic term is the opposition surge.

    NOTE: 3.84 is NOT the Moon's apparent V magnitude (which is -12.73 at full).
    It is a normalisation constant in KS91's unit system, chosen so that the
    resulting brightness comes out in nanoLamberts. Substituting -12.73 -- a
    tempting "correction" -- inflates I* by a factor of 4.2e6 and yields a sky
    of ~2 mag/arcsec^2, which is nonsense. The sanity check is that a full moon
    at 90 deg separation must land near 18-19 mag/arcsec^2.
    """
    a = np.abs(np.asarray(phase_angle_deg, dtype=np.float64))
    return np.asarray(3.84 + 0.026 * a + 4.0e-9 * a**4, dtype=np.float64)


def scattering_function(separation_deg: NDArray[np.float64] | float) -> NDArray[np.float64]:
    """KS91 eq. 21-22: Rayleigh + Mie scattering into the line of sight.

        f_R(rho) = 10^5.36 * (1.06 + cos^2 rho)      Rayleigh, wide-angle
        f_M(rho) = 10^(6.15 - rho/40)                Mie/aerosol, forward-peaked

    Note the Mie term takes rho in DEGREES inside the exponent -- a classic
    transcription bug is to convert it to radians first.
    """
    rho = np.asarray(separation_deg, dtype=np.float64)
    rho_rad = np.radians(rho)
    f_rayleigh = 10.0**5.36 * (1.06 + np.cos(rho_rad) ** 2)
    f_mie = 10.0 ** (6.15 - rho / 40.0)
    return np.asarray(f_rayleigh + f_mie, dtype=np.float64)


def moon_sky_brightness_nl(
    phase_angle_deg: NDArray[np.float64] | float,
    separation_deg: NDArray[np.float64] | float,
    moon_altitude_deg: NDArray[np.float64] | float,
    target_altitude_deg: NDArray[np.float64] | float,
    extinction_k: float = 0.20,
) -> NDArray[np.float64]:
    """KS91 eq. 15: moonlight added to the sky at the target, in nanoLamberts.

        B = f(rho) * I* * 10^(-0.4 k X(Z_moon)) * (1 - 10^(-0.4 k X(Z_target)))

    The two extinction terms do different jobs and are easy to conflate:
    the first attenuates moonlight on its way INTO the atmosphere, the second is
    the FRACTION of that light scattered into our line of sight along the
    target's column. Dropping either one is a large error.
    """
    alpha = np.asarray(phase_angle_deg, dtype=np.float64)
    rho = np.asarray(separation_deg, dtype=np.float64)
    moon_alt = np.asarray(moon_altitude_deg, dtype=np.float64)
    targ_alt = np.asarray(target_altitude_deg, dtype=np.float64)

    i_star = 10.0 ** (-0.4 * moon_magnitude(alpha))
    f_rho = scattering_function(rho)

    x_moon = airmass_ks91(90.0 - moon_alt)
    x_targ = airmass_ks91(90.0 - targ_alt)

    b = (
        f_rho
        * i_star
        * 10.0 ** (-0.4 * extinction_k * x_moon)
        * (1.0 - 10.0 ** (-0.4 * extinction_k * x_targ))
    )

    # Smooth cosine taper through moonset rather than a step.
    lo, hi = MOON_SET_ALT_DEG, MOON_TAPER_TOP_DEG
    ramp = np.clip((moon_alt - lo) / (hi - lo), 0.0, 1.0)
    taper = 0.5 * (1.0 - np.cos(np.pi * ramp))

    return np.asarray(np.where(moon_alt <= lo, 0.0, b * taper), dtype=np.float64)
