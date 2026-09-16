"""The HTTP surface, exercised through ASGI -- no socket, no live server.

The first three tests are the ones that matter. They assert the three
properties the API exists to provide and that a refactor could plausibly break:

* a plan states the interval it is valid over, and consecutive intervals tile
  the night with no gap and no overlap -- this is what makes a scrub cheap;
* scrubbing backwards returns the *earlier* plan, not the newest one, and its
  evidence carries nothing published after its own as_of;
* re-planning never rewrites a slot that is already in the past.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from itertools import pairwise

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from tscheduler.api.app import create_app

pytest.importorskip("fastapi", reason="server tests need the api extra: uv sync --extra api")

NIGHT = "2026-09-13"


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(scope="module")
def session_id(client: TestClient) -> str:
    """One folded session, shared: the fold costs a couple of seconds."""
    r = client.post(
        "/api/sessions",
        json={"date": NIGHT, "hours": 8, "snrGoal": 40, "solveSeconds": 2.0},
    )
    assert r.status_code == 202, r.text
    sid: str = r.json()["id"]
    # TestClient runs background tasks to completion before returning, so the
    # session is ready here; assert it rather than sleeping on a poll.
    body = client.get(f"/api/sessions/{sid}").json()
    assert body["status"] == "ready", body.get("error") or body["message"]
    return sid


def test_health(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    # Credentials must never appear, only the boolean that says whether they exist.
    assert set(body["settings"]) == {
        "cache_dir",
        "host",
        "port",
        "solve_seconds",
        "spacetrack_configured",
    }
    assert isinstance(body["settings"]["spacetrack_configured"], bool)


def test_validity_intervals_tile_the_night(client: TestClient, session_id: str) -> None:
    """Consecutive plans must tile [start, end) exactly.

    A gap means some cursor position has no plan; an overlap means two plans
    claim the same instant and the client caches whichever it saw last.
    """
    sess = client.get(f"/api/sessions/{session_id}").json()
    n = len(sess["decisionPoints"])
    assert n >= 2, "fixture night should produce several decision points"

    spans = []
    for dp in sess["decisionPoints"]:
        p = client.get(f"/api/sessions/{session_id}/plan", params={"as_of": dp["at"]}).json()
        assert p["decisionIndex"] == dp["index"]
        spans.append((p["validFrom"], p["validUntil"]))

    assert spans[0][0] == sess["grid"]["start"]
    assert spans[-1][1] == sess["grid"]["end"]
    for (_, end), (start, _) in pairwise(spans):
        assert end == start, f"validity gap/overlap at {end} -> {start}"


def test_scrub_backwards_returns_the_earlier_plan(client: TestClient, session_id: str) -> None:
    """The load-bearing replay property, observed through the wire format.

    A plan served for an earlier cursor position must not carry evidence that
    was published later -- otherwise the slider is showing a plan nobody could
    have had at that moment.
    """
    sess = client.get(f"/api/sessions/{session_id}").json()
    dps = sess["decisionPoints"]
    late = client.get(f"/api/sessions/{session_id}/plan", params={"as_of": dps[-1]["at"]}).json()
    early = client.get(f"/api/sessions/{session_id}/plan", params={"as_of": dps[0]["at"]}).json()

    assert early["decisionIndex"] == 0
    assert late["decisionIndex"] == len(dps) - 1

    for p in (early, late):
        newest = p["evidence"]["newestPublished"]
        if newest is not None:
            assert datetime.fromisoformat(newest) <= datetime.fromisoformat(p["asOf"]), (
                f"plan at {p['asOf']} carries evidence published at {newest}"
            )


def test_the_past_is_never_rewritten(client: TestClient, session_id: str) -> None:
    """Re-planning touches the remaining night only.

    Checked two ways: the server's own count, and an independent slot-by-slot
    comparison against the previous plan, because a bug in the counter would
    otherwise make the guarantee self-certifying.
    """
    sess = client.get(f"/api/sessions/{session_id}").json()
    plans = [
        client.get(f"/api/sessions/{session_id}/plan", params={"as_of": dp["at"]}).json()
        for dp in sess["decisionPoints"]
    ]
    for prev, cur in pairwise(plans):
        assert cur["changes"]["pastSlotsRewritten"] == 0
        through = cur["changes"]["lockedThroughSlot"]
        assert through > 0, "a mid-night re-plan must have locked history"
        for s in range(through):
            assert cur["slots"][s]["targetId"] == prev["slots"][s]["targetId"], (
                f"slot {s} changed after it was already in the past"
            )
            assert cur["slots"][s]["locked"] is True


def test_cursor_outside_the_night_clamps(client: TestClient, session_id: str) -> None:
    """A slider must work at its own extremes."""
    sess = client.get(f"/api/sessions/{session_id}").json()
    start = datetime.fromisoformat(sess["grid"]["start"])
    before = (start - timedelta(days=2)).isoformat()
    after = (start + timedelta(days=2)).isoformat()

    assert (
        client.get(f"/api/sessions/{session_id}/plan", params={"as_of": before}).json()[
            "decisionIndex"
        ]
        == 0
    )
    last = len(sess["decisionPoints"]) - 1
    assert (
        client.get(f"/api/sessions/{session_id}/plan", params={"as_of": after}).json()[
            "decisionIndex"
        ]
        == last
    )


def test_blocks_carry_everything_the_card_renders(client: TestClient, session_id: str) -> None:
    """No client-side astronomy: the payload is complete on its own."""
    p = client.get(f"/api/sessions/{session_id}/plan").json()
    assert p["blocks"], "the fixture night should schedule something"
    for b in p["blocks"]:
        assert b["ra"].endswith("s") and ("h" in b["ra"])
        assert b["dec"][0] in "+-" and b["dec"].endswith('"')
        assert b["compass"] in {
            "N",
            "NNE",
            "NE",
            "ENE",
            "E",
            "ESE",
            "SE",
            "SSE",
            "S",
            "SSW",
            "SW",
            "WSW",
            "W",
            "WNW",
            "NW",
            "NNW",
        }
        assert b["altitudeMinDeg"] >= 30.0 - 1e-6, "scheduled below the altitude floor"
        assert b["airmassMax"] < 3.0
        assert b["nSubs"] >= 1
        assert b["expectedSnr"] > 0.0, "expected SNR must be a real number, not a placeholder"
        assert datetime.fromisoformat(b["integratingFrom"]) >= datetime.fromisoformat(b["startsAt"])


def test_json_carries_no_infinities(client: TestClient, session_id: str) -> None:
    """``airmass`` is inf below the horizon, which json.dumps emits as a bare
    ``Infinity`` that no compliant parser will read back."""
    raw = client.get(f"/api/sessions/{session_id}/grid").text
    assert "Infinity" not in raw and "NaN" not in raw


def test_grid_shapes_match_the_night(client: TestClient, session_id: str) -> None:
    sess = client.get(f"/api/sessions/{session_id}").json()
    n = sess["grid"]["nSlots"]
    g = client.get(f"/api/sessions/{session_id}/grid").json()
    assert len(g["rows"]) == len(sess["targets"])
    assert len(g["cloudFraction"]) == n
    for row in g["rows"]:
        for key in ("efficiency", "preference", "altitudeDeg", "airmass", "visible"):
            assert len(row[key]) == n, f"{row['targetId']}.{key} has wrong length"


def test_twilight_bands_cover_every_slot(client: TestClient, session_id: str) -> None:
    sess = client.get(f"/api/sessions/{session_id}").json()
    bands = sess["twilight"]
    assert bands[0]["fromSlot"] == 0
    assert bands[-1]["toSlot"] == sess["grid"]["nSlots"]
    for a, b in pairwise(bands):
        assert a["toSlot"] == b["fromSlot"]
        assert a["kind"] != b["kind"]


def test_presets_are_self_consistent(client: TestClient) -> None:
    body = client.get("/api/presets").json()
    ids = {e["id"] for e in body["catalog"]}
    assert set(body["defaultTargetIds"]) <= ids
    for e in body["equipment"]:
        # 206.265 * pixel / focal length, independently recomputed.
        expect = 206.264806247 * e["pixelSizeUm"] / e["focalLengthMm"]
        assert abs(e["pixelScaleArcsec"] - expect) < 1e-3
        assert abs(e["focalRatio"] - e["focalLengthMm"] / e["apertureMm"]) < 1e-2
    for s in body["sites"]:
        assert 17.0 < s["zenithSkyMagArcsec2"] < 22.1


def test_unknown_session_is_404(client: TestClient) -> None:
    assert client.get("/api/sessions/nope").status_code == 404
    assert client.get("/api/sessions/nope/plan").status_code == 404


def test_bad_request_bodies_are_rejected(client: TestClient) -> None:
    assert client.post("/api/sessions", json={"date": "not-a-date"}).status_code == 422
    assert (
        client.post("/api/sessions", json={"date": NIGHT, "equipmentId": "nope"}).status_code == 422
    )
    assert client.post("/api/sessions", json={"date": NIGHT, "siteId": "nope"}).status_code == 422
    assert client.post("/api/sessions", json={"date": NIGHT, "weather": "vibes"}).status_code == 422
    assert (
        client.post(
            "/api/sessions", json={"date": NIGHT, "targets": [{"id": "unknown-thing"}]}
        ).status_code
        == 422
    )


def test_bad_as_of_is_422_not_500(client: TestClient, session_id: str) -> None:
    r = client.get(f"/api/sessions/{session_id}/plan", params={"as_of": "yesterday"})
    assert r.status_code == 422
    assert "ISO-8601" in r.text


def test_session_lifecycle(client: TestClient) -> None:
    r = client.post("/api/sessions", json={"date": NIGHT, "hours": 3, "solveSeconds": 1.0})
    sid = r.json()["id"]
    assert any(s["id"] == sid for s in client.get("/api/sessions").json())
    assert client.delete(f"/api/sessions/{sid}").status_code == 204
    assert client.get(f"/api/sessions/{sid}").status_code == 404
    assert client.delete(f"/api/sessions/{sid}").status_code == 404


def test_naive_as_of_is_treated_as_utc(client: TestClient, session_id: str) -> None:
    sess = client.get(f"/api/sessions/{session_id}").json()
    at = sess["decisionPoints"][-1]["at"]
    aware = client.get(f"/api/sessions/{session_id}/plan", params={"as_of": at}).json()
    naive = client.get(
        f"/api/sessions/{session_id}/plan",
        params={"as_of": at.replace("Z", "").removesuffix("+00:00")},
    ).json()
    assert naive["decisionIndex"] == aware["decisionIndex"]


def test_custom_site_and_targets(client: TestClient) -> None:
    r = client.post(
        "/api/sessions",
        json={
            "date": NIGHT,
            "hours": 4,
            "solveSeconds": 1.0,
            "site": {
                "latitudeDeg": -30.24,
                "longitudeDeg": -70.74,
                "elevationM": 2400,
                "name": "La Silla",
                "bortle": 1,
                "extinctionK": 0.12,
            },
            "targets": [
                {
                    "id": "eta-car",
                    "name": "Eta Carinae Nebula",
                    "raDeg": 161.265,
                    "decDeg": -59.684,
                    "magnitude": 19.0,
                    "priority": 3.0,
                },
                {"id": "m31"},
            ],
            "snrGoal": 30,
        },
    )
    assert r.status_code == 202, r.text
    sid = r.json()["id"]
    sess = client.get(f"/api/sessions/{sid}").json()
    assert sess["status"] == "ready", sess.get("error")
    assert sess["site"]["name"] == "La Silla"
    names = {t["id"] for t in sess["targets"]}
    assert names == {"eta-car", "m31"}
    # M31 at Dec +41 from latitude -30 never clears 30 degrees.
    m31 = next(t for t in sess["targets"] if t["id"] == "m31")
    assert m31["visibleSlots"] == 0


@pytest.fixture(scope="module")
def live_server() -> Iterator[str]:
    """A real uvicorn on an ephemeral port.

    SSE cannot be tested through ``httpx.ASGITransport``: that transport awaits
    the whole ASGI call before handing back a response, so an endpoint that
    never finishes simply hangs. Binding a socket is the only way to exercise
    the thing the observer's browser will actually connect to.
    """
    config = uvicorn.Config(create_app(), host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 30.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not come up"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


@pytest.mark.slow
def test_sse_carries_notifications_not_data(live_server: str) -> None:
    """The stream must carry identifiers and status, never plan payloads.

    That is the whole reason live and replay can share one fetching path: the
    handler's only job is to invalidate a cache entry, so the push layer can be
    swapped for a poll without touching a component. A payload here would
    quietly create a second, divergent source of plan data.
    """
    seen: list[dict[str, object]] = []
    with (
        httpx.Client(base_url=live_server, timeout=120.0) as c,
        c.stream("GET", "/api/events") as stream,
    ):
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")

        created: list[httpx.Response] = []

        def create() -> None:
            with httpx.Client(base_url=live_server, timeout=120.0) as c2:
                created.append(
                    c2.post(
                        "/api/sessions",
                        json={"date": NIGHT, "hours": 3, "solveSeconds": 1.0},
                    )
                )

        poster = threading.Thread(target=create, daemon=True)
        poster.start()

        for line in stream.iter_lines():
            if not line.startswith("data:"):
                continue
            seen.append(json.loads(line[5:]))
            if seen[-1].get("type") in {"session.ready", "session.failed"}:
                break
        poster.join(timeout=30)

    assert created and created[0].status_code == 202
    kinds = [e.get("type") for e in seen]
    assert "session.ready" in kinds, kinds
    assert "plan.available" in kinds, kinds
    assert "session.failed" not in kinds

    allowed = {
        "type",
        "sessionId",
        "progress",
        "message",
        "decisionIndex",
        "at",
        "planId",
        "decisionPoints",
        "distinctPlans",
        "foldSeconds",
        "error",
        "ok",
    }
    for e in seen:
        assert set(e) <= allowed, f"stream leaked payload keys: {set(e) - allowed}"


def test_openapi_schema_is_generated(client: TestClient) -> None:
    """The frontend's types are generated from this, so a broken schema is a
    broken build, not a cosmetic problem."""
    spec = client.get("/openapi.json").json()
    assert "/api/sessions/{session_id}/plan" in spec["paths"]
    props = spec["components"]["schemas"]["PlanOut"]["properties"]
    assert {"validFrom", "validUntil", "planId", "decisionIndex"} <= set(props)
