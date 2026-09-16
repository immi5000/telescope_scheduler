"""CelesTrak provider, with emphasis on the 403-means-cached behaviour."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from tscheduler.core.clock import AsOf, Vantage
from tscheduler.providers.satellites.base import TleQuery
from tscheduler.providers.satellites.celestrak import (
    CelesTrakProvider,
    FixtureTleProvider,
    parse_tle_text,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tle" / "visual.tle"
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
LIVE = AsOf(NOW, Vantage.LIVE)

NOT_MODIFIED_BODY = (
    "GP data has not updated since your last successful\r\n"
    "download of GROUP=visual at 2026-09-16 19:34:13 UTC.\r\n"
    "Data is updated once every 2 hours."
)


def _mock(status: int, text: str) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=text)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_parses_real_tle_fixture() -> None:
    recs = parse_tle_text(FIXTURE.read_text())
    assert len(recs) > 50
    r = recs[0]
    assert r.line1.startswith("1 ") and r.line2.startswith("2 ")
    assert r.norad_id > 0
    assert 2020 < r.epoch.year < 2040
    assert r.epoch.tzinfo is not None


def test_malformed_entries_cost_one_satellite_not_the_whole_feed() -> None:
    """A corrupt element set in a 10,000-object feed must not blank out the
    night's satellite awareness."""
    good = FIXTURE.read_text().splitlines()[:6]
    text = "\n".join(["JUNK NAME", "not a line 1", "not a line 2", *good])
    recs = parse_tle_text(text)
    assert len(recs) == 2


def test_epoch_is_parsed_not_confused_with_publication_time() -> None:
    """EPOCH is the orbital state's reference time. Filtering replay on it leaks
    in BOTH directions, which is why this field is never used as published_at."""
    recs = parse_tle_text(FIXTURE.read_text())
    epochs = [r.epoch for r in recs]
    assert len(set(epochs)) > 1, "distinct objects should carry distinct epochs"


def test_refuses_a_replay_as_of() -> None:
    """It holds only current elements, so answering a past as_of would serve
    today's orbit for last Tuesday -- a leak the gate cannot catch, because the
    record would be internally consistent."""
    p = CelesTrakProvider(client=_mock(200, FIXTURE.read_text()))
    with pytest.raises(ValueError, match="cannot answer a replay"):
        p._fetch(TleQuery(), AsOf.at(NOW))


def test_successful_download_is_parsed_and_stamped_with_retrieval_time() -> None:
    p = CelesTrakProvider(client=_mock(200, FIXTURE.read_text()))
    recs = p.fetch(TleQuery(), LIVE)
    assert len(recs) > 50
    assert all(r.published_at == NOW for r in recs.records)


def test_403_not_modified_serves_the_cache_instead_of_failing(tmp_path: Path) -> None:
    """The operational bug this provider exists to avoid: CelesTrak answers a
    too-soon re-request with 403 plus an explanation, and a naive client treats
    that as a hard error and loses all satellite data."""
    cache = tmp_path / "cache"
    ok = CelesTrakProvider(cache_dir=cache, client=_mock(200, FIXTURE.read_text()))
    first = ok.fetch(TleQuery(), LIVE)
    assert len(first) > 50

    # A fresh provider (cold memory) hitting a 403 must fall back to disk.
    refused = CelesTrakProvider(cache_dir=cache, client=_mock(403, NOT_MODIFIED_BODY))
    later = refused.fetch(TleQuery(), AsOf(NOW + timedelta(hours=3), Vantage.LIVE))
    assert len(later) == len(first), "should have served the cached copy"


def test_403_with_no_cache_is_a_clear_actionable_error(tmp_path: Path) -> None:
    p = CelesTrakProvider(cache_dir=tmp_path, client=_mock(403, NOT_MODIFIED_BODY))
    with pytest.raises(RuntimeError, match="no cached copy"):
        p.fetch(TleQuery(), LIVE)


def test_network_failure_falls_back_to_cache(tmp_path: Path) -> None:
    """An observatory laptop loses wifi. That must not end the session."""
    cache = tmp_path / "cache"
    CelesTrakProvider(cache_dir=cache, client=_mock(200, FIXTURE.read_text())).fetch(
        TleQuery(), LIVE
    )

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    dead = CelesTrakProvider(
        cache_dir=cache, client=httpx.Client(transport=httpx.MockTransport(boom))
    )
    recs = dead.fetch(TleQuery(), AsOf(NOW + timedelta(hours=5), Vantage.LIVE))
    assert len(recs) > 50


def test_respects_the_two_hour_refresh_interval() -> None:
    """CelesTrak states GP data updates every 2 hours; polling faster earns a
    403, so we should not even ask."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=FIXTURE.read_text())

    p = CelesTrakProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    p.fetch(TleQuery(), LIVE)
    p.fetch(TleQuery(), AsOf(NOW + timedelta(minutes=30), Vantage.LIVE))
    assert calls["n"] == 1, "second call within 2 h should have used the cache"

    p.fetch(TleQuery(), AsOf(NOW + timedelta(hours=3), Vantage.LIVE))
    assert calls["n"] == 2


def test_fixture_provider_is_static_and_offline() -> None:
    p = FixtureTleProvider(FIXTURE)
    recs = p.fetch(TleQuery(), AsOf.at(NOW))
    assert len(recs) > 50
