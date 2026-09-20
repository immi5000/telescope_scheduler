"""Request edges that must come back as a clear 422 or the right answer, never
a 500 or a silently different request.

Each of these was found by review against the running server: a NaN that the
default 422 could not serialise, a longitude east of 180 that planned the night
before, two spellings of one object becoming two targets, and a custom target
silently dropped for sharing an id.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from tscheduler.api import schemas
from tscheduler.api.app import _resolve_targets, create_app

pytest.importorskip("fastapi", reason="server tests need the api extra: uv sync --extra api")


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def test_nan_in_a_request_is_a_422_not_a_500(client: TestClient) -> None:
    body = (
        '{"optics": {"apertureMm": NaN, "focalLengthMm": 2032},'
        ' "camera": {"pixelSizeUm": 3.76, "sensorWidthPx": 6248, "sensorHeightPx": 4176,'
        ' "quantumEfficiency": 0.8, "readNoiseE": 1.5, "darkCurrentEPerS": 0.001}}'
    )
    r = client.post(
        "/api/equipment/derive", content=body, headers={"content-type": "application/json"}
    )
    assert r.status_code == 422
    assert r.json()["detail"], r.text


def test_the_catalogue_notice_is_served(client: TestClient) -> None:
    r = client.get("/api/catalog/notice")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "CC BY-SA 4.0" in r.text or "creativecommons.org/licenses/by-sa/4.0" in r.text


def test_longitude_east_of_180_is_the_same_place(client: TestClient) -> None:
    west = client.get(
        "/api/night-window", params={"date": "2026-09-18", "lat": 40.1164, "lon": -88.2434}
    ).json()
    east = client.get(
        "/api/night-window", params={"date": "2026-09-18", "lat": 40.1164, "lon": 271.7566}
    ).json()
    assert east["dusk"] == west["dusk"]
    assert east["utcOffsetHours"] == pytest.approx(west["utcOffsetHours"])


def test_a_site_request_wraps_its_longitude() -> None:
    site = schemas.SiteRequest(latitudeDeg=40.0, longitudeDeg=271.75)
    assert site.longitude_deg == pytest.approx(-88.25)
    assert schemas.SiteRequest(latitudeDeg=0, longitudeDeg=180.0).longitude_deg == -180.0


def test_effective_focal_length_must_exceed_twenty_mm() -> None:
    with pytest.raises(ValueError, match="effective focal length"):
        schemas.OpticsRequest(apertureMm=50, focalLengthMm=30, reducer=0.5)


def _request(targets: list[dict[str, object]]) -> schemas.SessionRequest:
    return schemas.SessionRequest.model_validate({"date": "2026-09-18", "targets": targets})


def test_two_spellings_of_one_object_are_one_target_asking_the_most() -> None:
    out = _resolve_targets(
        _request(
            [
                {"id": "n7000", "priority": 1.0, "snrGoal": 20},
                {"id": "NGC 7000", "priority": 5.0, "snrGoal": 30},
            ]
        )
    )
    assert [t.id for t in out] == ["ngc7000"]
    assert out[0].priority == 5.0
    assert out[0].snr_goal == 30


def test_a_custom_target_sharing_an_id_is_refused_not_dropped() -> None:
    from fastapi import HTTPException

    custom = {"id": "a", "raDeg": 10.0, "decDeg": 41.0, "magnitude": 22.0}
    other = {"id": "a", "raDeg": 200.0, "decDeg": -10.0, "magnitude": 21.0}
    with pytest.raises(HTTPException) as err:
        _resolve_targets(_request([custom, other]))
    assert err.value.status_code == 422
    assert "unique id" in str(err.value.detail)


def test_an_unplannable_catalogue_object_says_why() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as err:
        _resolve_targets(_request([{"id": "c14"}]))
    assert err.value.status_code == 422
    assert "cannot be planned" in str(err.value.detail)


def test_a_modified_part_keeps_the_label_the_form_gave_it(client: TestClient) -> None:
    body = {
        "telescopeName": "Celestron C8 SCT (modified)",
        "cameraName": "ZWO ASI2600MM Pro (modified)",
        "optics": {"apertureMm": 203.2, "focalLengthMm": 2000, "centralObstructionMm": 64},
        "camera": {
            "pixelSizeUm": 3.76,
            "sensorWidthPx": 6248,
            "sensorHeightPx": 4176,
            "quantumEfficiency": 0.8,
            "readNoiseE": 1.5,
            "darkCurrentEPerS": 0.0005,
        },
    }
    out = client.post("/api/equipment/derive", json=body).json()
    assert out["telescopeName"] == "Celestron C8 SCT (modified)"
    assert out["cameraName"] == "ZWO ASI2600MM Pro (modified)"
    assert out["telescopeId"] is None and out["cameraId"] is None


def test_a_preset_part_is_named_from_the_preset_not_the_label(client: TestClient) -> None:
    presets = client.get("/api/presets").json()
    tel = next(t for t in presets["telescopes"] if t["id"] == presets["defaultTelescopeId"])
    body = {
        "telescopeId": tel["id"],
        "telescopeName": "something else",
        "optics": {
            "apertureMm": tel["apertureMm"],
            "focalLengthMm": tel["focalLengthMm"],
            "centralObstructionMm": tel["centralObstructionMm"],
        },
        "camera": {
            "pixelSizeUm": 3.76,
            "sensorWidthPx": 6248,
            "sensorHeightPx": 4176,
            "quantumEfficiency": 0.8,
            "readNoiseE": 1.5,
            "darkCurrentEPerS": 0.0005,
        },
    }
    out = client.post("/api/equipment/derive", json=body).json()
    assert out["telescopeName"] == tel["name"]


def test_a_session_is_named_for_its_local_night(client: TestClient) -> None:
    # Urbana, the night of 17 Sep: it starts at 01:30 UTC on the 18th.
    body = {
        "date": "2026-09-18",
        "startHourUtc": 1.5,
        "hours": 1,
        "siteId": "urbana",
        "weather": "synthetic",
        "solveSeconds": 1,
    }
    created = client.post("/api/sessions", json=body)
    assert created.status_code in (200, 202), created.text
    assert created.json()["name"].endswith("night of 2026-09-17")
