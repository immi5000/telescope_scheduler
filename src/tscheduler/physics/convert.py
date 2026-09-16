"""Surface-brightness unit conversions.

**The one rule this module exists to enforce: sky brightness components are
summed in LINEAR flux, never in magnitudes.** Natural airglow, artificial light
pollution, scattered moonlight and twilight each contribute photons; photons
add, magnitudes do not. Adding magnitudes is the single most common bug in this
domain and it is silent -- the answer just comes out wrong by a few tenths.

Working unit is the nanoLambert (nL), because that is what Krisciunas &
Schaefer's moonlight model is written in. Magnitudes appear only at API
boundaries.
"""

from __future__ import annotations

from typing import Final

import numpy as np
from numpy.typing import NDArray

#: 1 nL = 1e-9 Lambert = (1e-9/pi) cd cm^-2 = 3.1831e-6 cd m^-2
CD_M2_PER_NL: Final = 3.1831e-6

#: Luminance zero point: mu = LUMINANCE_ZP_MAG - 2.5*log10(L[cd/m^2])
LUMINANCE_ZP_MAG: Final = 12.58

# KS91 eq. 27/28 constants. 0.92104 = ln(10)/2.5, and 34.08*exp(20.7233) = 3.408e10.
_KS_A: Final = 34.08
_KS_B: Final = 20.7233
_KS_C: Final = 0.92104

#: Bortle class -> approximate zenith V surface brightness (mag/arcsec^2).
#: Bortle (2001), Sky & Telescope. These are VISUAL estimates and noisy: prefer
#: a Falchi atlas lookup when available and treat these as a +-0.3 mag fallback.
BORTLE_ZENITH_MAG: Final[dict[int, float]] = {
    1: 21.9,
    2: 21.8,
    3: 21.5,
    4: 21.2,
    5: 20.7,
    6: 20.0,
    7: 19.2,
    8: 18.5,
    9: 18.0,
}


def mag_arcsec2_to_nl(mu_mag_arcsec2: NDArray[np.float64] | float) -> NDArray[np.float64]:
    """Surface brightness (mag/arcsec^2) -> nanoLamberts. KS91 eq. 27."""
    mu = np.asarray(mu_mag_arcsec2, dtype=np.float64)
    return np.asarray(_KS_A * np.exp(_KS_B - _KS_C * mu), dtype=np.float64)


def nl_to_mag_arcsec2(b_nl: NDArray[np.float64] | float) -> NDArray[np.float64]:
    """nanoLamberts -> surface brightness (mag/arcsec^2). KS91 eq. 28.

    A perfectly dark sky (0 nL) is infinitely faint; we return +inf rather than
    raising, so a component that happens to be zero (moon below the horizon)
    flows through arithmetic without a special case.
    """
    b = np.asarray(b_nl, dtype=np.float64)
    out = np.full(b.shape, np.inf, dtype=np.float64)
    pos = b > 0.0
    if b.ndim:
        out[pos] = (_KS_B - np.log(b[pos] / _KS_A)) / _KS_C
    elif pos:
        out = np.asarray((_KS_B - np.log(b / _KS_A)) / _KS_C, dtype=np.float64)
    return out


def cd_m2_to_mag_arcsec2(l_cd_m2: NDArray[np.float64] | float) -> NDArray[np.float64]:
    lum = np.asarray(l_cd_m2, dtype=np.float64)
    return np.asarray(LUMINANCE_ZP_MAG - 2.5 * np.log10(lum), dtype=np.float64)


def nl_to_cd_m2(b_nl: NDArray[np.float64] | float) -> NDArray[np.float64]:
    return np.asarray(np.asarray(b_nl, dtype=np.float64) * CD_M2_PER_NL, dtype=np.float64)


def ucd_m2_to_nl(l_ucd_m2: NDArray[np.float64] | float) -> NDArray[np.float64]:
    """Micro-cd/m^2 -> nL. This is the Falchi et al. (2016) atlas unit.

    Beware: the GeoTIFF redistributed by lightpollutionmap.info is labelled
    mcd/m^2 while the paper's thresholds are in ucd/m^2. Validate before trusting.
    """
    return np.asarray(
        np.asarray(l_ucd_m2, dtype=np.float64) * 1e-6 / CD_M2_PER_NL, dtype=np.float64
    )


def bortle_to_artificial_nl(bortle_class: int, natural_zenith_mag_arcsec2: float = 22.0) -> float:
    """Bortle class -> ARTIFICIAL component only, in nL.

    The subtraction must happen in flux, not magnitudes: the Bortle table gives
    TOTAL zenith brightness, and the artificial part is what remains after the
    natural sky is removed.
    """
    if bortle_class not in BORTLE_ZENITH_MAG:
        raise ValueError(f"Bortle class must be 1-9, got {bortle_class}")
    total = float(mag_arcsec2_to_nl(BORTLE_ZENITH_MAG[bortle_class]))
    natural = float(mag_arcsec2_to_nl(natural_zenith_mag_arcsec2))
    return max(total - natural, 0.0)


def validate_artificial_brightness(
    artificial_nl: float, natural_zenith_mag_arcsec2: float = 22.0
) -> None:
    """Reject values implying an absurd sky, with an actionable message.

    Catches the ucd/mcd unit confusion described above, which is otherwise a
    factor-of-1000 error that still produces plausible-looking numbers.
    """
    if artificial_nl < 0.0:
        raise ValueError(f"artificial sky brightness cannot be negative: {artificial_nl} nL")
    total = artificial_nl + float(mag_arcsec2_to_nl(natural_zenith_mag_arcsec2))
    mu = float(nl_to_mag_arcsec2(total))
    if not 15.0 <= mu <= 22.1:
        raise ValueError(
            f"artificial brightness {artificial_nl:.4g} nL implies a total zenith sky of "
            f"{mu:.2f} mag/arcsec^2, outside the plausible 15-22.1 range. "
            "Check units: the Falchi atlas is ucd/m^2, but some redistributions label it mcd/m^2."
        )


def add_in_flux(*components_nl: NDArray[np.float64] | float) -> NDArray[np.float64]:
    """Sum sky-brightness components. The ONLY correct way to combine them."""
    total = np.zeros_like(np.asarray(components_nl[0], dtype=np.float64))
    for c in components_nl:
        total = total + np.asarray(c, dtype=np.float64)
    return total
