"""POST /api/targets/tonight over the whole catalogue.

What the contract promises (see ``TonightOut``): every catalogue object is in
``targets``, recommendations first, then the rest of the visible sky best
first, then everything else; recommendations are visible and plannable;
anything invisible says why. Beyond the contract, a few pins on whether the
picks make sense for the rig -- the point of ranking at all -- and a
performance bound, because this endpoint runs on every change to the form.
"""

from __future__ import annotations

import dataclasses
import math
import re
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tscheduler import catalog
from tscheduler.api import equipment, tonight
from tscheduler.api.app import create_app
from tscheduler.domain.site import Site
from tscheduler.physics.airmass import airmass_kasten_young
from tscheduler.physics.quality import extended_snr2_rate, resolution_element
from tscheduler.pipeline.conditions import download_duty_cycle

pytest.importorskip("fastapi", reason="server tests need the api extra: uv sync --extra api")

NIGHT = "2026-09-18"
SOUTH = {
    "latitudeDeg": -30.0,
    "longitudeDeg": -70.7,
    "elevationM": 2200,
    "name": "Southern site",
    "bortle": 2,
}
#: No astronomical darkness at midsummer: the Sun bottoms out near -14.6°.
NORTH52 = {"latitudeDeg": 52.0, "longitudeDeg": 0.0, "elevationM": 50, "name": "52 N", "bortle": 3}
TROMSO = {
    "latitudeDeg": 69.65,
    "longitudeDeg": 18.96,
    "elevationM": 10,
    "name": "Tromsø",
    "bortle": 3,
}
FRAMING = {"fits", "tight", "mosaic", "small"}
TWILIGHT_NOTE = "The Sun never gets 18° below the horizon tonight"


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def _tonight(client: TestClient, **body: Any) -> dict[str, Any]:
    r = client.post("/api/targets/tonight", json={"date": NIGHT, **body})
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


@pytest.fixture(scope="module")
def c8(client: TestClient) -> dict[str, Any]:
    return _tonight(client, siteId="urbana", equipmentId="sct8-2600mm")


@pytest.fixture(scope="module")
def refractor(client: TestClient) -> dict[str, Any]:
    return _tonight(client, siteId="urbana", equipmentId="refractor80-533")


@pytest.fixture(scope="module")
def south(client: TestClient) -> dict[str, Any]:
    return _tonight(client, site=SOUTH, equipmentId="refractor80-533")


@pytest.fixture(scope="module")
def north52(client: TestClient) -> dict[str, dict[str, Any]]:
    """Midsummer and midwinter at one site. A higher goal than the default
    keeps the hours well clear of the response's two-decimal rounding."""
    return {
        d: _tonight(client, date=d, site=NORTH52, equipmentId="sct8-2600mm", snrGoal=60)
        for d in ("2026-06-21", "2026-12-21")
    }


def _by_id(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["id"]: t for t in body["targets"]}


def _at(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


# --------------------------------------------------------------------------
# the contract
# --------------------------------------------------------------------------


def test_every_catalogue_object_is_present_once(c8: dict[str, Any]) -> None:
    ids = [t["id"] for t in c8["targets"]]
    assert len(ids) == len(set(ids)) == c8["catalogSize"] == len(catalog.objects())
    assert c8["attribution"].startswith("Catalogue: OpenNGC")
    assert c8["visibleCount"] == sum(t["visible"] for t in c8["targets"])


def test_recommendations_are_visible_plannable_and_listed_first(c8: dict[str, Any]) -> None:
    rec = c8["recommendedIds"]
    assert 6 <= len(rec) <= 12
    assert len(set(rec)) == len(rec)
    assert [t["id"] for t in c8["targets"][: len(rec)]] == rec
    by = _by_id(c8)
    for tid in rec:
        assert by[tid]["visible"] and by[tid]["plannable"], tid
        assert by[tid]["usableHours"] >= 1.0, tid


def test_order_is_recommended_then_visible_by_score_then_the_rest(c8: dict[str, Any]) -> None:
    rest = c8["targets"][len(c8["recommendedIds"]) :]
    visible = [t for t in rest if t["visible"]]
    assert rest[: len(visible)] == visible, "an invisible object sorted among the visible"
    scores = [t["score"] for t in visible]
    assert scores == sorted(scores, reverse=True)
    invisible = rest[len(visible) :]
    assert all(t["score"] == 0.0 for t in invisible)
    names = [t["name"] for t in invisible]
    assert names == sorted(names)


def test_rows_are_internally_consistent(c8: dict[str, Any]) -> None:
    start, end = _at(c8["windowStart"]), _at(c8["windowEnd"])
    for t in c8["targets"]:
        assert t["framing"] in FRAMING, t["id"]
        assert 0.0 <= t["score"] <= 1.0, t["id"]
        if t["visible"]:
            assert t["whyNot"] is None, t["id"]
            assert t["usableHours"] > 0 and t["bestAt"] and t["usableFrom"], t["id"]
            assert t["usableFrom"] <= t["bestAt"] < t["usableUntil"], t["id"]
            assert start <= _at(t["usableFrom"]) and _at(t["usableUntil"]) <= end, t["id"]
            assert t["reasons"], t["id"]
            if t["feasible"]:
                assert t["hoursToGoal"] <= t["usableHours"], t["id"]
        else:
            assert t["whyNot"], t["id"]
            assert t["usableHours"] == 0 and t["bestAt"] is None, t["id"]
            assert not t["feasible"] and t["reasons"] == [], t["id"]


def test_the_usable_span_is_whole_samples_inside_the_window(client: TestClient) -> None:
    """Samples are stamped at their midpoints. The span runs from the start of
    the first usable one to the end of the last -- it once ran half a sample
    late at both ends, past the window's end."""
    body = _tonight(
        client,
        siteId="urbana",
        equipmentId="refractor80-533",
        start="2026-09-19T02:00:00Z",
        hours=6,
    )
    start, end = _at(body["windowStart"]), _at(body["windowEnd"])
    assert end - start == timedelta(hours=6)
    step = timedelta(minutes=tonight.SAMPLE_MINUTES)
    for t in body["targets"]:
        if not t["visible"]:
            continue
        a, b = _at(t["usableFrom"]), _at(t["usableUntil"])
        assert start <= a <= _at(t["bestAt"]) < b <= end, t["id"]
        assert (a - start) % step == (b - start) % step == timedelta(0), t["id"]
    m31 = _by_id(body)["m31"]  # up and clear of the Moon all six hours
    assert m31["usableHours"] == 6.0
    assert (_at(m31["usableFrom"]), _at(m31["usableUntil"])) == (start, end)


def _says_highest(t: dict[str, Any]) -> None:
    """The reason's "highest N° at HH:MM" is the row's ``bestAltitudeDeg`` as
    the picker shows it (Math.round: half up) at ``bestAt``."""
    said = re.search(r"highest (\d+)° at (\d\d:\d\d) UTC", " ".join(t["reasons"]))
    assert said, t["id"]
    assert int(said[1]) == math.floor(t["bestAltitudeDeg"] + 0.5), t["id"]
    assert said[2] == t["bestAt"][11:16], t["id"]


def test_highest_is_the_altitude_at_best_at(c8: dict[str, Any], client: TestClient) -> None:
    """``bestAltitudeDeg`` goes with ``bestAt``, as the reasons state them;
    ``maxAltitudeDeg`` is the dark-sky peak, which the Moon may hide."""
    for t in c8["targets"]:
        if not t["visible"]:
            assert t["bestAltitudeDeg"] is None, t["id"]
            continue
        assert t["bestAltitudeDeg"] <= t["maxAltitudeDeg"], t["id"]
        _says_highest(t)

    # A 95%-lit Moon near NGC 1060 at its dark-sky peak, over Tromsø. Its
    # best altitude, 51.5°, is also a half-degree case for the rounding.
    moonlit = _tonight(client, date="2026-12-21", site=TROMSO, equipmentId="sct8-2600mm")
    n1060 = _by_id(moonlit)["ngc1060"]
    assert n1060["visible"]
    assert n1060["maxAltitudeDeg"] - n1060["bestAltitudeDeg"] > 1.0
    assert n1060["moonSeparationDeg"] >= 15.0  # the site's exclusion, at bestAt
    _says_highest(n1060)


def test_a_night_without_astronomical_darkness_prices_the_twilight(
    client: TestClient, north52: dict[str, dict[str, Any]]
) -> None:
    """At 52° N in June the Sun never gets 18° down. What the ranking calls
    dark is nautical twilight, and the estimates must pay for its sky."""
    june, dec = north52["2026-06-21"], north52["2026-12-21"]
    assert TWILIGHT_NOTE in june["rankingNote"]
    assert TWILIGHT_NOTE not in dec["rankingNote"]

    # darkHours: astronomical darkness, as /api/night-window counts it...
    nw = client.get(
        "/api/night-window",
        params={"date": "2026-12-21", "lat": 52.0, "lon": 0.0, "elevation_m": 50},
    ).json()
    assert dec["darkHours"] == pytest.approx(nw["darkHours"], abs=tonight.SAMPLE_MINUTES / 60)
    # ...and, with none, the nautical hours the ranking treated as dark.
    assert june["darkHours"] == pytest.approx(max(t["usableHours"] for t in june["targets"]))
    assert 2.0 < june["darkHours"] < 4.0

    # A galaxy 4° from the pole, so its best altitude is much the same on
    # both nights: dearer in June's twilight than the airmass explains. Even
    # June's cheapest sample, the Sun 14.6° down, costs about 1.8x a dark sky;
    # a few degrees of altitude at 50° would explain a few percent.
    j, d = _by_id(june)["ngc2276"], _by_id(dec)["ngc2276"]
    assert abs(j["bestAltitudeDeg"] - d["bestAltitudeDeg"]) < 10.0
    assert j["hoursToGoal"] > 1.5 * d["hoursToGoal"]


def test_twilight_is_priced_at_the_same_altitude() -> None:
    """The pricing function alone: one surface, one altitude, the Sun lower
    and lower. Deep night is the dark-sky price; astronomical dusk costs next
    to nothing; the midsummer midnight at 52° N about doubles it, and the
    nautical limit costs several times over."""
    site = Site(latitude_deg=52.0, longitude_deg=0.0, elevation_m=50.0)
    rig = equipment.resolve("sct8-2600mm", None)
    sun = np.array([-45.0, -18.0, -14.6, -12.0])
    sb, alt = np.full(4, 23.0), np.full(4, 50.0)
    # Four objects of one sample each.
    h = tonight._hours_to_goal(
        sb, alt[:, None], sun[:, None], site=site, rig=rig, snr_goal=20.0, t_sub_s=90.0
    )

    dark_rate = extended_snr2_rate(
        surface_brightness=23.0,
        airmass=airmass_kasten_young(50.0),
        sky_mag_arcsec2=site.natural_zenith_mag_arcsec2,
        optics=rig.optics,
        camera=rig.camera,
        t_sub_s=90.0,
        extinction_k=site.extinction_k,
        element=resolution_element(rig.optics, rig.camera),
    )
    duty = download_duty_cycle(90.0, rig.camera.readout_s)
    assert h[0] == pytest.approx(400.0 / float(dark_rate) / duty / 3600.0, rel=1e-6)
    assert h[0] < h[1] < h[2] < h[3]
    assert h[1] < 1.25 * h[0]
    assert h[2] > 1.8 * h[0]
    assert h[3] > 4.0 * h[0]


def _price(
    sb: float,
    alt: list[float],
    sun: list[float],
    usable: list[bool] | None = None,
    rig: equipment.EquipmentPreset | None = None,
) -> float:
    """``_hours_to_goal`` for one object over its samples."""
    h = tonight._hours_to_goal(
        np.array([sb]),
        np.array([alt]),
        np.array(sun),
        None if usable is None else np.array([usable]),
        site=Site(latitude_deg=52.0, longitude_deg=0.0, elevation_m=50.0),
        rig=rig or equipment.resolve("sct8-2600mm", None),
        snr_goal=20.0,
        t_sub_s=90.0,
    )
    return float(h[0])


def test_the_estimate_is_priced_at_the_cheapest_usable_sample() -> None:
    """The best case is the cheapest usable sample, not the highest: an
    object at its peak in bright twilight is dearer than lower in a dark sky.
    (Pricing the highest once quoted 1.5-2x too long for objects best at a
    twilit window edge.)"""
    peak, dark = _price(23.0, [80.0], [-12.5]), _price(23.0, [45.0], [-30.0])
    assert dark < peak  # lower, but darker: cheaper
    assert _price(23.0, [80.0, 45.0], [-12.5, -30.0]) == pytest.approx(dark, rel=1e-12)
    # Only the samples marked usable count, and none prices nothing.
    assert _price(23.0, [80.0, 45.0], [-12.5, -30.0], [True, False]) == pytest.approx(
        peak, rel=1e-12
    )
    assert math.isnan(_price(23.0, [80.0, 45.0], [-12.5, -30.0], [False, False]))


def test_the_estimate_counts_the_download_between_frames() -> None:
    """Plans count each frame's download (``download_duty_cycle``), so the
    picker does too: the two quote the same time for the same frames. A
    readout as long as the sub halves the duty cycle and doubles the time."""
    rig = equipment.resolve("sct8-2600mm", None)

    def with_readout(seconds: float) -> float:
        cam = dataclasses.replace(rig.camera, readout_s=seconds)
        return _price(23.0, [50.0], [-30.0], rig=dataclasses.replace(rig, camera=cam))

    open_shutter = with_readout(0.0)
    assert with_readout(90.0) == pytest.approx(2.0 * open_shutter, rel=1e-12)
    assert _price(23.0, [50.0], [-30.0], rig=rig) == pytest.approx(
        open_shutter * (90.0 + rig.camera.readout_s) / 90.0, rel=1e-12
    )


def test_a_window_shorter_than_one_sample_stays_inside_itself(client: TestClient) -> None:
    """Three minutes is shorter than one survey sample. Its one sample is the
    window itself: bestAt inside the usable span, no count longer than the
    window. (The sample once sat 5 minutes in, after the window's end, and
    counted 10 minutes.)"""
    body = _tonight(
        client,
        siteId="urbana",
        equipmentId="sct8-2600mm",
        start="2026-09-19T04:00:00Z",
        hours=0.05,
    )
    start, end = _at(body["windowStart"]), _at(body["windowEnd"])
    assert end - start == timedelta(minutes=3)
    assert 0.0 < body["darkHours"] <= 0.05
    assert body["moonUpHours"] <= 0.05
    visible = [t for t in body["targets"] if t["visible"]]
    assert visible
    for t in visible:
        a, best, b = _at(t["usableFrom"]), _at(t["bestAt"]), _at(t["usableUntil"])
        assert start <= a <= best <= b <= end, t["id"]
        assert 0.0 < t["usableHours"] <= 0.05, t["id"]
        assert t["transitAt"] is None, t["id"]


def test_a_window_under_a_second_stays_inside_itself(client: TestClient) -> None:
    """The request takes any length above zero. A third of a second is still
    one sample of its own length: bestAt once sat half a second in, after the
    window's end."""
    body = _tonight(
        client, siteId="urbana", equipmentId="sct8-2600mm", start="2026-09-19T04:00:00Z", hours=1e-4
    )
    start, end = _at(body["windowStart"]), _at(body["windowEnd"])
    assert timedelta(0) < end - start < timedelta(seconds=1)
    visible = [t for t in body["targets"] if t["visible"]]
    assert visible
    for t in visible:
        a, best, b = _at(t["usableFrom"]), _at(t["bestAt"]), _at(t["usableUntil"])
        assert start <= a <= best <= b <= end, t["id"]


def test_non_plannable_objects_are_never_visible_and_say_why(c8: dict[str, Any]) -> None:
    by = _by_id(c8)
    for o in catalog.objects():
        if o.plannable:
            continue
        t = by[o.id]
        assert not t["plannable"] and not t["visible"], o.id
        assert t["whyNot"] == o.why_not, o.id
        assert o.id not in c8["recommendedIds"]


def test_a_southern_object_is_invisible_from_urbana_and_says_why(
    c8: dict[str, Any], south: dict[str, Any]
) -> None:
    tuc47 = _by_id(c8)["ngc104"]  # 47 Tucanae, Dec -72
    assert not tuc47["visible"]
    assert tuc47["whyNot"] == "never rises from here tonight"
    m7 = _by_id(c8)["m7"]  # Dec -35: rises, but not to 30 degrees
    assert not m7["visible"]
    assert "below the 30° floor" in m7["whyNot"]
    assert _by_id(south)["ngc104"]["visible"]


# --------------------------------------------------------------------------
# do the picks make sense?
# --------------------------------------------------------------------------


def test_picks_are_varied(c8: dict[str, Any], refractor: dict[str, Any]) -> None:
    for body in (c8, refractor):
        by = _by_id(body)
        types = [by[t]["type"] for t in body["recommendedIds"]]
        assert len(set(types)) >= 3, types
        assert max(types.count(x) for x in set(types)) <= 7, types


def test_long_focal_length_skips_the_giants(c8: dict[str, Any]) -> None:
    """At two metres M31 is a dozen-panel mosaic and NGC 7000 twenty."""
    rec = set(c8["recommendedIds"])
    assert not rec & {"m31", "ngc7000", "ic1396", "ngc6960", "m45"}
    by = _by_id(c8)
    assert all(by[t]["framing"] != "mosaic" or by[t]["framingFill"] < 2.5 for t in rec)


def test_short_focal_length_skips_the_specks(refractor: dict[str, Any]) -> None:
    """At 480 mm the Ring Nebula is a few dozen pixels."""
    rec = set(refractor["recommendedIds"])
    assert not rec & {"m57", "ngc7662", "ngc6543", "ngc7009"}
    by = _by_id(refractor)
    assert (
        sum(by[t]["type"].endswith("nebula") or by[t]["type"] == "supernova-remnant" for t in rec)
        >= 4
    )


def test_the_south_gets_southern_showpieces(south: dict[str, Any]) -> None:
    rec = south["recommendedIds"]
    assert "ngc253" in rec  # the Sculptor Galaxy, overhead in September at -30
    assert "m31" not in rec


def test_moon_and_window_are_reported(c8: dict[str, Any]) -> None:
    assert 0.0 <= c8["moonIllumination"] <= 1.0
    assert 6.0 < c8["darkHours"] < 12.0
    assert c8["fovWidthDeg"] > c8["fovHeightDeg"] > 0


# --------------------------------------------------------------------------
# sessions from what the list offers
# --------------------------------------------------------------------------


def test_a_session_built_from_catalogue_ids_folds(client: TestClient, c8: dict[str, Any]) -> None:
    ids = [tid for tid in c8["recommendedIds"] if tid != "ngc7331"][:3]
    r = client.post(
        "/api/sessions",
        json={
            "date": NIGHT,
            "hours": 6,
            "solveSeconds": 1.0,
            "weather": "synthetic",
            "targets": [{"id": tid} for tid in ids] + [{"id": "n7331"}],
        },
    )
    assert r.status_code == 202, r.text
    sess = client.get(f"/api/sessions/{r.json()['id']}").json()
    assert sess["status"] == "ready", sess.get("error") or sess["message"]
    got = [t["id"] for t in sess["targets"]]
    assert got == [*ids, "ngc7331"]  # the legacy id comes back canonical
    by = _by_id(c8)
    for t in sess["targets"]:
        o = catalog.by_id(t["id"])
        assert o is not None
        assert t["name"] == o.target_name
        assert t["magnitude"] == pytest.approx(by[t["id"]]["magnitude"], abs=0.01)


def test_a_session_refuses_a_search_only_object(client: TestClient) -> None:
    r = client.post(
        "/api/sessions",
        json={"date": NIGHT, "weather": "synthetic", "targets": [{"id": "m40"}]},
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------
# cost
# --------------------------------------------------------------------------


def test_a_warm_request_is_fast_and_the_payload_bounded(client: TestClient) -> None:
    body = {"date": NIGHT, "siteId": "urbana", "equipmentId": "edge11-6200"}
    client.post("/api/targets/tonight", json=body)  # warm the survey cache
    t0 = time.perf_counter()
    r = client.post("/api/targets/tonight", json=body)
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    # ~40 ms on a laptop; generous for a loaded CI runner.
    assert elapsed < 0.5, f"warm tonight request took {elapsed * 1000:.0f} ms"
    assert len(r.content) < 2_000_000
    gz = client.post("/api/targets/tonight", json=body, headers={"Accept-Encoding": "gzip"})
    assert gz.headers.get("content-encoding") == "gzip"
