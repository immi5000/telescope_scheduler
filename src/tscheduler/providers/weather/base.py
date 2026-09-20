"""Weather forecast provider base."""

from __future__ import annotations

from abc import ABC
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from tscheduler.core.clock import AsOf
from tscheduler.core.timegrid import TimeGrid
from tscheduler.providers.base import EvidenceLedger, Provider, ProviderKind, Record
from tscheduler.providers.weather.model import WeatherQuery, WeatherSample

#: What the plan assumes when no forecast says otherwise.
ASSUMED_SEEING_ARCSEC = 2.5


@dataclass(frozen=True, slots=True)
class WeatherSeries:
    """Everything a forecast says about the night, on the slot grid.

    ``cloud`` and ``seeing`` are always present, because the scheduler cannot
    run without them; with no records they are the clear-sky assumption, and
    ``ledger`` is then empty -- which is how every consumer tells an assumption
    from a forecast. The rest are ``None`` when no record carried them.

    ``covered`` says which slots the forecast actually reaches. Outside it
    (past the last forecast hour, or before the first) ``cloud`` and
    ``seeing`` are the clear-sky assumption too, and every optional field is
    NaN: holding the last forecast hour to the end of the night would report
    a forecast nobody made.
    """

    cloud: NDArray[np.float64]
    seeing: NDArray[np.float64]
    humidity: NDArray[np.float64] | None
    dew_spread_c: NDArray[np.float64] | None
    wind_ms: NDArray[np.float64] | None
    wind_gust_ms: NDArray[np.float64] | None
    ledger: EvidenceLedger
    temperature_c: NDArray[np.float64] | None = None
    dew_point_c: NDArray[np.float64] | None = None
    precipitation_mm: NDArray[np.float64] | None = None
    """mm per hour, placed at the middle of the hour each value sums."""
    cloud_low: NDArray[np.float64] | None = None
    cloud_mid: NDArray[np.float64] | None = None
    cloud_high: NDArray[np.float64] | None = None
    covered: NDArray[np.bool_] | None = None
    """Per slot: True where a forecast record reaches it (see above). ``None``
    only for a series built without it, which reads as covered wherever
    ``has_data``."""

    @property
    def has_data(self) -> bool:
        return bool(self.ledger.refs)


#: Precipitation and wind gusts are both over the hour ENDING at
#: ``valid_time`` -- Open-Meteo's "Preceding hour sum" and "Preceding hour
#: max" -- so they are interpolated from the middle of that hour. Placed at
#: ``valid_time`` like the instantaneous fields, an hour of rain (or a gusty
#: hour) would show half in each neighbouring hour and none of it where it was.
PRECEDING_HOUR_SHIFT_S = -1800.0

#: How far before its first hour and after its last a forecast is taken to
#: reach. An hourly value stands for the hour around it; beyond this the slot
#: is not covered, and the plan assumes a clear sky there.
COVERAGE_SLACK_S = 3600.0


def _interp_optional(
    mids: NDArray[np.float64],
    times: NDArray[np.float64],
    records: Sequence[Record[WeatherSample]],
    get: Callable[[WeatherSample], float | None],
    shift_s: float = 0.0,
) -> NDArray[np.float64] | None:
    """Interpolate a field over only the records that carry it.

    ``shift_s`` moves where each value sits in time (see
    ``PRECEDING_HOUR_SHIFT_S``); it is the same for every record, so the order
    of ``times`` is kept.
    """
    pairs = [(t, v) for t, r in zip(times, records, strict=True) if (v := get(r.value)) is not None]
    if not pairs:
        return None
    xs = np.array([p[0] for p in pairs], dtype=float) + shift_s
    ys = np.array([p[1] for p in pairs], dtype=float)
    out: NDArray[np.float64] = np.interp(mids, xs, ys)
    return out


class WeatherForecastProvider(Provider[WeatherQuery, WeatherSample], ABC):
    kind = ProviderKind.WEATHER_FORECAST

    def weather_series(self, query: WeatherQuery, as_of: AsOf, grid: TimeGrid) -> WeatherSeries:
        """Every forecast field on the slot grid, plus the evidence used.

        ``latest_per(valid_from)`` is the whole trick: of every forecast run that
        had been PUBLISHED by as_of, keep the most recent one for each valid
        hour. Getting this backwards -- filtering on valid time, or keeping the
        oldest run -- is the classic way to silently break replay.
        """
        rs = self.fetch(query, as_of).latest_per(lambda r: r.valid_from)
        ledger = rs.evidence()

        if not rs.records:
            return WeatherSeries(
                cloud=np.zeros(grid.n_slots),
                seeing=np.full(grid.n_slots, ASSUMED_SEEING_ARCSEC),
                humidity=None,
                dew_spread_c=None,
                wind_ms=None,
                wind_gust_ms=None,
                ledger=ledger,
                covered=np.zeros(grid.n_slots, dtype=bool),
            )

        times = np.array([r.value.valid_time.timestamp() for r in rs.records], dtype=float)
        cloud = np.array([r.value.cloud_cover for r in rs.records], dtype=float)
        seeing = np.array([r.value.seeing_fwhm_arcsec for r in rs.records], dtype=float)
        mids = grid.mid_unix()

        order = np.argsort(times)
        t_sorted = times[order]
        recs = [rs.records[int(i)] for i in order]
        # np.interp repeats the end values forever; past the forecast's reach
        # that would be a forecast nobody made.
        covered: NDArray[np.bool_] = (mids >= t_sorted[0] - COVERAGE_SLACK_S) & (
            mids <= t_sorted[-1] + COVERAGE_SLACK_S
        )

        def optional(
            get: Callable[[WeatherSample], float | None], shift_s: float = 0.0
        ) -> NDArray[np.float64] | None:
            v = _interp_optional(mids, t_sorted, recs, get, shift_s)
            if v is None:
                return None
            # A preceding-hour value describes the hour ENDING at its valid
            # time, so nothing at all describes the hour after the last one.
            reach = covered if shift_s == 0.0 else covered & (mids <= t_sorted[-1])
            return np.where(reach, v, np.nan)

        return WeatherSeries(
            cloud=np.where(covered, np.interp(mids, t_sorted, cloud[order]), 0.0),
            seeing=np.where(
                covered, np.interp(mids, t_sorted, seeing[order]), ASSUMED_SEEING_ARCSEC
            ),
            humidity=optional(lambda s: s.humidity),
            dew_spread_c=optional(lambda s: s.dewpoint_spread_c),
            wind_ms=optional(lambda s: s.wind_speed_ms),
            wind_gust_ms=optional(lambda s: s.wind_gusts_ms, PRECEDING_HOUR_SHIFT_S),
            ledger=ledger,
            temperature_c=optional(lambda s: s.temperature_c),
            dew_point_c=optional(lambda s: s.dew_point_c),
            precipitation_mm=optional(lambda s: s.precipitation_mm, PRECEDING_HOUR_SHIFT_S),
            cloud_low=optional(lambda s: s.cloud_low),
            cloud_mid=optional(lambda s: s.cloud_mid),
            cloud_high=optional(lambda s: s.cloud_high),
            covered=covered,
        )

    def series(
        self, query: WeatherQuery, as_of: AsOf, grid: TimeGrid
    ) -> tuple[NDArray[np.float64], NDArray[np.float64], EvidenceLedger]:
        """Cloud cover and seeing on the slot grid, plus the evidence used.

        The original narrow form, kept for its callers; see ``weather_series``.
        """
        s = self.weather_series(query, as_of, grid)
        return s.cloud, s.seeing, s.ledger
