"""Deterministic offline weather, for tests and the property suite.

The fixture format is deliberately the same shape a real archive mirror would
hold: a flat list of (published_at, valid_time, value) records, several runs
deep. That is what makes the truncation-equivalence test meaningful -- the
fixture genuinely contains future data that the gate must refuse to return.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from tscheduler.core.clock import AsOf
from tscheduler.providers.base import Confidence, Record
from tscheduler.providers.weather.base import WeatherForecastProvider
from tscheduler.providers.weather.model import WeatherQuery, WeatherSample


class FixtureForecast(WeatherForecastProvider):
    source_id = "fixture:weather"

    def __init__(self, records: Sequence[Record[WeatherSample]]) -> None:
        self._records = tuple(records)

    def _fetch(self, query: WeatherQuery, as_of: AsOf) -> Sequence[Record[WeatherSample]]:
        # Bound by as_of here; Provider.fetch then VERIFIES we did.
        return [
            r
            for r in self._records
            if r.published_at <= as_of.t
            and r.valid_from is not None
            and query.valid_from - timedelta(hours=2)
            <= r.valid_from
            <= query.valid_to + timedelta(hours=2)
        ]

    def coverage_key(self, query: WeatherQuery) -> str:
        return f"{query.lat:.4f},{query.lon:.4f},{query.model}"

    @classmethod
    def from_runs(
        cls,
        runs: Sequence[tuple[datetime, Sequence[tuple[datetime, float]]]],
        *,
        seeing: float = 2.5,
        confidence: Confidence = Confidence.ESTIMATED,
    ) -> FixtureForecast:
        """Build from ``[(published_at, [(valid_time, cloud), ...]), ...]``."""
        out: list[Record[WeatherSample]] = []
        for published, samples in runs:
            for valid, cloud in samples:
                out.append(
                    Record(
                        value=WeatherSample(
                            valid_time=valid, cloud_cover=cloud, seeing_fwhm_arcsec=seeing
                        ),
                        published_at=published,
                        source_id=cls.source_id,
                        record_id=f"{published:%Y%m%dT%H%M}:{valid:%Y%m%dT%H}",
                        valid_from=valid,
                        valid_to=valid + timedelta(hours=1),
                        confidence=confidence,
                    )
                )
        return cls(out)
