"""``GET /api/site/locate``: the endpoint behind "use my location".

The rule it has to keep is that a lookup failure is not a request failure. The
browser has already answered the only question that matters -- where the
observer is -- and this endpoint only decorates that answer, so a geocoder
that is down must come back 200 with nulls. A 500 here would take a working
site off the form for no reason.

The lookups themselves are covered offline in ``tests/unit/test_geolocate.py``;
these tests replace them so the suite never touches the network.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from tscheduler.api import geolocate
from tscheduler.api.app import create_app
from tscheduler.api.geolocate import Place

pytest.importorskip("fastapi", reason="server tests need the api extra: uv sync --extra api")


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this module is allowed to open a socket."""
    monkeypatch.setattr(geolocate, "locate", lambda lat, lon: Place("Champaign, Illinois", 227.0))


def test_a_coordinate_comes_back_named_and_at_its_elevation(client: TestClient) -> None:
    r = client.get("/api/site/locate", params={"lat": 40.1164, "lon": -88.2434})
    assert r.status_code == 200
    body = r.json()
    assert body["latitudeDeg"] == 40.1164
    assert body["longitudeDeg"] == -88.2434
    assert body["name"] == "Champaign, Illinois"
    assert body["elevationM"] == 227.0
    assert "OpenStreetMap" in body["attribution"]


def test_a_failed_lookup_is_a_200_with_nulls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(geolocate, "locate", lambda lat, lon: Place())
    r = client.get("/api/site/locate", params={"lat": 51.4769, "lon": 0.0})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] is None
    assert body["elevationM"] is None
    # The half of the answer that never depended on a lookup is still exact.
    assert (body["latitudeDeg"], body["longitudeDeg"]) == (51.4769, 0.0)


def test_longitude_east_of_180_is_wrapped_like_every_other_endpoint(client: TestClient) -> None:
    r = client.get("/api/site/locate", params={"lat": 40.1164, "lon": 271.7566})
    assert r.status_code == 200
    assert r.json()["longitudeDeg"] == pytest.approx(-88.2434)


@pytest.mark.parametrize("params", [{"lat": 91, "lon": 0}, {"lat": 0, "lon": 400}, {"lat": 0}])
def test_a_coordinate_off_the_globe_is_a_422(client: TestClient, params: dict[str, float]) -> None:
    assert client.get("/api/site/locate", params=params).status_code == 422
