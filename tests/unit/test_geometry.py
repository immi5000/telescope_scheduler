"""Geometry tests anchored on independently-checkable astronomy.

Each assertion here is something that must be true of the real sky, not
something derived from this code -- so they catch a wrong frame, a sign error
in longitude, or a swapped axis, which internal-consistency checks would not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from tscheduler.core.timegrid import TimeGrid
from tscheduler.domain.site import Site
from tscheduler.domain.targets import Target
from tscheduler.physics.geometry import build_night_geometry

LAT, LON = 40.1164, -88.2434  # Urbana, IL
SITE = Site(latitude_deg=LAT, longitude_deg=LON, elevation_m=227.0)
NIGHT_START = datetime(2026, 9, 12, 23, 0, tzinfo=UTC)

POLARIS = Target("polaris", "Polaris", 37.9545, 89.2641, 2.0)
M51 = Target("m51", "M51", 202.4696, 47.1952, 8.4)
M31 = Target("m31", "M31", 10.6847, 41.2690, 3.4)
NGC7331 = Target("n7331", "NGC 7331", 339.2671, 34.4158, 9.5)


@pytest.fixture(scope="module")
def geo():
    grid = TimeGrid.from_window(NIGHT_START, NIGHT_START + timedelta(hours=10))
    return build_night_geometry(SITE, grid, (POLARIS, M51, M31, NGC7331))


def test_polaris_altitude_equals_latitude(geo) -> None:
    """The single best end-to-end check of the alt/az chain: Polaris sits at an
    altitude equal to the observer's latitude, all night, to within its ~0.7 deg
    offset from the true pole. A wrong frame or a longitude sign error breaks this.
    """
    alt = geo.altitude_deg[0]
    assert np.all(np.abs(alt - LAT) < 1.0)
    assert alt.std() < 0.5, "Polaris must not swing through the night"


def test_transit_altitude_matches_90_minus_zenith_distance(geo) -> None:
    """Peak altitude must equal 90 - |latitude - APPARENT declination|.

    Note "apparent": using the J2000 catalogue declination is off by ~0.15 deg
    for a 2026 epoch, because precession has moved M31's declination by
    26.7 yr * ~19.7 arcsec/yr. Transforming to the true equator of date and
    getting agreement to 0.02 deg is therefore a much stronger statement than a
    loose tolerance would be -- it proves precession is handled, rather than
    hiding the fact that it is.
    """
    from astropy import units as u
    from astropy.coordinates import TETE, SkyCoord
    from astropy.time import Time

    for idx, tgt in ((2, M31), (3, NGC7331)):
        peak_slot = int(np.argmax(geo.altitude_deg[idx]))
        t = Time(geo.grid.slot_mid(peak_slot))
        apparent = SkyCoord(tgt.ra_deg * u.deg, tgt.dec_deg * u.deg).transform_to(TETE(obstime=t))
        expected = 90.0 - abs(LAT - apparent.dec.deg)
        assert geo.altitude_deg[idx].max() == pytest.approx(expected, abs=0.02), tgt.name


def test_azimuth_is_in_range_and_targets_move(geo) -> None:
    assert np.all((geo.azimuth_deg >= 0.0) & (geo.azimuth_deg < 360.0))
    assert np.ptp(geo.altitude_deg[1]) > 5.0, "M51 should visibly move over 10 hours"


def test_airmass_is_inf_exactly_where_target_is_down(geo) -> None:
    down = geo.altitude_deg <= 0.0
    assert np.all(np.isinf(geo.airmass[down]))
    assert np.all(np.isfinite(geo.airmass[~down]))
    assert not np.any(np.isnan(geo.airmass))


def test_sun_is_below_horizon_through_a_september_night(geo) -> None:
    """23:00-09:00 UTC is roughly 18:00-04:00 local; the sun sets during it."""
    assert geo.sun_altitude_deg.min() < -30.0
    assert np.sum(geo.sun_altitude_deg < -18.0) > 40, "expect real astronomical darkness"


def test_moon_separation_is_topocentric_and_sane(geo) -> None:
    """Regression guard for a real bug: comparing an ICRS target against a GCRS
    Moon gave separations wrong by ~70 deg, which would have placed scattered
    moonlight in entirely the wrong part of the sky.

    M51 (RA 202) and M31 (RA 011) are ~150 deg apart, so their separations from
    the same Moon cannot both be small.
    """
    assert np.all((geo.moon_separation_deg >= 0.0) & (geo.moon_separation_deg <= 180.0))
    sep_m51 = geo.moon_separation_deg[1].mean()
    sep_m31 = geo.moon_separation_deg[2].mean()
    assert abs(sep_m51 - sep_m31) > 50.0
    # Cross-check against the true angular distance between the two targets.
    from astropy import units as u
    from astropy.coordinates import SkyCoord

    m51 = SkyCoord(M51.ra_deg * u.deg, M51.dec_deg * u.deg)
    m31 = SkyCoord(M31.ra_deg * u.deg, M31.dec_deg * u.deg)
    m51_m31 = m51.separation(m31).deg
    # Triangle inequality on the sphere must hold for every slot.
    assert np.all(np.abs(geo.moon_separation_deg[1] - geo.moon_separation_deg[2]) <= m51_m31 + 1e-6)


def test_phase_angle_and_illumination_are_consistent(geo) -> None:
    """alpha = 0 is FULL moon, 180 is new -- the opposite of the illuminated
    fraction, and the convention Krisciunas & Schaefer actually take."""
    alpha = geo.moon_phase_angle_deg
    illum = geo.moon_illumination
    assert np.all((alpha >= 0.0) & (alpha <= 180.0))
    assert np.all((illum >= 0.0) & (illum <= 1.0))
    assert np.allclose(illum, (1.0 + np.cos(np.radians(alpha))) / 2.0)


def test_moon_veto_only_applies_while_the_moon_is_up() -> None:
    """A target 5 deg from a Moon that has set is perfectly observable; vetoing
    it would silently discard good sky."""
    grid = TimeGrid.from_window(NIGHT_START, NIGHT_START + timedelta(hours=10))
    g = build_night_geometry(SITE, grid, (M51,))
    close = g.moon_separation_deg[0] < SITE.min_moon_separation_deg
    moon_down = g.moon_altitude_deg <= 0.0
    high = g.altitude_deg[0] >= SITE.min_altitude_deg
    ok = close & moon_down & high
    if np.any(ok):
        assert np.all(g.visible[0][ok])


def test_visible_mask_respects_the_altitude_floor(geo) -> None:
    assert not np.any(geo.visible & (geo.altitude_deg < SITE.min_altitude_deg))


def test_geometry_is_deterministic() -> None:
    """Required for the no-lookahead fingerprint test: the same inputs must
    produce byte-identical arrays across runs."""
    grid = TimeGrid.from_window(NIGHT_START, NIGHT_START + timedelta(hours=2))
    a = build_night_geometry(SITE, grid, (M51, M31))
    b = build_night_geometry(SITE, grid, (M51, M31))
    for k, v in a.arrays().items():
        assert np.array_equal(v, b.arrays()[k]), k


def test_southern_hemisphere_site_works() -> None:
    """Sign errors in latitude are a classic bug; the SMC should be high from Chile."""
    chile = Site(latitude_deg=-30.24, longitude_deg=-70.74, elevation_m=2400.0)
    grid = TimeGrid.from_window(NIGHT_START, NIGHT_START + timedelta(hours=8))
    smc = Target("smc", "SMC", 13.1583, -72.8003, 2.7)
    g = build_night_geometry(chile, grid, (smc,))
    assert g.altitude_deg[0].max() > 40.0
