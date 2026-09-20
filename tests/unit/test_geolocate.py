"""The coordinate decorator: names, elevations, and every way they can fail.

The rule under test is that nothing here is load-bearing. A geocoder that is
down, rate-limiting, or answering with something unexpected must cost the
caller a ``None`` and nothing more -- never an exception, because the site is
the browser's coordinates and those arrived without any of this.
"""

from __future__ import annotations

import time

import httpx
import pytest

from tscheduler.api import geolocate
from tscheduler.api.geolocate import Place, locate


@pytest.fixture(autouse=True)
def _fresh_module() -> None:
    """No cache and no gap left to serve from the last test."""
    geolocate._CACHE.clear()
    geolocate._NAME_LAST = 0.0


def client_for(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def route(elevation: object = None, reverse: object = None, status: int = 200):
    """A transport answering the two services with whatever is given."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = elevation if "elevation" in request.url.path else reverse
        if body is None:
            return httpx.Response(404)
        return httpx.Response(status, json=body)

    return handler


def test_both_services_answer() -> None:
    with client_for(
        route(
            elevation={"elevation": [227.0]},
            reverse={"address": {"city": "Champaign", "state": "Illinois"}},
        )
    ) as c:
        assert locate(40.1164, -88.2434, client=c) == Place("Champaign, Illinois", 227.0)


def test_a_dead_geocoder_still_yields_an_elevation() -> None:
    with client_for(route(elevation={"elevation": [701.0]})) as c:
        assert locate(41.664, -77.8264, client=c) == Place(None, 701.0)


def test_coordinates_are_rounded_before_they_leave_this_process() -> None:
    """A ~10 cm fix of somebody's garden is not sent to two third parties to
    buy a town name and a 90 m DEM cell."""
    sent: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        body = (
            {"elevation": [227.0]}
            if "elevation" in request.url.path
            else {"address": {"city": "Champaign", "state": "Illinois"}}
        )
        return httpx.Response(200, json=body)

    with client_for(record) as c:
        locate(40.116402312, -88.243398714, client=c)
    assert sent, "nothing was requested"
    for url in sent:
        assert "40.116402312" not in url and "88.243398714" not in url
        assert "40.116" in url and "88.243" in url


def test_a_partial_answer_is_not_cached() -> None:
    """A service that was down for one second must not be down for the life of
    the process."""
    names: list[str | None] = [None, "Champaign, Illinois"]

    def flaky(request: httpx.Request) -> httpx.Response:
        if "elevation" in request.url.path:
            return httpx.Response(200, json={"elevation": [227.0]})
        name = names.pop(0) if names else "Champaign, Illinois"
        if name is None:
            return httpx.Response(429)
        return httpx.Response(
            200, json={"address": {"city": name.split(",")[0], "state": "Illinois"}}
        )

    with client_for(flaky) as c:
        assert locate(40.1164, -88.2434, client=c) == Place(None, 227.0)
        geolocate._NAME_LAST = 0.0
        assert locate(40.1164, -88.2434, client=c) == Place("Champaign, Illinois", 227.0)


def test_a_second_caller_inside_the_gap_goes_without_a_name() -> None:
    """Nominatim allows one request a second. The second caller degrades to
    coordinates rather than queueing on a request thread or breaking the
    policy."""
    geolocate._NAME_LAST = time.monotonic()
    held = geolocate._NAME_TURN.acquire(blocking=False)
    assert held
    try:
        with client_for(
            route(
                elevation={"elevation": [227.0]},
                reverse={"address": {"city": "Champaign", "state": "Illinois"}},
            )
        ) as c:
            assert locate(40.1164, -88.2434, client=c) == Place(None, 227.0)
    finally:
        geolocate._NAME_TURN.release()


def test_a_dead_elevation_service_still_yields_a_name() -> None:
    with client_for(
        route(reverse={"address": {"town": "Coudersport", "state": "Pennsylvania"}})
    ) as c:
        assert locate(41.664, -77.8264, client=c) == Place("Coudersport, Pennsylvania", None)


def test_everything_down_is_an_empty_place_not_an_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with client_for(refuse) as c:
        assert locate(0.0, 0.0, client=c) == Place()


def test_rate_limited_is_an_empty_place() -> None:
    with client_for(route(elevation={"elevation": [1.0]}, reverse={"x": 1}, status=429)) as c:
        assert locate(0.0, 0.0, client=c) == Place()


def test_nonsense_payloads_are_ignored() -> None:
    with client_for(route(elevation={"elevation": "high"}, reverse={"address": []})) as c:
        assert locate(0.0, 0.0, client=c) == Place()


def test_display_name_is_the_fallback_when_there_are_no_address_parts() -> None:
    with client_for(route(reverse={"display_name": "Somewhere, Antarctica"})) as c:
        assert locate(-75.0, 0.0, client=c).name == "Somewhere, Antarctica"


def test_a_name_is_at_most_two_parts_and_never_repeats_one() -> None:
    address = {
        "city": "Singapore",
        "county": "Singapore",
        "state": "Singapore",
        "country": "Singapore",
    }
    assert geolocate._name_from(address) == "Singapore"


def test_a_place_with_no_town_falls_back_to_its_county() -> None:
    address = {"county": "Hawaii County", "state": "Hawaii", "country": "United States"}
    assert geolocate._name_from(address) == "Hawaii County, Hawaii"


def test_an_answer_is_cached_by_rounded_coordinate() -> None:
    calls = 0

    def count(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = (
            {"elevation": [227.0]}
            if "elevation" in request.url.path
            else {"address": {"city": "Champaign", "state": "Illinois"}}
        )
        return httpx.Response(200, json=body)

    with client_for(count) as c:
        locate(40.11641, -88.24339, client=c)
        first = calls
        # Four metres away: the same key, so no second round trip.
        locate(40.11644, -88.24341, client=c)
        assert calls == first


def test_a_total_failure_is_not_cached() -> None:
    """Otherwise "the wifi was not up yet" becomes permanent for the process."""
    calls = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("down")

    with client_for(flaky) as c:
        locate(1.0, 2.0, client=c)
        before = calls
        locate(1.0, 2.0, client=c)
        assert calls > before


@pytest.mark.network
def test_the_real_services_name_a_real_place() -> None:
    place = locate(40.1164, -88.2434)
    assert place.name is not None
    assert place.elevation_m is not None
    assert 150.0 < place.elevation_m < 320.0
