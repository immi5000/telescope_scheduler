"""The planets for the sky view: positions that agree with the night's frame,
and magnitudes in the range the real planets occupy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from tscheduler import catalog
from tscheduler.api import presets
from tscheduler.core.timegrid import TimeGrid
from tscheduler.domain.site import Site
from tscheduler.domain.targets import Target
from tscheduler.physics.convert import naked_eye_limit_mag
from tscheduler.physics.geometry import build_night_geometry
from tscheduler.physics.planets import (
    PLANETS,
    build_planet_tracks,
    disc_surface_brightness,
    planet_magnitude,
)

SITE = Site(latitude_deg=40.1164, longitude_deg=-88.2434, elevation_m=227.0, name="Urbana")
START = datetime(2026, 9, 17, 1, 0, tzinfo=UTC)
GRID = TimeGrid.from_window(START, START + timedelta(hours=8), 5)


@pytest.fixture(scope="module")
def tracks():  # type: ignore[no-untyped-def]
    return build_planet_tracks(SITE, GRID)


def test_all_seven_planets_every_slot(tracks) -> None:  # type: ignore[no-untyped-def]
    assert [t.body for t in tracks] == [b for b, _ in PLANETS]
    for t in tracks:
        assert t.altitude_deg.shape == t.azimuth_deg.shape == (GRID.n_slots,)
        assert np.all(np.abs(t.altitude_deg) <= 90.0)


def test_magnitudes_are_where_the_real_planets_are(tracks) -> None:  # type: ignore[no-untyped-def]
    mag = {t.body: t.magnitude for t in tracks}
    assert -4.9 < mag["venus"] < -3.7
    assert -2.9 < mag["jupiter"] < -1.5
    assert -0.6 < mag["saturn"] < 1.5
    assert 5.3 < mag["uranus"] < 6.1
    assert 7.6 < mag["neptune"] < 8.0


def test_phase_law_at_known_geometry() -> None:
    """Jupiter at opposition, 5.2 AU from the Sun and 4.2 from Earth."""
    assert planet_magnitude("jupiter", 5.2, 4.2, 0.0) == pytest.approx(-2.70, abs=0.02)


def test_positions_agree_with_the_frame_the_browser_uses(tracks) -> None:  # type: ignore[no-untyped-def]
    """Rotate each planet's published RA/Dec into the horizon by the published
    frame quaternion and it must land on its published altitude.

    This is the check that ties the planets to the same sky as the stars:
    a planet whose alt/az came from one transform and whose frame came from
    another would sit a little off the ecliptic, beautifully.
    """
    target = Target(id="x", name="x", ra_deg=0.0, dec_deg=0.0, magnitude=10.0)
    geo = build_night_geometry(SITE, GRID, (target,))
    mid = GRID.n_slots // 2
    x, y, z, w = geo.frame_quat[mid]
    # Rotation matrix of the quaternion; local = R^T icrs.
    r = np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )
    for t in tracks:
        ra, dec = np.radians(t.ra_deg), np.radians(t.dec_deg)
        v = np.array([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)])
        alt = np.degrees(np.arcsin((r.T @ v)[2]))
        assert alt == pytest.approx(t.altitude_deg[mid], abs=0.05), t.name


@pytest.mark.parametrize(
    ("mu", "lo", "hi"),
    [(22.0, 6.5, 6.7), (20.7, 5.8, 6.1), (19.0, 4.6, 5.0), (17.0, 3.1, 3.6)],
)
def test_naked_eye_limit(mu: float, lo: float, hi: float) -> None:
    assert lo < float(naked_eye_limit_mag(mu)) < hi


# --- a planet as a schedulable target ----------------------------------------
def test_apparent_diameters_are_where_the_real_planets_are(tracks) -> None:  # type: ignore[no-untyped-def]
    d = {t.body: t.apparent_diameter_arcsec for t in tracks}
    # Ranges wide enough to hold any date, tight enough to catch a unit slip:
    # a radius/diameter confusion or a degrees/arcsec mix fails every one.
    assert 4.5 < d["mercury"] < 13.0
    assert 9.5 < d["venus"] < 66.0
    assert 3.5 < d["mars"] < 26.0
    assert 29.0 < d["jupiter"] < 51.0
    assert 14.0 < d["saturn"] < 21.0
    assert 3.3 < d["uranus"] < 4.2
    assert 2.2 < d["neptune"] < 2.5


def test_surface_brightness_is_far_brighter_than_any_catalogue_object(tracks) -> None:
    """The whole reason a planet needs its own figure: the exposure model plans
    in mag/arcsec^2, and a planet's disc is some fifteen magnitudes per
    arcsec^2 brighter than the faintest surfaces the catalogue holds (~23)."""
    for t in tracks:
        assert 0.0 < t.surface_brightness < 11.0, f"{t.name}: {t.surface_brightness}"
    sb = {t.body: t.surface_brightness for t in tracks}
    # Ordering is physics, not coincidence: the ice giants are dim surfaces,
    # the inner planets bright ones, whatever the integrated magnitude says.
    assert sb["venus"] < sb["jupiter"] < sb["uranus"] < sb["neptune"]


def test_surface_brightness_uses_the_illuminated_disc_not_the_whole_one() -> None:
    """A crescent spreads the same flux over less area, so it is BRIGHTER per
    arcsec^2. Dividing by the full disc would lose about two magnitudes at the
    phase where a planet is most obviously bright."""
    full = disc_surface_brightness(-4.0, 30.0, 0.0)
    crescent = disc_surface_brightness(-4.0, 30.0, 140.0)
    assert crescent < full, "a crescent must not come out fainter per arcsec^2"
    assert 1.5 < full - crescent < 3.0


def test_saturn_the_planet_wins_over_saturn_the_nebula() -> None:
    """`catalog.resolve("saturn")` finds the Saturn Nebula, so the resolution
    order is load-bearing: catalogue-first silently swaps a planet 9 AU away
    for a planetary nebula 4,000 light years away, and nothing says so."""
    assert catalog.resolve("saturn") is not None, "the collision this guards is real"
    assert catalog.resolve("saturn").id == "ngc7009"

    planet = presets.planet_target(SITE, GRID, "saturn")
    assert planet is not None
    assert planet.id == "saturn"
    assert planet.name == "Saturn"
    # The designations still reach the nebula.
    for q in ("saturn nebula", "ngc7009"):
        assert presets.planet_target(SITE, GRID, q) is None
        assert presets.catalog_target(q).id == "ngc7009"


def test_planet_target_carries_the_surface_brightness_not_the_magnitude() -> None:
    """The scheduler reads `Target.magnitude` as a SURFACE brightness. Handing
    it the integrated V magnitude would make Jupiter (V about -2) some seven
    magnitudes brighter per arcsec^2 than it is."""
    (jupiter,) = [t for t in build_planet_tracks(SITE, GRID) if t.body == "jupiter"]
    target = presets.planet_target(SITE, GRID, "jupiter")
    assert target is not None
    assert target.magnitude == pytest.approx(jupiter.surface_brightness)
    assert target.magnitude != pytest.approx(jupiter.magnitude)


def test_a_name_that_is_no_planet_resolves_to_nothing() -> None:
    for q in ("m31", "pluto", "the moon", "", "jupiterr"):
        assert presets.planet_target(SITE, GRID, q) is None
