"""Adding an object to a planned night.

What must hold, whatever the optimiser decides about the new target:

* everything before the amendment is untouched -- the same decision points,
  the same plans, and no slot before the cursor rewritten;
* the amended night still tiles: consecutive plans cover it with no gap;
* the new target exists everywhere the old ones do (geometry, session);
* asking for what is already planned, or for nothing real, is refused;
* an object the optimiser cannot place is refused too, and the refusal leaves
  NOTHING behind -- same targets, same decision points, same geometry;
* an object whose observing is already FINISHED is refused as well, rather
  than reported as scheduled for time that has been and gone.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from itertools import pairwise

import pytest

pytest.importorskip("fastapi", reason="server tests need the api extra: uv sync --extra api")

from fastapi.testclient import TestClient

from tscheduler.api.app import create_app

NIGHT = "2026-09-13"
AT = "2026-09-13T03:00:00Z"


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def _session(client: TestClient) -> str:
    r = client.post(
        "/api/sessions",
        json={
            "date": NIGHT,
            "hours": 8,
            "snrGoal": 20,
            "solveSeconds": 2.0,
            "weather": "synthetic",
        },
    )
    assert r.status_code == 202, r.text
    sid: str = r.json()["id"]
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "ready"
    return sid


def _plan(client: TestClient, sid: str, at: str) -> dict:
    return client.get(f"/api/sessions/{sid}/plan", params={"as_of": at}).json()


# --- stars, added by their own coordinates ------------------------------------
# A star has no server-side identity: all 41,411 live in the browser's stars.bin
# keyed by position in that file, and ~36,000 have no name. So the client sends
# coordinates, and `isPointSource` says how to read the magnitude.
def test_a_star_can_be_added_by_its_own_coordinates(client: TestClient) -> None:
    sid = _session(client)
    r = client.post(
        f"/api/sessions/{sid}/targets",
        json={
            "target": "star-102098",
            "name": "Deneb",
            # RA 20h41m26s, Dec +45 16' 49" -- high over Urbana in September.
            "raDeg": 310.3579,
            "decDeg": 45.2803,
            "magnitude": 1.25,
            "isPointSource": True,
            "at": AT,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["targetId"] == "star-102098"
    assert r.json()["targetName"] == "Deneb"

    after = client.get(f"/api/sessions/{sid}").json()
    assert "star-102098" in {t["id"] for t in after["targets"]}
    plan = _plan(client, sid, r.json()["at"])
    assert [b for b in plan["blocks"] if b["targetId"] == "star-102098"]


def test_partial_coordinates_are_refused_rather_than_treated_as_a_name(
    client: TestClient,
) -> None:
    """Falling back to a name search would resolve "star-1" to nothing, or
    worse, to something else. Two of the three fields is a client bug."""
    sid = _session(client)
    for body in (
        {"target": "star-1", "raDeg": 10.0},
        {"target": "star-1", "raDeg": 10.0, "decDeg": 20.0},
        {"target": "star-1", "magnitude": 8.0},
    ):
        r = client.post(f"/api/sessions/{sid}/targets", json=body)
        assert r.status_code == 422, r.text
        assert "must be given together" in r.json()["detail"]


def test_a_point_source_is_priced_as_one_not_as_a_surface(client: TestClient) -> None:
    """The same magnitude at the same place, once as a star and once as a
    surface. The surface reading accumulates faster -- it multiplies the light
    of one square arcsecond by the whole aperture -- so the star must need MORE
    integrating. If the flag were dropped on the wire the two would agree."""
    sid = _session(client)
    # Magnitude 18, not something bright: `requiredRefMinutes` is rounded to two
    # decimals on the wire, and anything brighter than ~16 reaches its goal so
    # fast that BOTH readings round to 0.00 and the test proves nothing.
    common = {"raDeg": 310.3579, "decDeg": 45.2803, "magnitude": 18.0, "at": AT}
    assert (
        client.post(
            f"/api/sessions/{sid}/targets",
            json={**common, "target": "as-star", "isPointSource": True},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/sessions/{sid}/targets",
            json={**common, "target": "as-surface", "isPointSource": False},
        ).status_code
        == 200
    )
    prog = {p["targetId"]: p for p in _plan(client, sid, AT)["progress"]}
    star, surface = prog["as-star"], prog["as-surface"]
    assert star["requiredRefMinutes"] > surface["requiredRefMinutes"], (
        f"point source needs {star['requiredRefMinutes']} ref-min, "
        f"surface {surface['requiredRefMinutes']} -- the flag did not reach the model"
    )


# --- planets -----------------------------------------------------------------
# A planet is not in the catalogue -- where it is and how bright it is depend on
# the date -- so it resolves per night, against the session's own site and grid.
def test_a_planet_can_be_added_to_the_night(client: TestClient) -> None:
    """Saturn clears Urbana's 30-degree floor for five hours on this night, so
    it is schedulable in the ordinary way: a target, a block, a card."""
    sid = _session(client)
    r = client.post(f"/api/sessions/{sid}/targets", json={"target": "saturn", "at": AT})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["targetId"] == "saturn"
    assert out["targetName"] == "Saturn"

    after = client.get(f"/api/sessions/{sid}").json()
    assert "saturn" in {t["id"] for t in after["targets"]}
    geo = client.get(f"/api/sessions/{sid}/geometry").json()
    assert "saturn" in {row["targetId"] for row in geo["rows"]}

    plan = _plan(client, sid, out["at"])
    blocks = [b for b in plan["blocks"] if b["targetId"] == "saturn"]
    assert blocks, "the planet must actually hold time, not merely join the spec"
    # The exposure instruction is the ordinary one: frames, a sub, an SNR.
    assert all(b["nSubs"] > 0 and b["tSubS"] > 0 for b in blocks)


def test_a_planet_already_in_the_plan_is_refused(client: TestClient) -> None:
    """The same refusal a catalogue object gets, so the UI can show "Scheduled"
    and the server still has the last word if the two disagree."""
    sid = _session(client)
    assert client.post(f"/api/sessions/{sid}/targets", json={"target": "saturn"}).status_code == 200
    again = client.post(f"/api/sessions/{sid}/targets", json={"target": "saturn"})
    assert again.status_code == 409
    assert "already in the plan" in again.json()["detail"]


def test_a_planet_that_never_clears_the_floor_is_refused_with_the_reason(
    client: TestClient,
) -> None:
    """Jupiter peaks at 3 degrees from Urbana on this night. The refusal must
    say that, and must leave the night exactly as it was."""
    sid = _session(client)
    before = client.get(f"/api/sessions/{sid}").json()
    r = client.post(f"/api/sessions/{sid}/targets", json={"target": "jupiter"})
    assert r.status_code == 409, r.text
    assert "altitude floor" in r.json()["detail"]

    after = client.get(f"/api/sessions/{sid}").json()
    assert {t["id"] for t in after["targets"]} == {t["id"] for t in before["targets"]}
    assert len(after["decisionPoints"]) == len(before["decisionPoints"])


def test_saturn_means_the_planet_not_the_saturn_nebula(client: TestClient) -> None:
    """The collision that makes resolution order load-bearing: "saturn" finds
    NGC 7009 in the catalogue, and a catalogue-first lookup would add a
    planetary nebula to a night that asked for a planet."""
    sid = _session(client)
    r = client.post(f"/api/sessions/{sid}/targets", json={"target": "saturn", "at": AT})
    assert r.status_code == 200, r.text
    assert r.json()["targetId"] == "saturn"
    assert r.json()["targetName"] == "Saturn"

    # ...and the nebula is still reachable by its own names.
    r2 = client.post(f"/api/sessions/{sid}/targets", json={"target": "Saturn Nebula", "at": AT})
    assert r2.status_code in {200, 409}, r2.text
    if r2.status_code == 200:
        assert r2.json()["targetId"] == "ngc7009"


def test_adding_from_the_cursor_keeps_the_past(client: TestClient) -> None:
    sid = _session(client)
    before = client.get(f"/api/sessions/{sid}").json()
    at = datetime.fromisoformat(AT)
    kept = [dp for dp in before["decisionPoints"] if datetime.fromisoformat(dp["at"]) < at]

    r = client.post(f"/api/sessions/{sid}/targets", json={"target": "M15", "at": AT})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["targetId"] == "m15"
    assert datetime.fromisoformat(out["at"]) == at
    assert out["message"]

    after = client.get(f"/api/sessions/{sid}").json()
    assert "m15" in {t["id"] for t in after["targets"]}
    geo = client.get(f"/api/sessions/{sid}/geometry").json()
    assert "m15" in {r["targetId"] for r in geo["rows"]}

    # The decision points before the cursor are the very same plans.
    assert [(d["at"], d["planId"]) for d in after["decisionPoints"][: len(kept)]] == [
        (d["at"], d["planId"]) for d in kept
    ]
    new = after["decisionPoints"][out["decisionIndex"]]
    assert new["kind"] == "amend"
    assert "added" in new["reason"].lower()
    # Later forecast arrivals are re-solved, but are still forecast arrivals.
    assert {d["kind"] for d in after["decisionPoints"][out["decisionIndex"] + 1 :]} <= {"weather"}

    # Nothing before the cursor rewritten, at the new point or any after it.
    for dp in after["decisionPoints"][out["decisionIndex"] :]:
        assert _plan(client, sid, dp["at"])["changes"]["pastSlotsRewritten"] == 0

    # Still tiles the night.
    plans = [_plan(client, sid, dp["at"]) for dp in after["decisionPoints"]]
    for a, b in pairwise(plans):
        assert a["validUntil"] == b["validFrom"]

    # A 200 says it is scheduled, and it can only be scheduled from the cursor on.
    blocks = [b for b in _plan(client, sid, AT)["blocks"] if b["targetId"] == "m15"]
    assert blocks
    assert all(datetime.fromisoformat(b["startsAt"]) >= at for b in blocks)


def test_already_planned_and_unknown_are_refused(client: TestClient) -> None:
    sid = _session(client)
    plan = _plan(client, sid, AT)
    planned = plan["blocks"][-1]["targetId"]
    r = client.post(f"/api/sessions/{sid}/targets", json={"target": planned, "at": AT})
    assert r.status_code == 409, r.text

    r = client.post(f"/api/sessions/{sid}/targets", json={"target": "not a real object", "at": AT})
    assert r.status_code == 422, r.text


def test_catalog_is_positioned_and_says_what_cannot_be_planned(client: TestClient) -> None:
    body = client.get("/api/catalog").json()
    assert body["size"] == len(body["objects"]) > 1000
    m31 = next(o for o in body["objects"] if o["id"] == "m31")
    assert 10.0 < m31["raDeg"] < 11.5 and 40.5 < m31["decDeg"] < 42.0
    assert m31["plannable"] and m31["whyNot"] is None
    refused = [o for o in body["objects"] if not o["plannable"]]
    assert refused and all(o["whyNot"] for o in refused)


def test_an_add_the_optimiser_cannot_place_changes_nothing(client: TestClient) -> None:
    """The whole point of refusing rather than adding-unscheduled.

    A far-southern object never rises at the default northern site, so no
    priority boost can buy it a slot. The session must come out of the attempt
    byte-identical: nothing in the target list, nothing in the geometry, and no
    decision point recording a re-plan that changed nothing.
    """
    sid = _session(client)
    catalog = client.get("/api/catalog").json()["objects"]
    below = next(o for o in catalog if o["plannable"] and o["decDeg"] < -70)

    before = client.get(f"/api/sessions/{sid}").json()
    geo_before = client.get(f"/api/sessions/{sid}/geometry").json()

    r = client.post(f"/api/sessions/{sid}/targets", json={"target": below["id"], "at": AT})
    assert r.status_code == 409, r.text
    # The reason names the object and says what is wrong with the NIGHT, not
    # with the request: this text is all the observer gets instead of a change.
    detail = r.json()["detail"]
    assert below["name"] in detail or below["designation"] in detail
    assert "altitude floor" in detail or "usable minutes" in detail

    after = client.get(f"/api/sessions/{sid}").json()
    assert [t["id"] for t in after["targets"]] == [t["id"] for t in before["targets"]]
    assert [(d["at"], d["planId"]) for d in after["decisionPoints"]] == [
        (d["at"], d["planId"]) for d in before["decisionPoints"]
    ]
    geo_after = client.get(f"/api/sessions/{sid}/geometry").json()
    assert [r["targetId"] for r in geo_after["rows"]] == [r["targetId"] for r in geo_before["rows"]]


def test_a_target_with_no_time_left_is_refused_not_reported_as_scheduled(client: TestClient) -> None:
    """The trap in asking `included` whether the add worked.

    A target whose blocks are all behind the cursor keeps the SNR it banked,
    so the optimiser can include it for free while giving it not one new slot.
    Asked near the end of the night, every planned target is in that state:
    each must be refused, and none may be answered with "is scheduled" and a
    pair of times that have already passed.
    """
    sid = _session(client)
    session = client.get(f"/api/sessions/{sid}").json()
    end = datetime.fromisoformat(session["grid"]["end"])
    late = (end - timedelta(minutes=2)).isoformat()

    # A target the solver counts as INCLUDED at that instant, which is what
    # makes this a real test: `included` is the thing the old code asked, and
    # by now it is satisfied out of banked history alone. A target that merely
    # ran out of time would be refused either way and would prove nothing.
    progress = _plan(client, sid, late)["progress"]
    done = [p["targetId"] for p in progress if p["included"]]
    assert done, "no target is included at the end of the night; test proves nothing"
    planned = done[0]

    before = client.get(f"/api/sessions/{sid}").json()
    r = client.post(f"/api/sessions/{sid}/targets", json={"target": planned, "at": late})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    # Not the "already in the plan" guard: by now its time is behind the
    # cursor, which is precisely what makes it askable again.
    assert "already in the plan" not in detail
    assert "usable minutes" in detail or "never clears" in detail

    after = client.get(f"/api/sessions/{sid}").json()
    assert [(d["at"], d["planId"]) for d in after["decisionPoints"]] == [
        (d["at"], d["planId"]) for d in before["decisionPoints"]
    ]
