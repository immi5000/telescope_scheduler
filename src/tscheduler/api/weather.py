"""Offline weather with a real publication history.

A demo that serves one constant forecast cannot show the thing this project
exists to show. What makes the plan move is not that the weather is bad -- it
is that *the forecast changed after the plan was made*. So the synthetic source
emits several model runs with genuine ``published_at`` times and a genuine
error structure: a run issued 18 hours out mostly reproduces climatology, and
each later run resolves more of the front that actually arrives.

That means the early plan is wrong in the way early plans are really wrong --
too optimistic about the second half of the night -- and the re-plan at 03:00
is a response to information that did not exist at 01:00. Nothing here is
random: the same request always produces the same runs, which is what lets the
replay fold be compared across sessions.

It is synthetic, and it is labelled synthetic everywhere it surfaces. Pass
``weather=open_meteo`` for archived real runs.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from tscheduler.core.timegrid import TimeGrid
from tscheduler.providers.weather.fixture import FixtureForecast

#: Hours before the night starts at which each model run is issued. Loosely the
#: cadence of a global model with a 6-hourly cycle plus a short-range update.
RUN_OFFSETS_H: tuple[float, ...] = (-18.0, -12.0, -6.0, -1.0, 2.0, 4.0, 6.0)

#: Lead time over which a run's skill decays toward climatology, hours.
SKILL_TAU_H = 9.0

CLIMATOLOGY = 0.38


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def truth_cloud(
    hours_into_night: float,
    *,
    night_hours: float,
    clear_start: float = 0.08,
    overcast_end: float = 0.88,
    front_at: float = 0.58,
    front_width_h: float = 1.1,
) -> float:
    """The cloud cover that actually materialises: clear, then a front."""
    x = (hours_into_night - front_at * night_hours) / front_width_h
    return clear_start + (overcast_end - clear_start) * _sigmoid(x)


def synthetic_forecast(grid: TimeGrid, *, seed: int = 0) -> FixtureForecast:
    """Model runs whose skill improves with shortening lead time."""
    night_hours = (grid.end - grid.start).total_seconds() / 3600.0
    # Forecasts cover an hour either side of the window so interpolation at the
    # edges has something to bite on.
    valid_hours = [grid.start + timedelta(hours=h) for h in range(-1, math.ceil(night_hours) + 2)]

    runs: list[tuple[datetime, list[tuple[datetime, float]]]] = []
    for k, off in enumerate(RUN_OFFSETS_H):
        published = grid.start + timedelta(hours=off)
        samples: list[tuple[datetime, float]] = []
        for valid in valid_hours:
            lead_h = (valid - published).total_seconds() / 3600.0
            if lead_h < -1.0:
                continue  # a run does not forecast the past
            skill = math.exp(-max(lead_h, 0.0) / SKILL_TAU_H)
            hours_in = (valid - grid.start).total_seconds() / 3600.0
            actual = truth_cloud(hours_in, night_hours=night_hours)
            # A deterministic run-to-run wobble, so consecutive runs disagree
            # slightly even where they agree about the front.
            wobble = 0.05 * math.sin(2.7 * hours_in + 1.9 * k + 0.31 * seed)
            value = skill * actual + (1.0 - skill) * CLIMATOLOGY + wobble
            samples.append((valid, min(max(value, 0.0), 1.0)))
        if samples:
            runs.append((published, samples))

    # Seeing degrades as the front arrives -- the two are physically linked, and
    # a constant 2.5" would make the second half of the night look better than
    # it is.
    return FixtureForecast.from_runs(runs, seeing=2.6)
