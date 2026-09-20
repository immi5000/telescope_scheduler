"""Ready-made sites, rigs and targets, so a session can be created in one POST.

Rigs, telescopes and cameras live in ``api/equipment.py`` and are re-exported
here, so ``presets.EQUIPMENT`` and ``presets.equipment_by_id`` keep working.

These exist because the alternative -- making the client invent a plausible
2600MM read-noise figure -- produces demos that are wrong in ways nobody
notices. Every number here is either a manufacturer figure or an explicitly
labelled placeholder.
"""

from __future__ import annotations

from dataclasses import dataclass

from tscheduler import catalog
from tscheduler.api.equipment import EQUIPMENT, EquipmentPreset, equipment_by_id
from tscheduler.core.timegrid import TimeGrid
from tscheduler.domain.site import Site
from tscheduler.domain.targets import Target
from tscheduler.physics.convert import bortle_to_artificial_nl
from tscheduler.physics.planets import build_planet_tracks, planet_body


@dataclass(frozen=True, slots=True)
class SitePreset:
    id: str
    name: str
    latitude_deg: float
    longitude_deg: float
    elevation_m: float
    bortle: int
    extinction_k: float = 0.20
    min_altitude_deg: float = 30.0
    min_moon_separation_deg: float = 15.0
    """The two hard vetoes, carried on the preset rather than left to the
    ``Site`` defaults.

    They live here because they are *reported* -- the UI draws an altitude
    floor ring and a lunar exclusion circle from them. A preset that could not
    carry them meant a custom site with a 25 degree floor was scheduled at 25
    and drawn at 30, with nothing anywhere to say so.
    """

    def to_site(self) -> Site:
        return Site(
            latitude_deg=self.latitude_deg,
            longitude_deg=self.longitude_deg,
            elevation_m=self.elevation_m,
            name=self.name,
            min_altitude_deg=self.min_altitude_deg,
            min_moon_separation_deg=self.min_moon_separation_deg,
            artificial_zenith_nl=bortle_to_artificial_nl(self.bortle),
            extinction_k=self.extinction_k,
        )


SITES: tuple[SitePreset, ...] = (
    SitePreset("urbana", "Urbana, IL", 40.1164, -88.2434, 227.0, bortle=5),
    SitePreset(
        "cherry-springs",
        "Cherry Springs, PA",
        41.6640,
        -77.8264,
        701.0,
        bortle=2,
        extinction_k=0.16,
    ),
    SitePreset(
        "mauna-kea", "Mauna Kea, HI", 19.8207, -155.4681, 4205.0, bortle=1, extinction_k=0.12
    ),
    SitePreset("brooklyn", "Brooklyn, NY", 40.6782, -73.9442, 10.0, bortle=8, extinction_k=0.28),
)

#: The deep-sky catalogue: ~1,850 objects from OpenNGC and the Sharpless
#: catalogue, each with a V MEAN surface brightness in mag/arcsec^2 -- the
#: number the scheduler plans with (an extended object's integrated magnitude
#: says nothing about how fast one pixel fills). Built by
#: ``scripts/build_catalog.py``; see ``tscheduler.catalog``.
CATALOG: tuple[catalog.DeepSkyObject, ...] = catalog.objects()

#: Used when a session request names no targets. Canonical ids; the old
#: ``n7000``-style ids still resolve.
DEFAULT_TARGET_IDS: tuple[str, ...] = ("m31", "m27", "m57", "ngc7000", "m13", "m33", "ngc7331")


def site_by_id(site_id: str) -> SitePreset | None:
    return next((s for s in SITES if s.id == site_id), None)


def catalog_target(
    target_id: str, *, priority: float = 1.0, snr_goal: float | None = None
) -> Target | None:
    """A schedulable target from anything that names a catalogue object: its
    canonical id (``ngc7000``), a legacy id (``n7000``), any designation
    ("NGC 224", "Sh 2-101", "C20") or a common name ("Horsehead").

    The target carries the CANONICAL id, and a name that starts with the
    designation ("M31 Andromeda Galaxy") because the timeline's short labels
    are cut from the front. None when nothing matches, and None for an object
    that cannot be planned (a double star, an asterism, a dark nebula, a
    Caldwell number naming two objects) -- ``catalog.refusal`` says which.
    """
    obj = catalog.resolve(target_id)
    if obj is None or not obj.plannable:
        return None
    return Target(
        id=obj.id,
        name=obj.target_name,
        ra_deg=obj.ra_deg,
        dec_deg=obj.dec_deg,
        magnitude=obj.surface_brightness,
        priority=priority,
        snr_goal=snr_goal,
    )


def planet_target(
    site: Site,
    grid: TimeGrid,
    query: str,
    *,
    priority: float = 1.0,
    snr_goal: float | None = None,
) -> Target | None:
    """A schedulable target from a planet's name, or None if it names no planet.

    Resolved per NIGHT, not from the catalogue: where a planet is and how
    bright it is both depend on the date, so there is no static row to look up.
    That is why this needs the site and the grid when ``catalog_target`` needs
    nothing.

    THE POSITION IS THE NIGHT'S MIDDLE SLOT, held fixed for the night, and that
    is accurate enough to schedule with. Against this catalogue's own rigs a
    planet drifts a fraction of a frame per hour -- Jupiter 10 arcseconds,
    Saturn 6, and Venus, the fastest, three and a half arcminutes against a
    26-arcminute frame. What moves a target across the sky is the Earth
    turning, and that is computed per slot from this position like any other.

    CALL THIS BEFORE ``catalog_target``. "Saturn" resolves in the catalogue to
    the Saturn Nebula, so a catalogue-first order silently swaps a planet for a
    planetary nebula 4,000 light years away.
    """
    body = planet_body(query)
    if body is None:
        return None
    track = next((p for p in build_planet_tracks(site, grid) if p.body == body), None)
    if track is None:  # pragma: no cover - PLANETS and _RADIUS_KM agree
        return None
    return Target(
        id=track.body,
        name=track.name,
        ra_deg=track.ra_deg,
        dec_deg=track.dec_deg,
        magnitude=track.surface_brightness,
        priority=priority,
        snr_goal=snr_goal,
    )


#: Shown wherever the catalogue is listed.
CATALOG_ATTRIBUTION = catalog.ATTRIBUTION


def catalog_size() -> int:
    return len(CATALOG)


__all__ = [
    "CATALOG",
    "CATALOG_ATTRIBUTION",
    "DEFAULT_TARGET_IDS",
    "EQUIPMENT",
    "SITES",
    "EquipmentPreset",
    "SitePreset",
    "catalog_size",
    "catalog_target",
    "equipment_by_id",
    "planet_target",
    "site_by_id",
]
