"""Weather "auto": resolution by when the night is, and the live forecast's gate.

All offline. Open-Meteo is played by an ``httpx.MockTransport`` that serves
meta.json, archived single runs and the live forecast, logs every request, and
can be told to fail. Each archived run carries its own cloud value, so a test
can tell from a plan's numbers which run it was built on.

The load-bearing tests are the gate ones: a live record must be invisible at
every as_of before the session's ``now``, and no request may ever be made for
a model run that could not have been published by ``now``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from tscheduler.core.clock import AsOf, Vantage
from tscheduler.core.timegrid import TimeGrid
from tscheduler.providers.weather import auto
from tscheduler.providers.weather import open_meteo as om
from tscheduler.providers.weather.auto import (
    NightTiming,
    OpenMeteoAuto,
    WeatherMode,
    resolve_weather,
)
from tscheduler.providers.weather.fixture import FixtureForecast
from tscheduler.providers.weather.model import WeatherQuery
from tscheduler.providers.weather.open_meteo import (
    LIVE_SOURCE_ID,
    RUNS_SOURCE_ID,
    _parse_hourly,
)

# Urbana, the default site, and a night 01:00-09:00 UTC.
LAT, LON = 40.1164, -88.2434
START = datetime(2026, 9, 13, 1, 0, tzinfo=UTC)
END = START + timedelta(hours=8)

#: meta.json: the newest run is 12 Sep 18z, available 8.4 h later (as the real
#: 12z run of 18 Sep was: 8.39 h). With the 0.5 h margin, rounded up to the
#: quarter hour, the lag is 9.0 h -- which is also ECMWF IFS 0.25's floor. So
#: the 12z run is published 21:00, the 18z run 03:00 and the 00z run 09:00.
META_INIT = datetime(2026, 9, 12, 18, 0, tzinfo=UTC)
META_AVAIL = META_INIT + timedelta(hours=8.4)
LAG = timedelta(hours=9.0)
LIVE_CLOUD = 90.0


def run_cloud(run: datetime) -> float:
    """A distinct cloud percentage per run, so plans reveal which run they used."""
    return float(10 + (run.day * 24 + run.hour) % 50)


class FakeOpenMeteo:
    """Just enough of Open-Meteo: three endpoints and a request log."""

    def __init__(
        self,
        *,
        meta_init: datetime = META_INIT,
        meta_avail: datetime = META_AVAIL,
        data_end: datetime | None = None,
        fail: Callable[[httpx.Request], httpx.Response | None] | None = None,
        live_cloud: float = LIVE_CLOUD,
    ) -> None:
        self.meta_init = meta_init
        self.meta_avail = meta_avail
        self.data_end = data_end or meta_init + timedelta(days=15)
        self.fail = fail
        self.live_cloud = live_cloud
        self.requests: list[httpx.Request] = []

    # -- request log --------------------------------------------------------

    def runs_requested(self) -> list[datetime]:
        return [
            datetime.fromisoformat(r.url.params["run"]).replace(tzinfo=UTC)
            for r in self.requests
            if r.url.host.startswith("single-runs")
        ]

    def live_requests(self) -> list[httpx.Request]:
        return [
            r for r in self.requests if r.url.path == "/v1/forecast" and "run" not in r.url.params
        ]

    def meta_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("meta.json")]

    # -- transport ----------------------------------------------------------

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail is not None:
            failed = self.fail(request)
            if failed is not None:
                return failed
        if request.url.path.endswith("meta.json"):
            return httpx.Response(
                200,
                json={
                    "last_run_initialisation_time": self.meta_init.timestamp(),
                    "last_run_availability_time": self.meta_avail.timestamp(),
                    "data_end_time": self.data_end.timestamp(),
                },
            )
        params = request.url.params
        assert params["wind_speed_unit"] == "ms", "wind must be requested in m/s"
        if "run" in params:
            run = datetime.fromisoformat(params["run"]).replace(tzinfo=UTC)
            hours = [run + timedelta(hours=h) for h in range(240)]
            return httpx.Response(200, json=self._hourly(hours, run_cloud(run)))
        first = datetime.fromisoformat(params["start_date"]).replace(tzinfo=UTC)
        last = datetime.fromisoformat(params["end_date"]).replace(tzinfo=UTC)
        n = int((last - first).total_seconds() // 3600) + 24
        hours = [first + timedelta(hours=h) for h in range(n)]
        # Past the model's reach the service pads with nulls.
        clouds: list[float | None] = [
            self.live_cloud if t <= self.data_end else None for t in hours
        ]
        return httpx.Response(200, json=self._hourly(hours, clouds))

    @staticmethod
    def _hourly(hours: list[datetime], cloud: float | list[float | None]) -> dict[str, Any]:
        n = len(hours)
        clouds = cloud if isinstance(cloud, list) else [cloud] * n
        return {
            "hourly": {
                "time": [t.strftime("%Y-%m-%dT%H:%M") for t in hours],
                "cloud_cover": clouds,
                "cloud_cover_low": [0.0] * n,
                "cloud_cover_mid": [0.0] * n,
                "cloud_cover_high": clouds,
                "relative_humidity_2m": [80.0] * n,
                "dew_point_2m": [10.0] * n,
                "temperature_2m": [14.0] * n,
                "wind_speed_10m": [3.0] * n,
                "wind_gusts_10m": [6.0] * n,
            }
        }


@pytest.fixture(autouse=True)
def _fresh_caches() -> Iterator[None]:
    auto.clear_caches()
    yield
    auto.clear_caches()


def _query() -> WeatherQuery:
    return WeatherQuery(lat=LAT, lon=LON, valid_from=START, valid_to=END)


def _live(t: datetime) -> AsOf:
    """An injected wall-clock instant, as the API layer would hand it over."""
    return AsOf(t, Vantage.LIVE)


def _provider(fake: FakeOpenMeteo, now: datetime, mode: WeatherMode) -> OpenMeteoAuto:
    return OpenMeteoAuto(_live(now), mode, client=fake.client())


def _newest_run_before(now: datetime) -> dict[str, datetime]:
    """meta.json as it would read at ``now``: the newest 12z run out by then,
    available 8.4 h after it started."""
    latest = now - timedelta(hours=8.4)
    init = latest.replace(hour=12, minute=0, second=0, microsecond=0)
    if init > latest:
        init -= timedelta(days=1)
    return {"meta_init": init, "meta_avail": init + timedelta(hours=8.4)}


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------


def test_a_night_that_is_over_is_replayed_from_the_archive() -> None:
    res = resolve_weather("auto", START, END, END + timedelta(hours=10))
    assert res.mode is WeatherMode.ARCHIVE
    assert res.timing is NightTiming.PAST


def test_tonight_before_dusk_is_a_forecast() -> None:
    res = resolve_weather("auto", START, END, START - timedelta(hours=5))
    assert res.mode is WeatherMode.FORECAST
    assert res.timing is NightTiming.UPCOMING


def test_a_night_in_progress_is_a_forecast() -> None:
    res = resolve_weather("auto", START, END, START + timedelta(hours=3))
    assert res.mode is WeatherMode.FORECAST
    assert res.timing is NightTiming.IN_PROGRESS


def test_a_night_beyond_the_horizon_is_unavailable_with_the_reason() -> None:
    res = resolve_weather("auto", START, END, START - timedelta(days=20))
    assert res.mode is WeatherMode.UNAVAILABLE
    assert res.reason is not None
    assert "20.0 days" in res.reason and "15-day" in res.reason


def test_a_night_just_past_the_horizon_does_not_read_as_inside_it() -> None:
    """15.2 days rounded to "15 days, beyond the 15-day reach" contradicts itself."""
    res = resolve_weather("auto", START, END, START - timedelta(days=15.2))
    assert res.mode is WeatherMode.UNAVAILABLE
    assert res.reason is not None
    assert "15.2 days from now, beyond the 15-day reach" in res.reason, res.reason
    # Within ~70 min of the horizon one decimal still reads "15.0 days".
    res = resolve_weather("auto", START, END, START - timedelta(days=15, minutes=30))
    assert res.reason is not None
    assert "more than 15 days from now, beyond the 15-day reach" in res.reason, res.reason


def test_a_night_older_than_the_archive_is_unavailable() -> None:
    res = resolve_weather(
        "auto", START, END, END + timedelta(days=1), archive_start=START + timedelta(days=1)
    )
    assert res.mode is WeatherMode.UNAVAILABLE
    assert res.reason is not None and "archive" in res.reason


def test_explicit_sources_keep_their_behaviour() -> None:
    now = START - timedelta(hours=5)
    assert resolve_weather("synthetic", START, END, now).mode is WeatherMode.SYNTHETIC
    assert resolve_weather("open_meteo", START, END, now).mode is WeatherMode.ARCHIVE


def test_auto_without_a_now_is_unavailable_not_a_guess() -> None:
    res = resolve_weather("auto", START, END, None)
    assert res.mode is WeatherMode.UNAVAILABLE
    assert res.reason


def test_forecast_mode_refuses_a_replay_now() -> None:
    """Stamping a live forecast with a replay instant would be a lookahead."""
    with pytest.raises(ValueError, match="LIVE"):
        OpenMeteoAuto(AsOf.at(START), WeatherMode.FORECAST)


# --------------------------------------------------------------------------
# which runs are requested
# --------------------------------------------------------------------------


def test_past_night_requests_only_the_runs_that_can_reach_a_plan() -> None:
    fake = FakeOpenMeteo()
    now = END + timedelta(days=2)
    p = _provider(fake, now, WeatherMode.ARCHIVE)
    pubs = p.publication_times(_query(), AsOf.at(END))

    # Dusk run: newest with init + 9 h <= 01:00 -> 12 Sep 12z (21:00).
    # During the night: 12 Sep 18z (03:00). 13 Sep 00z lands at 09:00, the
    # end of the night, where no plan can use it.
    expected = [
        datetime(2026, 9, 12, 12, tzinfo=UTC),
        datetime(2026, 9, 12, 18, tzinfo=UTC),
    ]
    assert sorted(fake.runs_requested()) == expected
    assert not fake.live_requests(), "a past night has no live forecast"
    assert pubs == tuple(r + LAG for r in expected)
    assert all(r + LAG <= END for r in fake.runs_requested())
    assert p.report.requests == len(fake.requests)


def test_tonight_before_dusk_uses_only_the_live_forecast() -> None:
    """The live forecast supersedes every archived run at every as_of the fold
    will ask about, so none is requested."""
    now = START - timedelta(hours=5)
    fake = FakeOpenMeteo(
        meta_init=datetime(2026, 9, 12, 12, tzinfo=UTC),
        meta_avail=datetime(2026, 9, 12, 19, 6, tzinfo=UTC),
    )
    p = _provider(fake, now, WeatherMode.FORECAST)
    recs = p.fetch(_query(), AsOf.at(START)).records

    assert fake.runs_requested() == []
    assert len(fake.live_requests()) == 1
    assert recs and all(r.source_id == LIVE_SOURCE_ID for r in recs)
    assert {r.published_at for r in recs} == {now}
    assert p.publication_times(_query(), AsOf.at(END)) == (now,)
    assert p.report.live_fetched_at == now
    assert p.report.live_run == datetime(2026, 9, 12, 12, tzinfo=UTC)


def _live_fails(r: httpx.Request) -> httpx.Response | None:
    live = r.url.host == "api.open-meteo.com" and r.url.path == "/v1/forecast"
    return httpx.Response(500) if live else None


def test_no_request_is_ever_made_for_a_run_published_after_now() -> None:
    for now in (
        START - timedelta(hours=5),  # tonight, live forecast failing -> archive fallback
        START + timedelta(hours=3),  # in progress
        START - timedelta(days=3),  # a coming night
    ):
        fake = FakeOpenMeteo(fail=_live_fails)
        p = _provider(fake, now, WeatherMode.FORECAST)
        p.fetch(_query(), AsOf.at(END))
        runs = fake.runs_requested()
        assert runs, f"expected an archived fallback at now={now}"
        assert all(r + LAG <= now for r in runs), f"run from the future requested: {runs}"
        auto.clear_caches()


def test_explicit_archive_of_a_future_night_is_bounded_by_now() -> None:
    fake = FakeOpenMeteo()
    now = START - timedelta(hours=5)
    p = _provider(fake, now, WeatherMode.ARCHIVE)
    p.fetch(_query(), AsOf.at(END))
    # 20:00: the 12z run (21:00) is not out yet; the 06z run (15:00) is.
    assert fake.runs_requested() == [datetime(2026, 9, 12, 6, tzinfo=UTC)]
    assert not fake.live_requests()


# --------------------------------------------------------------------------
# the delivery lag
# --------------------------------------------------------------------------


def test_a_fast_06z_run_does_not_shorten_the_lag_of_every_run() -> None:
    """Real numbers, 18 Sep 2026: meta.json named the 06z run, available after
    7.16 h; the 12z run took 8.39 h. Applied to every run, the 06z figure
    (7.75 h with the margin) showed each 00z/12z run to replayed plans before
    it existed. The 9.0 h floor holds however fast meta.json's run was."""
    init_06z = datetime(2026, 9, 12, 6, tzinfo=UTC)
    fake = FakeOpenMeteo(meta_init=init_06z, meta_avail=init_06z + timedelta(hours=7.16))
    p = _provider(fake, END + timedelta(days=2), WeatherMode.ARCHIVE)
    pubs = p.publication_times(_query(), AsOf.at(END))

    assert p.report.lag == timedelta(hours=9)
    assert p.report.lag_measured is True
    assert pubs, "fixture sanity: some run reached the night"
    for r in p.fetch(_query(), AsOf.at(END)).records:
        run = om.run_of(r.record_id)
        assert run is not None
        assert r.published_at == run + timedelta(hours=9), r.record_id
    # With the 7.75 h lag the 18z run would have been a decision point at 01:45.
    assert datetime(2026, 9, 13, 1, 45, tzinfo=UTC) not in pubs
    assert datetime(2026, 9, 13, 3, 0, tzinfo=UTC) in pubs


def test_a_lag_measured_above_the_floor_is_used_as_measured() -> None:
    fake = FakeOpenMeteo(meta_avail=META_INIT + timedelta(hours=9.2))
    p = _provider(fake, END + timedelta(days=2), WeatherMode.ARCHIVE)
    p.publication_times(_query(), AsOf.at(END))
    assert p.report.lag == timedelta(hours=9.75)  # 9.2 + 0.5, up to the quarter hour


def test_without_meta_json_the_lag_is_the_floor() -> None:
    fake = FakeOpenMeteo(
        fail=lambda r: httpx.Response(503) if r.url.path.endswith("meta.json") else None
    )
    p = _provider(fake, END + timedelta(days=2), WeatherMode.ARCHIVE)
    p.publication_times(_query(), AsOf.at(END))
    assert p.report.lag == timedelta(hours=9)
    assert p.report.lag_measured is False
    assert all(r + timedelta(hours=9) <= END for r in fake.runs_requested())


# --------------------------------------------------------------------------
# coverage and the archive's start
# --------------------------------------------------------------------------


def test_a_coming_night_the_live_forecast_only_partly_covers() -> None:
    """The live forecast stops at 03:00, two hours into the night. No archived
    run is requested -- each is older and reaches no further -- and past the
    forecast's reach (plus its hour of slack) the series is the clear-sky
    assumption with every optional field missing, not the last hour repeated."""
    reach = START + timedelta(hours=2)
    now = START - timedelta(days=14)
    fake = FakeOpenMeteo(**_newest_run_before(now), data_end=reach)
    p = _provider(fake, now, WeatherMode.FORECAST)
    grid = TimeGrid.from_window(START, END)
    s = p.weather_series(_query(), AsOf.at(START), grid)

    assert fake.runs_requested() == [], "archived runs cannot fill a gap past the newest run"
    assert len(fake.live_requests()) == 1
    mids = grid.mid_unix()
    inside = mids <= (reach + timedelta(hours=1)).timestamp()
    assert s.covered is not None
    np.testing.assert_array_equal(s.covered, inside)
    assert 0 < int(inside.sum()) < grid.n_slots
    np.testing.assert_allclose(s.cloud[inside], LIVE_CLOUD / 100.0)
    assert np.all(s.cloud[~inside] == 0.0), "past the reach the plan assumes a clear sky"
    for name in ("humidity", "wind_ms", "temperature_c", "cloud_high"):
        arr = getattr(s, name)
        assert arr is not None and np.all(np.isfinite(arr[inside])), name
        assert np.all(np.isnan(arr[~inside])), name
    # Gusts are the maximum over the hour ENDING at each valid time: nothing
    # describes the hour after the last one.
    gust = s.wind_gust_ms
    assert gust is not None
    assert np.all(np.isfinite(gust[mids <= reach.timestamp()]))
    assert np.all(np.isnan(gust[mids > reach.timestamp()]))


def test_a_night_the_forecast_fully_covers_is_covered_throughout() -> None:
    fake = FakeOpenMeteo()
    p = _provider(fake, END + timedelta(days=1), WeatherMode.ARCHIVE)
    s = p.weather_series(_query(), AsOf.at(END), TimeGrid.from_window(START, END))
    assert s.covered is not None and bool(s.covered.all())


def test_no_record_means_nothing_is_covered() -> None:
    s = auto.NoForecast().weather_series(_query(), AsOf.at(END), TimeGrid.from_window(START, END))
    assert s.covered is not None and not bool(s.covered.any())
    assert np.all(s.cloud == 0.0)


def test_runs_older_than_the_archive_are_not_requested() -> None:
    """With the archive starting at dusk, both the dusk run (12z, 21:00) and the
    one published during the night (18z, 03:00) are older than it: asking would
    only earn two HTTP 400s. The report says why instead."""
    fake = FakeOpenMeteo()
    p = OpenMeteoAuto(
        _live(END + timedelta(days=2)),
        WeatherMode.ARCHIVE,
        client=fake.client(),
        archive_start=START,
    )
    assert p.fetch(_query(), AsOf.at(END)).records == ()
    assert fake.runs_requested() == []
    assert p.report.before_archive == [
        datetime(2026, 9, 12, 12, tzinfo=UTC),
        datetime(2026, 9, 12, 18, tzinfo=UTC),
    ]
    reason = p.report.empty_reason(START)
    assert "archive" in reason and "begins 13 Sep 2026" in reason, reason
    # Per plan: at dusk only the 12z run could have been out.
    assert p.report.degradation(START) == (
        "1 older run predates Open-Meteo's archive, which begins 13 Sep 2026"
    )
    assert p.report.degradation(END) == (
        "2 older runs predate Open-Meteo's archive, which begins 13 Sep 2026"
    )


def test_a_failure_is_only_news_to_plans_made_after_the_run_was_due() -> None:
    """The 18z run (published 03:00) fails. A plan made at dusk could not have
    known; a plan made at 03:00 or later could."""
    run_18z = datetime(2026, 9, 12, 18, tzinfo=UTC)
    fake = FakeOpenMeteo(
        fail=lambda r: (
            httpx.Response(400, json={"error": True, "reason": "no such run"})
            if r.url.params.get("run") == run_18z.strftime("%Y-%m-%dT%H:%M")
            else None
        )
    )
    p = _provider(fake, END + timedelta(days=1), WeatherMode.ARCHIVE)
    p.fetch(_query(), AsOf.at(END))
    assert p.report.degradation() == "1 archived run could not be fetched"
    assert p.report.degradation(START) is None
    assert p.report.degradation(run_18z + LAG) == "1 archived run could not be fetched"


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------


def test_live_records_are_invisible_at_every_as_of_before_now() -> None:
    fake = FakeOpenMeteo()
    now = START + timedelta(hours=3, minutes=7)
    p = _provider(fake, now, WeatherMode.FORECAST)
    q = _query()

    for t in (START, START + timedelta(hours=1), now - timedelta(seconds=1)):
        rs = p.fetch(q, AsOf.at(t))  # Provider.fetch asserts the gate itself
        assert all(r.published_at <= t for r in rs.records)
        assert not any(r.source_id == LIVE_SOURCE_ID for r in rs.records), t
    at_now = p.fetch(q, AsOf.at(now))
    assert any(r.source_id == LIVE_SOURCE_ID for r in at_now.records)

    assert sorted(fake.runs_requested()) == [
        datetime(2026, 9, 12, 12, tzinfo=UTC),
        datetime(2026, 9, 12, 18, tzinfo=UTC),
    ]
    # Decision points: dusk, the 18z run at 03:00, and the live fetch at now.
    pubs = p.publication_times(q, AsOf.at(END))
    assert pubs[-1] == now
    assert [x for x in pubs if START < x < END] == [
        datetime(2026, 9, 13, 3, 0, tzinfo=UTC),
        now,
    ]


def test_the_newest_record_per_hour_is_the_live_one_from_now_on() -> None:
    fake = FakeOpenMeteo()
    now = START + timedelta(hours=3)
    p = _provider(fake, now, WeatherMode.FORECAST)
    grid = TimeGrid.from_window(START, END)
    before = p.weather_series(_query(), AsOf.at(now - timedelta(minutes=1)), grid)
    after = p.weather_series(_query(), AsOf.at(now), grid)
    assert before.cloud.max() < LIVE_CLOUD / 100.0
    assert after.cloud.min() == pytest.approx(LIVE_CLOUD / 100.0)


def test_live_stamp_moves_later_if_the_newest_run_arrived_after_now() -> None:
    """meta.json is read AFTER the live fetch. If it shows a run that became
    available after the session's now, the live data may be from it, so the
    stamp moves to that instant -- later, the safe direction."""
    now = START - timedelta(hours=5)
    fake = FakeOpenMeteo(meta_init=now - timedelta(hours=7), meta_avail=now + timedelta(seconds=40))
    p = _provider(fake, now, WeatherMode.FORECAST)
    recs = p.fetch(_query(), AsOf.at(START)).records
    assert {r.published_at for r in recs} == {now + timedelta(seconds=40)}
    assert p.fetch(_query(), AsOf.at(now)).records == ()
    order = [r.url.path for r in fake.requests]
    assert order.index("/v1/forecast") < next(i for i, x in enumerate(order) if "meta" in x)


# --------------------------------------------------------------------------
# failure is degraded data, never a crash
# --------------------------------------------------------------------------


def test_http_429_degrades_to_nothing_with_a_reason() -> None:
    fake = FakeOpenMeteo(fail=lambda r: httpx.Response(429))
    p = _provider(fake, START - timedelta(hours=5), WeatherMode.FORECAST)
    assert p.fetch(_query(), AsOf.at(END)).records == ()
    assert len(fake.requests) == 1, "the first 429 must stop every further request"
    assert p.report.halted == "rate_limited"
    assert "429" in p.report.empty_reason(START)


def test_connection_error_degrades_to_nothing_with_a_reason() -> None:
    def refuse(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=r)

    fake = FakeOpenMeteo(fail=refuse)
    p = _provider(fake, END + timedelta(days=1), WeatherMode.ARCHIVE)
    assert p.publication_times(_query(), AsOf.at(END)) == ()
    assert len(fake.requests) == 1
    assert p.report.halted == "unreachable"
    reason = p.report.empty_reason(START)
    assert "could not be reached" in reason and "ConnectError" in reason


def test_a_429_on_the_runs_stops_the_fallbacks() -> None:
    fake = FakeOpenMeteo(fail=lambda r: httpx.Response(429) if "run" in r.url.params else None)
    p = _provider(fake, END + timedelta(days=1), WeatherMode.ARCHIVE)
    assert p.fetch(_query(), AsOf.at(END)).records == ()
    # One meta.json plus at most the three primary runs already in flight.
    assert len(fake.runs_requested()) <= 3
    assert datetime(2026, 9, 12, 6, tzinfo=UTC) not in fake.runs_requested()


def test_a_missing_dusk_run_falls_back_to_the_one_before() -> None:
    dusk = datetime(2026, 9, 12, 12, tzinfo=UTC)
    fake = FakeOpenMeteo(
        fail=lambda r: (
            httpx.Response(400, json={"error": True, "reason": "no such run"})
            if r.url.params.get("run") == dusk.strftime("%Y-%m-%dT%H:%M")
            else None
        )
    )
    p = _provider(fake, END + timedelta(days=1), WeatherMode.ARCHIVE)
    recs = p.fetch(_query(), AsOf.at(START)).records
    assert recs, "the fallback run should cover dusk"
    assert datetime(2026, 9, 12, 6, tzinfo=UTC) in fake.runs_requested()
    assert p.report.run_errors[0] == (dusk, "HTTP 400: no such run")


def test_a_night_past_the_models_reach_reports_the_horizon() -> None:
    now = START - timedelta(days=14)
    fake = FakeOpenMeteo(data_end=START - timedelta(hours=6))
    p = _provider(fake, now, WeatherMode.FORECAST)
    assert p.fetch(_query(), AsOf.at(END)).records == ()
    assert "reaches only" in p.report.empty_reason(START)
    assert fake.runs_requested() == [], "an older run cannot reach further than the newest"


def test_a_forecast_ending_just_before_dusk_is_not_a_forecast_for_the_night() -> None:
    """The live forecast's last hour is 00:00, an hour before dusk: kept only as
    interpolation padding, it reaches no slot. Records from it alone would make
    the session read as "Live forecast" while every slot assumes a clear sky."""
    now = START - timedelta(days=14)
    fake = FakeOpenMeteo(**_newest_run_before(now), data_end=START - timedelta(minutes=30))
    p = _provider(fake, now, WeatherMode.FORECAST)
    assert p.fetch(_query(), AsOf.at(END)).records == ()
    assert p.report.live_error == "no forecast hours for this night"
    assert "reaches only" in p.report.empty_reason(START)
    assert fake.runs_requested() == []


# --------------------------------------------------------------------------
# frugality and parsing
# --------------------------------------------------------------------------


def test_archived_runs_are_cached_across_sessions() -> None:
    fake = FakeOpenMeteo()
    now = END + timedelta(days=2)
    _provider(fake, now, WeatherMode.ARCHIVE).fetch(_query(), AsOf.at(END))
    first = len(fake.requests)
    again = _provider(fake, now, WeatherMode.ARCHIVE)
    recs = again.fetch(_query(), AsOf.at(END)).records
    assert recs
    assert len(fake.requests) == first, "a second session re-downloaded immutable runs"


def test_a_null_cloud_row_is_dropped_not_read_as_clear() -> None:
    payload = {
        "hourly": {
            "time": ["2026-09-13T01:00", "2026-09-13T02:00"],
            "cloud_cover": [55.0, None],
            "relative_humidity_2m": [None, 90.0],
            "temperature_2m": [12.0, 11.0],
            "dew_point_2m": [11.0, 10.5],
            "wind_speed_10m": [4.0, 5.0],
        }
    }
    samples = _parse_hourly(payload)
    assert len(samples) == 1
    s = samples[0]
    assert s.cloud_cover == pytest.approx(0.55)
    assert s.humidity is None, "a missing humidity must stay missing"
    assert s.dewpoint_spread_c == pytest.approx(1.0)
    assert s.wind_gusts_ms is None


def test_weather_series_carries_humidity_dew_and_wind() -> None:
    fake = FakeOpenMeteo()
    p = _provider(fake, END + timedelta(days=1), WeatherMode.ARCHIVE)
    s = p.weather_series(_query(), AsOf.at(END), TimeGrid.from_window(START, END))
    assert s.humidity is not None and s.humidity.max() == pytest.approx(0.8)
    assert s.dew_spread_c is not None and s.dew_spread_c.min() == pytest.approx(4.0)
    assert s.wind_ms is not None and s.wind_ms.max() == pytest.approx(3.0)
    assert s.wind_gust_ms is not None and s.wind_gust_ms.max() == pytest.approx(6.0)
    assert {r.provider for r in s.ledger.refs} == {RUNS_SOURCE_ID}


def test_synthetic_weather_has_no_humidity_or_wind_to_report() -> None:
    fx = FixtureForecast.from_runs([(START - timedelta(hours=6), [(START, 0.4)])])
    s = fx.weather_series(_query(), AsOf.at(START), TimeGrid.from_window(START, END))
    assert s.has_data
    assert s.humidity is None and s.wind_ms is None and s.dew_spread_c is None


# --------------------------------------------------------------------------
# through the API
# --------------------------------------------------------------------------


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., dict[str, Any]]]:
    """A server whose Open-Meteo is a fake and whose wall clock reads ``now``.

    ``AsOf.live`` is the one sanctioned clock reader, so pinning it pins the
    session's creation time and its weather resolution together.
    """
    from tscheduler.api.app import create_app

    with TestClient(create_app()) as client:

        def create(fake: FakeOpenMeteo, now: datetime, **body: Any) -> dict[str, Any]:
            monkeypatch.setattr(auto, "client_factory", fake.client)
            monkeypatch.setattr(AsOf, "live", classmethod(lambda cls: cls(now, Vantage.LIVE)))
            r = client.post(
                "/api/sessions",
                json={"date": "2026-09-13", "hours": 8, "solveSeconds": 1.0, **body},
            )
            assert r.status_code == 202, r.text
            sess: dict[str, Any] = client.get(f"/api/sessions/{r.json()['id']}").json()
            assert sess["status"] == "ready", sess.get("error") or sess["message"]
            return sess

        create.client = client  # type: ignore[attr-defined]
        yield create


def test_api_session_in_progress_with_auto_weather(api: Callable[..., dict[str, Any]]) -> None:
    now = START + timedelta(hours=3, minutes=7)
    fake = FakeOpenMeteo()
    sess = api(fake, now, weather="auto")
    client: TestClient = api.client  # type: ignore[attr-defined]

    w = sess["weather"]
    assert w["requested"] == "auto"
    assert w["mode"] == "forecast"
    assert w["label"] == "Live forecast"
    assert "ECMWF IFS 0.25°" in w["description"] and "fetched" in w["description"]
    assert datetime.fromisoformat(w["fetchedAt"]) == now
    assert w["runs"] == 3  # 12z, 18z and the live forecast
    assert w["records"] > 0
    assert sess["createdAt"] == w["fetchedAt"], "one wall-clock instant for both"

    dps = sess["decisionPoints"]
    assert datetime.fromisoformat(dps[-1]["at"]) == now
    assert dps[-1]["reason"] == f"live forecast fetched {now:%H:%M} UTC"
    assert dps[1]["reason"].startswith("forecast run published 03:00 UTC")

    sid = sess["id"]
    first = client.get(f"/api/sessions/{sid}/plan", params={"as_of": dps[0]["at"]}).json()
    last = client.get(f"/api/sessions/{sid}/plan", params={"as_of": dps[-1]["at"]}).json()

    o0 = first["outlook"]
    assert o0["hasData"] is True
    assert "run of 12 Sep 12:00 UTC" in o0["basis"]
    assert o0["verdict"] in {"go", "marginal", "poor"}
    assert o0["humidityMax"] == pytest.approx(0.8)
    assert o0["windMaxMs"] == pytest.approx(3.0)
    # No standing note about seeing: that is a fact about the source, the same
    # on every night, and the plan carries the assumed value itself.
    assert not any("seeing" in n["text"].lower() for n in o0["notes"])
    assert first["seeingFwhmArcsec"][0] == pytest.approx(2.5)

    o1 = last["outlook"]
    assert o1["basis"].startswith("Live ECMWF IFS 0.25° forecast")
    assert o1["category"] == "overcast" and o1["verdict"] == "poor"
    assert o1["meanCloudFraction"] == pytest.approx(LIVE_CLOUD / 100.0)
    assert o1["clearHours"] == 0.0 and o1["bestWindow"] is None
    # Made at 04:07, it is about 04:05 onwards (the slot it cannot unlock),
    # not the dark hours already gone.
    locked = last["changes"]["lockedThroughSlot"]
    assert locked == int((now - START).total_seconds() // sess["grid"]["slotSeconds"])
    assert o1["darkHours"] < o0["darkHours"]
    assert "remaining dark hours" in o1["headline"], o1["headline"]
    assert "remaining" not in o0["headline"]
    for p in (first, last):
        newest = p["evidence"]["newestPublished"]
        assert datetime.fromisoformat(newest) <= datetime.fromisoformat(p["asOf"])
    assert "open_meteo:live" not in first["evidence"]["sources"]


def test_api_session_with_the_service_down_says_so_loudly(
    api: Callable[..., dict[str, Any]],
) -> None:
    def refuse(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=r)

    fake = FakeOpenMeteo(fail=refuse)
    sess = api(fake, END + timedelta(days=2), weather="auto")
    client: TestClient = api.client  # type: ignore[attr-defined]

    w = sess["weather"]
    assert w["mode"] == "unavailable"
    assert w["label"] == "No forecast available"
    assert "could not be reached" in w["description"]
    assert "assumes a clear sky" in w["description"]
    assert w["runs"] == 0 and w["records"] == 0 and w["fetchedAt"] is None

    plan = client.get(f"/api/sessions/{sess['id']}/plan").json()
    o = plan["outlook"]
    assert o["hasData"] is False
    assert o["category"] == "unknown" and o["verdict"] == "unknown"
    assert "assumes a clear sky" in o["headline"]
    crit = [n for n in o["notes"] if n["severity"] == "critical"]
    assert crit and "could not be reached" in crit[0]["text"]


def test_api_session_beyond_the_horizon_makes_no_request(
    api: Callable[..., dict[str, Any]],
) -> None:
    fake = FakeOpenMeteo()
    sess = api(fake, START - timedelta(days=30), weather="auto")
    assert fake.requests == []
    assert sess["weather"]["mode"] == "unavailable"
    assert "beyond the 15-day reach" in sess["weather"]["description"]


def test_api_synthetic_session_is_labelled_as_a_demo(api: Callable[..., dict[str, Any]]) -> None:
    fake = FakeOpenMeteo()
    sess = api(fake, END + timedelta(days=1), weather="synthetic")
    client: TestClient = api.client  # type: ignore[attr-defined]
    assert fake.requests == []
    w = sess["weather"]
    assert w["mode"] == "synthetic" and w["model"] is None and w["runs"] > 0
    o = client.get(f"/api/sessions/{sess['id']}/plan").json()["outlook"]
    assert o["hasData"] is True
    assert o["humidityMax"] is None and o["windMaxMs"] is None
    assert any("not a forecast" in n["text"] for n in o["notes"])


def test_api_forecast_with_the_live_fetch_failing_falls_back_and_says_so(
    api: Callable[..., dict[str, Any]],
) -> None:
    """Degraded, not broken: the newest archived run published by now fills in,
    the source says the live forecast is missing, and the outlook says what to do."""
    now = START - timedelta(hours=5)
    fake = FakeOpenMeteo(fail=_live_fails)
    sess = api(fake, now, weather="auto")
    client: TestClient = api.client  # type: ignore[attr-defined]

    w = sess["weather"]
    assert w["mode"] == "forecast"
    assert w["fetchedAt"] is None
    assert "could not be fetched (HTTP 500)" in w["description"]
    assert w["runs"] == 1
    assert all(r + LAG <= now for r in fake.runs_requested())

    o = client.get(f"/api/sessions/{sess['id']}/plan").json()["outlook"]
    assert o["hasData"] is True
    # 20:00: the newest run out is 12 Sep 06z (15:00); 12z lands at 21:00.
    assert "run of 12 Sep 06:00 UTC" in o["basis"]
    warn = [n["text"] for n in o["notes"] if n["severity"] == "warn"]
    assert any(t.startswith("Not the live forecast") for t in warn), warn


def test_archived_runs_still_cover_dusk_if_the_live_stamp_lands_after_it() -> None:
    """A run that arrived between now and dusk moves the live stamp past dusk,
    so the dusk plan cannot see it and must not be left with nothing."""
    now = START - timedelta(seconds=30)
    fake = FakeOpenMeteo(
        meta_init=now - timedelta(hours=7), meta_avail=START + timedelta(seconds=5)
    )
    p = _provider(fake, now, WeatherMode.FORECAST)
    at_dusk = p.fetch(_query(), AsOf.at(START)).records
    assert at_dusk and all(r.source_id == RUNS_SOURCE_ID for r in at_dusk)
    assert START + timedelta(seconds=5) in p.publication_times(_query(), AsOf.at(END))


def test_api_a_partly_covered_night_is_not_reported_as_fully_forecast(
    api: Callable[..., dict[str, Any]],
) -> None:
    """Fourteen days ahead the live forecast stops at 04:00 UTC, mid-night. The
    outlook summarises only the hours it reaches, says from when the plan
    assumes a clear sky, and does not call the night; every per-slot value past
    the reach is None; and no archived run is requested to fill the gap."""
    now = START - timedelta(days=14)
    fake = FakeOpenMeteo(
        **_newest_run_before(now), data_end=START + timedelta(hours=3), live_cloud=10.0
    )
    sess = api(fake, now, weather="auto")
    client: TestClient = api.client  # type: ignore[attr-defined]
    assert sess["weather"]["mode"] == "forecast"
    assert fake.runs_requested() == []

    plan = client.get(f"/api/sessions/{sess['id']}/plan").json()
    o = plan["outlook"]
    assert o["hasData"] is True
    crit = [n["text"] for n in o["notes"] if n["severity"] == "critical"]
    reach = [t for t in crit if t.startswith("The forecast reaches only ")]
    assert reach and reach[0].endswith("UTC; after that the plan assumes a clear sky."), crit
    assert "the forecast reaches" in o["headline"], o["headline"]
    dark = o["darkHours"]
    assert o["clearHours"] < dark
    assert f"{dark:.1f} of {dark:.1f}" not in o["verdictText"]
    assert o["verdict"] == "unknown" and o["verdictText"].startswith("Too soon to call")

    sw = plan["slotWeather"]
    covered = sw["covered"]
    assert len(covered) == len(plan["slots"])
    first_gap = covered.index(False)
    assert first_gap > 0 and not any(covered[first_gap:]), "coverage is one prefix"
    for i, ok in enumerate(covered):
        for name in ("humidity", "temperatureC", "windMs", "cloudHigh"):
            assert (sw[name][i] is not None) == ok, (name, i)
        if not ok:
            assert plan["cloudFraction"][i] == 0.0


def test_api_a_night_just_after_the_archive_starts_says_why_dusk_has_no_forecast(
    api: Callable[..., dict[str, Any]],
) -> None:
    """Night of 1 Apr 2026, dusk 2 Apr 00:51. Every run that could reach the
    dusk plan was initialised on 1 Apr, before the Single Runs archive: none is
    requested, and the dusk plan's note says so instead of "4 archived runs
    could not be fetched"."""
    archive = datetime(2026, 4, 2, tzinfo=UTC)

    def before_archive(r: httpx.Request) -> httpx.Response | None:
        run = r.url.params.get("run")
        if run and datetime.fromisoformat(run).replace(tzinfo=UTC) < archive:
            return httpx.Response(400, json={"error": True, "reason": "run not available"})
        return None

    fake = FakeOpenMeteo(
        meta_init=datetime(2026, 9, 18, 12, tzinfo=UTC),
        meta_avail=datetime(2026, 9, 18, 20, 23, 17, tzinfo=UTC),
        fail=before_archive,
    )
    sess = api(
        fake,
        datetime(2026, 9, 18, 21, 45, tzinfo=UTC),
        weather="auto",
        date="2026-04-02",
        startHourUtc=0.85,
        hours=9.5,
    )
    client: TestClient = api.client  # type: ignore[attr-defined]
    assert fake.runs_requested() == [datetime(2026, 4, 2, 0, tzinfo=UTC)]
    w = sess["weather"]
    assert w["mode"] == "archive"
    assert (
        "2 older runs predate Open-Meteo's archive, which begins 2 Apr 2026" in (w["description"])
    ), w["description"]
    assert "could not be fetched" not in w["description"]

    dps = sess["decisionPoints"]
    dusk = client.get(f"/api/sessions/{sess['id']}/plan", params={"as_of": dps[0]["at"]})
    o = dusk.json()["outlook"]
    assert o["hasData"] is False
    crit = [n["text"] for n in o["notes"] if n["severity"] == "critical"]
    assert any(
        "1 older run predates Open-Meteo's archive, which begins 2 Apr 2026" in t for t in crit
    ), crit
    assert datetime.fromisoformat(dps[-1]["at"]) == datetime(2026, 4, 2, 9, tzinfo=UTC)


def test_api_early_plans_are_not_warned_about_a_later_live_failure(
    api: Callable[..., dict[str, Any]],
) -> None:
    """A night in progress whose live fetch fails at 04:07. The dusk plan was
    superseded at 03:00, long before that fetch was tried: it is not told. The
    03:00 plan is the one in force when it failed, so it is."""
    now = START + timedelta(hours=3, minutes=7)
    fake = FakeOpenMeteo(fail=_live_fails)
    sess = api(fake, now, weather="auto")
    client: TestClient = api.client  # type: ignore[attr-defined]
    dps = sess["decisionPoints"]
    assert [datetime.fromisoformat(d["at"]) for d in dps] == [
        START,
        datetime(2026, 9, 13, 3, 0, tzinfo=UTC),
    ]

    def warnings(at: str) -> list[str]:
        o = client.get(f"/api/sessions/{sess['id']}/plan", params={"as_of": at}).json()
        return [n["text"] for n in o["outlook"]["notes"] if n["severity"] == "warn"]

    assert not any("live forecast" in t for t in warnings(dps[0]["at"]))
    assert any(t.startswith("Not the live forecast") for t in warnings(dps[1]["at"]))


def test_api_a_dusk_plan_is_not_told_about_a_run_that_fails_later(
    api: Callable[..., dict[str, Any]],
) -> None:
    """The 18z run, due 03:00, fails. The session says so; the dusk plan, made
    at 01:00, does not -- it could not have known."""
    run_18z = datetime(2026, 9, 12, 18, tzinfo=UTC)
    fake = FakeOpenMeteo(
        fail=lambda r: (
            httpx.Response(400, json={"error": True, "reason": "no such run"})
            if r.url.params.get("run") == run_18z.strftime("%Y-%m-%dT%H:%M")
            else None
        )
    )
    sess = api(fake, END + timedelta(days=2), weather="auto")
    client: TestClient = api.client  # type: ignore[attr-defined]
    assert "1 archived run could not be fetched" in sess["weather"]["description"]
    o = client.get(
        f"/api/sessions/{sess['id']}/plan", params={"as_of": sess["decisionPoints"][0]["at"]}
    ).json()["outlook"]
    assert not any("could not be fetched" in n["text"] for n in o["notes"]), o["notes"]
