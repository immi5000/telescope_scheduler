"""Satellite streak risk.

Runs offline against a committed TLE fixture (CelesTrak 'visual' group, the
bright objects that actually matter for streaks).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from tscheduler.core.timegrid import TimeGrid
from tscheduler.physics.satellites import (
    EARTH_RADIUS_KM,
    DensityMapRisk,
    NullSatelliteRisk,
    build_density_map,
    is_sunlit,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tle" / "visual.tle"
LAT, LON, ELEV = 40.1164, -88.2434, 227.0
BASE = datetime(2026, 9, 17, 0, 0, tzinfo=UTC)


def load_tles() -> list[tuple[str, str, str]]:
    lines = FIXTURE.read_text().splitlines()
    return [
        (lines[i].strip(), lines[i + 1], lines[i + 2])
        for i in range(0, len(lines) - 2, 3)
        if lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 ")
    ]


# --- the shadow test ---------------------------------------------------------
def test_satellite_on_the_sunward_side_is_lit() -> None:
    sun = np.array([1.0, 0.0, 0.0])
    assert bool(is_sunlit(np.array([7000.0, 0.0, 0.0]), sun))


def test_satellite_directly_behind_earth_is_in_shadow() -> None:
    sun = np.array([1.0, 0.0, 0.0])
    assert not bool(is_sunlit(np.array([-7000.0, 0.0, 0.0]), sun))


def test_satellite_behind_earth_but_outside_the_shadow_cylinder_is_lit() -> None:
    """The case a naive "anti-sun means dark" test gets wrong: a high-latitude
    satellite on the night side is still in sunlight if it clears the cylinder."""
    sun = np.array([1.0, 0.0, 0.0])
    assert bool(is_sunlit(np.array([-7000.0, EARTH_RADIUS_KM + 500.0, 0.0]), sun))


def test_shadow_boundary_is_one_earth_radius() -> None:
    sun = np.array([1.0, 0.0, 0.0])
    assert not bool(is_sunlit(np.array([-7000.0, EARTH_RADIUS_KM - 50.0, 0.0]), sun))
    assert bool(is_sunlit(np.array([-7000.0, EARTH_RADIUS_KM + 50.0, 0.0]), sun))


def test_is_sunlit_vectorises() -> None:
    sun = np.tile(np.array([1.0, 0.0, 0.0]), (4, 1))
    pos = np.array([[7000.0, 0, 0], [-7000.0, 0, 0], [-7000.0, 9000.0, 0], [0, 7000.0, 0]])
    assert list(is_sunlit(pos, sun)) == [True, False, True, True]


# --- the density map ---------------------------------------------------------
@pytest.fixture(scope="module")
def tles() -> list[tuple[str, str, str]]:
    t = load_tles()
    assert len(t) > 50, "TLE fixture looks empty"
    return t


def _lit_median(tles, hour: int) -> float:
    g = TimeGrid.from_window(BASE + timedelta(hours=hour), BASE + timedelta(hours=hour, minutes=30))
    dm = build_density_map(tles, LAT, LON, ELEV, g.mids())
    return float(np.median(dm.counts.sum(axis=(0, 1))))


def test_fewer_satellites_are_lit_at_deep_night_than_at_twilight(tles) -> None:
    """The headline validation of the shadow geometry.

    Local midnight at this site is ~06 UTC. Satellites are sunlit near twilight
    and pass into Earth's shadow in the middle of the night, so the count must
    show a clear minimum there. If the shadow test were broken or inverted this
    curve would be flat or upside down.
    """
    evening = _lit_median(tles, 0)  # 19h local
    deep = _lit_median(tles, 6)  # 01h local
    morning = _lit_median(tles, 10)  # 05h local
    assert deep < evening, f"deep night {deep} should be below evening {evening}"
    assert deep < morning, f"deep night {deep} should be below morning {morning}"


def test_density_map_shape_and_bookkeeping(tles) -> None:
    g = TimeGrid.from_window(BASE + timedelta(hours=1), BASE + timedelta(hours=2))
    dm = build_density_map(tles, LAT, LON, ELEV, g.mids(), n_alt_bins=9, n_az_bins=12)
    assert dm.counts.shape == (9, 12, g.n_slots)
    assert dm.n_objects == len(tles)
    assert np.all(dm.counts >= 0)


def test_epoch_spread_is_reported(tles) -> None:
    """SGP4 degrades quickly away from its element epoch, so a caller needs to
    know how far the elements were stretched before trusting a position."""
    g = TimeGrid.from_window(BASE, BASE + timedelta(minutes=30))
    dm = build_density_map(tles, LAT, LON, ELEV, g.mids())
    assert dm.epoch_spread_days >= 0.0
    assert dm.epoch_spread_days < 60.0, "fixture TLEs are implausibly stale"


def test_solid_angle_weighting_shrinks_toward_the_zenith(tles) -> None:
    """A raw count per bin would badly overstate zenith density, because a bin
    near the pole of the alt-az sphere covers far less sky than one at the
    horizon."""
    g = TimeGrid.from_window(BASE, BASE + timedelta(minutes=10))
    n_az = 12
    dm = build_density_map(tles, LAT, LON, ELEV, g.mids(), n_alt_bins=9, n_az_bins=n_az)
    omega = dm.bin_solid_angle_deg2()
    assert omega[0] > omega[-1], "horizon bins must subtend more sky than zenith bins"
    assert np.all(omega > 0)
    # The hemisphere above the horizon is 2*pi sr = 20626.5 deg^2, and the
    # bins must tile it exactly -- a good check that the sin() weighting is
    # right rather than merely monotonic.
    assert float(omega.sum() * n_az) == pytest.approx(20626.5, rel=0.02)


def test_risk_is_higher_where_more_satellites_are(tles) -> None:
    g = TimeGrid.from_window(BASE + timedelta(hours=23), BASE + timedelta(hours=25))
    dm = build_density_map(tles, LAT, LON, ELEV, g.mids())
    risk = DensityMapRisk(dm)
    busiest = int(np.argmax(dm.counts.sum(axis=(0, 1))))
    quietest = int(np.argmin(dm.counts.sum(axis=(0, 1))))
    if dm.counts[:, :, busiest].sum() > dm.counts[:, :, quietest].sum():
        ai, zi = np.unravel_index(int(np.argmax(dm.counts[:, :, busiest])), dm.counts.shape[:2])
        alt = float((dm.alt_edges[ai] + dm.alt_edges[ai + 1]) / 2)
        az = float((dm.az_edges[zi] + dm.az_edges[zi + 1]) / 2)
        hot = risk.streak_rate_per_min(np.array([alt]), np.array([az]), busiest, 0.29)[0]
        cold = risk.streak_rate_per_min(np.array([alt]), np.array([az]), quietest, 0.29)[0]
        assert hot >= cold


def test_bigger_field_of_view_catches_more_streaks(tles) -> None:
    """Aimed at a bin that actually holds satellites -- most of the sky is empty
    at any instant, so a fixed alt/az would compare 0 against 0 and prove
    nothing."""
    g = TimeGrid.from_window(BASE + timedelta(hours=23), BASE + timedelta(hours=25))
    dm = build_density_map(tles, LAT, LON, ELEV, g.mids())
    risk = DensityMapRisk(dm)

    flat = int(np.argmax(dm.counts))
    ai, zi, slot = np.unravel_index(flat, dm.counts.shape)
    assert dm.counts[ai, zi, slot] > 0, "fixture has no satellites anywhere"
    alt = float((dm.alt_edges[ai] + dm.alt_edges[ai + 1]) / 2)
    az = float((dm.az_edges[zi] + dm.az_edges[zi + 1]) / 2)

    small = risk.streak_rate_per_min(np.array([alt]), np.array([az]), int(slot), 0.1)[0]
    big = risk.streak_rate_per_min(np.array([alt]), np.array([az]), int(slot), 4.0)[0]
    assert big > small > 0.0


def test_null_model_is_the_ablation_switch() -> None:
    """`no_satellites` must be a config change, not a refactor -- this layer is
    the most code for the least likely influence on an actual decision."""
    n = NullSatelliteRisk()
    out = n.streak_rate_per_min(np.array([30.0, 60.0]), np.array([0.0, 180.0]), 0, 1.0)
    assert np.all(out == 0.0)


def test_empty_tle_set_is_an_error_not_a_silent_zero(tles) -> None:
    g = TimeGrid.from_window(BASE, BASE + timedelta(minutes=10))
    with pytest.raises(ValueError, match="no TLEs"):
        build_density_map([], LAT, LON, ELEV, g.mids())
