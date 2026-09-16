"""Ready-made sites, rigs and targets, so a session can be created in one POST.

These exist because the alternative -- making the client invent a plausible
2600MM read-noise figure -- produces demos that are wrong in ways nobody
notices. Every number here is either a manufacturer figure or an explicitly
labelled placeholder.
"""

from __future__ import annotations

from dataclasses import dataclass

from tscheduler.domain.equipment import Camera, Mount, Optics
from tscheduler.domain.site import Site
from tscheduler.domain.targets import Target
from tscheduler.physics.convert import bortle_to_artificial_nl


@dataclass(frozen=True, slots=True)
class SitePreset:
    id: str
    name: str
    latitude_deg: float
    longitude_deg: float
    elevation_m: float
    bortle: int
    extinction_k: float = 0.20

    def to_site(self) -> Site:
        return Site(
            latitude_deg=self.latitude_deg,
            longitude_deg=self.longitude_deg,
            elevation_m=self.elevation_m,
            name=self.name,
            artificial_zenith_nl=bortle_to_artificial_nl(self.bortle),
            extinction_k=self.extinction_k,
        )


@dataclass(frozen=True, slots=True)
class EquipmentPreset:
    id: str
    name: str
    optics: Optics
    camera: Camera
    mount: Mount

    @property
    def pixel_scale_arcsec(self) -> float:
        return self.camera.pixel_scale_arcsec(self.optics)

    @property
    def fov_deg(self) -> tuple[float, float]:
        return self.camera.fov_deg(self.optics)


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

#: Manufacturer figures. Throughput is the one genuine estimate in each row and
#: it scales the signal linearly, so it is called out rather than buried.
EQUIPMENT: tuple[EquipmentPreset, ...] = (
    EquipmentPreset(
        "sct8-2600mm",
        '8" SCT f/10 + ASI2600MM',
        Optics(
            aperture_mm=203.0, focal_length_mm=2032.0, central_obstruction_mm=68.0, throughput=0.75
        ),
        Camera(
            pixel_size_um=3.76,
            sensor_width_px=6248,
            sensor_height_px=4176,
            quantum_efficiency=0.75,
            read_noise_e=1.5,
            dark_current_e_per_s=0.002,
            full_well_e=50_000.0,
        ),
        Mount(switch_minutes=5.0, min_block_minutes=20.0),
    ),
    EquipmentPreset(
        "refractor80-533",
        "80mm apo f/6 + ASI533MC",
        Optics(aperture_mm=80.0, focal_length_mm=480.0, throughput=0.92),
        Camera(
            pixel_size_um=3.76,
            sensor_width_px=3008,
            sensor_height_px=3008,
            quantum_efficiency=0.80,
            read_noise_e=1.0,
            dark_current_e_per_s=0.001,
            full_well_e=50_000.0,
        ),
        Mount(switch_minutes=3.0, min_block_minutes=15.0),
    ),
    EquipmentPreset(
        "edge11-6200",
        'Celestron EdgeHD 11" + ASI6200MM',
        Optics(
            aperture_mm=279.0, focal_length_mm=2800.0, central_obstruction_mm=95.0, throughput=0.74
        ),
        Camera(
            pixel_size_um=3.76,
            sensor_width_px=9576,
            sensor_height_px=6388,
            quantum_efficiency=0.80,
            read_noise_e=1.5,
            dark_current_e_per_s=0.003,
            full_well_e=51_400.0,
        ),
        Mount(switch_minutes=5.0, min_block_minutes=20.0),
    ),
)

#: id, name, RA deg, Dec deg, V SURFACE brightness mag/arcsec^2.
#: The scheduler works in surface brightness throughout: an extended object's
#: integrated magnitude says nothing about how fast one pixel fills.
CATALOG: tuple[tuple[str, str, float, float, float], ...] = (
    ("m31", "M31 Andromeda", 10.6847, 41.2690, 18.4),
    ("m27", "M27 Dumbbell", 299.9015, 22.7211, 18.4),
    ("m57", "M57 Ring", 283.3963, 33.0292, 19.0),
    ("m13", "M13 Hercules", 250.4235, 36.4613, 18.8),
    ("m33", "M33 Triangulum", 23.4621, 30.6602, 18.7),
    ("m81", "M81 Bode", 148.8882, 69.0653, 19.0),
    ("m101", "M101 Pinwheel", 210.8023, 54.3488, 20.1),
    ("n7000", "NGC 7000 North America", 314.7500, 44.3700, 18.0),
    ("n6946", "NGC 6946 Fireworks", 308.7180, 60.1539, 19.1),
    ("n7331", "NGC 7331", 339.2671, 34.4158, 19.3),
    ("n891", "NGC 891", 35.6392, 42.3492, 19.4),
    ("ic1396", "IC 1396 Elephant Trunk", 324.7500, 57.5000, 19.2),
)

DEFAULT_TARGET_IDS: tuple[str, ...] = ("m31", "m27", "m57", "n7000", "m13", "m33", "n7331")


def site_by_id(site_id: str) -> SitePreset | None:
    return next((s for s in SITES if s.id == site_id), None)


def equipment_by_id(eq_id: str) -> EquipmentPreset | None:
    return next((e for e in EQUIPMENT if e.id == eq_id), None)


def catalog_target(
    target_id: str, *, priority: float = 1.0, snr_goal: float | None = None
) -> Target | None:
    row = next((r for r in CATALOG if r[0] == target_id), None)
    if row is None:
        return None
    tid, name, ra, dec, mag = row
    return Target(
        id=tid,
        name=name,
        ra_deg=ra,
        dec_deg=dec,
        magnitude=mag,
        priority=priority,
        snr_goal=snr_goal,
    )
