"""Folding a night while saying how far along it is, and adding to one.

Two things are being tested, and they are one endpoint because they are one
wait. ``POST /api/night/stream`` reports its stages down the response it will
deliver the night on -- there is no session on the server to poll, so this is
the only channel a percentage can come from -- and an ``add`` in the body is
the same fold with an amendment on the end.

What must hold of the add, and it is the whole reason it is not a re-fold with
one more target:

* the new object gets NO slot before the instant it was asked for, and that
  instant is rounded UP to a slot boundary, so "added at 02:03" never comes
  back as "scheduled from 02:00";
* on a night still happening the instant is the wall clock, however far back
  the cursor has been dragged;
* everything before that instant is untouched;
* an object that cannot be given time after it is REFUSED, with the reason,
  and the night comes back unamended -- which over a stream means an ``error``
  frame and no ``night`` frame at all.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="server tests need the api extra: uv sync --extra api")

import httpx
import uvicorn
from fastapi.testclient import TestClient

from tscheduler.api.app import create_app
from tscheduler.core.clock import AsOf, Vantage

NIGHT = "2026-09-13"
#: Offline and deterministic. The stream's framing is the subject here, not
#: what Open-Meteo happens to be saying today.
OFFLINE = "synthetic"

BASE: dict[str, Any] = {
    "date": NIGHT,
    "startHourUtc": 1.0,
    "hours": 8,
    "snrGoal": 20,
    "solveSeconds": 1.5,
    "weather": OFFLINE,
    "targets": [{"id": t} for t in ("m31", "m27", "m57", "m13")],
}


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def frames(client: TestClient, body: dict[str, Any]) -> list[dict[str, Any]]:
    """Every frame of one stream, in order. The status must be 200 to get here."""
    with client.stream("POST", "/api/night/stream", json=body) as r:
        assert r.status_code == 200, r.read()
        assert r.headers["content-type"].startswith("application/x-ndjson")
        return [json.loads(line) for line in r.iter_lines() if line.strip()]


def night_of(fs: list[dict[str, Any]]) -> dict[str, Any]:
    nights = [f["night"] for f in fs if f["type"] == "night"]
    errors = [f for f in fs if f["type"] == "error"]
    assert not errors, errors
    assert len(nights) == 1, f"{len(nights)} night frames"
    return nights[0]


def error_of(fs: list[dict[str, Any]]) -> dict[str, Any]:
    errors = [f for f in fs if f["type"] == "error"]
    assert len(errors) == 1, fs[-3:]
    assert not [f for f in fs if f["type"] == "night"], "refused, and yet a night came back"
    return errors[0]


def slot_of(night: dict[str, Any], t: str) -> int:
    grid = night["session"]["grid"]
    start = datetime.fromisoformat(grid["start"].replace("Z", "+00:00"))
    when = datetime.fromisoformat(t.replace("Z", "+00:00"))
    return int((when - start).total_seconds() // 60 // grid["slotMinutes"])


def blocks_of(plan: dict[str, Any], target_id: str) -> list[tuple[int, int]]:
    return [(b["slotStart"], b["slotEnd"]) for b in plan["blocks"] if b["targetId"] == target_id]


# -- the report ------------------------------------------------------------


def test_the_stream_reports_progress_and_then_the_night(client: TestClient) -> None:
    fs = frames(client, BASE)
    fractions = [f["fraction"] for f in fs if f["type"] == "progress"]

    assert len(fractions) >= 4, fractions
    assert fractions == sorted(fractions), "the bar must never walk backwards"
    assert all(0.0 <= f <= 1.0 for f in fractions), fractions
    assert fractions[0] == 0.0 and fractions[-1] == 1.0
    assert all(f["message"] for f in fs if f["type"] == "progress"), "a stage with no words"

    # The night is the LAST frame: nothing is reported after it arrives.
    assert fs[-1]["type"] == "night"
    night = night_of(fs)
    assert night["amend"] is None
    assert [d["index"] for d in night["decisions"]] == list(range(len(night["decisions"])))


def test_the_stream_is_compressed(client: TestClient) -> None:
    """Worth three to one on the final frame, and easy to lose by accident.

    ``application/x-ndjson`` is deliberately NOT in ``GZIP_EXCLUDED_CONTENT_TYPES``:
    Starlette flushes a streaming body per chunk, so the progress frames still
    arrive as they are produced. Adding it there -- following the SSE
    precedent without checking -- would ship three quarters of a megabyte
    uncompressed and nothing would fail.
    """
    with client.stream(
        "POST", "/api/night/stream", json=BASE, headers={"accept-encoding": "gzip"}
    ) as r:
        assert r.status_code == 200
        assert r.headers.get("content-encoding") == "gzip", dict(r.headers)


def test_the_plain_endpoint_is_unchanged(client: TestClient) -> None:
    """``/api/night`` still answers in one JSON body, and never amends."""
    r = client.post("/api/night", json=BASE)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["amend"] is None


# -- adding an object, from an instant onward ------------------------------


def test_an_added_object_gets_no_time_before_the_instant_it_was_asked_for(
    client: TestClient,
) -> None:
    at = "2026-09-13T03:00:00Z"
    before = night_of(frames(client, BASE))
    after = night_of(frames(client, {**BASE, "add": {"target": "ngc7000", "at": at}}))

    amend = after["amend"]
    assert amend is not None and amend["targetId"] == "ngc7000"
    assert amend["at"] == at

    cursor = slot_of(after, at)
    # Every decision point, not only the one the amendment takes over at: the
    # later ones are re-solved on top of it and must not undo the lock.
    for d in after["decisions"]:
        for start, end in blocks_of(d["plan"], "ngc7000"):
            assert start >= cursor, f"dp{d['index']} gave it {start}-{end}, before slot {cursor}"

    # And the night before the cursor is the night that was already planned.
    was = before["decisions"][-1]["plan"]["slots"]
    now = after["decisions"][amend["decisionIndex"]]["plan"]["slots"]
    assert [s["targetId"] for s in was[:cursor]] == [s["targetId"] for s in now[:cursor]]


def test_the_instant_is_rounded_up_to_a_slot_boundary(client: TestClient) -> None:
    """02:03 must not come back as "scheduled from 02:00".

    ``TimeGrid.index_of`` is a floor, so the slot containing the request has
    already begun. Handing the solver that slot leaves it free to give the new
    object three minutes that have been and gone -- and the success message
    prints them.
    """
    at = "2026-09-13T03:03:00Z"
    after = night_of(frames(client, {**BASE, "add": {"target": "ngc7000", "at": at}}))
    amend = after["amend"]
    assert amend is not None
    assert amend["at"] == "2026-09-13T03:05:00Z", amend["at"]

    first_whole = slot_of(after, "2026-09-13T03:05:00Z")
    for d in after["decisions"]:
        for start, _ in blocks_of(d["plan"], "ngc7000"):
            assert start >= first_whole
    # The sentence the observer reads names no time before they clicked.
    assert "02:" not in amend["message"] and "03:00" not in amend["message"]


def test_a_night_still_happening_is_amended_from_now_not_from_the_cursor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dragging back to review does not license rewriting what was handed over.

    The stateful path asks its live watch; a stateless fold has no watch, so
    the rule is made from the session's own creation instant instead. The wall
    clock is pinned two hours into this night, the cursor is put an hour and
    forty behind THAT, and the amendment must still land at the present.
    """
    now = datetime(2026, 9, 13, 3, 0, tzinfo=UTC)
    monkeypatch.setattr(AsOf, "live", classmethod(lambda cls: cls(now, Vantage.LIVE)))
    behind = datetime(2026, 9, 13, 1, 20, tzinfo=UTC).isoformat().replace("+00:00", "Z")

    after = night_of(frames(client, {**BASE, "add": {"target": "ngc7000", "at": behind}}))
    amend = after["amend"]
    assert amend is not None
    assert amend["at"] == "2026-09-13T03:00:00Z", "the cursor, not the clock, decided"

    cursor = slot_of(after, amend["at"])
    for d in after["decisions"]:
        for s, _ in blocks_of(d["plan"], "ngc7000"):
            assert s >= cursor, f"dp{d['index']} scheduled it at {s}, before the present"


# -- refusals --------------------------------------------------------------


def test_an_object_that_cannot_be_fitted_is_refused_and_the_night_is_unchanged(
    client: TestClient,
) -> None:
    """The refusal is the feature: a 409 frame, a reason, and no night."""
    err = error_of(
        frames(client, {**BASE, "add": {"target": "ngc7000", "at": "2026-09-13T08:50:00Z"}})
    )
    assert err["status"] == 409
    assert "NGC 7000" in err["detail"], err


def test_a_refusal_names_what_is_actually_wrong(client: TestClient) -> None:
    """Not "outranked" for an object that has no room, nor "below the horizon"
    for one that is sixty degrees up.

    The solver writes a sentence per drop code from the inputs it solved;
    where it has one, the refusal is that sentence rather than a second
    account synthesised from the same arrays.
    """
    err = error_of(frames(client, {**BASE, "add": {"target": "m8", "at": "2026-09-13T07:30:00Z"}}))
    assert err["status"] == 409
    assert "altitude floor" in err["detail"], err
    assert "gain more from that time" not in err["detail"], "a competition that never happened"


def test_an_object_already_in_the_plan_is_refused(client: TestClient) -> None:
    err = error_of(frames(client, {**BASE, "add": {"target": "m31", "at": "2026-09-13T02:00:00Z"}}))
    assert err["status"] == 409
    assert "already in the plan" in err["detail"]


def test_an_object_nothing_can_name_is_refused_before_the_stream_starts(
    client: TestClient,
) -> None:
    """A real status code, because nothing has been sent yet.

    Only what cannot be decided until the fold has run arrives as a frame. A
    body the server can reject on sight must still be rejected on sight, or
    every client has to learn two ways of being told the same thing.
    """
    r = client.post("/api/night/stream", json={**BASE, "add": {"target": "not-a-thing"}})
    assert r.status_code == 422, r.text
    assert "not in the catalogue" in r.json()["detail"]

    r = client.post("/api/night/stream", json={**BASE, "add": {"target": "x", "raDeg": 10.0}})
    assert r.status_code == 422, r.text
    assert "must be given together" in r.json()["detail"]


def test_a_star_is_added_by_its_own_coordinates(client: TestClient) -> None:
    """The one object the server cannot look up. See ``AddTargetRequest``."""
    after = night_of(
        frames(
            client,
            {
                **BASE,
                "add": {
                    "target": "star-deneb",
                    "name": "Deneb",
                    "raDeg": 310.358,
                    "decDeg": 45.28,
                    "magnitude": 1.25,
                    "isPointSource": True,
                    "at": "2026-09-13T03:00:00Z",
                },
            },
        )
    )
    amend = after["amend"]
    assert amend is not None and amend["targetName"] == "Deneb"
    assert "star-deneb" in {t["id"] for t in after["session"]["targets"]}
    cursor = slot_of(after, amend["at"])
    for d in after["decisions"]:
        for s, _ in blocks_of(d["plan"], "star-deneb"):
            assert s >= cursor


# -- the part a TestClient cannot see --------------------------------------


@pytest.fixture(scope="module")
def live_server() -> Iterator[str]:
    """A real uvicorn on an ephemeral port.

    ``httpx.ASGITransport`` awaits the whole ASGI call before handing back a
    response. The stream is finite, so a TestClient test does not hang -- it
    passes vacuously, seeing every frame at once after the fold has finished,
    which is exactly the failure a loading bar has. Only a socket can tell the
    difference.
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
def test_progress_arrives_before_the_night_does(live_server: str) -> None:
    """The bar has to move WHILE the fold runs, which is the whole claim.

    Measured over a socket, with the app's real middleware stack, so a change
    that buffers the response -- a compressor without a per-chunk flush, an
    endpoint that assembles the frames and sends them at the end -- fails here
    rather than shipping as a bar that jumps from nothing to done.
    """
    marks: list[tuple[float, str, float | None]] = []
    t0 = time.monotonic()
    with (
        httpx.Client(base_url=live_server, timeout=180.0) as c,
        c.stream("POST", "/api/night/stream", json=BASE) as stream,
    ):
        assert stream.status_code == 200
        for line in stream.iter_lines():
            if not line.strip():
                continue
            frame = json.loads(line)
            marks.append((time.monotonic() - t0, frame["type"], frame.get("fraction")))

    kinds = [k for _, k, _ in marks]
    assert kinds[-1] == "night", kinds
    arrived = marks[-1][0]
    before = [t for t, k, _ in marks if k == "progress" and t < arrived]
    assert len(before) >= 4, marks

    # Not merely "sent first" -- spread out. Every frame landing in the last
    # tenth of the wait is what a buffered stream looks like from here.
    assert min(before) < arrived * 0.5, marks
