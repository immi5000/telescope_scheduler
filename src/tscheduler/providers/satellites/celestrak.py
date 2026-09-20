"""CelesTrak GP elements, for LIVE mode.

Encodes one operational fact that a naive client gets wrong: **CelesTrak returns
HTTP 403 with a plain-text body when the data has not changed since your last
successful download.**

    "GP data has not updated since your last successful download of
     GROUP=starlink at 2026-09-16 19:34:13 UTC. Data is updated once every
     2 hours."

That is caching etiquette, not an error, and treating it as a failure means a
scheduler that breaks whenever it polls twice in two hours. We detect the
message and serve the cached copy instead.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import httpx

from tscheduler.core.clock import AsOf, Vantage
from tscheduler.providers.base import Confidence, Record
from tscheduler.providers.satellites.base import (
    SatelliteElementsProvider,
    TleQuery,
    TleRecord,
)

GP_URL: Final = "https://celestrak.org/NORAD/elements/gp.php"

#: CelesTrak states GP data updates every 2 hours; polling faster is impolite
#: and answered with a 403.
MIN_REFETCH_INTERVAL: Final = timedelta(hours=2)

_NOT_MODIFIED_MARKER: Final = "has not updated since your last successful"


def parse_tle_text(text: str) -> list[TleRecord]:
    """Parse 3-line TLE format, skipping malformed entries rather than raising.

    A single corrupt element set in a 10,000-object feed should cost one
    satellite, not the whole night's satellite awareness.
    """
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    out: list[TleRecord] = []
    for i in range(0, len(lines) - 2, 3):
        name, l1, l2 = lines[i], lines[i + 1], lines[i + 2]
        if not (l1.startswith("1 ") and l2.startswith("2 ")):
            continue
        try:
            norad = int(l1[2:7])
            yy = int(l1[18:20])
            doy = float(l1[20:32])
            year = 2000 + yy if yy < 57 else 1900 + yy
            epoch = datetime(year, 1, 1, tzinfo=UTC) + timedelta(days=doy - 1.0)
        except (ValueError, IndexError):
            continue
        out.append(TleRecord(name=name.strip(), line1=l1, line2=l2, epoch=epoch, norad_id=norad))
    return out


class CelesTrakProvider(SatelliteElementsProvider):
    """Current elements only. LIVE vantage only -- it has no history."""

    source_id = "celestrak:gp"

    def __init__(self, cache_dir: Path | None = None, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=60.0, follow_redirects=True)
        self._cache_dir = cache_dir
        self._mem: dict[str, tuple[datetime, list[TleRecord]]] = {}

    def _cache_path(self, group: str) -> Path | None:
        if self._cache_dir is None:
            return None
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        return self._cache_dir / f"celestrak_{group}.tle"

    def _stamp_path(self, group: str) -> Path | None:
        """Where the cached copy's DOWNLOAD instant is recorded.

        It gets its own file because the alternative -- reading the payload's
        mtime -- substitutes a *retrieval* fact for a *publication* fact, and
        those are two of the three timestamps this system refuses to conflate.
        An mtime also changes for reasons that have nothing to do with the
        data: a copy, a restore, a checkout.
        """
        path = self._cache_path(group)
        return None if path is None else path.with_suffix(".stamp.json")

    def _fetch(self, query: TleQuery, as_of: AsOf) -> Sequence[Record[TleRecord]]:
        if as_of.vantage is Vantage.REPLAY:
            raise ValueError(
                "CelesTrakProvider serves only current elements and cannot answer a "
                "replay as_of. Use Space-Track gp_history, filtered on CREATION_DATE."
            )

        cached = self._mem.get(query.group)
        if cached is not None and as_of.t - cached[0] < MIN_REFETCH_INTERVAL:
            fetched_at, records = cached
        else:
            fetched_at, records = self._download(query, as_of, cached)

        return [
            Record(
                value=r,
                published_at=fetched_at,
                source_id=self.source_id,
                record_id=f"{r.norad_id}:{r.epoch:%Y%m%dT%H%M%S}",
                valid_from=r.epoch,
                confidence=Confidence.EXACT,
            )
            for r in records
        ]

    def _download(
        self,
        query: TleQuery,
        as_of: AsOf,
        cached: tuple[datetime, list[TleRecord]] | None,
    ) -> tuple[datetime, list[TleRecord]]:
        try:
            r = self._client.get(GP_URL, params={"GROUP": query.group, "FORMAT": "tle"})
        except httpx.HTTPError:
            revived = self._revive(query, cached)
            if revived is not None:
                return revived
            raise

        if r.status_code == 403 and _NOT_MODIFIED_MARKER in r.text:
            # Not an error: CelesTrak is telling us our copy is current.
            revived = self._revive(query, cached)
            if revived is not None:
                return revived
            raise RuntimeError(
                "CelesTrak reports our data is already current, but no cached copy "
                f"exists for GROUP={query.group}. Wait for the 2-hour refresh, or "
                "point cache_dir at the previous download."
            )

        r.raise_for_status()
        records = parse_tle_text(r.text)
        if not records:
            revived = self._revive(query, cached)
            if revived is not None:
                return revived
            raise RuntimeError(f"CelesTrak returned no parseable elements for {query.group}")

        path = self._cache_path(query.group)
        if path is not None:
            path.write_text(r.text)
        stamp = self._stamp_path(query.group)
        if stamp is not None:
            stamp.write_text(json.dumps({"downloaded_at": as_of.t.isoformat()}))
        self._mem[query.group] = (as_of.t, records)
        return as_of.t, records

    def _revive(
        self, query: TleQuery, cached: tuple[datetime, list[TleRecord]] | None
    ) -> tuple[datetime, list[TleRecord]] | None:
        if cached is not None:
            return cached
        path = self._cache_path(query.group)
        if path is not None and path.exists():
            records = parse_tle_text(path.read_text())
            if records:
                when = self._cached_download_time(query.group, path)
                self._mem[query.group] = (when, records)
                return when, records
        return None

    def _cached_download_time(self, group: str, payload: Path) -> datetime:
        """When the cached copy was actually downloaded.

        Falls back to the payload's mtime when no stamp exists -- a file
        written by an older build, or copied in by hand. That fallback is
        deliberately NOT clamped to ``as_of``: if we cannot prove when these
        elements became knowable, the honest answer is the latest instant they
        might have, and letting the publication gate refuse them is the correct
        outcome. Clamping would manufacture provenance to make an assertion
        pass, which is the one thing this layer must never do.
        """
        stamp = self._stamp_path(group)
        if stamp is not None and stamp.exists():
            try:
                raw = json.loads(stamp.read_text())["downloaded_at"]
                when = datetime.fromisoformat(raw)
            except (OSError, ValueError, KeyError, TypeError):
                pass
            else:
                return when if when.tzinfo is not None else when.replace(tzinfo=UTC)
        return datetime.fromtimestamp(payload.stat().st_mtime, UTC)

    def coverage_key(self, query: TleQuery) -> str:
        return f"celestrak:{query.group}"


class FixtureTleProvider(SatelliteElementsProvider):
    """Offline elements from a committed file."""

    source_id = "fixture:tle"

    def __init__(self, path: Path, published_at: datetime | None = None) -> None:
        self._records = parse_tle_text(Path(path).read_text())
        self._published = published_at or datetime(1970, 1, 1, tzinfo=UTC)

    def _fetch(self, query: TleQuery, as_of: AsOf) -> Sequence[Record[TleRecord]]:
        return [
            Record(
                value=r,
                published_at=self._published,
                source_id=self.source_id,
                record_id=f"{r.norad_id}:{r.epoch:%Y%m%dT%H%M%S}",
                valid_from=r.epoch,
                confidence=Confidence.EXACT,
            )
            for r in self._records
            if self._published <= as_of.t
        ]

    def coverage_key(self, query: TleQuery) -> str:
        return f"fixture:{query.group}"
