"""Weather forecast provider base."""

from __future__ import annotations

from abc import ABC

import numpy as np
from numpy.typing import NDArray

from tscheduler.core.clock import AsOf
from tscheduler.core.timegrid import TimeGrid
from tscheduler.providers.base import EvidenceLedger, Provider, ProviderKind
from tscheduler.providers.weather.model import WeatherQuery, WeatherSample


class WeatherForecastProvider(Provider[WeatherQuery, WeatherSample], ABC):
    kind = ProviderKind.WEATHER_FORECAST

    def series(
        self, query: WeatherQuery, as_of: AsOf, grid: TimeGrid
    ) -> tuple[NDArray[np.float64], NDArray[np.float64], EvidenceLedger]:
        """Cloud cover and seeing on the slot grid, plus the evidence used.

        ``latest_per(valid_from)`` is the whole trick: of every forecast run that
        had been PUBLISHED by as_of, keep the most recent one for each valid
        hour. Getting this backwards -- filtering on valid time, or keeping the
        oldest run -- is the classic way to silently break replay.
        """
        rs = self.fetch(query, as_of).latest_per(lambda r: r.valid_from)
        ledger = rs.evidence()

        if not rs.records:
            return (
                np.zeros(grid.n_slots),
                np.full(grid.n_slots, 2.5),
                ledger,
            )

        times = np.array([r.value.valid_time.timestamp() for r in rs.records], dtype=float)
        cloud = np.array([r.value.cloud_cover for r in rs.records], dtype=float)
        seeing = np.array([r.value.seeing_fwhm_arcsec for r in rs.records], dtype=float)
        mids = grid.mid_unix()

        order = np.argsort(times)
        return (
            np.interp(mids, times[order], cloud[order]),
            np.interp(mids, times[order], seeing[order]),
            ledger,
        )
