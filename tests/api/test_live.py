"""A night that is still happening: the fold stops at the wall clock, and the
watch extends it only when data actually arrives.

The properties that matter, each pinned by a test below:

* tonight's first fold contains no decision point after the wall clock, even
  from a source (the synthetic demo) that schedules publications in advance;
* a check that finds nothing new changes nothing -- no decision point, no
  revision -- so a quiet night does not churn;
* when something new arrives, the decision point is stamped when the server
  LEARNED it, and every slot before that instant is left exactly as it was;
* before dusk the opening plan is re-made in place instead of appended to;
* a failed check (HTTP 429) keeps the last good forecast;
* after dawn the watch stops, and a night that was over when created is not
  watched at all.

The wall clock is ``AsOf.live``, pinned per test; Open-Meteo is a fake.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tscheduler.api.app import create_app
from tscheduler.config import Settings
from tscheduler.core.clock import AsOf, Vantage
from tscheduler.providers.weather import auto

pytest.importorskip("fastapi", reason="server tests need the api extra: uv sync --extra api")

NIGHT = "2026-09-13"
START = datetime(2026, 9, 13, 1, 0, tzinfo=UTC)
END = START + timedelta(hours=8)
BODY = {"date": NIGHT, "hours": 8, "solveSeconds": 1.0}


# --------------------------------------------------------------------------
# a controllable wall clock and weather service
# --------------------------------------------------------------------------


@dataclass
class Clock:
    t: datetime

    def advance(self, **kw: float) -> datetime:
        self.t += timedelta(**kw)
        return self.t


@dataclass
class FakeOpenMeteo:
    """meta.json, archived single runs and the live forecast, with a log."""

    meta_init: datetime = datetime(2026, 9, 12, 18, tzinfo=UTC)
    meta_avail: datetime = datetime(2026, 9, 13, 1, 6, tzinfo=UTC)
    live_cloud: float = 90.0
    fail: Callable[[httpx.Request], httpx.Response | None] | None = None
    requests: list[httpx.Request] = field(default_factory=list)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def live_requests(self) -> list[httpx.Request]:
        return [
            r for r in self.requests if r.url.path == "/v1/forecast" and "run" not in r.url.params
        ]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail is not None and (failed := self.fail(request)) is not None:
            return failed
        if request.url.path.endswith("meta.json"):
            return httpx.Response(
                200,
                json={
                    "last_run_initialisation_time": self.meta_init.timestamp(),
                    "last_run_availability_time": self.meta_avail.timestamp(),
                    "data_end_time": (self.meta_init + timedelta(days=15)).timestamp(),
                },
            )
        params = request.url.params
        if "run" in params:
            run = datetime.fromisoformat(params["run"]).replace(tzinfo=UTC)
            hours = [run + timedelta(hours=h) for h in range(240)]
            return httpx.Response(200, json=_hourly(hours, 20.0))
        first = datetime.fromisoformat(params["start_date"]).replace(tzinfo=UTC)
        last = datetime.fromisoformat(params["end_date"]).replace(tzinfo=UTC)
        n = int((last - first).total_seconds() // 3600) + 24
        return httpx.Response(
            200, json=_hourly([first + timedelta(hours=h) for h in range(n)], self.live_cloud)
        )


def _hourly(hours: list[datetime], cloud: float) -> dict[str, Any]:
    n = len(hours)
    return {
        "hourly": {
            "time": [t.strftime("%Y-%m-%dT%H:%M") for t in hours],
            "cloud_cover": [cloud] * n,
            "cloud_cover_low": [0.0] * n,
            "cloud_cover_mid": [0.0] * n,
            "cloud_cover_high": [cloud] * n,
            "relative_humidity_2m": [70.0] * n,
            "dew_point_2m": [8.0] * n,
            "temperature_2m": [14.0] * n,
            "wind_speed_10m": [3.0] * n,
            "wind_gusts_10m": [6.0] * n,
        }
    }


@dataclass
class Harness:
    client: TestClient
    clock: Clock
    weather: FakeOpenMeteo

    def create(self, **body: Any) -> dict[str, Any]:
        r = self.client.post("/api/sessions", json={**BODY, **body})
        assert r.status_code == 202, r.text
        sess = self.get(r.json()["id"])
        assert sess["status"] == "ready", sess.get("error") or sess["message"]
        return sess

    def get(self, sid: str) -> dict[str, Any]:
        body: dict[str, Any] = self.client.get(f"/api/sessions/{sid}").json()
        return body

    def refresh(self, sid: str) -> httpx.Response:
        return self.client.post(f"/api/sessions/{sid}/refresh")

    def plan(self, sid: str, at: str) -> dict[str, Any]:
        body: dict[str, Any] = self.client.get(
            f"/api/sessions/{sid}/plan", params={"as_of": at}
        ).json()
        return body


def _harness(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[Harness]:
    auto.clear_caches()
    clock = Clock(START)
    weather = FakeOpenMeteo()
    monkeypatch.setattr(auto, "client_factory", weather.client)
    monkeypatch.setattr(AsOf, "live", classmethod(lambda cls: cls(clock.t, Vantage.LIVE)))
    with TestClient(create_app(settings)) as client:
        yield Harness(client, clock, weather)
    auto.clear_caches()


@pytest.fixture
def h(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    """Scheduled checks off: each test drives the watch through the refresh route."""
    yield from _harness(monkeypatch, Settings(live_refresh_minutes=0))


def _at(dp: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(dp["at"])


# --------------------------------------------------------------------------
# which nights are live
# --------------------------------------------------------------------------


def test_a_night_that_was_over_is_a_replay_and_is_not_watched(h: Harness) -> None:
    h.clock.t = END + timedelta(days=1)
    sess = h.create(weather="synthetic")
    assert sess["live"] is None
    r = h.refresh(sess["id"])
    assert r.status_code == 409
    assert "replay" in r.json()["detail"]


def test_tonights_fold_stops_at_the_wall_clock(h: Harness) -> None:
    """The synthetic source schedules runs at dusk+2h, +4h and +6h. At 04:00
    only the first of those has happened, so only it may be a decision point."""
    h.clock.t = END + timedelta(days=1)
    replay = h.create(weather="synthetic")

    h.clock.t = START + timedelta(hours=3)
    live = h.create(weather="synthetic")

    assert all(_at(dp) <= h.clock.t for dp in live["decisionPoints"])
    assert any(_at(dp) > h.clock.t for dp in replay["decisionPoints"]), "fixture sanity"
    assert [dp["at"] for dp in live["decisionPoints"]] == [
        dp["at"] for dp in replay["decisionPoints"] if _at(dp) <= h.clock.t
    ]
    assert live["live"]["following"] is True
    assert live["live"]["checks"] == 0 and live["live"]["revision"] == 0


# --------------------------------------------------------------------------
# the watch
# --------------------------------------------------------------------------


def test_a_check_that_finds_nothing_changes_nothing(h: Harness) -> None:
    h.clock.t = START + timedelta(hours=3)
    before = h.create(weather="synthetic")
    h.clock.advance(minutes=20)

    r = h.refresh(before["id"])
    assert r.status_code == 200, r.text
    after = r.json()
    assert after["decisionPoints"] == before["decisionPoints"]
    live = after["live"]
    assert live["checks"] == 1 and live["updates"] == 0 and live["revision"] == 0
    assert datetime.fromisoformat(live["lastCheckedAt"]) == h.clock.t
    assert "nothing new" in live["lastResult"]


def test_new_data_is_stamped_when_learned_and_never_rewrites_the_past(h: Harness) -> None:
    h.clock.t = START + timedelta(hours=3)
    sess = h.create(weather="synthetic")
    sid = sess["id"]
    last_before = sess["decisionPoints"][-1]

    # The +4h synthetic run publishes at 05:00; the watch notices at 05:10.
    learned = h.clock.advance(hours=1, minutes=10)
    r = h.refresh(sid)
    assert r.status_code == 200, r.text
    after = r.json()

    dps = after["decisionPoints"]
    assert len(dps) == len(sess["decisionPoints"]) + 1
    new = dps[-1]
    assert _at(new) == learned, "stamped when the server learned it, not when published"
    assert datetime.fromisoformat(new["newestPublished"]) == START + timedelta(hours=4)
    live = after["live"]
    assert live["updates"] == 1 and live["revision"] == 1
    assert live["lastResult"].startswith("new data 05:10 UTC")

    # Every slot before the one containing 05:10 is what the observer was
    # already told.
    old = h.plan(sid, last_before["at"])
    cur = h.plan(sid, new["at"])
    assert cur["decisionIndex"] == new["index"]
    locked = int((learned - START).total_seconds() // (after["grid"]["slotSeconds"]))
    assert [s["targetId"] for s in cur["slots"][:locked]] == [
        s["targetId"] for s in old["slots"][:locked]
    ]
    # And its evidence is still gated at its own instant.
    assert datetime.fromisoformat(cur["evidence"]["newestPublished"]) <= learned


def test_manual_checks_are_rate_limited(h: Harness) -> None:
    h.clock.t = START + timedelta(hours=3)
    sid = h.create(weather="synthetic")["id"]
    assert h.refresh(sid).status_code == 200
    h.clock.advance(seconds=20)
    r = h.refresh(sid)
    assert r.status_code == 429
    assert h.get(sid)["live"]["checks"] == 1, "a refused check is not a check"


def test_after_dawn_the_watch_stops(h: Harness) -> None:
    h.clock.t = START + timedelta(hours=7)
    sess = h.create(weather="synthetic")
    h.clock.t = END + timedelta(minutes=1)
    after = h.refresh(sess["id"]).json()
    assert after["live"]["following"] is False
    assert after["live"]["nextCheckAt"] is None
    assert after["decisionPoints"] == sess["decisionPoints"]


# --------------------------------------------------------------------------
# with Open-Meteo
# --------------------------------------------------------------------------


def test_the_live_forecast_is_refetched_only_when_a_newer_run_exists(h: Harness) -> None:
    h.clock.t = START + timedelta(hours=3, minutes=7)
    sess = h.create(weather="auto")
    sid = sess["id"]
    assert sess["weather"]["mode"] == "forecast"
    assert _at(sess["decisionPoints"][-1]) == h.clock.t
    n_live = len(h.weather.live_requests())

    # Same run as at creation: one meta.json request, and nothing else.
    h.clock.advance(minutes=20)
    sent = len(h.weather.requests)
    quiet = h.refresh(sid).json()
    assert [r.url.path for r in h.weather.requests[sent:]] == [
        "/data/ecmwf_ifs025/static/meta.json"
    ]
    assert quiet["decisionPoints"] == sess["decisionPoints"]
    assert "no new ECMWF IFS 0.25° run since the 12 Sep 18Z run" in quiet["live"]["lastResult"]

    # The 00Z run lands; the next check fetches it and re-plans on it.
    h.weather.meta_init = datetime(2026, 9, 13, 0, tzinfo=UTC)
    h.weather.meta_avail = h.clock.t + timedelta(minutes=5)
    h.weather.live_cloud = 5.0
    learned = h.clock.advance(minutes=15)
    fresh = h.refresh(sid).json()

    assert len(h.weather.live_requests()) == n_live + 1
    dps = fresh["decisionPoints"]
    assert len(dps) == len(sess["decisionPoints"]) + 1
    assert _at(dps[-1]) == learned
    assert dps[-1]["reason"] == f"live forecast fetched {learned:%H:%M} UTC"
    assert datetime.fromisoformat(fresh["weather"]["fetchedAt"]) == learned
    assert fresh["live"]["updates"] == 1

    old = h.plan(sid, dps[-2]["at"])["outlook"]
    new = h.plan(sid, dps[-1]["at"])["outlook"]
    assert old["meanCloudFraction"] == pytest.approx(0.9)
    assert new["meanCloudFraction"] == pytest.approx(0.05)
    # Each plan names the run IT was built on. The refresh replaced the
    # session's report; the earlier plan must not be relabelled with 00Z.
    assert "newest model run 12 Sep 18:00 UTC" in old["basis"], old["basis"]
    assert "newest model run 13 Sep 00:00 UTC" in new["basis"], new["basis"]


def test_a_failed_check_keeps_the_last_good_forecast(h: Harness) -> None:
    h.clock.t = START + timedelta(hours=3)
    sess = h.create(weather="auto")
    h.weather.fail = lambda r: httpx.Response(429)
    h.clock.advance(minutes=20)
    after = h.refresh(sess["id"]).json()
    assert after["live"]["lastError"]
    assert after["live"]["updates"] == 0
    assert after["decisionPoints"] == sess["decisionPoints"]
    assert after["weather"] == sess["weather"]


def test_before_dusk_a_newer_run_remakes_the_opening_plan(h: Harness) -> None:
    h.clock.t = START - timedelta(hours=5)
    h.weather.meta_init = datetime(2026, 9, 12, 12, tzinfo=UTC)
    h.weather.meta_avail = datetime(2026, 9, 12, 19, 6, tzinfo=UTC)
    sess = h.create(weather="auto")
    sid = sess["id"]
    assert len(sess["decisionPoints"]) == 1

    h.weather.meta_init = datetime(2026, 9, 12, 18, tzinfo=UTC)
    h.weather.meta_avail = datetime(2026, 9, 12, 22, 30, tzinfo=UTC)
    learned = h.clock.advance(hours=3)
    after = h.refresh(sid).json()

    dps = after["decisionPoints"]
    assert len(dps) == 1, "nobody has been handed a plan yet: re-make it, do not append"
    assert dps[0]["at"] == sess["decisionPoints"][0]["at"]
    assert dps[0]["reason"].startswith("opening plan, re-made on the forecast fetched")
    assert after["live"]["revision"] == 1
    plan = h.plan(sid, dps[0]["at"])
    assert datetime.fromisoformat(plan["evidence"]["newestPublished"]) == learned


def test_an_archive_source_live_night_picks_up_newer_runs(h: Harness) -> None:
    """``weather=open_meteo`` makes no live fetch at all; its checks must still
    work, on archived runs as they are published."""
    h.clock.t = START + timedelta(hours=1)
    sess = h.create(weather="open_meteo")
    sid = sess["id"]
    assert sess["live"]["following"] is True

    # Nothing newer than the 12Z run yet.
    h.clock.advance(minutes=30)
    quiet = h.refresh(sid).json()
    assert quiet["live"]["lastError"] is None
    assert quiet["decisionPoints"] == sess["decisionPoints"]

    # The 18Z run is published at 03:00: 18Z + ECMWF IFS 0.25's 9.0 h lag
    # floor, which overrides the fake's measured 7.1 h (7.75 h with margin).
    learned = h.clock.t = START + timedelta(hours=2, minutes=10)
    after = h.refresh(sid).json()
    assert after["live"]["lastError"] is None, after["live"]
    dps = after["decisionPoints"]
    assert len(dps) == len(sess["decisionPoints"]) + 1
    assert _at(dps[-1]) == learned
    assert after["live"]["lastResult"].startswith(f"new data {learned:%H:%M} UTC")

    # And the same run is not "new" again at the next check.
    h.clock.advance(minutes=20)
    again = h.refresh(sid).json()
    assert again["decisionPoints"] == dps
    assert again["live"]["updates"] == 1


def test_before_dusk_a_scheduled_source_is_gated_at_the_wall_clock(h: Harness) -> None:
    """The synthetic demo 'publishes' a run at dusk-1h (00:00). At 20:00 the
    opening plan must not contain it; the watch brings it in once it exists."""
    h.clock.t = START - timedelta(hours=5)
    sess = h.create(weather="synthetic")
    sid = sess["id"]
    dp0 = sess["decisionPoints"][0]
    assert datetime.fromisoformat(dp0["newestPublished"]) <= h.clock.t

    h.clock.t = START - timedelta(minutes=50)
    after = h.refresh(sid).json()
    dps = after["decisionPoints"]
    assert len(dps) == 1
    assert dps[0]["reason"].startswith("opening plan, re-made on data published by")
    assert datetime.fromisoformat(dps[0]["newestPublished"]) == START - timedelta(hours=1)
    assert after["live"]["revision"] == 1


def test_a_run_landing_during_the_first_fetch_is_refetched_once(h: Harness) -> None:
    """meta.json is read after the live fetch. If it shows a run that became
    available after `now`, the data may be from the run before it, so the
    watch must not treat that run as held."""
    h.clock.t = START + timedelta(hours=3)
    h.weather.meta_init = datetime(2026, 9, 13, 0, tzinfo=UTC)
    h.weather.meta_avail = h.clock.t + timedelta(seconds=30)
    sess = h.create(weather="auto")
    sid = sess["id"]
    n_live = len(h.weather.live_requests())

    h.clock.advance(minutes=20)
    first = h.refresh(sid).json()
    assert len(h.weather.live_requests()) == n_live + 1, "refetched despite the same meta.init"
    assert first["live"]["updates"] == 1

    h.clock.advance(minutes=20)
    second = h.refresh(sid).json()
    assert len(h.weather.live_requests()) == n_live + 1, "and only once"
    assert second["live"]["updates"] == 1


def test_an_unexpected_failure_is_reported_not_raised(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tscheduler.api import live

    h.clock.t = START + timedelta(hours=3)
    sid = h.create(weather="synthetic")["id"]

    def boom(*a: Any, **k: Any) -> Any:
        raise ValueError("malformed payload")

    monkeypatch.setattr(live, "_check", boom)
    r = h.refresh(sid)
    assert r.status_code == 200, r.text
    w = r.json()["live"]
    assert w["lastError"] == "ValueError: malformed payload"
    assert w["lastResult"] == "the check failed unexpectedly"
    assert w["following"] is True


# --------------------------------------------------------------------------
# the scheduled watch
# --------------------------------------------------------------------------


@pytest.fixture
def ticking(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    """Scheduled checks every ~0.3 s, so the background task can be observed."""
    yield from _harness(monkeypatch, Settings(live_refresh_minutes=0.005))


def test_the_watch_runs_on_its_own_and_stops_with_the_session(ticking: Harness) -> None:
    ticking.clock.t = START + timedelta(hours=3)
    sess = ticking.create(weather="synthetic")
    sid = sess["id"]
    ticking.clock.advance(hours=1, minutes=10)

    deadline = time.monotonic() + 15.0
    body = ticking.get(sid)
    while body["live"]["updates"] == 0 and time.monotonic() < deadline:
        time.sleep(0.1)
        body = ticking.get(sid)
    assert body["live"]["updates"] == 1, body["live"]
    assert len(body["decisionPoints"]) == len(sess["decisionPoints"]) + 1

    # A deleted session's watch is cancelled, not left polling.
    assert ticking.client.delete(f"/api/sessions/{sid}").status_code == 204


def test_the_watch_survives_a_failed_check(
    ticking: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tscheduler.api import live

    real = live._check
    calls = {"n": 0}

    def flaky(*a: Any, **k: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("solver fell over")
        return real(*a, **k)

    monkeypatch.setattr(live, "_check", flaky)
    ticking.clock.t = START + timedelta(hours=3)
    sid = ticking.create(weather="synthetic")["id"]
    ticking.clock.advance(hours=1, minutes=10)

    deadline = time.monotonic() + 15.0
    body = ticking.get(sid)
    while body["live"]["updates"] == 0 and time.monotonic() < deadline:
        time.sleep(0.1)
        body = ticking.get(sid)
    assert calls["n"] >= 2, "the loop kept going after the failure"
    assert body["live"]["updates"] == 1, body["live"]
