"""Surface-brightness conversions.

The headline test is the closure: two INDEPENDENT chains -- KS91's nanoLambert
relation and the luminance zero point -- must land on the same answer, and
Falchi's published natural-sky value must land there too.
"""

from __future__ import annotations

import numpy as np
import pytest

from tscheduler.physics.convert import (
    add_in_flux,
    bortle_to_artificial_nl,
    cd_m2_to_mag_arcsec2,
    mag_arcsec2_to_nl,
    nl_to_cd_m2,
    nl_to_mag_arcsec2,
    ucd_m2_to_nl,
    validate_artificial_brightness,
)


def test_the_closure() -> None:
    """mu=22.00 -> 54.0 nL -> 1.719e-4 cd/m2 -> 21.99, via two unrelated routes."""
    nl = float(mag_arcsec2_to_nl(22.0))
    assert nl == pytest.approx(54.0, abs=0.1)
    cd = float(nl_to_cd_m2(nl))
    assert cd == pytest.approx(1.719e-4, rel=1e-3)
    assert float(cd_m2_to_mag_arcsec2(cd)) == pytest.approx(22.0, abs=0.01)


def test_falchi_natural_sky_agrees_with_the_ks91_chain() -> None:
    """Falchi et al. (2016) use 174 ucd/m^2 for the natural sky. It must land on
    ~22.0 mag/arcsec^2 through a completely different unit path."""
    assert float(nl_to_mag_arcsec2(ucd_m2_to_nl(174.0))) == pytest.approx(22.0, abs=0.02)


def test_round_trip_is_exact() -> None:
    for mu in (16.0, 18.5, 20.0, 21.3, 22.0, 23.0):
        assert float(nl_to_mag_arcsec2(mag_arcsec2_to_nl(mu))) == pytest.approx(mu, abs=1e-12)


def test_brighter_sky_is_more_flux_and_a_smaller_magnitude() -> None:
    assert float(mag_arcsec2_to_nl(18.0)) > float(mag_arcsec2_to_nl(22.0))


def test_zero_flux_is_infinitely_faint_not_an_error() -> None:
    """A component that happens to be zero (moon below the horizon) must flow
    through arithmetic without a special case."""
    assert float(nl_to_mag_arcsec2(0.0)) == np.inf


def test_magnitudes_must_not_be_added__the_canonical_bug() -> None:
    """Two equal components add to 2x the FLUX, i.e. 0.753 mag brighter -- NOT
    to double the magnitude. This is the #1 error in this domain; pin it forever.
    """
    a = mag_arcsec2_to_nl(21.0)
    total_flux = add_in_flux(a, a)
    # Tolerance is 1e-4, not 1e-9, and the reason is interesting: KS91 publishes
    # 0.92104 where ln(10)/2.5 = 0.9210340..., so the rounded constant shifts the
    # absolute nL scale by ~5e-6 mag relative to a pure 10^(-0.4 mu) scale. The
    # round trip is still exact to 1e-12; only the tie to the magnitude zero
    # point carries the rounding. Physically irrelevant, worth not "fixing".
    assert float(nl_to_mag_arcsec2(total_flux)) == pytest.approx(21.0 - 2.5 * np.log10(2), abs=1e-4)
    assert float(nl_to_mag_arcsec2(total_flux)) == pytest.approx(20.2474, abs=1e-3)


def test_summing_four_components_in_flux_differs_sharply_from_magnitudes() -> None:
    """Construct the case the guard exists for: naive magnitude-averaging is off
    by more than a third of a magnitude."""
    parts = [21.0, 21.5, 20.5, 22.0]
    flux_total = float(nl_to_mag_arcsec2(add_in_flux(*[mag_arcsec2_to_nl(p) for p in parts])))
    naive_mean = float(np.mean(parts))
    assert flux_total == pytest.approx(19.605, abs=1e-3)
    # Averaging magnitudes instead of summing flux is wrong by 1.64 mag here --
    # a factor of 4.5 in sky brightness, which would wreck every exposure time.
    assert abs(flux_total - naive_mean) > 1.5


def test_bortle_subtracts_the_natural_component_in_flux() -> None:
    """Bortle gives TOTAL brightness; the artificial part is the remainder after
    removing the natural sky -- in flux, not magnitudes."""
    for cls in range(1, 10):
        art = bortle_to_artificial_nl(cls)
        total = art + float(mag_arcsec2_to_nl(22.0))
        from tscheduler.physics.convert import BORTLE_ZENITH_MAG

        assert float(nl_to_mag_arcsec2(total)) == pytest.approx(BORTLE_ZENITH_MAG[cls], abs=0.02)


def test_bortle_is_monotone_and_class1_is_nearly_pristine() -> None:
    arts = [bortle_to_artificial_nl(c) for c in range(1, 10)]
    assert arts == sorted(arts)
    assert arts[0] < 10.0
    assert arts[-1] > 1000.0


def test_bortle_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="1-9"):
        bortle_to_artificial_nl(0)


def test_validator_catches_the_ucd_vs_mcd_unit_confusion() -> None:
    """A factor-of-1000 error still produces plausible-looking numbers, so the
    validator has to reason about the implied sky rather than the value itself."""
    validate_artificial_brightness(float(ucd_m2_to_nl(500.0)))
    with pytest.raises(ValueError, match="Check units"):
        validate_artificial_brightness(float(ucd_m2_to_nl(500_000.0)))


def test_validator_rejects_negative() -> None:
    with pytest.raises(ValueError, match="negative"):
        validate_artificial_brightness(-1.0)


def test_vectorises() -> None:
    mu = np.array([[20.0, 21.0], [22.0, 23.0]])
    assert mag_arcsec2_to_nl(mu).shape == (2, 2)
    assert np.allclose(nl_to_mag_arcsec2(mag_arcsec2_to_nl(mu)), mu)
