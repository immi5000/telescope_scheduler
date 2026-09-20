"""Per-slot weather: the forecast under the time cursor, one value per plan slot.

``PlanOut.slotWeather`` carries temperature, dew point, humidity, wind, gusts,
precipitation and the three cloud layers for every slot, read from the same
conditions layer as the outlook. Three properties matter:

* **Alignment.** Every array has one entry per ``plan.slots``, and a value from
  the source lands on the slots whose centres it covers.
* **Honesty.** A field the source does not give is ``None`` in every slot --
  synthetic weather and "no forecast" give nothing, and nothing is invented.
* **No lookahead.** An early decision point's slot weather comes from the run
  published by its as_of, never from a later run or the live forecast.

All offline: Open-Meteo is an ``httpx.MockTransport``, as in test_weather_auto.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from tscheduler.api import outlook
from tscheduler.core.clock import AsOf, Vantage
from tscheduler.core.timegrid import TimeGrid
from tscheduler.providers.base import Confidence, Record
from tscheduler.providers.weather import auto, base
from tscheduler.providers.weather import open_meteo as om
from tscheduler.providers.weather.fixture import FixtureForecast
from tscheduler.providers.weather.model import WeatherQuery, WeatherSample

LAT, LON = 40.1164, -88.2434
START = datetime(2026, 9, 13, 1, 0, tzinfo=UTC)
END = START + timedelta(hours=8)

#: Newest run 12 Sep 18z, available 7.1 h later: 7.75 h with the margin, which
#: ECMWF IFS 0.25's 9.0 h floor overrides. So the 12z run is published at
#: 21:00 (before dusk) and the 18z run at 03:00 (during the night).
META_INIT = datetime(2026, 9, 12, 18, 0, tzinfo=UTC)
META_AVAIL = META_INIT + timedelta(hours=7.1)
RUN_12Z = datetime(2026, 9, 12, 12, tzinfo=UTC)
RUN_18Z = datetime(2026, 9, 12, 18, tzinfo=UTC)
PUB_18Z = RUN_18Z + timedelta(hours=9.0)

#: The live forecast's rain: 2 mm in the hour ending 05:00 UTC, dry otherwise.
RAIN_ENDS = START + timedelta(hours=4)
RAIN_MM = 2.0

SLOT_FIELDS = (
    "temperatureC",
    "dewPointC",
    "humidity",
    "windMs",
    "windGustMs",
    "precipitationMm",
    "cloudLow",
    "cloudMid",
    "cloudHigh",
)


def run_temp(run: datetime) -> float:
    """Each archived run's (constant) temperature: its initialisation hour."""
    return float(run.hour)


def live_temp(t: datetime) -> float:
    """The live forecast's temperature: 30 C at dusk, one degree per hour --
    far from every archived run's, so a plan shows which it used."""
    return 30.0 + (t - START).total_seconds() / 3600.0


class FakeOpenMeteo:
    """meta.json, archived single runs and the live forecast.

    What each says, so a plan's slot weather names its source:

    * archived run ``r``: temperature ``run_temp(r)``, dew point 3 C lower,
      humidity 70%, wind 4 m/s, low cloud 40%, high cloud 20% -- and NO
      precipitation or mid-cloud column, and null gusts.
    * live: temperature ``live_temp(t)``, dew point 5 C lower, humidity 90%,
      wind 6 m/s, low 10%, high 30%, and ``RAIN_MM`` in the hour ending
      ``RAIN_ENDS`` -- again no mid cloud and null gusts.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("meta.json"):
            return httpx.Response(
                200,
                json={
                    "last_run_initialisation_time": META_INIT.timestamp(),
                    "last_run_availability_time": META_AVAIL.timestamp(),
                    "data_end_time": (META_INIT + timedelta(days=15)).timestamp(),
                },
            )
        p = request.url.params
        if "run" in p:
            run = datetime.fromisoformat(p["run"]).replace(tzinfo=UTC)
            hours = [run + timedelta(hours=h) for h in range(72)]
            n = len(hours)
            return httpx.Response(
                200,
                json={
                    "hourly": {
                        "time": _iso(hours),
                        "cloud_cover": [50.0] * n,
                        "cloud_cover_low": [40.0] * n,
                        "cloud_cover_high": [20.0] * n,
                        "relative_humidity_2m": [70.0] * n,
                        "temperature_2m": [run_temp(run)] * n,
                        "dew_point_2m": [run_temp(run) - 3.0] * n,
                        "wind_speed_10m": [4.0] * n,
                        "wind_gusts_10m": [None] * n,
                    }
                },
            )
        first = datetime.fromisoformat(p["start_date"]).replace(tzinfo=UTC)
        last = datetime.fromisoformat(p["end_date"]).replace(tzinfo=UTC)
        n = int((last - first).total_seconds() // 3600) + 24
        hours = [first + timedelta(hours=h) for h in range(n)]
        return httpx.Response(
            200,
            json={
                "hourly": {
                    "time": _iso(hours),
                    "cloud_cover": [80.0] * n,
                    "cloud_cover_low": [10.0] * n,
                    "cloud_cover_high": [30.0] * n,
                    "relative_humidity_2m": [90.0] * n,
                    "temperature_2m": [live_temp(t) for t in hours],
                    "dew_point_2m": [live_temp(t) - 5.0 for t in hours],
                    "wind_speed_10m": [6.0] * n,
                    "wind_gusts_10m": [None] * n,
                    "precipitation": [RAIN_MM if t == RAIN_ENDS else 0.0 for t in hours],
                }
            },
        )


def _iso(hours: list[datetime]) -> list[str]:
    return [t.strftime("%Y-%m-%dT%H:%M") for t in hours]


@pytest.fixture(autouse=True)
def _fresh_caches() -> Iterator[None]:
    auto.clear_caches()
    yield
    auto.clear_caches()


# --------------------------------------------------------------------------
# the request and the parse
# --------------------------------------------------------------------------


def test_every_request_asks_for_temperature_dew_point_and_precipitation() -> None:
    q = WeatherQuery(lat=LAT, lon=LON, valid_from=START, valid_to=END)
    for params in (om.single_run_params(q, RUN_12Z), om.live_params(q)):
        wanted = params["hourly"].split(",")
        assert {"temperature_2m", "dew_point_2m", "precipitation"} <= set(wanted)
        assert len(wanted) <= 10, "an eleventh variable doubles the rate-limit cost"
        assert params["temperature_unit"] == "celsius"
        assert params["precipitation_unit"] == "mm"
        assert params["wind_speed_unit"] == "ms"


def test_parse_reads_the_new_fields_and_keeps_nulls_none() -> None:
    payload = {
        "hourly": {
            "time": ["2026-09-13T01:00", "2026-09-13T02:00"],
            "cloud_cover": [55.0, 60.0],
            "cloud_cover_low": [20.0, None],
            "cloud_cover_mid": [None, 130.0],
            "cloud_cover_high": [35.0, 0.0],
            "temperature_2m": [12.5, None],
            "dew_point_2m": [9.0, 8.0],
            "precipitation": [0.4, None],
        }
    }
    a, b = om._parse_hourly(payload)
    assert a.temperature_c == pytest.approx(12.5)
    assert a.dew_point_c == pytest.approx(9.0)
    assert a.dewpoint_spread_c == pytest.approx(3.5)
    assert a.precipitation_mm == pytest.approx(0.4)
    assert a.cloud_low == pytest.approx(0.2) and a.cloud_high == pytest.approx(0.35)
    assert a.cloud_mid is None, "a null layer must stay null, not read as clear"

    assert b.temperature_c is None and b.dewpoint_spread_c is None
    assert b.dew_point_c == pytest.approx(8.0)
    assert b.precipitation_mm is None, "a null precipitation is not a dry hour"
    assert b.cloud_low is None
    assert b.cloud_mid == pytest.approx(1.0), "layers are clamped to 0-1"
    assert b.cloud_high == 0.0
    assert b.humidity is None and b.wind_speed_ms is None


def test_a_missing_column_is_none_not_zero() -> None:
    (s,) = om._parse_hourly({"hourly": {"time": ["2026-09-13T01:00"], "cloud_cover": [10.0]}})
    assert s.cloud_cover == pytest.approx(0.1)
    for name in ("cloud_low", "cloud_mid", "cloud_high", "temperature_c", "dew_point_c"):
        assert getattr(s, name) is None, name
    assert s.precipitation_mm is None


# --------------------------------------------------------------------------
# onto the slot grid
# --------------------------------------------------------------------------


def _fixture(samples: list[WeatherSample], published: datetime) -> FixtureForecast:
    return FixtureForecast(
        [
            Record(
                value=s,
                published_at=published,
                source_id=FixtureForecast.source_id,
                record_id=f"t:{s.valid_time:%Y%m%dT%H}",
                valid_from=s.valid_time,
                valid_to=s.valid_time + timedelta(hours=1),
                confidence=Confidence.ESTIMATED,
            )
            for s in samples
        ]
    )


def test_series_puts_each_value_on_the_slots_it_covers() -> None:
    hours = [START + timedelta(hours=h) for h in range(-2, 11)]
    fx = _fixture(
        [
            WeatherSample(
                valid_time=t,
                cloud_cover=0.5,
                temperature_c=live_temp(t),
                dew_point_c=live_temp(t) - 5.0,
                precipitation_mm=RAIN_MM if t == RAIN_ENDS else 0.0,
                cloud_low=0.1,
            )
            for t in hours
        ],
        published=START - timedelta(hours=6),
    )
    grid = TimeGrid.from_window(START, END)
    q = WeatherQuery(lat=LAT, lon=LON, valid_from=START, valid_to=END)
    s = fx.weather_series(q, AsOf.at(START), grid)

    hours_in = (grid.mid_unix() - START.timestamp()) / 3600.0
    assert s.temperature_c is not None and s.dew_point_c is not None
    np.testing.assert_allclose(s.temperature_c, 30.0 + hours_in)
    np.testing.assert_allclose(s.dew_point_c, 25.0 + hours_in)

    # The rain fell in the hour ending RAIN_ENDS, so it peaks half an hour
    # before it and is gone an hour either side of that.
    assert s.precipitation_mm is not None
    peak = (RAIN_ENDS - START).total_seconds() / 3600.0 - 0.5
    expected = RAIN_MM * np.clip(1.0 - np.abs(hours_in - peak), 0.0, None)
    np.testing.assert_allclose(s.precipitation_mm, expected, atol=1e-12)
    during = (hours_in > peak - 0.5) & (hours_in < peak + 0.5)
    assert s.precipitation_mm[during].min() >= RAIN_MM / 2
    assert int(np.argmax(s.precipitation_mm)) in np.flatnonzero(during)

    assert s.cloud_low is not None and np.allclose(s.cloud_low, 0.1)
    assert s.cloud_mid is None and s.cloud_high is None
    assert s.humidity is None and s.wind_ms is None and s.precipitation_mm.min() >= 0.0


def test_synthetic_weather_forecasts_none_of_it() -> None:
    fx = FixtureForecast.from_runs([(START - timedelta(hours=6), [(START, 0.4)])])
    q = WeatherQuery(lat=LAT, lon=LON, valid_from=START, valid_to=END)
    s = fx.weather_series(q, AsOf.at(START), TimeGrid.from_window(START, END))
    assert s.has_data
    for name in (
        "temperature_c",
        "dew_point_c",
        "precipitation_mm",
        "cloud_low",
        "cloud_mid",
        "cloud_high",
    ):
        assert getattr(s, name) is None, name


def test_gusts_are_placed_mid_hour_like_precipitation() -> None:
    """Open-Meteo's wind_gusts_10m is the maximum over the PRECEDING hour, like
    precipitation's sum: a gusty hour ending 05:00 peaks at 04:30, not 05:00."""
    hours = [START + timedelta(hours=h) for h in range(-2, 11)]
    fx = _fixture(
        [
            WeatherSample(
                valid_time=t,
                cloud_cover=0.2,
                wind_speed_ms=3.0,
                wind_gusts_ms=15.0 if t == RAIN_ENDS else 5.0,
            )
            for t in hours
        ],
        published=START - timedelta(hours=6),
    )
    grid = TimeGrid.from_window(START, END)
    q = WeatherQuery(lat=LAT, lon=LON, valid_from=START, valid_to=END)
    s = fx.weather_series(q, AsOf.at(START), grid)
    assert base.PRECEDING_HOUR_SHIFT_S == -1800.0
    assert s.wind_gust_ms is not None and s.wind_ms is not None
    hours_in = (grid.mid_unix() - START.timestamp()) / 3600.0
    peak = (RAIN_ENDS - START).total_seconds() / 3600.0 - 0.5
    np.testing.assert_allclose(
        s.wind_gust_ms, 5.0 + 10.0 * np.clip(1.0 - np.abs(hours_in - peak), 0.0, None)
    )
    # Mean wind is instantaneous: not shifted.
    np.testing.assert_allclose(s.wind_ms, 3.0)


def test_past_the_forecasts_reach_is_the_assumption_not_the_last_hour() -> None:
    """Records up to 03:00. The hour after is covered (an hourly value stands
    for the hour around it); beyond that cloud is the clear-sky assumption, and
    every other field is NaN -- None on the wire -- not 03:00 repeated."""
    hours = [START + timedelta(hours=h) for h in range(-2, 3)]
    fx = _fixture(
        [
            WeatherSample(
                valid_time=t,
                cloud_cover=0.8,
                seeing_fwhm_arcsec=3.1,
                humidity=0.9,
                temperature_c=12.0,
                precipitation_mm=0.5,
                wind_gusts_ms=9.0,
            )
            for t in hours
        ],
        published=START - timedelta(hours=6),
    )
    grid = TimeGrid.from_window(START, END)
    q = WeatherQuery(lat=LAT, lon=LON, valid_from=START, valid_to=END)
    s = fx.weather_series(q, AsOf.at(START), grid)

    inside = grid.mid_unix() <= (START + timedelta(hours=3)).timestamp()
    assert s.covered is not None
    np.testing.assert_array_equal(s.covered, inside)
    assert np.allclose(s.cloud[inside], 0.8) and np.all(s.cloud[~inside] == 0.0)
    assert np.allclose(s.seeing[inside], 3.1)
    assert np.all(s.seeing[~inside] == base.ASSUMED_SEEING_ARCSEC)
    for name in ("humidity", "temperature_c"):
        arr = getattr(s, name)
        assert np.all(np.isfinite(arr[inside])) and np.all(np.isnan(arr[~inside])), name
    # Rain and gusts at 03:00 are over 02:00-03:00. Nothing says what falls or
    # blows after 03:00, so the covered hour after it has none of either.
    summed = grid.mid_unix() <= hours[-1].timestamp()
    for name in ("precipitation_mm", "wind_gust_ms"):
        arr = getattr(s, name)
        assert np.all(np.isfinite(arr[summed])) and np.all(np.isnan(arr[~summed])), name
    assert s.wind_ms is None, "a field no record carries is still None, not NaN"


def test_nan_and_out_of_range_slots_are_none() -> None:
    got = outlook._per_slot(np.array([1.234, np.nan, np.inf]), [0, 1, 2, 7], 2)
    assert got == [1.23, None, None, None]
    assert outlook._per_slot(None, [0, 1], 2) == [None, None]


# --------------------------------------------------------------------------
# through the API
# --------------------------------------------------------------------------


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., dict[str, Any]]]:
    """A server whose Open-Meteo is a fake and whose wall clock reads ``now``."""
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


def _plans(api: Callable[..., dict[str, Any]], sess: dict[str, Any]) -> list[dict[str, Any]]:
    client: TestClient = api.client  # type: ignore[attr-defined]
    return [
        client.get(f"/api/sessions/{sess['id']}/plan", params={"as_of": dp["at"]}).json()
        for dp in sess["decisionPoints"]
    ]


def _assert_aligned(plan: dict[str, Any]) -> None:
    sw = plan["slotWeather"]
    for name in (*SLOT_FIELDS, "covered"):
        assert len(sw[name]) == len(plan["slots"]), name
    assert sw["seeingForecast"] is False
    for i, ok in enumerate(sw["covered"]):
        if not ok:
            assert all(sw[name][i] is None for name in SLOT_FIELDS), i


def test_api_slot_weather_follows_the_run_each_plan_could_see(
    api: Callable[..., dict[str, Any]],
) -> None:
    """A night in progress: the dusk plan sees the 12z run, the 01:45 plan the
    18z run, and only the plan made at ``now`` sees the live forecast."""
    now = START + timedelta(hours=3, minutes=7)
    fake = FakeOpenMeteo()
    sess = api(fake, now, weather="auto")
    plans = _plans(api, sess)
    mids = [datetime.fromisoformat(t) for t in sess["grid"]["slotMids"]]
    assert len(plans) >= 3 and len(mids) == sess["grid"]["nSlots"]

    wanted = {"temperature_2m", "dew_point_2m", "precipitation"}
    for r in fake.requests:
        if not r.url.path.endswith("meta.json"):
            assert wanted <= set(r.url.params["hourly"].split(","))

    seen = set()
    for plan in plans:
        _assert_aligned(plan)
        sw = plan["slotWeather"]
        at = datetime.fromisoformat(plan["asOf"])
        assert sw["basis"] == plan["outlook"]["basis"] and sw["basis"]
        assert all(sw["covered"]), "every run here reaches the whole night"
        assert all(h is not None and 0.0 <= h <= 1.0 for h in sw["humidity"])
        # The source never gives gusts or mid cloud: None everywhere, always.
        assert sw["windGustMs"] == [None] * len(mids)
        assert sw["cloudMid"] == [None] * len(mids)

        if at < PUB_18Z:
            src, temp, dew = "12z", run_temp(RUN_12Z), run_temp(RUN_12Z) - 3.0
        elif at < now:
            src, temp, dew = "18z", run_temp(RUN_18Z), run_temp(RUN_18Z) - 3.0
        else:
            src, temp, dew = "live", float("nan"), float("nan")
        seen.add(src)

        if src != "live":
            assert sw["temperatureC"] == [pytest.approx(temp)] * len(mids), src
            assert sw["dewPointC"] == [pytest.approx(dew)] * len(mids), src
            assert sw["humidity"] == [pytest.approx(0.7)] * len(mids)
            assert sw["windMs"] == [pytest.approx(4.0)] * len(mids)
            assert sw["cloudLow"] == [pytest.approx(0.4)] * len(mids)
            assert sw["cloudHigh"] == [pytest.approx(0.2)] * len(mids)
            # No archived run gives precipitation, and the live forecast that
            # does is not published yet: nothing, not a dry night.
            assert sw["precipitationMm"] == [None] * len(mids), src
            assert f"run of 12 Sep {'12' if src == '12z' else '18'}:00 UTC" in sw["basis"]
            continue

        # The live forecast, value by value, on the slots it belongs to.
        for i, slot in enumerate(plan["slots"]):
            t = mids[slot["slot"]]
            assert sw["temperatureC"][i] == pytest.approx(live_temp(t), abs=0.006)
            assert sw["dewPointC"][i] == pytest.approx(live_temp(t) - 5.0, abs=0.006)
        assert sw["humidity"] == [pytest.approx(0.9)] * len(mids)
        precip = sw["precipitationMm"]
        assert all(p is not None for p in precip)
        wet = [mids[s["slot"]] for s, p in zip(plan["slots"], precip, strict=True) if p > 0.0]
        # Mid-hour placement: wet from 03:30 to 05:30, peaking at 04:30.
        assert min(wet) > RAIN_ENDS - timedelta(minutes=90)
        assert max(wet) < RAIN_ENDS + timedelta(minutes=30)
        rain_hour = [
            p
            for s, p in zip(plan["slots"], precip, strict=True)
            if RAIN_ENDS - timedelta(hours=1) < mids[s["slot"]] < RAIN_ENDS
        ]
        assert min(rain_hour) >= RAIN_MM / 2 and max(rain_hour) <= RAIN_MM
        assert sw["basis"].startswith("Live ECMWF IFS 0.25° forecast")

    assert seen == {"12z", "18z", "live"}


def test_api_synthetic_slot_weather_is_all_none(api: Callable[..., dict[str, Any]]) -> None:
    fake = FakeOpenMeteo()
    sess = api(fake, END + timedelta(days=1), weather="synthetic")
    assert fake.requests == []
    for plan in _plans(api, sess):
        _assert_aligned(plan)
        sw = plan["slotWeather"]
        for name in SLOT_FIELDS:
            assert sw[name] == [None] * len(plan["slots"]), name
        # Synthetic cloud IS its forecast: covered, though nothing else is given.
        assert all(sw["covered"])
        assert sw["basis"] == plan["outlook"]["basis"]
        assert sw["basis"].startswith("Synthetic demo run issued")


def test_api_no_forecast_slot_weather_is_all_none(api: Callable[..., dict[str, Any]]) -> None:
    fake = FakeOpenMeteo()
    sess = api(fake, START - timedelta(days=30), weather="auto")
    assert sess["weather"]["mode"] == "unavailable" and fake.requests == []
    for plan in _plans(api, sess):
        _assert_aligned(plan)
        sw = plan["slotWeather"]
        for name in SLOT_FIELDS:
            assert sw[name] == [None] * len(plan["slots"]), name
        assert not any(sw["covered"]), "no forecast covers any slot"
        assert sw["basis"] is None and plan["outlook"]["basis"] == ""
