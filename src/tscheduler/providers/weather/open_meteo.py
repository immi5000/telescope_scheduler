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
for ECMWF IFS025 the observed gap has been between about 7 and 9 h depending on
the run, in line with the documented dissemination schedule plus Open-Meteo's
processing. The newest run's gap is applied to every run, plus a safety
margin, rounded UP to the quarter hour, and never below a per-model floor
(``LAG_FLOOR_H``), because a fast 06z/18z measurement must not be applied to
a slower 00z/12z run: being pessimistic about when data
arrived understates the adaptive scheduler's advantage -- the safe direction
to be wrong. (Rounding also keeps the lag, and so every replayed decision
point, stable from one session to the next while the measured gap wobbles by
minutes.)

Units: Open-Meteo reports wind in km/h unless told otherwise. Every request
here sends ``wind_speed_unit=ms``, because ``WeatherSample`` is in m/s and a
silent factor of 3.6 would turn a breeze into a gale warning.

Rate limits: the service is free and rate-limited. Archived single runs never
change once published, so they are cached for the life of the process
(``_RUN_CACHE``) and shared by every session at the same site. A failed
response is never cached.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
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

LIVE_SOURCE_ID: Final = "open_meteo:live"
RUNS_SOURCE_ID: Final = "open_meteo:single_runs"

#: Ten variables, deliberately: Open-Meteo counts a request with MORE than ten
#: as several calls against the rate limit, so an eleventh doubles the cost.
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
        "wind_gusts_10m",
        "precipitation",
    ]
)

#: Sent with every request. ``wind_speed_unit`` is the load-bearing one; the
#: temperature and precipitation units are the service defaults, pinned so a
#: change of default cannot silently rescale them.
COMMON_PARAMS: Final[dict[str, str]] = {
    "timezone": "UTC",
    "wind_speed_unit": "ms",
    "temperature_unit": "celsius",
    "precipitation_unit": "mm",
}

#: Human names, for the sentences the UI shows.
MODEL_NAMES: Final[dict[str, str]] = {
    "ecmwf_ifs025": "ECMWF IFS 0.25°",
    "gfs_seamless": "NOAA GFS",
    "icon_seamless": "DWD ICON",
}

#: Fallback lags (hours) if meta.json is unreachable. Deliberately generous,
#: and never below the model's floor (``LAG_FLOOR_H``).
FALLBACK_LAG_H: Final[dict[str, float]] = {
    "ecmwf_ifs025": 9.0,
    "gfs_seamless": 5.0,
    "icon_seamless": 4.0,
}

#: Extra margin added to any measured lag, in hours. Rounding late is safe;
#: rounding early manufactures an advantage for the adaptive arm.
LAG_SAFETY_MARGIN_H: Final = 0.5

#: The shortest lag ever applied to a model's runs, in hours, however quickly
#: meta.json says its newest run arrived. meta.json measures only the NEWEST
#: run, and one lag is applied to every run: for ECMWF IFS 0.25 the short
#: 06z/18z runs arrive faster than the long 00z/12z ones (18 Sep 2026: 06z
#: available after 7.16 h, 12z after 8.39 h, and the 12z run was still missing
#: from Single Runs at 7.5 h). A 06z measurement applied to a 00z run would show
#: that run to replayed plans before it existed. 9.0 = the observed 8.39 h plus
#: the 0.5 h margin, rounded up. A measured lag above the floor is used as is.
LAG_FLOOR_H: Final[dict[str, float]] = {
    "ecmwf_ifs025": 9.0,
}

#: Model runs are issued every this many hours.
RUN_INTERVAL_H: Final[dict[str, int]] = {
    "ecmwf_ifs025": 6,
    "gfs_seamless": 6,
    "icon_seamless": 6,
}

#: Initialisation hours (UTC) that exist in the Single Runs archive. A request
#: for any other hour is a wasted call against the rate limit.
RUN_HOURS: Final[dict[str, tuple[int, ...]]] = {
    "ecmwf_ifs025": (0, 6, 12, 18),
    "gfs_seamless": (0, 6, 12, 18),
    "icon_seamless": (0, 6, 12, 18),
}

#: Samples kept either side of the queried window, so interpolation at the
#: edges of the night has something to bite on.
WINDOW_PAD: Final = timedelta(hours=2)


def model_name(model: str) -> str:
    return MODEL_NAMES.get(model, model)


def default_client() -> httpx.Client:
    """Modest timeouts: a fold would rather degrade than hang on one request."""
    return httpx.Client(
        timeout=httpx.Timeout(12.0, connect=5.0),
        headers={"User-Agent": "tscheduler (telescope night scheduler)"},
    )


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class OpenMeteoError(Exception):
    """A request that did not yield data, classified for the caller.

    ``kind`` decides what happens next: ``rate_limited`` and ``unreachable``
    stop every further request in the session (hammering a service that just
    said no, or waiting out timeouts one by one, helps nobody); ``http`` and
    ``bad_payload`` only lose the one response.
    """

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail

    @property
    def halts(self) -> bool:
        return self.kind in ("rate_limited", "unreachable")


def get_json(client: httpx.Client, url: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET and decode, turning every failure into an ``OpenMeteoError``."""
    try:
        r = client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise OpenMeteoError("unreachable", type(exc).__name__) from exc
    if r.status_code == 429:
        raise OpenMeteoError("rate_limited", "HTTP 429")
    try:
        payload = r.json()
    except ValueError:
        payload = None
    if r.status_code != 200:
        reason = payload.get("reason") if isinstance(payload, dict) else None
        detail = f"HTTP {r.status_code}" + (f": {reason}" if reason else "")
        raise OpenMeteoError("http", detail)
    if not isinstance(payload, dict):
        raise OpenMeteoError("bad_payload", "response was not a JSON object")
    if payload.get("error"):
        raise OpenMeteoError("http", str(payload.get("reason") or "error in response"))
    return payload


# --------------------------------------------------------------------------
# model metadata
# --------------------------------------------------------------------------


def _ceil_quarter_hour(hours: float) -> timedelta:
    return timedelta(minutes=15 * math.ceil(hours * 4.0 - 1e-9))


@dataclass(frozen=True, slots=True)
class ModelMeta:
    """What ``meta.json`` says about the newest run of a model."""

    init: datetime
    """Initialisation time of the newest run the API serves."""
    available: datetime
    """When that run became available through the API."""
    data_end: datetime | None
    """The last valid time the newest run reaches: the forecast horizon."""

    @property
    def lag(self) -> timedelta:
        """Measured dissemination lag plus the margin, rounded up."""
        hours = (self.available - self.init).total_seconds() / 3600.0
        return _ceil_quarter_hour(hours + LAG_SAFETY_MARGIN_H)


def fetch_meta(model: str, client: httpx.Client) -> ModelMeta:
    """Read ``meta.json``. Raises ``OpenMeteoError`` or ``ValueError``."""
    meta = get_json(client, META_URL.format(model=model), {})
    init = datetime.fromtimestamp(float(meta["last_run_initialisation_time"]), UTC)
    avail = datetime.fromtimestamp(float(meta["last_run_availability_time"]), UTC)
    hours = (avail - init).total_seconds() / 3600.0
    if not 0.0 < hours < 24.0:
        raise ValueError(f"implausible lag {hours:.2f} h")
    end_raw = meta.get("data_end_time")
    data_end = datetime.fromtimestamp(float(end_raw), UTC) if end_raw else None
    return ModelMeta(init=init, available=avail, data_end=data_end)


def lag_floor(model: str) -> timedelta:
    """The shortest lag any run of ``model`` is given (see ``LAG_FLOOR_H``)."""
    return timedelta(hours=LAG_FLOOR_H.get(model, 0.0))


def fallback_lag(model: str) -> timedelta:
    return max(timedelta(hours=FALLBACK_LAG_H.get(model, 8.0)), lag_floor(model))


def conservative_lag(model: str, measured: timedelta | None) -> timedelta:
    """The lag to apply to every run: the measured one (or the fallback when
    meta.json could not be read), never below the model's floor."""
    return max(measured if measured is not None else fallback_lag(model), lag_floor(model))


def measure_dissemination_lag(model: str, client: httpx.Client | None = None) -> timedelta:
    """Measured (init -> available) gap for a model, rounded conservatively up
    and never below the model's floor.

    Falls back to a generous constant if meta.json cannot be read, because a
    scheduler that refuses to run without a metadata endpoint is worse than one
    that is slightly pessimistic about publication times.
    """
    owns = client is None
    c = client or default_client()
    try:
        return conservative_lag(model, fetch_meta(model, c).lag)
    except Exception:
        return fallback_lag(model)
    finally:
        if owns:
            c.close()


# --------------------------------------------------------------------------
# parsing and the shared run cache
# --------------------------------------------------------------------------


def _parse_hourly(payload: dict[str, Any]) -> list[WeatherSample]:
    """Hourly rows to samples. A row with no cloud cover is DROPPED.

    Past the end of a run Open-Meteo pads with nulls. Reading a null cloud as
    0.0 would forecast a clear sky out of missing data -- the one silent error
    this whole path must never make -- so such a row is not a sample at all,
    and an older run (or the honest "no forecast") fills the hour instead.
    """
    h = payload.get("hourly") or {}
    times = h.get("time") or []

    def col(name: str) -> list[float | None]:
        v = h.get(name)
        if not v or len(v) != len(times):
            return [None] * len(times)
        return [None if x is None else float(x) for x in v]

    total = col("cloud_cover")
    low = col("cloud_cover_low")
    mid = col("cloud_cover_mid")
    high = col("cloud_cover_high")
    rh = col("relative_humidity_2m")
    dew = col("dew_point_2m")
    temp = col("temperature_2m")
    wind = col("wind_speed_10m")
    gust = col("wind_gusts_10m")
    precip = col("precipitation")

    out: list[WeatherSample] = []
    for i, t in enumerate(times):
        cloud = total[i]
        if cloud is None:
            continue
        ts = datetime.fromisoformat(t).replace(tzinfo=UTC)
        temp_i, dew_i, rh_i = temp[i], dew[i], rh[i]
        low_i, mid_i, high_i = low[i], mid[i], high[i]
        out.append(
            WeatherSample(
                valid_time=ts,
                cloud_cover=_fraction(cloud),
                cloud_low=None if low_i is None else _fraction(low_i),
                cloud_mid=None if mid_i is None else _fraction(mid_i),
                cloud_high=None if high_i is None else _fraction(high_i),
                humidity=None if rh_i is None else rh_i / 100.0,
                dewpoint_spread_c=None if temp_i is None or dew_i is None else temp_i - dew_i,
                wind_speed_ms=wind[i],
                wind_gusts_ms=gust[i],
                temperature_c=temp_i,
                dew_point_c=dew_i,
                precipitation_mm=precip[i],
            )
        )
    return out


def _fraction(percent: float) -> float:
    """A percentage as a 0-1 fraction, clamped."""
    return min(max(percent / 100.0, 0.0), 1.0)


def in_window(s: WeatherSample, query: WeatherQuery) -> bool:
    return query.valid_from - WINDOW_PAD <= s.valid_time <= query.valid_to + WINDOW_PAD


RUN_CACHE_MAX: Final = 256
_RUN_CACHE: OrderedDict[tuple[str, str, str, str], tuple[WeatherSample, ...]] = OrderedDict()
_RUN_CACHE_LOCK = threading.Lock()


def run_cache_key(query: WeatherQuery, run: datetime) -> tuple[str, str, str, str]:
    return (query.model, f"{query.lat:.4f}", f"{query.lon:.4f}", run.strftime("%Y%m%dT%H"))


def cached_run(key: tuple[str, str, str, str]) -> tuple[WeatherSample, ...] | None:
    with _RUN_CACHE_LOCK:
        hit = _RUN_CACHE.get(key)
        if hit is not None:
            _RUN_CACHE.move_to_end(key)
        return hit


def store_run(key: tuple[str, str, str, str], samples: Sequence[WeatherSample]) -> None:
    """Cache a complete, successful run. Empty results are not cached."""
    if not samples:
        return
    with _RUN_CACHE_LOCK:
        _RUN_CACHE[key] = tuple(samples)
        _RUN_CACHE.move_to_end(key)
        while len(_RUN_CACHE) > RUN_CACHE_MAX:
            _RUN_CACHE.popitem(last=False)


def clear_run_cache() -> None:
    with _RUN_CACHE_LOCK:
        _RUN_CACHE.clear()


def single_run_params(query: WeatherQuery, run: datetime) -> dict[str, Any]:
    return {
        "latitude": query.lat,
        "longitude": query.lon,
        "hourly": HOURLY_VARS,
        "models": query.model,
        "run": run.strftime("%Y-%m-%dT%H:%M"),
        **COMMON_PARAMS,
    }


def fetch_single_run(
    client: httpx.Client, query: WeatherQuery, run: datetime
) -> tuple[WeatherSample, ...]:
    """One archived run, from the process cache when we already hold it."""
    key = run_cache_key(query, run)
    hit = cached_run(key)
    if hit is not None:
        return hit
    samples = _parse_hourly(get_json(client, SINGLE_RUNS_URL, single_run_params(query, run)))
    store_run(key, samples)
    return tuple(samples)


def live_params(query: WeatherQuery) -> dict[str, Any]:
    """Just the days the night touches: fewer rows, fewer weighted API calls."""
    first = (query.valid_from - WINDOW_PAD).date()
    last = (query.valid_to + WINDOW_PAD).date()
    return {
        "latitude": query.lat,
        "longitude": query.lon,
        "hourly": HOURLY_VARS,
        "models": query.model,
        "start_date": first.isoformat(),
        "end_date": last.isoformat(),
        **COMMON_PARAMS,
    }


def fetch_live(client: httpx.Client, query: WeatherQuery) -> list[WeatherSample]:
    """The current forecast for every hour of the days the night touches.

    Never cached: it is "now" by definition. Callers that want only the
    night's hours filter with ``in_window``.
    """
    return _parse_hourly(get_json(client, FORECAST_URL, live_params(query)))


def live_record(s: WeatherSample, model: str, published_at: datetime) -> Record[WeatherSample]:
    return Record(
        value=s,
        published_at=published_at,
        source_id=LIVE_SOURCE_ID,
        record_id=f"{model}:live:{s.valid_time:%Y%m%dT%H}",
        valid_from=s.valid_time,
        valid_to=s.valid_time + timedelta(hours=1),
        confidence=Confidence.EXACT,
    )


def run_record(
    s: WeatherSample, model: str, run: datetime, published_at: datetime
) -> Record[WeatherSample]:
    return Record(
        value=s,
        published_at=published_at,
        source_id=RUNS_SOURCE_ID,
        record_id=f"{model}:{run:%Y%m%dT%H}:{s.valid_time:%Y%m%dT%H}",
        valid_from=s.valid_time,
        valid_to=s.valid_time + timedelta(hours=1),
        # ESTIMATED, not EXACT: the lag is measured from the CURRENT run and
        # assumed stable for past ones.
        confidence=Confidence.ESTIMATED,
    )


def run_of(record_id: str) -> datetime | None:
    """The initialisation time encoded in a single-run record id, if any."""
    parts = record_id.split(":")
    if len(parts) != 3:
        return None
    try:
        return datetime.strptime(parts[1], "%Y%m%dT%H").replace(tzinfo=UTC)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------


class OpenMeteoLiveForecast(WeatherForecastProvider):
    """Live mode. ``published_at`` is the retrieval time.

    That is not laziness: we genuinely cannot know a forecast earlier than we
    fetched it, and claiming an earlier publication time would be the one lie
    that leaks. In LIVE vantage it cannot cause a lookahead anyway, since the
    future has not happened.
    """

    source_id = LIVE_SOURCE_ID

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or default_client()

    def _fetch(self, query: WeatherQuery, as_of: AsOf) -> Sequence[Record[WeatherSample]]:
        if as_of.vantage is Vantage.REPLAY:
            raise ValueError(
                "OpenMeteoLiveForecast cannot answer a replay as_of; it has no "
                "historical runs. Use OpenMeteoReplayForecast."
            )
        return [live_record(s, query.model, as_of.t) for s in fetch_live(self._client, query)]

    def coverage_key(self, query: WeatherQuery) -> str:
        return f"{query.lat:.4f},{query.lon:.4f},{query.model},live"


class OpenMeteoReplayForecast(WeatherForecastProvider):
    """Replay mode. Fetches each model run that covers the night.

    Every run is requested once; ``_fetch`` then filters to those published by
    ``as_of``, so scrubbing a slider through the night costs zero extra HTTP.
    Runs come from the process-wide cache when another session already holds
    them. The session API uses the frugal ``OpenMeteoAuto`` instead, which asks
    only for the runs that can reach a plan; this class stays for the scripts
    and the evaluation, which want the whole history.
    """

    source_id = RUNS_SOURCE_ID

    def __init__(
        self,
        client: httpx.Client | None = None,
        lag: timedelta | None = None,
        lookback_hours: int = 72,
    ) -> None:
        self._client = client or default_client()
        self._lag = lag
        self._lookback_hours = lookback_hours
        self._cache: dict[str, list[Record[WeatherSample]]] = {}

    def _lag_for(self, model: str) -> timedelta:
        # An explicit ``lag`` is the caller's; a measured one is floored.
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
                samples = fetch_single_run(self._client, query, run)
            except OpenMeteoError as exc:
                if exc.halts:
                    break
                continue
            published = run + lag
            records.extend(
                run_record(s, query.model, run, published) for s in samples if in_window(s, query)
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
