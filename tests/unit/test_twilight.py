"""Patat et al. (2006) twilight model and its extrapolation."""

from __future__ import annotations

import numpy as np
import pytest

from tscheduler.physics.convert import mag_arcsec2_to_nl, nl_to_mag_arcsec2
from tscheduler.physics.twilight import (
    PATAT_TURNOVER_ZETA,
    PatatZenithTwilight,
    twilight_sky_nl,
)

M = PatatZenithTwilight()
DARK = float(mag_arcsec2_to_nl(22.0))


def mu_at(zeta: float) -> float:
    return float(nl_to_mag_arcsec2(M.zenith_nl(90.0 - zeta)))


@pytest.mark.parametrize(("zeta", "expected"), [(95.0, 11.840), (100.0, 18.005), (105.0, 21.320)])
def test_matches_published_table_exactly(zeta: float, expected: float) -> None:
    assert mu_at(zeta) == pytest.approx(expected, abs=1e-3)


def test_turnover_is_where_the_paper_says_it_would_be() -> None:
    assert pytest.approx(108.32, abs=0.01) == PATAT_TURNOVER_ZETA


def test_no_brightening_past_the_turnover() -> None:
    """The raw parabola gets BRIGHTER past zeta=108.32, which is unphysical.
    Our extrapolation must keep decaying instead."""
    assert mu_at(112.0) > mu_at(108.0) > mu_at(106.0)


def test_flux_is_monotonically_decreasing_everywhere() -> None:
    z = np.linspace(94.0, 130.0, 3000)
    b = M.zenith_nl(90.0 - z)
    assert np.all(np.diff(b) <= 1e-12)


def test_no_discontinuity_at_the_validity_boundary() -> None:
    """Clamping to zero at the turnover would leave an ~0.8 mag cliff, because
    the polynomial there still gives 21.95 -- comparable to the dark sky itself.
    A cliff makes the schedule flip between re-solves for no physical reason."""
    z = np.linspace(94.0, 125.0, 4000)
    total = nl_to_mag_arcsec2(M.zenith_nl(90.0 - z) + DARK)
    assert float(np.max(np.abs(np.diff(total)))) < 0.05


def test_twilight_is_negligible_but_not_absent_at_astronomical_dusk() -> None:
    """The extrapolation is calibrated so -18 deg means what the convention says
    it means: contributing, but only marginally."""
    total = float(nl_to_mag_arcsec2(M.zenith_nl(-18.0) + DARK))
    assert 0.05 < (22.0 - total) < 0.30


def test_fully_dark_well_past_astronomical_twilight() -> None:
    assert float(nl_to_mag_arcsec2(M.zenith_nl(-25.0) + DARK)) == pytest.approx(22.0, abs=0.01)


def test_daylight_end_is_held_not_extrapolated_wildly() -> None:
    """Before zeta=95 the fit extrapolates badly; hold it rather than emit a
    nonsense value that would dominate the flux sum."""
    assert mu_at(80.0) == pytest.approx(mu_at(95.0), abs=1e-9)


def test_site_offset_shifts_the_whole_curve() -> None:
    """Paranal is ~30% fainter in V twilight than a low-altitude site; the offset
    exists so that is calibratable rather than baked in."""
    hazy = PatatZenithTwilight(offset_mag=-0.3)
    assert float(hazy.zenith_nl(-10.0)) > float(M.zenith_nl(-10.0))


def test_off_zenith_is_brighter_and_sunward_is_brighter_still() -> None:
    zenith = float(twilight_sky_nl(-10.0, 85.0))
    low = float(twilight_sky_nl(-10.0, 25.0))
    assert low > zenith, "twilight grows with airmass"

    sunward = float(twilight_sky_nl(-10.0, 25.0, sun_azimuth_deg=270.0, target_azimuth_deg=270.0))
    away = float(twilight_sky_nl(-10.0, 25.0, sun_azimuth_deg=270.0, target_azimuth_deg=90.0))
    assert sunward > away


def test_off_zenith_handles_targets_below_the_horizon() -> None:
    """Airmass is inf down there; the scaling must not produce NaN."""
    out = twilight_sky_nl(-10.0, np.array([-20.0, 5.0, 60.0]))
    assert np.all(np.isfinite(out))
