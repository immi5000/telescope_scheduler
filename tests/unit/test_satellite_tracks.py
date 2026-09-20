"""Satellite passes for the sky view.

Offline, against the committed CelesTrak 'visual' fixture. The first test is
the one that matters: the fast TEME -> horizon path must agree with astropy's
full transform, because a satellite drawn a degree off its real track looks
exactly as convincing as one drawn on it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from astropy import units as u
from astropy.coordinates import ITRS, TEME, AltAz, CartesianRepresentation, EarthLocation
from astropy.time import Time
from sgp4.api import Satrec, jday

from tscheduler.physics.satellite_tracks import (
    DEFAULT_STANDARD_MAGNITUDE,
    build_satellite_passes,
    gmst82_rad,
)
from tscheduler.providers.satellites.celestrak import parse_tle_text

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tle" / "visual.tle"
LAT, LON, ELEV = 40.1164, -88.2434, 227.0
START = datetime(2026, 9, 17, 0, 30, tzinfo=UTC)
END = START + timedelta(hours=9)
ISS = 25544


@pytest.fixture(scope="module")
def tles() -> list[tuple[int, str, str, str]]:
    return [(r.norad_id, r.name, r.line1, r.line2) for r in parse_tle_text(FIXTURE.read_text())]


@pytest.fixture(scope="module")
def tracks(tles: list[tuple[int, str, str, str]]):  # type: ignore[no-untyped-def]
    return build_satellite_passes(tles, LAT, LON, ELEV, START, END)


def test_fast_frame_matches_astropy(tles: list[tuple[int, str, str, str]], tracks) -> None:  # type: ignore[no-untyped-def]
    """Every tenth sample of the first few passes, through astropy's TEME frame.

    Topocentric ITRS first, exactly as astropy's own satellite guide does it.
    Handing a GEOCENTRIC ITRS position straight to AltAz sends it through CIRS
    with stellar-aberration corrections that are right for a star and wrong for
    an object 400 km away -- that route disagrees with plain geometry by 0.08
    degrees on a close pass, and it is the reference that is wrong.
    """
    loc = EarthLocation.from_geodetic(lon=LON * u.deg, lat=LAT * u.deg, height=ELEV * u.m)
    lines = {n: (l1, l2) for n, _, l1, l2 in tles}
    worst = 0.0
    for p in tracks.passes[:12]:
        sat = Satrec.twoline2rv(*lines[p.norad_id])
        for i in range(0, len(p.altitude_deg), 10):
            t = p.start + timedelta(seconds=p.step_seconds * i)
            jd, fr = jday(t.year, t.month, t.day, t.hour, t.minute, t.second + t.microsecond / 1e6)
            _, r, _ = sat.sgp4(jd, fr)
            tt = Time(t)
            geo = TEME(CartesianRepresentation(np.array(r) * u.km), obstime=tt).transform_to(
                ITRS(obstime=tt)
            )
            topo = geo.cartesian.without_differentials() - loc.get_itrs(tt).cartesian
            aa = ITRS(topo, obstime=tt, location=loc).transform_to(AltAz(obstime=tt, location=loc))
            d_az = ((aa.az.deg - p.azimuth_deg[i] + 180.0) % 360.0) - 180.0
            sep = np.hypot(aa.alt.deg - p.altitude_deg[i], d_az * np.cos(np.radians(aa.alt.deg)))
            worst = max(worst, float(sep))
    assert worst < 0.01, f"fast frame is {worst:.4f} deg off astropy"


def test_gmst82_agrees_with_astropy_mean_sidereal_time() -> None:
    t = Time(["2026-09-17T00:00:00", "2026-09-17T06:00:00", "2031-01-01T12:00:00"], scale="utc")
    ours = np.degrees(gmst82_rad(t.jd))
    theirs = t.sidereal_time("mean", longitude=0.0 * u.deg, model="IAU1982").deg
    assert np.max(np.abs(((ours - theirs + 180.0) % 360.0) - 180.0)) < 0.01


def test_every_pass_is_seen_at_least_once(tracks) -> None:  # type: ignore[no-untyped-def]
    """No pass ships that is invisible end to end -- that would be bytes for nothing."""
    assert len(tracks.passes) > 50
    for p in tracks.passes:
        seen = np.isfinite(p.magnitude) & (p.altitude_deg > 0.0)
        assert seen.any(), f"{p.name} pass at {p.start} is never sunlit above the horizon"


def test_arrays_line_up_and_samples_are_evenly_spaced(tracks) -> None:  # type: ignore[no-untyped-def]
    for p in tracks.passes:
        n = len(p.altitude_deg)
        assert len(p.azimuth_deg) == len(p.magnitude) == len(p.range_km) == n
        assert START <= p.start <= END
        assert p.start + timedelta(seconds=p.step_seconds * (n - 1)) <= END + timedelta(seconds=10)
        assert np.all((p.azimuth_deg >= 0.0) & (p.azimuth_deg < 360.0))


def test_consecutive_samples_move_by_at_most_a_few_degrees(tracks) -> None:  # type: ignore[no-untyped-def]
    """The browser interpolates between samples, so they must be close enough to."""
    for p in tracks.passes:
        alt, az = np.radians(p.altitude_deg), np.radians(p.azimuth_deg)
        v = np.stack([np.cos(alt) * np.sin(az), np.cos(alt) * np.cos(az), np.sin(alt)], axis=-1)
        step = np.degrees(np.arccos(np.clip(np.sum(v[1:] * v[:-1], axis=-1), -1.0, 1.0)))
        assert step.max() < 15.0, f"{p.name} jumps {step.max():.1f} deg in one sample"


def test_iss_is_brighter_than_the_default_object(tracks) -> None:  # type: ignore[no-untyped-def]
    iss = [p for p in tracks.passes if p.norad_id == ISS]
    assert iss, "the ISS should pass over Urbana at least once in nine hours"
    brightest = min(float(np.nanmin(p.magnitude)) for p in iss)
    assert brightest < 1.0
    assert brightest < DEFAULT_STANDARD_MAGNITUDE


def test_stale_elements_are_not_propagated(tles: list[tuple[int, str, str, str]]) -> None:
    """Two months from the elements' epoch, SGP4 would draw fiction. Refuse."""
    later = START + timedelta(days=60)
    out = build_satellite_passes(tles, LAT, LON, ELEV, later, later + timedelta(hours=2))
    assert out.n_propagated == 0
    assert out.passes == ()
