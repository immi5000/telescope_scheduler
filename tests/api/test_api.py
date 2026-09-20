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
from pathlib import Path

import httpx
import numpy as np
import pytest
import uvicorn
from fastapi.testclient import TestClient

from tscheduler.api.app import create_app

pytest.importorskip("fastapi", reason="server tests need the api extra: uv sync --extra api")

NIGHT = "2026-09-13"

OFFLINE = "synthetic"
"""Every session these tests fold uses the deterministic offline weather. The
API's own default is ``auto`` -- real data -- which would make the suite depend
on a third-party service being up, and its numbers on what that service said."""


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(scope="module")
def session_id(client: TestClient) -> str:
    """One folded session, shared: the fold costs a couple of seconds."""
    r = client.post(
        "/api/sessions",
        json={"date": NIGHT, "hours": 8, "snrGoal": 40, "solveSeconds": 2.0, "weather": OFFLINE},
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
        "live_refresh_minutes",
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
    """The two halves of the wire agree on the night's shape.

    Geometry and conditions are served from different endpoints now, so the
    thing worth asserting is that they still describe the same grid -- a
    mismatch here would have the renderer indexing one array with another's
    slot number, which draws a plausible sky at the wrong time.
    """
    sess = client.get(f"/api/sessions/{session_id}").json()
    n = sess["grid"]["nSlots"]

    g = client.get(f"/api/sessions/{session_id}/grid").json()
    assert len(g["rows"]) == len(sess["targets"])
    assert len(g["cloudFraction"]) == n
    for row in g["rows"]:
        for key in ("efficiency", "preference", "skyMagArcsec2"):
            assert len(row[key]) == n, f"{row['targetId']}.{key} has wrong length"
        for name, arr in row["skyComponentsNl"].items():
            assert len(arr) == n, f"{row['targetId']}.sky.{name} has wrong length"
        for name, arr in row["preferenceFactors"].items():
            assert len(arr) == n, f"{row['targetId']}.pref.{name} has wrong length"

    geo = client.get(f"/api/sessions/{session_id}/geometry").json()
    assert len(geo["rows"]) == len(g["rows"]) == len(sess["targets"])
    assert [r["targetId"] for r in geo["rows"]] == [r["targetId"] for r in g["rows"]]
    for key in ("slotMids", "lstHours", "frameQuat", "sunAltitudeDeg", "sunAzimuthDeg"):
        assert len(geo[key]) == n, f"geometry.{key} has wrong length"
    for row in geo["rows"]:
        for key in ("altitudeDeg", "azimuthDeg", "airmass", "moonSeparationDeg", "visible"):
            assert len(row[key]) == n, f"{row['targetId']}.{key} has wrong length"


def test_grid_carries_no_geometry(client: TestClient, session_id: str) -> None:
    """The split is the point: five of the seven arrays the heatmap used to
    carry are identical at every decision point, so re-sending them on every
    scrub past a forecast run was pure waste. If they come back, the saving is
    gone and nobody will notice from the UI."""
    row = client.get(f"/api/sessions/{session_id}/grid").json()["rows"][0]
    leaked = {"altitudeDeg", "azimuthDeg", "airmass", "moonSeparationDeg", "visible"} & set(row)
    assert not leaked, f"geometry is back on /grid: {sorted(leaked)}"


def test_geometry_has_no_as_of_parameter(client: TestClient, session_id: str) -> None:
    """Geometry cannot leak, because there is no clock in the question.

    An ``as_of`` here would be meaningless at best -- nothing in this payload
    depends on published data -- and at worst an invitation to start filtering
    it, which is how a cache key grows a time axis and a memoization leak.
    """
    spec = client.get("/openapi.json").json()
    params = spec["paths"]["/api/sessions/{session_id}/geometry"]["get"].get("parameters", [])
    assert [p["name"] for p in params] == ["session_id"]

    r = client.get(f"/api/sessions/{session_id}/geometry", params={"as_of": "2026-09-13T04:00Z"})
    assert r.status_code == 200, "an unknown query param must not change the answer"


def test_geometry_is_identical_at_every_decision_point(client: TestClient, session_id: str) -> None:
    """The claim the endpoint is built on, asserted rather than assumed."""
    sess = client.get(f"/api/sessions/{session_id}").json()
    dps = sess["decisionPoints"]
    assert len(dps) > 1, "need at least two decision points for this to mean anything"
    first = client.get(f"/api/sessions/{session_id}/geometry").json()
    grids = {
        json.dumps(
            client.get(f"/api/sessions/{session_id}/grid", params={"as_of": dp["at"]}).json()[
                "rows"
            ],
            sort_keys=True,
        )
        for dp in dps
    }
    assert len(grids) > 1, "conditions should differ across decision points"
    assert first == client.get(f"/api/sessions/{session_id}/geometry").json()


def test_geometry_etag_revalidates_to_304(client: TestClient, session_id: str) -> None:
    r = client.get(f"/api/sessions/{session_id}/geometry")
    etag = r.headers["etag"]
    assert etag.startswith('W/"')
    # Revalidated, not immutable: adding a target to the night adds a row.
    assert "no-cache" in r.headers["cache-control"]

    again = client.get(f"/api/sessions/{session_id}/geometry", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.headers["etag"] == etag

    # A proxy may add its own, and a conforming client may send several.
    many = client.get(
        f"/api/sessions/{session_id}/geometry",
        headers={"If-None-Match": f'W/"nonsense", {etag}'},
    )
    assert many.status_code == 304


def test_frame_rotation_agrees_with_the_published_altitudes(
    client: TestClient, session_id: str
) -> None:
    """The check that makes the 3D sky trustworthy, run against the WIRE.

    The unit test proves geometry.py is self-consistent; this proves the
    numbers that actually reach the browser are, after rounding, aliasing and
    JSON. Rotating a target's ICRS unit vector by the published quaternion must
    reproduce the published altitude.
    """
    geo = client.get(f"/api/sessions/{session_id}/geometry").json()
    q = np.asarray(geo["frameQuat"], dtype=float)
    x, y, z, w = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    r = np.stack(
        [
            np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)], -1),
            np.stack([2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)], -1),
            np.stack([2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)], -1),
        ],
        -2,
    )
    for row in geo["rows"]:
        ra, dec = np.radians(row["raDeg"]), np.radians(row["decDeg"])
        v = np.array([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)])
        up = np.einsum("sji,j->si", r, v)[:, 2]
        alt = np.degrees(np.arcsin(np.clip(up, -1.0, 1.0)))
        err = np.max(np.abs(alt - np.asarray(row["altitudeDeg"], dtype=float)))
        assert err < 0.02, f"{row['targetId']}: frame is off by {err:.4f} deg"


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
    assert body["catalogSize"] >= len(body["defaultTargetIds"]) > 0
    telescopes = {t["id"] for t in body["telescopes"]}
    cameras = {c["id"] for c in body["cameras"]}
    assert body["defaultTelescopeId"] in telescopes
    assert body["defaultCameraId"] in cameras
    for e in body["equipment"]:
        # A rig is its parts: it can never name a telescope or camera that is
        # not itself offered as a preset.
        assert e["telescopeId"] in telescopes
        assert e["cameraId"] in cameras
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
    r = client.post(
        "/api/sessions",
        json={"date": NIGHT, "hours": 3, "solveSeconds": 1.0, "weather": OFFLINE},
    )
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
            "weather": OFFLINE,
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
                        json={"date": NIGHT, "hours": 3, "solveSeconds": 1.0, "weather": OFFLINE},
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


# --------------------------------------------------------------------------
# thumbnails
# --------------------------------------------------------------------------


def test_thumbnail_survey_comes_from_an_allowlist(client: TestClient) -> None:
    """The survey is chosen by short key and never taken from the request.

    Passing a caller-supplied HiPS identifier through to the upstream service
    would turn this route into an open proxy to whatever that service can be
    talked into fetching. The allowlist is the whole defence, so it is tested
    rather than assumed.
    """
    bad = client.get(
        "/api/thumbnail",
        params={"ra": 10.68, "dec": 41.27, "survey": "http://169.254.169.254/latest/meta-data"},
    )
    assert bad.status_code == 422
    assert "unknown survey" in bad.text


def test_thumbnail_rejects_out_of_range_geometry(client: TestClient) -> None:
    for params in (
        {"ra": 400.0, "dec": 0.0},
        {"ra": 0.0, "dec": 100.0},
        {"ra": 0.0, "dec": 0.0, "fov": 90.0},
        {"ra": 0.0, "dec": 0.0, "size": 4096},
    ):
        assert client.get("/api/thumbnail", params=params).status_code == 422, params


def test_thumbnail_is_served_from_cache_without_the_network(tmp_path: Path) -> None:
    """A cutout costs ~1.4 s upstream, so it is fetched once per target.

    Asserted by putting a file in the cache and refusing the client any
    network at all: if the route reaches out anyway, this raises.
    """
    from tscheduler.api.thumbnails import ThumbnailCache, ThumbnailRequest

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"went to the network for a cached image: {request.url}")

    cache = ThumbnailCache(tmp_path, client=httpx.Client(transport=httpx.MockTransport(refuse)))
    req = ThumbnailRequest(ra_deg=10.6847, dec_deg=41.269)
    (tmp_path / "thumbs").mkdir(parents=True)
    cache.path_for(req).write_bytes(b"\xff\xd8fake jpeg")
    assert cache.fetch(req) == b"\xff\xd8fake jpeg"


def test_thumbnail_refuses_a_non_jpeg_body(tmp_path: Path) -> None:
    """hips2fits answers some errors with HTTP 200 and a JSON body.

    Caching that would put a text file where an image belongs and serve it
    happily forever, so the magic bytes are checked before anything is written.
    """
    from tscheduler.api.thumbnails import ThumbnailCache, ThumbnailRequest

    def json_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "no such HiPS"})

    cache = ThumbnailCache(tmp_path, client=httpx.Client(transport=httpx.MockTransport(json_error)))
    req = ThumbnailRequest(ra_deg=10.6847, dec_deg=41.269)
    with pytest.raises(httpx.HTTPError, match="not a JPEG"):
        cache.fetch(req)
    assert not cache.path_for(req).exists(), "a failed fetch must leave nothing on disk"


def test_targets_carry_their_visibility_window(client: TestClient, session_id: str) -> None:
    """Rise/set and USABLE are different questions, and the gap is the answer
    to 'why is this not scheduled'."""
    targets = client.get(f"/api/sessions/{session_id}").json()["targets"]
    assert targets
    for t in targets:
        for key in ("riseSlot", "setSlot", "firstUsableSlot", "lastUsableSlot", "transitSlot"):
            assert key in t, f"{t['id']} is missing {key}"
        if t["firstUsableSlot"] is not None:
            assert t["lastUsableSlot"] is not None
            assert t["firstUsableSlot"] <= t["lastUsableSlot"]
            assert t["visibleSlots"] > 0
        else:
            assert t["visibleSlots"] == 0


# --------------------------------------------------------------------------
# the sky view: planets, darkness and satellites (display only)
# --------------------------------------------------------------------------

TLE_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tle" / "visual.tle"


def test_sky_carries_the_planets_and_the_darkness(client: TestClient, session_id: str) -> None:
    r = client.get(f"/api/sessions/{session_id}/sky")
    assert r.status_code == 200, r.text
    body = r.json()
    n = body["nSlots"]
    assert [p["id"] for p in body["planets"]] == [
        "mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune",
    ]
    for p in body["planets"]:
        assert len(p["altitudeDeg"]) == len(p["azimuthDeg"]) == n
    assert len(body["zenithSkyMagArcsec2"]) == len(body["nakedEyeLimitMag"]) == n
    # Darker sky, fainter stars: the limit must follow the sky brightness.
    mu, lim = np.array(body["zenithSkyMagArcsec2"]), np.array(body["nakedEyeLimitMag"])
    order = np.argsort(mu)
    assert np.all(np.diff(lim[order]) >= -1e-6)
    assert 3.0 < lim.max() < 7.0

    # Derived from the geometry alone, so it revalidates for free.
    again = client.get(
        f"/api/sessions/{session_id}/sky", headers={"If-None-Match": r.headers["etag"]}
    )
    assert again.status_code == 304


def test_satellites_are_passes_the_browser_can_animate(session_id: str) -> None:
    from tscheduler.providers.satellites.celestrak import FixtureTleProvider

    app = create_app(satellite_elements=FixtureTleProvider(TLE_FIXTURE))
    with TestClient(app) as c:
        sid = c.post(
            "/api/sessions", json={"date": "2026-09-16", "hours": 8, "weather": "synthetic"}
        ).json()["id"]
        body = c.get(f"/api/sessions/{sid}/satellites").json()
        grid = c.get(f"/api/sessions/{sid}").json()["grid"]

    assert body["available"] is True, body["reason"]
    assert body["passes"], "the 'visual' group always has something overhead at dusk"
    assert body["stepSeconds"] == 10.0
    start, end = datetime.fromisoformat(grid["start"]), datetime.fromisoformat(grid["end"])
    for p in body["passes"]:
        n = len(p["altitudeDeg"])
        assert len(p["azimuthDeg"]) == len(p["magnitude"]) == len(p["rangeKm"]) == n >= 2
        assert start <= datetime.fromisoformat(p["startsAt"]) <= end
        # null is the shadow, and at least one sample must be lit and up.
        assert any(m is not None and a > 0 for m, a in zip(p["magnitude"], p["altitudeDeg"], strict=True))
    starts = [p["startsAt"] for p in body["passes"]]
    assert starts == sorted(starts)


def test_satellites_degrade_to_an_answer_when_offline(client: TestClient) -> None:
    """No elements is the normal case at a dark site. It is a 200 that says so."""
    from tscheduler.providers.satellites.base import SatelliteElementsProvider

    class Offline(SatelliteElementsProvider):
        source_id = "offline"

        def _fetch(self, query, as_of):  # type: ignore[no-untyped-def]
            raise httpx.ConnectError("no route to celestrak.org")

        def coverage_key(self, query) -> str:  # type: ignore[no-untyped-def]
            return "offline"

    app = create_app(satellite_elements=Offline())
    with TestClient(app) as c:
        sid = c.post(
            "/api/sessions", json={"date": NIGHT, "hours": 2, "weather": "synthetic"}
        ).json()["id"]
        r = c.get(f"/api/sessions/{sid}/satellites")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert "ConnectError" in body["reason"]
    assert body["passes"] == []
