"""The bulk survey agrees with the per-target geometry, and is actually fast.

The survey exists so the setup form can rank a whole catalogue for a night.
Its positions come from the per-slot horizon rotation rather than from one
astropy transform per object, which is only acceptable if the two agree to far
better than anything the ranking cares about. These tests pin that agreement
against ``build_night_geometry`` itself, not against a re-derivation.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import numpy as np

from tscheduler.core.timegrid import TimeGrid
from tscheduler.domain.site import Site
from tscheduler.domain.targets import Target
from tscheduler.physics.geometry import build_night_geometry
from tscheduler.physics.survey import survey_night

SITE = Site(latitude_deg=40.1164, longitude_deg=-88.2434, elevation_m=227.0, name="Urbana")
GRID = TimeGrid.from_window(
    datetime(2026, 9, 13, 1, 0, tzinfo=UTC), datetime(2026, 9, 13, 10, 0, tzinfo=UTC), 15
)
TARGETS = (
    Target("m31", "M31", 10.6847, 41.2690, 18.4),
    Target("m27", "M27", 299.9015, 22.7211, 18.4),
    Target("m81", "M81", 148.8882, 69.0653, 19.0),
    Target("south", "Low south", 280.0, -35.0, 19.0),
    Target("polaris", "Near the pole", 37.95, 89.26, 19.0),
)


def test_positions_agree_with_the_per_target_geometry() -> None:
    geo = build_night_geometry(SITE, GRID, TARGETS)
    sv = survey_night(SITE, GRID.mid_unix())
    ra = np.array([t.ra_deg for t in TARGETS])
    dec = np.array([t.dec_deg for t in TARGETS])
    alt, az, sep = sv.place(ra, dec)

    # Half an arcminute is the aberration floor the globe already carries.
    assert np.max(np.abs(alt - geo.altitude_deg)) < 1.0 / 60.0

    # Azimuth is ill-conditioned near the zenith, so compare it only where
    # the object is below 85 degrees, and on the circle.
    ok = geo.altitude_deg < 85.0
    daz = (az - geo.azimuth_deg + 180.0) % 360.0 - 180.0
    assert np.max(np.abs(daz[ok])) < 2.0 / 60.0

    assert np.max(np.abs(sep - geo.moon_separation_deg)) < 1.0 / 60.0
    np.testing.assert_allclose(sv.sun_altitude_deg, geo.sun_altitude_deg, atol=1e-6)
    np.testing.assert_allclose(sv.moon_altitude_deg, geo.moon_altitude_deg, atol=1e-6)
    np.testing.assert_allclose(sv.moon_illumination, geo.moon_illumination, atol=1e-9)


def test_placing_thousands_of_objects_is_cheap() -> None:
    """The point of the module. Placing is numpy only, so it must not scale
    like the astropy half does."""
    sv = survey_night(SITE, GRID.mid_unix())
    rng = np.random.default_rng(3)
    n = 5000
    ra = rng.uniform(0.0, 360.0, n)
    dec = np.degrees(np.arcsin(rng.uniform(-1.0, 1.0, n)))
    t0 = time.perf_counter()
    alt, az, sep = sv.place(ra, dec)
    elapsed = time.perf_counter() - t0
    assert alt.shape == az.shape == sep.shape == (n, GRID.n_slots)
    assert elapsed < 0.5, f"placing {n} objects took {elapsed:.2f}s"
    assert np.all((az >= 0.0) & (az < 360.0))
    assert np.all((sep >= 0.0) & (sep <= 180.0))
