"""Airmass known-answer tests.

Values here were computed from the published formulas and cross-checked against
the literature, NOT copied from a secondary source. One of them contradicts a
commonly-quoted figure -- see test_kasten_young_at_zenith.
"""

from __future__ import annotations

import numpy as np
import pytest

from tscheduler.physics.airmass import (
    airmass_kasten_young,
    airmass_ks91,
    airmass_plane_parallel,
)


def test_kasten_young_at_zenith_is_not_exactly_one() -> None:
    """KY is a FIT, never normalised to 1 at the zenith.

    It returns 0.99971, and sources that quote 'X(90) = 1.000000' are wrong.
    The 2.9e-4 shortfall is 0.00006 mag at k=0.2 -- harmless, but worth pinning
    so nobody later "fixes" the formula to force it to 1.
    """
    assert float(airmass_kasten_young(90.0)) == pytest.approx(0.99971, abs=1e-5)


def test_kasten_young_published_values() -> None:
    assert float(airmass_kasten_young(30.0)) == pytest.approx(1.99429, abs=1e-5)
    assert float(airmass_kasten_young(60.0)) == pytest.approx(1.15399, abs=1e-5)
    # The published horizon value, and the headline advantage over sec(z).
    assert float(airmass_kasten_young(1e-9)) == pytest.approx(37.92, abs=0.01)


def test_kasten_young_beats_sec_z_where_it_matters() -> None:
    """Near the zenith the two agree; near the horizon sec(z) is useless."""
    assert float(airmass_plane_parallel(60.0)) == pytest.approx(
        float(airmass_kasten_young(60.0)), rel=1e-3
    )
    assert float(airmass_plane_parallel(1.0)) > 2 * float(airmass_kasten_young(1.0))


def test_below_horizon_is_inf_never_nan() -> None:
    """inf compares correctly and falls out of np.isfinite; NaN silently
    poisons argmax and every comparison chain downstream."""
    for f in (airmass_kasten_young, airmass_plane_parallel):
        v = f(np.array([-30.0, -0.5, 0.0]))
        assert np.all(np.isinf(v))
        assert not np.any(np.isnan(v))


def test_monotone_decreasing_with_altitude() -> None:
    alt = np.linspace(1.0, 90.0, 400)
    x = airmass_kasten_young(alt)
    assert np.all(np.diff(x) < 0), "airmass must fall monotonically as a target rises"


def test_vectorised_matches_scalar() -> None:
    alt = np.array([10.0, 25.0, 47.5, 80.0])
    vec = airmass_kasten_young(alt)
    assert np.allclose(vec, [float(airmass_kasten_young(a)) for a in alt])


def test_ks91_matches_its_own_published_form() -> None:
    """KS91 eq. 3. Used ONLY inside the moonlight model, for fidelity to the fit."""
    assert float(airmass_ks91(0.0)) == pytest.approx(1.0, abs=1e-12)
    assert float(airmass_ks91(60.0)) == pytest.approx(1.88982, abs=1e-5)
    assert float(airmass_ks91(90.0)) == pytest.approx(5.0, abs=1e-9)


def test_ks91_is_clamped_past_the_horizon() -> None:
    """The function is symmetric about Z=90; without clamping a target below the
    horizon silently reports the airmass of its mirror image."""
    assert float(airmass_ks91(120.0)) == float(airmass_ks91(90.0))
