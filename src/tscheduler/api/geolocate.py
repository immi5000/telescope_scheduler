"""What a pair of coordinates is called, and how high it is.

The browser's geolocation gives a latitude, a longitude and nothing else. A
site needs more than that to be planned honestly -- an elevation, because air
mass and refraction are computed from it -- and an observer needs more than
that to believe it, because "40.1164, -88.2434" is not a place anyone
recognises as their own back garden.

Neither lookup is allowed to be load-bearing. The coordinates the browser gave
are the site; these two calls only *decorate* them, so every failure here
returns ``None`` for that field and nothing else changes. Offline, the site is
still exactly where the browser said it was, named by its coordinates, at sea
level -- and the UI says so rather than implying a lookup succeeded.

Two services, deliberately separate:

* **Elevation** is Open-Meteo's, the same provider the weather already comes
  from. It answers the ground elevation of the model cell (90 m Copernicus
  DEM), which is the right figure: a site's elevation enters as air mass, and
  a metre either way is far below the error in the extinction coefficient.
* **The name** is Nominatim's, OpenStreetMap's geocoder. Its usage policy
  requires an identifying ``User-Agent`` and no more than one request a
  second, and both are enforced here rather than hoped for: ``_NAME_TURN``
  lets one caller at a time wait out the gap since the last request, and
  anyone who arrives while that caller is waiting goes without a name rather
  than queueing. A burst therefore degrades to coordinates -- which is what
  the UI shows anyway when a lookup fails -- instead of either breaking the
  policy or tying up a request thread per caller.

Three deliberate restraints, all of them about not being careless with
somebody's position:

* Coordinates leave this process rounded to ``CACHE_PLACES`` decimals, ~110 m.
  Neither answer is finer than that -- the DEM cell is 90 m and the geocoder
  is asked for a town -- so full GPS precision would be sent to two third
  parties to buy nothing.
* Results are cached on the same rounded coordinate, so pressing the button
  twice from the same chair costs one round trip.
* Only a COMPLETE answer is cached. A half or total failure is usually "the
  laptop was not on the wifi yet" or a momentary 429, and caching it would
  make one bad second permanent for the life of the process.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Final

import httpx

ELEVATION_URL: Final = "https://api.open-meteo.com/v1/elevation"
REVERSE_URL: Final = "https://nominatim.openstreetmap.org/reverse"

#: Nominatim's policy requires a User-Agent that identifies the application.
USER_AGENT: Final = "tscheduler/0.1 (telescope night scheduler)"

#: Short: this sits between pressing a button and a form filling in. A site
#: that has to wait four seconds for a name it does not need is worse than a
#: site with no name.
TIMEOUT: Final = httpx.Timeout(4.0, connect=2.0)

#: Administrative detail level asked of Nominatim. 12 is the town or city --
#: coarse enough not to publish a house number back at someone, fine enough to
#: be recognised.
REVERSE_ZOOM: Final = 12

#: Coordinates are rounded to this many decimals before they are cached OR
#: sent anywhere: 3 is about 110 m, far inside the resolution of either answer.
CACHE_PLACES: Final = 3
CACHE_MAX: Final = 128

#: Nominatim's published limit is one request a second, absolute.
NAME_GAP_S: Final = 1.0

ATTRIBUTION: Final = "Elevation: Open-Meteo (Copernicus DEM). Place: OpenStreetMap (Nominatim)."


@dataclass(frozen=True, slots=True)
class Place:
    """What could be learned about a coordinate. Any field may be ``None``."""

    name: str | None = None
    elevation_m: float | None = None


_CACHE: OrderedDict[tuple[float, float], Place] = OrderedDict()
_LOCK = threading.Lock()

#: Held by whoever is currently waiting out Nominatim's one-second gap. Taken
#: without blocking: a second caller does not queue behind the first, it gives
#: up on the name. One sleeping thread at a time, for at most NAME_GAP_S.
_NAME_TURN = threading.Lock()
_NAME_LAST = 0.0


def locate(lat: float, lon: float, *, client: httpx.Client | None = None) -> Place:
    """Name and elevation for a coordinate. Never raises, never blocks long.

    A partial answer is normal and is returned as is: the elevation service can
    be up while the geocoder is down, and the caller shows whichever arrived.
    """
    key = (round(lat, CACHE_PLACES), round(lon, CACHE_PLACES))
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            return hit

    # Rounded before it is sent, not only before it is cached.
    at_lat, at_lon = key

    own = client is None
    c = client or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    try:
        place = Place(
            name=_reverse_name(c, at_lat, at_lon), elevation_m=_elevation(c, at_lat, at_lon)
        )
    finally:
        if own:
            c.close()

    # Only a complete answer is kept. Anything less is a service that was
    # down, rate-limiting, or skipped for the gap a moment ago, and the next
    # press should ask again rather than inherit that second for ever.
    if place.name is not None and place.elevation_m is not None:
        with _LOCK:
            _CACHE[key] = place
            _CACHE.move_to_end(key)
            while len(_CACHE) > CACHE_MAX:
                _CACHE.popitem(last=False)
    return place


def _elevation(client: httpx.Client, lat: float, lon: float) -> float | None:
    payload = _get(client, ELEVATION_URL, {"latitude": lat, "longitude": lon})
    if payload is None:
        return None
    got = payload.get("elevation")
    if isinstance(got, list) and got and isinstance(got[0], int | float):
        return float(got[0])
    return None


def _reverse_name(client: httpx.Client, lat: float, lon: float) -> str | None:
    """The town, if this caller can have the next second's request."""
    global _NAME_LAST
    if not _NAME_TURN.acquire(blocking=False):
        return None
    try:
        wait = NAME_GAP_S - (time.monotonic() - _NAME_LAST)
        if wait > 0:
            time.sleep(min(wait, NAME_GAP_S))
        _NAME_LAST = time.monotonic()
        return _reverse_call(client, lat, lon)
    finally:
        _NAME_TURN.release()


def _reverse_call(client: httpx.Client, lat: float, lon: float) -> str | None:
    payload = _get(
        client,
        REVERSE_URL,
        {
            "format": "jsonv2",
            "zoom": REVERSE_ZOOM,
            "lat": lat,
            "lon": lon,
            "addressdetails": 1,
        },
    )
    if payload is None:
        return None
    address = payload.get("address")
    if isinstance(address, dict):
        name = _name_from(address)
        if name:
            return name
    display = payload.get("display_name")
    return display if isinstance(display, str) and display else None


#: Tried in order for the first half of the label: whichever of these the place
#: has is the one a person would say they were in.
LOCALITY: Final = (
    "city",
    "town",
    "village",
    "hamlet",
    "municipality",
    "suburb",
    "city_district",
    "county",
)

#: And for the second half, so "Champaign" is not the one in Ohio.
REGION: Final = ("state", "province", "region", "country")


def _name_from(address: dict[str, Any]) -> str | None:
    """ "Champaign, Illinois" from Nominatim's address parts.

    Two parts at most. The full ``display_name`` runs to five ("Champaign,
    Champaign County, Illinois, United States"), which wraps the line it is
    shown on and says nothing the first two did not.
    """
    parts: list[str] = []
    for field in (LOCALITY, REGION):
        for key in field:
            got = address.get(key)
            if isinstance(got, str) and got and got not in parts:
                parts.append(got)
                break
    return ", ".join(parts) if parts else None


def _get(client: httpx.Client, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """GET and decode. Every failure is ``None``: nothing here is worth an error."""
    try:
        r = client.get(url, params=params)
        if r.status_code != 200:
            return None
        payload = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


__all__ = ["ATTRIBUTION", "Place", "locate"]
