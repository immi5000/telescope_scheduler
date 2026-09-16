"""Airmass.

Three formulas, because we need all three: one for production, one because
Krisciunas & Schaefer's moonlight fit uses its own and mixing another in would
be unfaithful to the published model, and one for tests.

**Kasten & Young is the default, chosen for robustness rather than accuracy.**
Within our operating range (X <= 3, altitude >= 20 deg) the accuracy argument is
weak: plain sec(z) errs by ~3% at Z = 80 deg, i.e. 0.006 mag at k = 0.2, which is
irrelevant next to the 0.1-0.25 mag error of the moonlight model itself. The real
reasons are:

* sec(z) diverges at the horizon, producing inf/NaN that poison the efficiency
  array and crash the solver's integer coefficient rounding. Kasten-Young is
  finite everywhere.
* It is the convention the photometric-extinction literature is written in, so
  ``k = 0.20 mag/airmass`` means what people expect it to mean.

Pickering (2002) is tuned for *visual* limiting-magnitude work with refraction-
corrected altitudes; it buys nothing here and is not the CCD convention.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

#: Altitude below which we refuse to quote an airmass at all.
HORIZON_FLOOR_DEG = 0.0


def airmass_kasten_young(altitude_deg: NDArray[np.float64] | float) -> NDArray[np.float64]:
    """Kasten & Young (1989), Applied Optics 28, 4735.

        X = 1 / (sin(h) + 0.50572 * (h + 6.07995)^-1.6364)

    with ``h`` the *apparent* (refracted) altitude in DEGREES -- note the mixed
    units, which is a classic transcription bug: the sine takes radians, the
    power term takes degrees.

    Returns ``inf`` below the horizon rather than NaN. This is deliberate: NaN
    silently poisons ``argmax`` and comparison chains, whereas inf compares
    correctly and makes "not observable" fall out of ``np.isfinite``.
    """
    h = np.asarray(altitude_deg, dtype=np.float64)
    out = np.full(h.shape, np.inf, dtype=np.float64)
    up = h > HORIZON_FLOOR_DEG
    if np.any(up):
        hu = h[up] if h.ndim else h
        denom = np.sin(np.radians(hu)) + 0.50572 * np.power(hu + 6.07995, -1.6364)
        if h.ndim:
            out[up] = 1.0 / denom
        else:
            out = np.asarray(1.0 / denom, dtype=np.float64)
    return out


def airmass_ks91(zenith_deg: NDArray[np.float64] | float) -> NDArray[np.float64]:
    """Krisciunas & Schaefer (1991) eq. 3: ``X = (1 - 0.96 sin^2 Z)^(-1/2)``.

    Used ONLY inside the moonlight model, where fidelity to the published fit
    matters more than the marginally better behaviour of Kasten-Young.

    The function is symmetric about Z = 90 deg and must be clamped there, or a
    target below the horizon silently reports the airmass of its mirror image.
    """
    z = np.asarray(zenith_deg, dtype=np.float64)
    z = np.clip(z, 0.0, 90.0)
    s2 = np.sin(np.radians(z)) ** 2
    return np.asarray(np.power(1.0 - 0.96 * s2, -0.5), dtype=np.float64)


def airmass_plane_parallel(altitude_deg: NDArray[np.float64] | float) -> NDArray[np.float64]:
    """``sec(z)``. For tests and comparison only -- diverges at the horizon."""
    h = np.asarray(altitude_deg, dtype=np.float64)
    out = np.full(h.shape, np.inf, dtype=np.float64)
    up = h > HORIZON_FLOOR_DEG
    if h.ndim:
        out[up] = 1.0 / np.sin(np.radians(h[up]))
    elif up:
        out = np.asarray(1.0 / np.sin(np.radians(h)), dtype=np.float64)
    return out
