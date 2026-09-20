"""Alerts carry a category, so the UI can say WHAT happened before it says how much.

A cloud bank, a short frame count and the Moon can all arrive at severity
"warn", and they call for different reactions. The frontend colours and icons
an alert by these fields; if one goes missing it silently falls back to a
generic style, which is the regression these tests exist to catch.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

pytest.importorskip("fastapi", reason="server tests need the api extra: uv sync --extra api")

from fastapi.testclient import TestClient

from tscheduler.api.app import create_app
from tscheduler.api.mappers import _block_warnings

NIGHT = "2026-09-13"


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def test_each_hazard_has_its_own_category() -> None:
    got = _block_warnings(alt_min=31.0, mean_cloud=0.6, moon_sep_min=20.0, n_subs=4, min_alt=30.0)
    assert {w.category: w.severity for w in got} == {
        "exposure": "critical",
        "horizon": "warn",
        "weather": "warn",
        "moon": "info",
    }


def test_a_clear_block_raises_nothing() -> None:
    assert (
        _block_warnings(alt_min=60.0, mean_cloud=0.0, moon_sep_min=90.0, n_subs=40, min_alt=30.0)
        == []
    )


def test_decision_points_and_warnings_are_categorised(client: TestClient) -> None:
    r = client.post(
        "/api/sessions",
        json={
            "date": NIGHT,
            "hours": 8,
            "snrGoal": 40,
            "solveSeconds": 2.0,
            "weather": "synthetic",
        },
    )
    assert r.status_code == 202, r.text
    sid = r.json()["id"]
    sess = client.get(f"/api/sessions/{sid}").json()
    assert sess["status"] == "ready", sess.get("error") or sess["message"]

    dps = sess["decisionPoints"]
    assert len(dps) > 1, "the synthetic night should carry at least one forecast update"
    assert dps[0]["kind"] == "start"
    assert {dp["kind"] for dp in dps[1:]} == {"weather"}

    for dp in dps:
        plan = client.get(f"/api/sessions/{sid}/plan", params={"as_of": dp["at"]}).json()
        for block in plan["blocks"]:
            for w in block["warnings"]:
                assert w["category"] in {"weather", "moon", "horizon", "exposure"}, w
