"""Krisciunas & Schaefer (1991) moonlight model.

The paper's stated accuracy is 8%-23% (0.1-0.25 mag), so tolerances here are
deliberately loose. Tightening them would be false precision.
"""

from __future__ import annotations

import numpy as np
import pytest

from tscheduler.physics.convert import mag_arcsec2_to_nl, nl_to_mag_arcsec2
from tscheduler.physics.moonlight import (
    moon_magnitude,
    moon_sky_brightness_nl,
    scattering_function,
)

DARK_NL = float(mag_arcsec2_to_nl(22.0))


def sky_mag(alpha: float, rho: float, moon_alt: float, targ_alt: float, k: float = 0.20) -> float:
    b = float(moon_sky_brightness_nl(alpha, rho, moon_alt, targ_alt, k))
    return float(nl_to_mag_arcsec2(b + DARK_NL))


def test_full_moon_near_zenith_matches_the_observational_anchor() -> None:
    """The one number every observer knows: a full moon high in the sky puts the
    V surface brightness around 18-19 mag/arcsec^2 at 90 deg separation."""
    assert sky_mag(0.0, 90.0, 85.0, 85.0) == pytest.approx(18.5, abs=0.6)


def test_normalisation_constant_is_ks91_scaled_not_apparent_magnitude() -> None:
    """Guards against "fixing" 3.84 to the Moon's real V magnitude of -12.73,
    which inflates the result by 4.2e6 and gives a ~2 mag/arcsec^2 sky."""
    assert float(moon_magnitude(0.0)) == pytest.approx(3.84, abs=1e-9)
    assert 17.0 < sky_mag(0.0, 90.0, 60.0, 60.0) < 20.0


def test_moon_magnitude_grows_toward_new_moon() -> None:
    assert float(moon_magnitude(0.0)) < float(moon_magnitude(90.0)) < float(moon_magnitude(180.0))
    assert float(moon_magnitude(90.0)) == pytest.approx(3.84 + 0.026 * 90 + 4e-9 * 90**4, abs=1e-9)


def test_scattering_function_components() -> None:
    """Rayleigh is symmetric in cos^2 so f_R(0) == f_R(180); the whole forward
    excess is Mie. Values computed directly from eq. 21-22."""
    f0, f180 = float(scattering_function(0.0)), float(scattering_function(180.0))
    assert f0 == pytest.approx(1.884e6, rel=1e-3)
    assert f180 == pytest.approx(4.720e5, rel=1e-3)
    assert f0 > f180, "forward scattering must dominate"


def test_brightness_falls_off_away_from_the_moon() -> None:
    mags = [sky_mag(0.0, r, 60.0, 60.0) for r in (15, 30, 60, 90)]
    assert mags == sorted(mags), "sky must get darker (numerically larger) away from the Moon"


def test_rayleigh_backscatter_brightens_the_antisolar_side() -> None:
    """cos^2 rises again past 90 deg, so the sky is slightly brighter at 150 deg
    than at 90. This is real physics, not a bug -- pin it so nobody 'fixes' it."""
    assert sky_mag(0.0, 150.0, 60.0, 60.0) < sky_mag(0.0, 90.0, 60.0, 60.0)


def test_new_moon_contributes_essentially_nothing() -> None:
    assert sky_mag(180.0, 60.0, 60.0, 60.0) == pytest.approx(22.0, abs=0.05)


def test_zero_below_the_horizon() -> None:
    assert float(moon_sky_brightness_nl(0.0, 60.0, -10.0, 60.0)) == 0.0
    assert float(moon_sky_brightness_nl(0.0, 60.0, -0.8, 60.0)) == 0.0


def test_moonset_is_tapered_not_stepped() -> None:
    """A hard step puts a discontinuity in the efficiency array, which makes the
    schedule flip between re-solves for no physical reason."""
    alts = np.linspace(-1.0, 4.0, 60)
    b = moon_sky_brightness_nl(0.0, 60.0, alts, 60.0)
    assert np.all(np.diff(b) >= -1e-9), "must rise monotonically through moonrise"
    rel = np.diff(b) / np.maximum(b[:-1], 1.0)
    assert np.max(rel) < 5.0, "no cliff in the taper"


def test_vectorises_over_a_grid() -> None:
    alpha = np.full((3, 4), 30.0)
    rho = np.full((3, 4), 70.0)
    malt = np.full((3, 4), 40.0)
    talt = np.full((3, 4), 55.0)
    out = moon_sky_brightness_nl(alpha, rho, malt, talt)
    assert out.shape == (3, 4)
    assert np.all(out > 0)


def test_extinction_coefficient_matters() -> None:
    """Both k terms are load-bearing; a hazier site scatters more moonlight in."""
    clear = sky_mag(0.0, 60.0, 60.0, 60.0, k=0.12)
    hazy = sky_mag(0.0, 60.0, 60.0, 60.0, k=0.35)
    assert hazy < clear, "more aerosol -> more scattered moonlight -> brighter sky"
