"""The no-lookahead property: the centrepiece of the whole test suite.

The system's load-bearing claim is that a plan computed at as_of = T was
influenced only by data published at or before T. Every other test checks a
MECHANISM. This one checks the OUTCOME, end to end, and would catch a leak
through a path nobody anticipated -- including one introduced by a future
refactor that bypasses the provider gate entirely.

The method needs no instrumentation: plan the same night twice, once against a
world that physically contains post-T data and once against a world truncated to
T, and demand byte-identical results. If any post-T byte reaches the plan, the
fingerprints diverge.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tscheduler.core.clock import AsOf
from tscheduler.core.timegrid import TimeGrid
from tscheduler.domain.equipment import Camera, Mount, Optics
from tscheduler.domain.site import Site
from tscheduler.domain.targets import Target
from tscheduler.physics.convert import bortle_to_artificial_nl
from tscheduler.pipeline.builder import SessionSpec, build_geometry, build_scheduler_input
from tscheduler.providers.base import Record
from tscheduler.providers.weather.fixture import FixtureForecast
from tscheduler.providers.weather.model import WeatherSample
from tscheduler.scheduling.cpsat import SolveOptions, build_and_solve

NIGHT_START = datetime(2026, 9, 13, 1, 0, tzinfo=UTC)
NIGHT_HOURS = 8

SITE = Site(
    latitude_deg=40.1164,
    longitude_deg=-88.2434,
    elevation_m=227.0,
    artificial_zenith_nl=bortle_to_artificial_nl(5),
)
OPTICS = Optics(aperture_mm=203.0, focal_length_mm=2032.0, central_obstruction_mm=68.0)
CAMERA = Camera(
    pixel_size_um=3.76,
    sensor_width_px=6248,
    sensor_height_px=4176,
    quantum_efficiency=0.75,
    read_noise_e=1.5,
    dark_current_e_per_s=0.002,
)

CATALOG = [
    ("m31", "M31", 10.6847, 41.2690, 18.4),
    ("m27", "M27", 299.9015, 22.7211, 18.4),
    ("m57", "M57", 283.3963, 33.0292, 19.0),
    ("n7000", "NGC7000", 314.75, 44.37, 18.0),
    ("m13", "M13", 250.4235, 36.4613, 18.8),
    ("n6946", "NGC6946", 308.718, 60.1539, 19.1),
]

# Deterministic solving is a PREREQUISITE for this property, not an optimisation:
# CP-SAT with a wall-clock limit and workers > 1 returns different equal-value
# optima run to run, which would make the test flaky and impossible to debug.
DET = SolveOptions(deterministic=True, max_deterministic_time=3.0, random_seed=1)


def _spec(n_targets: int) -> SessionSpec:
    targets = tuple(Target(i, n, ra, dec, mag) for i, n, ra, dec, mag in CATALOG[:n_targets])
    return SessionSpec(
        site=SITE,
        grid=TimeGrid.from_window(NIGHT_START, NIGHT_START + timedelta(hours=NIGHT_HOURS)),
        optics=OPTICS,
        camera=CAMERA,
        mount=Mount(),
        targets=targets,
        snr_goal=60.0,
    )


def _runs(seed: int, n_runs: int):
    """Forecast runs issued every 3 h, each covering the whole night.

    Successive runs genuinely DISAGREE -- that is the point. If later runs said
    the same thing as earlier ones, truncation would be a no-op and the test
    would prove nothing.
    """
    rng = np.random.default_rng(seed)
    out = []
    for k in range(n_runs):
        published = NIGHT_START - timedelta(hours=9) + timedelta(hours=3 * k)
        samples = [
            (NIGHT_START + timedelta(hours=h), float(rng.uniform(0.0, 0.95)))
            for h in range(NIGHT_HOURS + 2)
        ]
        out.append((published, samples))
    return out


def _plan_at(spec, geo, provider, t: datetime):
    inp, ledger = build_scheduler_input(spec, geo, provider, AsOf.at(t))
    return build_and_solve(inp, AsOf.at(t), DET, ledger)


@pytest.mark.slow
@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    seed=st.integers(min_value=0, max_value=500),
    n_targets=st.integers(min_value=3, max_value=6),
    n_runs=st.integers(min_value=3, max_value=6),
    t_frac=st.floats(min_value=0.05, max_value=0.95),
)
def test_truncation_equivalence(seed: int, n_targets: int, n_runs: int, t_frac: float) -> None:
    """The plan at T is byte-identical whether or not post-T data exists on disk."""
    spec = _spec(n_targets)
    geo = build_geometry(spec)
    runs = _runs(seed, n_runs)

    span = (spec.grid.end - spec.grid.start).total_seconds()
    t = spec.grid.start + timedelta(seconds=span * t_frac)

    full = FixtureForecast.from_runs(runs)
    truncated = FixtureForecast.from_runs([(p, s) for p, s in runs if p <= t])

    a = _plan_at(spec, geo, full, t)
    b = _plan_at(spec, geo, truncated, t)

    assert a.fingerprint() == b.fingerprint(), (
        "plan changed when post-T data was merely PRESENT on disk -- a leak"
    )


@pytest.mark.slow
@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(seed=st.integers(min_value=0, max_value=500), t_frac=st.floats(0.1, 0.9))
def test_poison_record_after_as_of_is_inert(seed: int, t_frac: float) -> None:
    """Sharper than truncation: an EXTREME post-T record must change nothing.

    If the gate leaks, a total-whiteout forecast published after T would visibly
    wreck the plan, so this localises the failure to a single record.
    """
    spec = _spec(4)
    geo = build_geometry(spec)
    runs = _runs(seed, 4)
    span = (spec.grid.end - spec.grid.start).total_seconds()
    t = spec.grid.start + timedelta(seconds=span * t_frac)

    poison = (
        t + timedelta(minutes=1),
        [(NIGHT_START + timedelta(hours=h), 1.0) for h in range(NIGHT_HOURS + 2)],
    )

    clean = _plan_at(spec, geo, FixtureForecast.from_runs(runs), t)
    spiked = _plan_at(spec, geo, FixtureForecast.from_runs([*runs, poison]), t)

    assert clean.fingerprint() == spiked.fingerprint()


@pytest.mark.slow
@settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(seed=st.integers(min_value=0, max_value=500))
def test_a_leak_would_actually_be_detected(seed: int) -> None:
    """Negative control -- proves the test above has teeth.

    A suite that can't fail is worthless. Here we deliberately plan at a LATER
    as_of and require the answer to differ, confirming that later forecast runs
    really do change the plan and that truncation equivalence is a non-trivial
    statement rather than an artefact of an insensitive fingerprint.
    """
    spec = _spec(5)
    geo = build_geometry(spec)
    runs = _runs(seed, 6)
    provider = FixtureForecast.from_runs(runs)

    early = _plan_at(spec, geo, provider, spec.grid.start)
    late = _plan_at(spec, geo, provider, spec.grid.start + timedelta(hours=NIGHT_HOURS - 1))

    assert early.fingerprint() != late.fingerprint() or early.included != late.included, (
        "planning at two very different as_of instants produced identical plans; "
        "the fixture forecasts are not varied enough for the property test to mean anything"
    )


def test_provider_gate_raises_on_a_non_compliant_provider() -> None:
    """Direct check of enforcement layer 2, independent of the property tests."""
    from tscheduler.core.clock import LookaheadError

    class Leaky(FixtureForecast):
        source_id = "fixture:leaky"

        def _fetch(self, query, as_of):
            return self._records

    future = NIGHT_START + timedelta(hours=99)
    rec = Record(
        value=WeatherSample(valid_time=NIGHT_START, cloud_cover=0.5),
        published_at=future,
        source_id="fixture:leaky",
        record_id="bad",
        valid_from=NIGHT_START,
    )
    spec = _spec(3)
    geo = build_geometry(spec)
    with pytest.raises(LookaheadError, match="published after"):
        build_scheduler_input(spec, geo, Leaky([rec]), AsOf.at(NIGHT_START))
