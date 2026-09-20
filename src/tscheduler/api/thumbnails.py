"""Deep-sky cutouts from CDS hips2fits, fetched once and cached on disk.

**These are pictures, and nothing else.** No thumbnail ever enters an
``EvidenceLedger``, a ``QualityGrid`` or a ``Plan``; the no-lookahead machinery
does not apply and does not need to, because a 1990s photographic sky survey
carries no information about the night being scheduled. If that ever stops
being true -- if someone points this at a live all-sky camera -- it acquires a
``published_at`` and moves behind a ``Provider`` like everything else.

Two operational facts drive the design:

* a 384x384 cutout takes **1.3-1.4 seconds**, so it can be fetched once per
  target per site and never per render;
* the survey is **allowlisted by a short key, never taken from the request**.
  Passing a caller-supplied HiPS identifier straight through would make this an
  open proxy to whatever the upstream service can be persuaded to fetch.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx

HIPS2FITS: Final = "https://alasky.cds.unistra.fr/hips-image-services/hips2fits"

#: Surveys we will serve, by short key.
#:
#: DSS2 colour is the default because it is what the eye expects a deep-sky
#: object to look like. **Licence caveat, recorded here on purpose:** STScI
#: names four copyright holders for the Digitized Sky Survey and grants no
#: explicit licence for redistribution. That is fine for a personal tool and a
#: real problem if this ever ships, which is exactly why the survey is a
#: one-line constant -- swapping to 2MASS, which is unambiguously public, is a
#: change to this dict and nothing else.
SURVEYS: Final[dict[str, str]] = {
    "dss2-color": "CDS/P/DSS2/color",
    "dss2-red": "CDS/P/DSS2/red",
    "2mass-color": "CDS/P/2MASS/color",
}
DEFAULT_SURVEY: Final = "dss2-color"

MAX_SIZE_PX: Final = 512
MAX_FOV_DEG: Final = 10.0
TIMEOUT_S: Final = 25.0


@dataclass(frozen=True, slots=True)
class ThumbnailRequest:
    ra_deg: float
    dec_deg: float
    fov_deg: float = 0.5
    size_px: int = 384
    survey: str = DEFAULT_SURVEY

    def __post_init__(self) -> None:
        if self.survey not in SURVEYS:
            raise ValueError(f"unknown survey {self.survey!r}; try {sorted(SURVEYS)}")
        if not -90.0 <= self.dec_deg <= 90.0:
            raise ValueError(f"dec out of range: {self.dec_deg}")
        if not 0.0 <= self.ra_deg < 360.0:
            raise ValueError(f"ra out of range: {self.ra_deg}")
        if not 0.0 < self.fov_deg <= MAX_FOV_DEG:
            raise ValueError(f"fov must be in (0, {MAX_FOV_DEG}], got {self.fov_deg}")
        if not 16 <= self.size_px <= MAX_SIZE_PX:
            raise ValueError(f"size must be in [16, {MAX_SIZE_PX}], got {self.size_px}")

    @property
    def key(self) -> str:
        """Cache key. Coordinates are rounded to ~0.36 arcsec before hashing.

        Without the rounding, a cursor-driven request would miss the cache on
        every pixel of mouse movement and hammer an upstream service that takes
        a second and a half to answer.
        """
        raw = (
            f"{self.survey}|{self.ra_deg:.4f}|{self.dec_deg:.4f}|{self.fov_deg:.4f}|{self.size_px}"
        )
        return hashlib.blake2b(raw.encode(), digest_size=12).hexdigest()


class ThumbnailCache:
    """A local mirror of a static image service. Fetch once, serve forever."""

    def __init__(self, cache_dir: Path, client: httpx.Client | None = None) -> None:
        self._dir = cache_dir / "thumbs"
        self._client = client

    def path_for(self, req: ThumbnailRequest) -> Path:
        return self._dir / f"{req.key}.jpg"

    def cached(self, req: ThumbnailRequest) -> bytes | None:
        p = self.path_for(req)
        if p.is_file() and p.stat().st_size > 0:
            return p.read_bytes()
        return None

    def fetch(self, req: ThumbnailRequest) -> bytes:
        """Return the image, from disk if we have it.

        Raises ``httpx.HTTPError`` on failure rather than returning a
        placeholder: the caller turns that into a 502 and the UI simply draws
        no thumbnail. An inline "image unavailable" graphic cached to disk
        would be indistinguishable from a real one the next time round.
        """
        hit = self.cached(req)
        if hit is not None:
            return hit

        client = self._client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=True)
        try:
            res = client.get(
                HIPS2FITS,
                params={
                    "hips": SURVEYS[req.survey],
                    "ra": req.ra_deg,
                    "dec": req.dec_deg,
                    "fov": req.fov_deg,
                    "width": req.size_px,
                    "height": req.size_px,
                    "projection": "TAN",
                    "coordsys": "icrs",
                    "format": "jpg",
                },
            )
            res.raise_for_status()
            body = res.content
        finally:
            if self._client is None:
                client.close()

        if not body.startswith(b"\xff\xd8"):
            raise httpx.HTTPError(
                f"hips2fits returned {len(body)} bytes that are not a JPEG "
                "(the service answers errors with a JSON body and HTTP 200)"
            )

        self._dir.mkdir(parents=True, exist_ok=True)
        # Written via a temporary file and renamed: two browsers asking for the
        # same target at once would otherwise interleave writes and leave a
        # half-image on disk that every later request would serve happily.
        tmp = self.path_for(req).with_suffix(".part")
        tmp.write_bytes(body)
        tmp.replace(self.path_for(req))
        return body
