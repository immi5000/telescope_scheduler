"""Open-Meteo weather: live forecasts, and historical runs for replay.

**Replay uses the Single Runs API, not the Previous Runs API**, and the
difference is the whole no-lookahead guarantee:

* Single Runs (``single-runs-api.open-meteo.com``) is addressed by model
  INITIALISATION time via ``&run=``. That timestamp is exactly what
  ``published_at`` needs to be derived from.
* Previous Runs indexes by LEAD time (``_previous_dayN`` suffixes) and never
  exposes an initialisation timestamp. Without it there is no defensible
  publication time, and replay becomes a guess wearing a lab coat. It is fine
  for a "how the forecast drifted" UI panel and nothing else.

The dissemination lag is MEASURED, not assumed. ``meta.json`` for each model
publishes ``last_run_initialisation_time`` and ``last_run_availability_time``;
for ECMWF IFS025 the observed gap is 7.12 h, which matches the documented
schedule. We round it UP to a whole hour and add a safety margin, because being
pessimistic about when data arrived understates the adaptive scheduler's
advantage -- the safe direction to be wrong.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx

from tscheduler.core.clock import AsOf, Vantage
from tscheduler.providers.base import Confidence, Record
from tscheduler.providers.weather.base import WeatherForecastProvider
from tscheduler.providers.weather.model import WeatherQuery, WeatherSample

FORECAST_URL: Final = "https://api.open-meteo.com/v1/forecast"
SINGLE_RUNS_URL: Final = "https://single-runs-api.open-meteo.com/v1/forecast"
META_URL: Final = "https://api.open-meteo.com/data/{model}/static/meta.json"

HOURLY_VARS: Final = ",".join(
    [
        "cloud_cover",
        "cloud_cover_low",
        "cloud_cover_mid",
        "cloud_cover_high",
        "relative_humidity_2m",
        "dew_point_2m",
        "temperature_2m",
        "wind_speed_10m",
    ]
)

#: Fallback lags (hours) if meta.json is unreachable. Deliberately generous.
FALLBACK_LAG_H: Final[dict[str, float]] = {
    "ecmwf_ifs025": 8.0,
    "gfs_seamless": 5.0,
    "icon_seamless": 4.0,
}

#: Extra margin added to any measured lag, in hours. Rounding late is safe;
#: rounding early manufactures an advantage for the adaptive arm.
LAG_SAFETY_MARGIN_H: Final = 0.5

#: Model runs are issued every this many hours.
RUN_INTERVAL_H: Final[dict[str, int]] = {
    "ecmwf_ifs025": 6,
    "gfs_seamless": 6,
    "icon_seamless": 6,
}


def measure_dissemination_lag(model: str, client: httpx.Client | None = None) -> timedelta:
    """Measured (init -> available) gap for a model, rounded conservatively up.

    Falls back to a generous constant if meta.json cannot be read, because a
    scheduler that refuses to run without a metadata endpoint is worse than one
    that is slightly pessimistic about publication times.
    """
    owns = client is None
    c = client or httpx.Client(timeout=10.0)
    try:
        r = c.get(META_URL.format(model=model))
        r.raise_for_status()
        meta: dict[str, Any] = r.json()
        init = float(meta["last_run_initialisation_time"])
        avail = float(meta["last_run_availability_time"])
        hours = (avail - init) / 3600.0
        if not 0.0 < hours < 24.0:
            raise ValueError(f"implausible lag {hours:.2f} h")
        return timedelta(hours=hours + LAG_SAFETY_MARGIN_H)
    except Exception:
        return timedelta(hours=FALLBACK_LAG_H.get(model, 8.0))
    finally:
        if owns:
            c.close()


def _parse_hourly(payload: dict[str, Any]) -> list[WeatherSample]:
    h = payload.get("hourly") or {}
    times = h.get("time") or []
    out: list[WeatherSample] = []

    def col(name: str, default: float) -> list[float]:
        v = h.get(name)
        if not v:
            return [default] * len(times)
        return [default if x is None else float(x) for x in v]

    total = col("cloud_cover", 0.0)
    low = col("cloud_cover_low", 0.0)
    mid = col("cloud_cover_mid", 0.0)
    high = col("cloud_cover_high", 0.0)
    rh = col("relative_humidity_2m", 50.0)
    dew = col("dew_point_2m", 0.0)
    temp = col("temperature_2m", 10.0)
    wind = col("wind_speed_10m", 2.0)

    for i, t in enumerate(times):
        ts = datetime.fromisoformat(t).replace(tzinfo=UTC)
        out.append(
            WeatherSample(
                valid_time=ts,
                cloud_cover=total[i] / 100.0,
                cloud_low=low[i] / 100.0,
                cloud_mid=mid[i] / 100.0,
                cloud_high=high[i] / 100.0,
                humidity=rh[i] / 100.0,
                dewpoint_spread_c=temp[i] - dew[i],
                wind_speed_ms=wind[i],
            )
        )
    return out


class OpenMeteoLiveForecast(WeatherForecastProvider):
    """Live mode. ``published_at`` is the retrieval time.

    That is not laziness: we genuinely cannot know a forecast earlier than we
    fetched it, and claiming an earlier publication time would be the one lie
    that leaks. In LIVE vantage it cannot cause a lookahead anyway, since the
    future has not happened.
    """

    source_id = "open_meteo:live"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=20.0)

    def _fetch(self, query: WeatherQuery, as_of: AsOf) -> Sequence[Record[WeatherSample]]:
        if as_of.vantage is Vantage.REPLAY:
            raise ValueError(
                "OpenMeteoLiveForecast cannot answer a replay as_of; it has no "
                "historical runs. Use OpenMeteoReplayForecast."
            )
        r = self._client.get(
            FORECAST_URL,
            params={
                "latitude": query.lat,
                "longitude": query.lon,
                "hourly": HOURLY_VARS,
                "models": query.model,
                "timezone": "UTC",
                "forecast_days": 3,
            },
        )
        r.raise_for_status()
        return [
            Record(
                value=s,
                published_at=as_of.t,
                source_id=self.source_id,
                record_id=f"{query.model}:live:{s.valid_time:%Y%m%dT%H}",
                valid_from=s.valid_time,
                valid_to=s.valid_time + timedelta(hours=1),
                confidence=Confidence.EXACT,
            )
            for s in _parse_hourly(r.json())
        ]

    def coverage_key(self, query: WeatherQuery) -> str:
        return f"{query.lat:.4f},{query.lon:.4f},{query.model},live"


class OpenMeteoReplayForecast(WeatherForecastProvider):
    """Replay mode. Fetches each model run that covers the night.

    Every run is requested once; ``_fetch`` then filters to those published by
    ``as_of``, so scrubbing a slider through the night costs zero extra HTTP.
    """

    source_id = "open_meteo:single_runs"

    def __init__(
        self,
        client: httpx.Client | None = None,
        lag: timedelta | None = None,
        lookback_hours: int = 72,
    ) -> None:
        self._client = client or httpx.Client(timeout=30.0)
        self._lag = lag
        self._lookback_hours = lookback_hours
        self._cache: dict[str, list[Record[WeatherSample]]] = {}

    def _lag_for(self, model: str) -> timedelta:
        if self._lag is None:
            self._lag = measure_dissemination_lag(model, self._client)
        return self._lag

    def _runs_covering(self, query: WeatherQuery, model: str) -> list[datetime]:
        step = RUN_INTERVAL_H.get(model, 6)
        latest = query.valid_to.replace(minute=0, second=0, microsecond=0)
        latest -= timedelta(hours=latest.hour % step)
        earliest = latest - timedelta(hours=self._lookback_hours)
        out, t = [], earliest
        while t <= latest:
            out.append(t)
            t += timedelta(hours=step)
        return out

    def _load_all_runs(self, query: WeatherQuery) -> list[Record[WeatherSample]]:
        key = self.coverage_key(query)
        if key in self._cache:
            return self._cache[key]

        lag = self._lag_for(query.model)
        records: list[Record[WeatherSample]] = []
        for run in self._runs_covering(query, query.model):
            try:
                r = self._client.get(
                    SINGLE_RUNS_URL,
                    params={
                        "latitude": query.lat,
                        "longitude": query.lon,
                        "hourly": HOURLY_VARS,
                        "models": query.model,
                        "run": run.strftime("%Y-%m-%dT%H:%M"),
                        "timezone": "UTC",
                    },
                )
                if r.status_code != 200:
                    continue
                payload = r.json()
                if payload.get("error"):
                    continue
            except httpx.HTTPError:
                continue

            published = run + lag
            for s in _parse_hourly(payload):
                if not (
                    query.valid_from - timedelta(hours=2)
                    <= s.valid_time
                    <= query.valid_to + timedelta(hours=2)
                ):
                    continue
                records.append(
                    Record(
                        value=s,
                        published_at=published,
                        source_id=self.source_id,
                        record_id=f"{query.model}:{run:%Y%m%dT%H}:{s.valid_time:%Y%m%dT%H}",
                        valid_from=s.valid_time,
                        valid_to=s.valid_time + timedelta(hours=1),
                        # ESTIMATED, not EXACT: the lag is measured from the
                        # CURRENT run and assumed stable for past ones.
                        confidence=Confidence.ESTIMATED,
                    )
                )
        self._cache[key] = records
        return records

    def _fetch(self, query: WeatherQuery, as_of: AsOf) -> Sequence[Record[WeatherSample]]:
        # Bound by as_of here; Provider.fetch then VERIFIES we did.
        return [r for r in self._load_all_runs(query) if r.published_at <= as_of.t]

    def coverage_key(self, query: WeatherQuery) -> str:
        return (
            f"{query.lat:.4f},{query.lon:.4f},{query.model},"
            f"{query.valid_from:%Y%m%d},{query.valid_to:%Y%m%d}"
        )
