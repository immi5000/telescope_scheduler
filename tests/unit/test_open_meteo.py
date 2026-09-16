"""Open-Meteo providers.

Network tests are marked and deselected by default (``-m "not network"``), so
the ordinary suite stays hermetic and fast. They exist because the one thing
that cannot be verified offline is whether the upstream contract still holds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tscheduler.core.clock import AsOf, Vantage
from tscheduler.core.timegrid import TimeGrid
from tscheduler.providers.weather.model import WeatherQuery
from tscheduler.providers.weather.open_meteo import (
    FALLBACK_LAG_H,
    LAG_SAFETY_MARGIN_H,
    OpenMeteoLiveForecast,
    OpenMeteoReplayForecast,
    measure_dissemination_lag,
)

NIGHT = datetime(2026, 9, 13, 1, 0, tzinfo=UTC)


def _query() -> WeatherQuery:
    return WeatherQuery(
        lat=40.1164, lon=-88.2434, valid_from=NIGHT, valid_to=NIGHT + timedelta(hours=8)
    )


def test_live_provider_refuses_a_replay_as_of() -> None:
    """It has no historical runs, so answering a past as_of would silently serve
    today's forecast for last Tuesday -- a leak the gate cannot catch, because
    the record would be internally consistent."""
    with pytest.raises(ValueError, match="cannot answer a replay"):
        OpenMeteoLiveForecast()._fetch(_query(), AsOf.at(NIGHT))


def test_lag_falls_back_generously_when_metadata_is_unreachable() -> None:
    """A scheduler that refuses to run without a metadata endpoint is worse than
    one that is slightly pessimistic about publication times."""
    lag = measure_dissemination_lag("definitely_not_a_model")
    assert lag == timedelta(hours=8.0)


def test_run_enumeration_covers_the_night_at_the_model_cadence() -> None:
    p = OpenMeteoReplayForecast(lag=timedelta(hours=7))
    runs = p._runs_covering(_query(), "ecmwf_ifs025")
    assert len(runs) >= 8
    assert all(r.hour % 6 == 0 for r in runs), "ECMWF runs are 6-hourly"
    assert runs == sorted(runs)


def test_coverage_key_excludes_as_of() -> None:
    """The disk cache is a mirror of the upstream archive; the as-of filter is
    applied downstream on every read. Keying the cache by as_of would let a
    later request populate an entry that an earlier one then reads -- the exact
    leak the system exists to prevent, introduced by the cache itself."""
    p = OpenMeteoReplayForecast(lag=timedelta(hours=7))
    k = p.coverage_key(_query())
    assert "as_of" not in k
    assert p.coverage_key(_query()) == k


@pytest.mark.network
def test_measured_lag_is_plausible_for_ecmwf() -> None:
    lag = measure_dissemination_lag("ecmwf_ifs025")
    hours = lag.total_seconds() / 3600.0
    assert 4.0 < hours < 12.0, f"implausible measured lag {hours:.2f} h"
    assert hours != FALLBACK_LAG_H["ecmwf_ifs025"] + LAG_SAFETY_MARGIN_H, "fell back silently"


@pytest.mark.network
def test_replay_returns_multiple_distinct_runs_for_a_past_night() -> None:
    p = OpenMeteoReplayForecast()
    recs = p._load_all_runs(_query())
    assert recs, "no historical runs returned"
    pubs = {r.published_at for r in recs}
    assert len(pubs) >= 4, f"expected several runs, got {len(pubs)}"


@pytest.mark.network
def test_replay_never_returns_records_published_after_as_of() -> None:
    """The gate, on real data rather than fixtures."""
    p = OpenMeteoReplayForecast()
    grid = TimeGrid.from_window(NIGHT, NIGHT + timedelta(hours=8))
    for frac in (0.0, 0.5, 1.0):
        t = NIGHT + timedelta(hours=8 * frac)
        _, _, ledger = p.series(_query(), AsOf.at(t), grid)
        assert ledger.max_published is None or ledger.max_published <= t


@pytest.mark.network
def test_the_forecast_actually_evolves_across_a_real_night() -> None:
    """Negative control for replay on real data: if successive runs agreed, the
    no-lookahead machinery would be guarding nothing observable."""
    import numpy as np

    p = OpenMeteoReplayForecast()
    grid = TimeGrid.from_window(NIGHT, NIGHT + timedelta(hours=8))
    early, _, _ = p.series(_query(), AsOf.at(NIGHT), grid)
    late, _, _ = p.series(_query(), AsOf.at(NIGHT + timedelta(hours=8)), grid)
    assert not np.allclose(early, late), "forecast identical across the night"


@pytest.mark.network
def test_live_provider_parses_a_real_response() -> None:
    p = OpenMeteoLiveForecast()
    recs = p.fetch(_query(), AsOf(datetime.now(UTC), Vantage.LIVE))
    assert len(recs) > 24
    for r in recs.records[:5]:
        assert 0.0 <= r.value.cloud_cover <= 1.0
        assert r.value.valid_time.tzinfo is not None
